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


_NOT_JSON = object()


class _Resp:
    def __init__(self, status, json_data=_NOT_JSON, headers=None):
        self.status_code = status
        self._json = json_data
        self.headers = headers or {}

    def json(self):
        if self._json is _NOT_JSON:
            raise ValueError("not json")
        return self._json


class _SeqClient:
    """Returns queued responses in order, repeating the last when exhausted."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0
        self.last_url = None

    def _next(self, url):
        self.last_url = url
        r = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return r

    def get(self, url, params=None):
        return self._next(url)

    def post(self, url, data=None, params=None):
        return self._next(url)


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

    p = _cboe_provider([_Resp(200)], monkeypatch)  # 200, body is not JSON
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
    """The distinction this whole change exists to preserve, on the Tradier side."""
    p = _tradier_provider(monkeypatch)
    p._client = _SeqClient([_Resp(200, {"options": None})])
    assert p.get_chain("AAPL", date.today()) == []


def test_tradier_non_object_json_is_not_an_empty_chain(monkeypatch):
    """A JSON non-object at HTTP 200 must raise, not read as "no options".

    ``_parse_json`` collapsed anything that was not a dict to ``{}``, so the
    chain/history parsers found no ``options`` / ``history`` key and returned an
    empty list -- byte-identical to a legitimate empty feed, which let a
    malformed response be reported as a valid empty board.
    """
    from app.providers.base import FeedError

    # list / string / number / JSON-null. (An HTTP-200 HTML body is the
    # separate `unparseable` branch, already covered below.)
    for body in (["not", "an", "object"], "null-body", 42, None):
        p = _tradier_provider(monkeypatch)
        p._client = _SeqClient([_Resp(200, body)])
        with pytest.raises(FeedError) as ei:
            p.get_chain("AAPL", date.today())
        assert "shape" in str(ei.value)
        assert ei.value.symbol == "AAPL"


def test_tradier_unparseable_200_is_not_an_empty_chain(monkeypatch):
    """An HTML (non-JSON) 200 body takes the unparseable branch, same contract."""
    from app.providers.base import FeedError

    p = _tradier_provider(monkeypatch)
    p._client = _SeqClient([_Resp(200)])  # default body is not JSON
    with pytest.raises(FeedError) as ei:
        p.get_chain("AAPL", date.today())
    assert "unparseable" in str(ei.value)


def test_tradier_non_object_json_raises_on_get_and_batch_post(monkeypatch):
    """Both transports: the GET path and the batched POST path."""
    from app.providers.base import FeedError

    # GET -- expirations and history both go through _get -> _parse_json
    for call in (
        lambda p: p.get_expirations("AAPL"),
        lambda p: p.get_history("AAPL"),
    ):
        p = _tradier_provider(monkeypatch)
        p._client = _SeqClient([_Resp(200, ["a", "list"])])
        with pytest.raises(FeedError) as ei:
            call(p)
        assert "shape" in str(ei.value)

    # POST -- get_quotes_batch goes through _post -> _parse_json
    p = _tradier_provider(monkeypatch)
    p._client = _SeqClient([_Resp(200, 7)])
    with pytest.raises(FeedError) as ei:
        p.get_quotes_batch(["AAPL", "MSFT"])
    assert "shape" in str(ei.value)
    assert "AAPL,MSFT" in ei.value.symbol  # the batch names the chunk it lost


def test_tradier_wellformed_empty_batch_is_still_empty(monkeypatch):
    """A well-formed 200 carrying no quotes is a normal empty dict, not an error."""
    p = _tradier_provider(monkeypatch)
    p._client = _SeqClient([_Resp(200, {"quotes": None})])
    assert p.get_quotes_batch(["AAPL"]) == {}


# --------------------------------------------------------------------------- #
# Endpoint coverage: the error contract as the HTTP client actually sees it.
#
# Until now the README claimed TestClient coverage that did not exist -- this
# module never imported TestClient. These tests drive the real FastAPI app, so
# the 502 / 200 / 422 mapping is proven through the stack, not asserted in prose.
# --------------------------------------------------------------------------- #


def _client_with_provider(monkeypatch, provider):
    """A TestClient whose /scan provider is a stub (no network, no token)."""
    from fastapi.testclient import TestClient

    import app.api as api
    from app.store import Store

    monkeypatch.setattr(api, "get_provider", lambda: provider)
    monkeypatch.setattr(api, "get_store", lambda: Store(":memory:"))
    return TestClient(api.app)


def test_endpoint_malformed_feed_returns_502(monkeypatch):
    """A FeedError from the provider must surface as 502, naming the reason."""
    from app.providers.base import FeedError

    class Boom:
        supports_batch_quotes = False

        def get_quote(self, symbol):
            raise FeedError(symbol, "chain: unexpected JSON shape (not an object)")

        def get_history(self, symbol, days=60):  # pragma: no cover - not reached
            raise FeedError(symbol, "unreachable")

    client = _client_with_provider(monkeypatch, Boom())
    r = client.post("/scan", json={"ticker": "AAPL"})
    assert r.status_code == 502
    body = r.json()
    assert "Data feed unavailable" in body["detail"]
    assert "unexpected JSON shape" in body["detail"]


def test_endpoint_wellformed_empty_feed_returns_200(monkeypatch):
    """A legitimate empty chain is 200 with an empty result -- not a 502."""
    from datetime import date as _date

    from app.models import Quote

    class EmptyButHealthy:
        supports_batch_quotes = False

        def get_quote(self, symbol):
            return Quote(symbol=symbol, last=100.0, dividend_yield=0.0)

        def get_history(self, symbol, days=60):
            return []

        def get_expirations(self, symbol):
            return []

        def get_chain(self, symbol, expiration, side="both"):
            return []

    client = _client_with_provider(monkeypatch, EmptyButHealthy())
    r = client.post("/scan", json={"ticker": "ZZZZ"})
    assert r.status_code == 200
    body = r.json()
    assert body["meta"]["ticker"] == "ZZZZ"
    assert body["results"] == []


def test_endpoint_inverted_and_blank_requests_are_422(monkeypatch):
    """Impossible requests are rejected before any provider work happens.

    The provider here raises if it is ever touched, which proves the 422 came
    from request validation and not from a feed error wearing a validation code.
    """

    class MustNotBeCalled:
        supports_batch_quotes = False

        def __getattr__(self, name):
            raise AssertionError(f"provider.{name} was called for an invalid request")

    client = _client_with_provider(monkeypatch, MustNotBeCalled())

    cases = [
        {"ticker": "AAPL", "expiration_from": "2026-12-01", "expiration_to": "2026-01-01"},
        {"ticker": "AAPL", "premium_min": 9.0, "premium_max": 1.0},
        {"ticker": "   "},
    ]
    for payload in cases:
        r = client.post("/scan", json=payload)
        assert r.status_code == 422, f"{payload} -> {r.status_code}"


def test_endpoint_market_scan_rejects_inverted_bands_422(monkeypatch):
    """The market endpoint's band validation is enforced at the HTTP boundary too."""
    from fastapi.testclient import TestClient

    import app.api as api

    client = TestClient(api.app)
    for payload in (
        {"dte_from": 60, "dte_to": 14},
        {"price_min": 500, "price_max": 100},
        {"premium_min": 9, "premium_max": 1},
    ):
        r = client.post("/market/scan", json=payload)
        assert r.status_code == 422, f"{payload} -> {r.status_code}"


def test_endpoint_health_and_key_still_serve(monkeypatch):
    """The endpoints the frontend legend depends on, over the real app."""
    from fastapi.testclient import TestClient

    import app.api as api

    client = TestClient(api.app)
    assert client.get("/health").status_code == 200
    r = client.get("/key")
    assert r.status_code == 200
    assert "grade_key" in r.json() and "sub_score_labels" in r.json()


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
