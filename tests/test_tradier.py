"""Offline tests for Tradier JSON parsing (no network)."""

from datetime import date, datetime, timezone

import httpx

from app.models import OHLC, OptionType, Quote
from app.providers.base import FeedError
from app.providers.tradier import (
    TradierProvider,
    _as_list,
    parse_chain,
    parse_expirations,
    parse_history,
    parse_quote,
    parse_quotes_batch,
    parse_retry_after,
)

QUOTE = {"quotes": {"quote": {"symbol": "AAPL", "last": 190.5, "close": 189.0}}}

EXPIRATIONS = {"expirations": {"date": ["2026-07-24", "2026-07-17", "2026-08-21"]}}

CHAIN = {
    "options": {
        "option": [
            {
                "symbol": "AAPL260717C00190000", "option_type": "call", "strike": 190.0,
                "bid": 3.0, "ask": 3.2, "last": 3.1, "volume": 120, "open_interest": 540,
                "expiration_date": "2026-07-17", "underlying": "AAPL",
                "greeks": {"mid_iv": 0.255},
            },
            {
                "symbol": "AAPL260717P00190000", "option_type": "put", "strike": 190.0,
                "bid": 2.5, "ask": 2.7, "last": 2.6, "volume": 80, "open_interest": 300,
                "expiration_date": "2026-07-17", "underlying": "AAPL",
                "greeks": {"mid_iv": 0.262},
            },
        ]
    }
}

HISTORY = {
    "history": {
        "day": [
            {"date": "2026-06-03", "open": 188, "high": 191, "low": 187, "close": 190, "volume": 1e7},
            {"date": "2026-06-01", "open": 185, "high": 189, "low": 184, "close": 188, "volume": 1e7},
            {"date": "2026-06-02", "open": 188, "high": 190, "low": 186, "close": 187, "volume": 1e7},
        ]
    }
}


def test_as_list_normalizes_scalars():
    assert _as_list(None) == []
    assert _as_list("x") == ["x"]
    assert _as_list(["a", "b"]) == ["a", "b"]
    assert _as_list({"k": 1}) == [{"k": 1}]


def test_parse_quote():
    q = parse_quote(QUOTE, "AAPL")
    assert q.symbol == "AAPL"
    assert q.last == 190.5
    assert q.dividend_yield == 0.0


def test_parse_expirations_sorted():
    exps = parse_expirations(EXPIRATIONS)
    assert exps == [date(2026, 7, 17), date(2026, 7, 24), date(2026, 8, 21)]


def test_parse_chain_both_and_filtered():
    today = date(2026, 6, 29)
    both = parse_chain(CHAIN, side="both", today=today)
    assert len(both) == 2
    call = next(c for c in both if c.option_type == OptionType.CALL)
    assert call.strike == 190.0
    assert call.dte == (date(2026, 7, 17) - today).days
    assert call.provider_iv == 0.255
    assert call.mid == 3.1

    calls_only = parse_chain(CHAIN, side="calls", today=today)
    assert len(calls_only) == 1
    assert calls_only[0].option_type == OptionType.CALL


def test_parse_history_sorted_oldest_first():
    bars = parse_history(HISTORY)
    assert [b.day for b in bars] == [date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3)]
    assert bars[0].close == 188


def test_empty_payloads_are_safe():
    assert parse_expirations({"expirations": None}) == []
    assert parse_chain({"options": None}) == []
    assert parse_history({"history": None}) == []


# --- batch quotes ----------------------------------------------------------- #

BATCH = {
    "quotes": {
        "quote": [
            {"symbol": "AAPL", "last": 190.5, "close": 189.0},
            {"symbol": "MSFT", "last": 0.0, "close": 410.0},  # last missing -> close
        ]
    }
}


def test_parse_quotes_batch_keys_by_symbol_and_falls_back():
    out = parse_quotes_batch(BATCH)
    assert set(out) == {"AAPL", "MSFT"}
    assert out["AAPL"].last == 190.5
    assert out["MSFT"].last == 410.0  # fell back from last=0 to close


def test_parse_quotes_batch_handles_single_object():
    # Tradier collapses a one-element array to a scalar object.
    out = parse_quotes_batch({"quotes": {"quote": {"symbol": "SPY", "last": 600.0}}})
    assert out["SPY"].last == 600.0


def test_get_quotes_batch_chunks_by_size(monkeypatch):
    import app.providers.tradier as tr

    monkeypatch.setattr(tr.settings, "quote_batch_size", 2)
    p = TradierProvider(token="x")
    sent = []

    def fake_post(path, data, retries=3):
        syms = data["symbols"].split(",")
        sent.append(syms)
        return {"quotes": {"quote": [{"symbol": s, "last": 10.0} for s in syms]}}

    p._post = fake_post
    out = p.get_quotes_batch(["A", "B", "C", "D", "E"])
    assert [len(c) for c in sent] == [2, 2, 1]   # chunked by size 2
    assert set(out) == {"A", "B", "C", "D", "E"}


