"""Offline tests for Tradier JSON parsing (no network)."""

from datetime import date

from app.models import OHLC, OptionType, Quote
from app.providers.tradier import (
    TradierProvider,
    _as_list,
    parse_chain,
    parse_expirations,
    parse_history,
    parse_quote,
    parse_quotes_batch,
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
