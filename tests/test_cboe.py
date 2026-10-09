"""Offline tests for CBOE + Yahoo parsing (no network)."""

import math
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


# --- Retry-After is untrusted input ----------------------------------------- #
#
# CBOE retried 403/429/5xx with a jittered backoff, but the wait came straight
# from the response's Retry-After header via float() and then min(). A negative
# or non-finite value reached time.sleep(), which raises ValueError -- so one bad
# header killed the retry loop with an UNTYPED error: no retry, no backoff, and
# not the FeedError the rest of this module promises. The HTTP-date form is legal
# (RFC 9110) but was unread, so a legitimate long wait was silently swapped for
# the exponential schedule. Both providers now share one parser
# (app.providers.base.parse_retry_after) that accepts both legal forms and
# rejects everything else.
#
# These tests can run unmodified against the pre-fix source: there the wait
# reached the real time.sleep(), so the spy below -- which encodes time.sleep's
# own contract -- raises, and the "honored, not ignored" assertion sees the
# exponential value instead of the header's.


def _provider_with_sleep_contract(responses, monkeypatch):
    """A provider whose time.sleep enforces time.sleep's real argument contract."""
    slept = []

    def spy(seconds):
        # time.sleep(negative) and time.sleep(nan) both raise ValueError.
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("sleep length must be non-negative")
        slept.append(seconds)

    monkeypatch.setattr(cboe_mod.time, "sleep", spy)
    p = CboeProvider()
    p._client = _FakeClient(responses)
    return p, slept


def test_negative_retry_after_does_not_crash_the_retry_loop(monkeypatch):
    p, slept = _provider_with_sleep_contract(
        [_Resp(429, headers={"Retry-After": "-1"}), _Resp(200, YAHOO)], monkeypatch
    )
    bars = p.get_history("AAPL")          # backed off and retried, instead of dying
    assert len(bars) == 2
    assert p._client.calls == 2
    assert slept and all(s >= 0 for s in slept)


@pytest.mark.parametrize("junk", ["NaN", "Inf", "-Inf", "1e999"])
def test_nonfinite_retry_after_does_not_crash_the_retry_loop(junk, monkeypatch):
    p, slept = _provider_with_sleep_contract(
        [_Resp(429, headers={"Retry-After": junk}), _Resp(200, YAHOO)], monkeypatch
    )
    bars = p.get_history("AAPL")
    assert len(bars) == 2
    assert p._client.calls == 2
    assert all(math.isfinite(s) for s in slept)


def test_unusable_retry_after_still_falls_back_to_exponential(monkeypatch):
    # An unreadable header is not a retry signal of its own; the caller's own
    # schedule is. Attempt 0 -> base * 2**0 = 1.0 (plus jitter).
    p, slept = _provider_with_sleep_contract(
        [_Resp(429, headers={"Retry-After": "soon"}), _Resp(200, YAHOO)], monkeypatch
    )
    p.get_history("AAPL")
    assert slept and 1.0 <= slept[0] <= 1.25


def test_http_date_retry_after_is_honored_not_ignored(monkeypatch):
    # A legal HTTP-date must be read, not silently replaced by the exponential
    # schedule (Tradier already read both forms; CBOE read only the numeric one).
    p, slept = _provider_with_sleep_contract(
        [
            _Resp(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
            _Resp(200, YAHOO),
        ],
        monkeypatch,
    )
    p.get_history("AAPL")
    # The date is far in the future -> clamped to the cap, never the 1.0s the
    # exponential schedule would have produced for attempt 0.
    assert slept and slept[0] >= p._backoff_cap


def test_retry_after_is_clamped_to_the_backoff_cap(monkeypatch):
    # A response header must not be able to park a worker thread for an hour.
    p, slept = _provider_with_sleep_contract(
        [_Resp(429, headers={"Retry-After": "3600"}), _Resp(200, YAHOO)], monkeypatch
    )
    p.get_history("AAPL")
    assert slept and p._backoff_cap <= slept[0] <= p._backoff_cap * 1.25


def test_exhausted_retries_still_raise_a_typed_feed_error(monkeypatch):
    # With a bad header in play, the terminal outcome is still the typed error.
    p, _ = _provider_with_sleep_contract(
        [_Resp(503, headers={"Retry-After": "-1"})], monkeypatch
    )
    with pytest.raises(FeedError):
        p.get_expirations("AAPL")
    assert p._client.calls == p._retries + 1


def test_retry_after_parser_is_shared_by_both_providers():
    # The README's claim is that both providers share the outcome contract; the
    # parser behind it is one function, not two drifting copies.
    from app.providers import base, cboe, tradier

    assert base.parse_retry_after is cboe.parse_retry_after is tradier.parse_retry_after