def test_get_price_and_history_combines_quote_and_history(monkeypatch):
    p = TradierProvider(token="x")
    bars = [OHLC(day=date(2026, 6, 1), open=1, high=2, low=1, close=1.5)]
    p.get_quote = lambda s: Quote(symbol=s, last=123.0)
    p.get_history = lambda s, days=60: bars
    price, hist = p.get_price_and_history("AAPL")
    assert price == 123.0
    assert hist == bars


# --- 429 retry policy ------------------------------------------------------- #

_URL = "https://api.tradier.com/v1/markets/quotes"


class _ScriptedClient:
    """Stand-in for httpx.Client that replays a fixed list of responses."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def _next(self):
        self.calls += 1
        if self.script:
            return self.script.pop(0)
        return _resp(200, payload={})

    def get(self, path, params=None):
        return self._next()

    def post(self, path, data=None):
        return self._next()


def _resp(status, headers=None, payload=None):
    # A real httpx.Response carries its request, which raise_for_status() needs.
    return httpx.Response(
        status, headers=headers, json=payload, request=httpx.Request("GET", _URL)
    )


def _scripted(monkeypatch, script, cap=None):
    """Provider on scripted responses, with every sleep recorded not performed."""
    import app.providers.tradier as tr

    if cap is not None:
        monkeypatch.setattr(tr.settings, "feed_backoff_cap", cap)
    p = TradierProvider(token="x")
    client = _ScriptedClient(script)
    p._client = client
    slept = []
    monkeypatch.setattr(tr.time, "sleep", slept.append)
    return p, slept, client


def test_parse_retry_after_handles_both_legal_forms():
    assert parse_retry_after("120") == 120.0
    assert parse_retry_after(" 0 ") == 0.0
    assert parse_retry_after("-5") == 0.0           # never a negative wait
    assert parse_retry_after("soon") is None        # unusable -> caller's schedule
    assert parse_retry_after("") is None
    assert parse_retry_after(None) is None

    now = datetime(2026, 10, 21, 7, 0, tzinfo=timezone.utc)
    assert parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT", now=now) == 1680.0
    assert parse_retry_after("Wed, 21 Oct 2026 06:00:00 GMT", now=now) == 0.0  # past -> now
    assert parse_retry_after("21 Oct 2026 07:28:00", now=now) == 1680.0        # no zone -> UTC


def test_429_with_http_date_retry_after_backs_off_instead_of_raising(monkeypatch):
    # The header is legal as an HTTP-date. Pre-fix it reached float(), raised
    # ValueError straight out of the retry loop, and the call died without a
    # retry, without a wait, and without a typed error.
    p, slept, client = _scripted(monkeypatch, [
        _resp(429, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
        _resp(200, payload={"quotes": {"quote": {"symbol": "AAPL", "last": 10.0}}}),
    ])
    out = p._get("/v1/markets/quotes", {"symbols": "AAPL"})
    assert out["quotes"]["quote"]["symbol"] == "AAPL"
    assert client.calls == 2      # it retried
    assert len(slept) == 1        # instead of dying on the header


def test_429_retry_after_is_clamped_to_the_backoff_cap(monkeypatch):
    # A response header must not be able to park a worker thread for an hour.
    p, slept, _ = _scripted(monkeypatch, [
        _resp(429, {"Retry-After": "3600"}),
        _resp(200, payload={"ok": True}),
    ], cap=12.0)
    p._get("/v1/markets/quotes", {"symbols": "AAPL"})
    assert slept == [12.0]


def test_429_falls_back_to_exponential_backoff(monkeypatch):
    p, slept, _ = _scripted(monkeypatch, [
        _resp(429),                                    # no header at all
        _resp(429, {"Retry-After": "not a delay"}),    # header, unusable
        _resp(200, payload={"ok": True}),
    ])
    p._get("/v1/markets/quotes", {"symbols": "AAPL"})
    assert slept == [1.0, 2.0]    # base * 2**attempt, default base 1.0


def test_exhausted_429_raises_a_named_feed_error(monkeypatch):
    p, slept, client = _scripted(monkeypatch, [_resp(429) for _ in range(4)])
    try:
        p._get("/v1/markets/quotes", {"symbols": "AAPL"})
        raise AssertionError("expected FeedError")
    except FeedError as e:
        assert e.symbol == "AAPL"          # named, so the sweep can count it
        assert "429" in e.reason
    assert client.calls == 4               # retries=3 -> four attempts, then stop
    assert len(slept) == 3                 # wait between attempts, none after the last


def test_exhausted_429_names_the_symbol_on_the_batch_path(monkeypatch):
    p, _, _ = _scripted(monkeypatch, [_resp(429) for _ in range(4)])
    try:
        p._post("/v1/markets/quotes", {"symbols": "MSFT,AMZN"})
        raise AssertionError("expected FeedError")
    except FeedError as e:
        assert e.symbol == "MSFT"          # first ticker of the batch


def test_non_429_error_status_still_raises_http_status_error(monkeypatch):
    # Pinned so the asymmetry that remains with CBOE is visible, not assumed.
    p, _, _ = _scripted(monkeypatch, [_resp(503)])
    try:
        p._get("/v1/markets/quotes", {"symbols": "AAPL"})
        raise AssertionError("expected HTTPStatusError")
    except httpx.HTTPStatusError:
        pass
