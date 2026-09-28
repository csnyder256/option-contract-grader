"""Regression tests for the "a failure must not look like an answer" class.

Every case here is a shape where a broken request or a broken feed used to
produce a plausible-looking normal result instead of an error:

- a non-200 (or non-JSON) feed response parsed into an empty chain
- a crossed book treated as a quotable two-sided market
- a future-dated IV snapshot counted as history
- an inverted filter range answered with an empty board

The tests run offline; the provider cases drive a fake transport.
"""

from datetime import date, timedelta

import pytest

from app.models import Contract, OptionType
from app.store import Store

# --------------------------------------------------------------------------- #
# Crossed / one-sided markets.
# --------------------------------------------------------------------------- #


def _contract(bid: float, ask: float, last: float = 0.0) -> Contract:
    return Contract(
        underlying="TEST", occ_symbol="TEST", option_type=OptionType.CALL,
        strike=100.0, expiration=date.today() + timedelta(days=30), dte=30,
        bid=bid, ask=ask, last=last, volume=0, open_interest=0,
    )


def test_crossed_book_is_not_a_two_sided_market():
    c = _contract(bid=5.00, ask=1.00, last=3.0)
    assert c.has_two_sided_market is False
    assert c.spread_pct is None          # no spread signal from a crossed pair
    assert c.spread == 0.0
    assert c.mid == 3.0                  # falls back to last, not the crossed mid


def test_one_sided_market_is_not_two_sided():
    c = _contract(bid=0.0, ask=3.20, last=3.0)
    assert c.has_two_sided_market is False
    assert c.spread_pct is None
    assert c.mid == 3.0


def test_clean_two_sided_market_still_works():
    c = _contract(bid=3.00, ask=3.20, last=3.1)
    assert c.has_two_sided_market is True
    assert c.mid == pytest.approx(3.10)
    assert c.spread_pct == pytest.approx(0.20 / 3.10)


def test_locked_market_ask_equals_bid_is_a_market():
    # Zero spread is a real (rare) state; it must not be treated as crossed.
    c = _contract(bid=3.00, ask=3.00)
    assert c.has_two_sided_market is True
    assert c.spread_pct == 0.0


# --------------------------------------------------------------------------- #
# IV history must not read the future.
# --------------------------------------------------------------------------- #


def test_iv_history_excludes_future_dated_rows():
    s = Store(":memory:")
    today = date.today()
    s.save_iv_snapshot("AAA", 0.20, today - timedelta(days=2))
    s.save_iv_snapshot("AAA", 0.21, today)
    s.save_iv_snapshot("AAA", 9.99, today + timedelta(days=30))  # phantom

    hist = s.get_iv_history("AAA")
    assert hist == [0.20, 0.21]
    assert 9.99 not in hist
    assert s.snapshot_count("AAA") == 2  # the "warming up" count agrees


def test_iv_rank_ignores_future_rows():
    from app.engine.volatility import iv_rank

    s = Store(":memory:")
    today = date.today()
    for i in range(12):
        s.save_iv_snapshot("AAA", 0.20 + i * 0.01, today - timedelta(days=12 - i))
    honest = iv_rank(0.25, s.get_iv_history("AAA"))
    assert honest is not None

    # A phantom high future row would otherwise stretch the range and move rank.
    s.save_iv_snapshot("AAA", 5.0, today + timedelta(days=1))
    assert iv_rank(0.25, s.get_iv_history("AAA")) == honest


def test_future_only_history_stays_below_the_rank_floor():
    from app.engine.volatility import iv_rank

    s = Store(":memory:")
    for i in range(12):
        s.save_iv_snapshot("AAA", 0.50, date.today() + timedelta(days=i + 1))
    assert s.get_iv_history("AAA") == []
    assert s.snapshot_count("AAA") == 0
    assert iv_rank(0.50, s.get_iv_history("AAA")) is None  # "warming up", honestly


# --------------------------------------------------------------------------- #
# Feed failures must raise, not return empty.
# --------------------------------------------------------------------------- #


class _Resp:
    def __init__(self, status, json_data=None, headers=None):
        self.status_code = status
        self._json = json_data
        self.headers = headers or {}

    def json(self):
        if self._json is None:
            raise ValueError("not json")
        return self._json


