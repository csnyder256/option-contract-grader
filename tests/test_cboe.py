"""Offline tests for CBOE + Yahoo parsing (no network)."""

from datetime import date

import pytest

import app.providers.cboe as cboe_mod
from app.models import OptionType
from app.providers.base import FeedError
from app.providers.cboe import (
    CboeProvider,
    parse_cboe_chain,
    parse_cboe_expirations,
    parse_cboe_quote,
    parse_occ_symbol,
    parse_yahoo_history,
)

CBOE = {
    "data": {
        "symbol": "AAPL",
        "current_price": 282.50,
        "close": 283.78,
        "options": [
            {
                "option": "AAPL260717C00280000", "bid": 8.0, "ask": 8.4,
                "iv": 0.31, "open_interest": 1200, "volume": 300,
                "delta": 0.55, "gamma": 0.02, "theta": -0.09, "vega": 0.20, "rho": 0.10,
                "last_trade_price": 8.2,
            },
            {
                "option": "AAPL260717P00280000", "bid": 6.0, "ask": 6.3,
                "iv": 0.33, "open_interest": 900, "volume": 150,
                "delta": -0.45, "gamma": 0.02, "theta": -0.08, "vega": 0.20, "rho": -0.09,
                "last_trade_price": 6.1,
            },
            {
                "option": "AAPL260815C00300000", "bid": 3.0, "ask": 3.3,
                "iv": 0.30, "open_interest": 500, "volume": 50,
                "delta": 0.30, "gamma": 0.01, "theta": -0.05, "vega": 0.18, "rho": 0.06,
                "last_trade_price": 3.1,
            },
        ],
    }
}

YAHOO = {
    "chart": {
        "result": [
            {
                "timestamp": [1717200000, 1717286400, 1717372800],
                "indicators": {
                    "quote": [
                        {
                            "open": [185.0, 188.0, 188.0],
                            "high": [189.0, 190.0, 191.0],
                            "low": [184.0, 186.0, 187.0],
                            "close": [188.0, 187.0, None],  # last row has a null close
                        }
                    ]
                },
            }
        ]
    }
}


def test_parse_occ_symbol():
    root, exp, otype, strike = parse_occ_symbol("AAPL260717C00280000")
    assert root == "AAPL"
    assert exp == date(2026, 7, 17)
    assert otype == OptionType.CALL
    assert strike == 280.0
    # Put + fractional strike + multi-char root
    _, _, ot2, k2 = parse_occ_symbol("SPY260919P00612500")
    assert ot2 == OptionType.PUT
    assert k2 == 612.5


def test_parse_cboe_quote():
    q = parse_cboe_quote(CBOE, "AAPL")
    assert q.last == 282.50


def test_parse_cboe_expirations():
    exps = parse_cboe_expirations(CBOE, today=date(2026, 6, 29))
    assert exps == [date(2026, 7, 17), date(2026, 8, 15)]


def test_parse_cboe_chain_filters_by_expiration_and_side():
    today = date(2026, 6, 29)
    july = parse_cboe_chain(CBOE, date(2026, 7, 17), side="both", today=today)
    assert len(july) == 2
    call = next(c for c in july if c.option_type == OptionType.CALL)
    assert call.strike == 280.0
    assert call.open_interest == 1200
    assert call.provider_iv == 0.31
    assert call.dte == (date(2026, 7, 17) - today).days

    calls = parse_cboe_chain(CBOE, date(2026, 7, 17), side="calls", today=today)
    assert len(calls) == 1


def test_parse_yahoo_history_drops_nulls_and_sorts():
    bars = parse_yahoo_history(YAHOO)
    assert len(bars) == 2  # null-close row dropped
    assert bars[0].day < bars[1].day
    assert bars[0].close == 188.0


# --- retry / backoff behavior (offline, mocked transport) ------------------- #

class _Resp:
    def __init__(self, status, json_data=None, headers=None):
        self.status_code = status
        self._json = json_data or {}
        self.headers = headers or {}

    def json(self):
        return self._json


class _FakeClient:
    """Returns queued responses in order; repeats the last one when exhausted."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def get(self, url, params=None):
        r = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return r


def _provider_with(responses, monkeypatch):
    monkeypatch.setattr(cboe_mod.time, "sleep", lambda *_a, **_k: None)  # no real waits
    p = CboeProvider()
    p._client = _FakeClient(responses)
    return p


def test_chart_retries_then_succeeds(monkeypatch):
    p = _provider_with([_Resp(429), _Resp(200, YAHOO)], monkeypatch)
    bars = p.get_history("AAPL")          # 429 -> retry -> 200
    assert len(bars) == 2
    assert p._client.calls == 2


def test_chart_raises_feederror_after_exhausting(monkeypatch):
    p = _provider_with([_Resp(429)], monkeypatch)  # always 429
    with pytest.raises(FeedError):
        p.get_history("AAPL")
    assert p._client.calls == p._retries + 1       # all attempts used


def test_options_json_tries_candidates_then_feederror(monkeypatch):
    # 404 on every spelling -> FeedError (not a silent None).
    p = _provider_with([_Resp(404)], monkeypatch)
    with pytest.raises(FeedError):
        p.get_expirations("NOPE")