class _SeqClient:
    """Returns queued responses in order, repeating the last when exhausted."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0
        self.last_url = None

    def get(self, url, params=None):
        self.last_url = url
        r = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return r


def _cboe_provider(responses, monkeypatch):
    import app.providers.cboe as cboe_mod
    from app.providers.cboe import CboeProvider

    monkeypatch.setattr(cboe_mod.time, "sleep", lambda *_a, **_k: None)
    p = CboeProvider()
    p._client = _SeqClient(responses)
    return p


def test_cboe_non_200_options_raises_feederror(monkeypatch):
    from app.providers.base import FeedError

    # A 200-with-HTML and a plain 500 both used to parse into an empty chain.
    p = _cboe_provider([_Resp(500, None)], monkeypatch)
    with pytest.raises(FeedError):
        p.get_expirations("AAPL")


def test_cboe_unparseable_200_raises_feederror(monkeypatch):
    from app.providers.base import FeedError

    p = _cboe_provider([_Resp(200, None)], monkeypatch)  # 200, body is not JSON
    with pytest.raises(FeedError) as ei:
        p.get_expirations("AAPL")
    assert "unparseable" in str(ei.value)


def test_cboe_non_object_json_raises_feederror(monkeypatch):
    from app.providers.base import FeedError

    p = _cboe_provider([_Resp(200, ["not", "an", "object"])], monkeypatch)
    with pytest.raises(FeedError):
        p.get_quote("AAPL")


def test_cboe_empty_chain_payload_is_still_a_normal_empty_result(monkeypatch):
    # A well-formed 200 with zero options is NOT an error -- the distinction
    # this whole change exists to preserve.
    p = _cboe_provider([_Resp(200, {"data": {"symbol": "ZZZZ", "options": []}})], monkeypatch)
    assert p.get_expirations("ZZZZ") == []
    assert p.get_chain("ZZZZ", date.today()) == []


def test_chart_404_still_raises_feederror(monkeypatch):
    from app.providers.base import FeedError

    p = _cboe_provider([_Resp(404, None)], monkeypatch)
    with pytest.raises(FeedError):
        p.get_history("DELISTED")


def test_check_status_is_the_shared_contract(monkeypatch):
    from app.providers.base import FeedError, OptionsDataProvider

    class Null(OptionsDataProvider):
        def get_quote(self, symbol):  # pragma: no cover - unused
            raise NotImplementedError

        def get_expirations(self, symbol):  # pragma: no cover - unused
            raise NotImplementedError

        def get_chain(self, symbol, expiration, side="both"):  # pragma: no cover
            raise NotImplementedError

        def get_history(self, symbol, days=60):  # pragma: no cover - unused
            raise NotImplementedError

    n = Null()
    n.check_status(_Resp(200, {}), "AAPL", "chart")     # no raise
    with pytest.raises(FeedError) as ei:
        n.check_status(_Resp(404, {}), "AAPL", "chart")
    assert "chart" in str(ei.value) and "404" in str(ei.value)
    assert ei.value.symbol == "AAPL"


# -- Tradier: same contract, different transport ---------------------------- #


def _tradier_provider(monkeypatch):
    import app.providers.tradier as tr

    monkeypatch.setattr(tr.time, "sleep", lambda *_a, **_k: None)
    return tr.TradierProvider(token="x")


def test_tradier_non_200_raises_feederror_not_httpx(monkeypatch):
    from app.providers.base import FeedError

    p = _tradier_provider(monkeypatch)
    p._client = _SeqClient([_Resp(503, None)])
    with pytest.raises(FeedError) as ei:
        p.get_expirations("AAPL")
    assert "503" in str(ei.value) and ei.value.symbol == "AAPL"


def test_tradier_429_after_retries_names_the_rate_limit(monkeypatch):
    from app.providers.base import FeedError

    p = _tradier_provider(monkeypatch)
    p._client = _SeqClient([_Resp(429, None)])  # always 429
    with pytest.raises(FeedError) as ei:
        p._get("/v1/markets/options/expirations", {"symbol": "AAPL"})
    assert "rate limit" in str(ei.value)


def test_tradier_errors_block_becomes_feederror(monkeypatch):
    from app.providers.base import FeedError

    p = _tradier_provider(monkeypatch)
    p._client = _SeqClient(
        [_Resp(200, {"errors": {"error": "Invalid symbol"}})]
    )
    with pytest.raises(FeedError) as ei:
        p.get_expirations("NOPE")
    assert "Invalid symbol" in str(ei.value)


def test_tradier_wellformed_empty_chain_is_not_an_error(monkeypatch):
    p = _tradier_provider(monkeypatch)
    p._client = _SeqClient([_Resp(200, {"options": None})])
    assert p.get_chain("AAPL", date.today()) == []


# --------------------------------------------------------------------------- #
# Request validation: impossible filters must not return an empty board.
# --------------------------------------------------------------------------- #


def test_scan_request_rejects_inverted_expiration_range():
    from app.api import ScanRequest

    with pytest.raises(Exception) as ei:
        ScanRequest(
            ticker="AAPL",
            expiration_from=date(2026, 12, 1),
            expiration_to=date(2026, 1, 1),
        )
    assert "expiration_from" in str(ei.value)


def test_scan_request_rejects_inverted_premium_range():
    from app.api import ScanRequest

    with pytest.raises(Exception) as ei:
        ScanRequest(ticker="AAPL", premium_min=9.0, premium_max=1.0)
    assert "premium_min" in str(ei.value)


def test_scan_request_rejects_whitespace_only_ticker():
    from app.api import ScanRequest

    with pytest.raises(Exception) as ei:
        ScanRequest(ticker="   ")
    assert "ticker" in str(ei.value)


def test_scan_request_accepts_the_documented_happy_path():
    from app.api import ScanRequest

    req = ScanRequest(ticker="AAPL", side="calls", limit=10)
    assert req.ticker == "AAPL"
    assert req.side == "calls"
    req2 = ScanRequest(
        ticker="AAPL",
        expiration_from=date(2026, 1, 1),
        expiration_to=date(2026, 12, 1),
        premium_min=0.5,
        premium_max=5.0,
    )
    assert req2.premium_max == 5.0


def test_scan_request_allows_equal_bounds():
    from app.api import ScanRequest

    d = date(2026, 6, 1)
    ScanRequest(ticker="AAPL", expiration_from=d, expiration_to=d, premium_min=1.0, premium_max=1.0)


def test_market_request_rejects_inverted_dte_and_price_bands():
    from app.api import MarketScanRequest

    with pytest.raises(Exception) as ei:
        MarketScanRequest(dte_from=60, dte_to=14)
    assert "dte_from" in str(ei.value)

    with pytest.raises(Exception) as ei:
        MarketScanRequest(price_min=500, price_max=100)
    assert "price_min" in str(ei.value)

    with pytest.raises(Exception) as ei:
        MarketScanRequest(premium_min=9, premium_max=1)
    assert "premium_min" in str(ei.value)


def test_market_request_accepts_the_documented_happy_path():
    from app.api import MarketScanRequest

    req = MarketScanRequest(price_min=20, price_max=120, dte_from=14, dte_to=60, limit=50)
    assert req.dte_to == 60
