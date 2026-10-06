"""The market sweep's request boundary, and the config knobs it claims to honor.

Every test here is offline: no network, no token, no market data.

The headline case is ``test_market_scan_rejects_unknown_side``. The single-ticker
path learned this lesson already (an unrecognized ``side`` used to widen to both
sides silently); ``/market/scan`` was the last way in that still did it, and the
sweep's answer arrives through a background thread, so the caller could not tell
a widened board from the one they asked for.
"""

from datetime import date, timedelta

import pytest

from app.api import MarketScanRequest
from app.config import Settings
from app.models import Contract, OHLC, OptionType, Quote
from app.providers.base import OptionsDataProvider, UnknownSide, filter_side, normalize_side
from app.scanner import ScanFilters, scan_symbol
from app.store import Store


# --------------------------------------------------------------------------- #
# Validation is shared by HTTP and direct scanner/provider callers.
# --------------------------------------------------------------------------- #


def test_filter_side_rejects_an_unknown_side():
    for unknown in ("callz", "putss", "X", "ccalls"):
        with pytest.raises(UnknownSide):
            filter_side(OptionType.CALL, unknown)


def test_filter_side_still_narrows_the_documented_values():
    assert filter_side(OptionType.CALL, "calls") is True
    assert filter_side(OptionType.PUT, "calls") is False
    assert filter_side(OptionType.PUT, "puts") is True
    assert filter_side(OptionType.CALL, "puts") is False
    for both in ("both", "all"):
        assert filter_side(OptionType.CALL, both) is True
        assert filter_side(OptionType.PUT, both) is True


# --------------------------------------------------------------------------- #
# The API boundary: a typo must not become a full board.
# --------------------------------------------------------------------------- #

class _BoomProvider(OptionsDataProvider):
    """Raises if a provider method is touched -- proves the 422 came first."""

    supports_batch_quotes = False

    def __getattr__(self, name):  # pragma: no cover - only fires on a bug
        raise AssertionError(f"provider.{name} was called for an invalid request")


def _client(monkeypatch):
    from fastapi.testclient import TestClient

    import app.api as api

    monkeypatch.setattr(api, "get_provider", lambda: _BoomProvider())
    monkeypatch.setattr(api, "get_store", lambda: Store(":memory:"))
    return TestClient(api.app)


@pytest.mark.parametrize("bad", ["callz", "putss", "X", "ccalls", "CALLZ", "  "])
def test_market_scan_rejects_unknown_side(monkeypatch, bad):
    """An unrecognized side is a 422, not a silent widening to both sides."""
    client = _client(monkeypatch)
    r = client.post("/market/scan", json={"side": bad})
    assert r.status_code == 422, f"side={bad!r} -> {r.status_code}"
    assert any("side must be one of" in item["msg"] for item in r.json()["detail"])


def test_market_scan_rejects_unknown_side_before_starting_a_sweep(monkeypatch):
    """A rejected request must not kick off any sweep behind it.

    The module-level state is imported once per process, so a sweep left over
    from an earlier test is possible; what must hold is that this request
    changed nothing. The provider raises if it is touched at all, which is the
    real proof that the 422 landed before any work started.
    """
    import app.market as market

    before = market.market_status()

    client = _client(monkeypatch)
    r = client.post("/market/scan", json={"side": "callz", "limit": 3})
    assert r.status_code == 422

    after = market.market_status()
    assert after["params"].get("side") != "callz"
    assert after["started_on"] == before["started_on"]
    assert after["status"] == before["status"]


@pytest.mark.parametrize("good", ["both", "all", "calls", "call", "puts", "PUT", " both "])
def test_market_scan_accepts_the_documented_sides(good):
    """The spellings the UI and the docs use still validate."""
    assert MarketScanRequest(side=good).side == normalize_side(good)


def test_market_scan_still_starts_on_a_good_request(monkeypatch):
    """The happy path is unchanged: a valid request still kicks off the sweep."""
    import app.market as market

    class Small(OptionsDataProvider):
        supports_batch_quotes = False

        def get_quote(self, symbol):
            return Quote(symbol=symbol, last=50.0, dividend_yield=0.0)

        def get_expirations(self, symbol):
            return []

        def get_chain(self, symbol, expiration, side="both"):
            return []

        def get_history(self, symbol, days=60):
            return []

    import app.api as api

    monkeypatch.setattr(api, "get_provider", lambda: Small())
    monkeypatch.setattr(api, "get_store", lambda: Store(":memory:"))
    monkeypatch.setattr(api, "get_cboe_provider", lambda: Small())
    monkeypatch.setattr(api, "get_realtime_provider", lambda: None)
    monkeypatch.setattr(market, "_universe_cache", [])

    from fastapi.testclient import TestClient

    client = TestClient(api.app)
    r = client.post("/market/scan", json={"side": "calls", "limit": 3})
    assert r.status_code == 200
    assert r.json()["status"] in ("running", "done")


# --------------------------------------------------------------------------- #
# The endpoint's answer for a *.4xx* typo has to be the same one /scan gives.
# --------------------------------------------------------------------------- #

def test_a_typo_never_produces_a_widened_board_through_scan_symbol():
    """The observable consequence, at the level the providers actually see.

    Direct scanner callers reject a typo too; every path shares the same
    provider-level validation rather than silently widening the request.
    """
    class TwoSided(OptionsDataProvider):
        supports_batch_quotes = False

        def get_quote(self, symbol):
            return Quote(symbol=symbol, last=100.0)

        def get_expirations(self, symbol):
            return [date.today() + timedelta(days=30)]

        def get_chain(self, symbol, expiration, side="both"):
            out = []
            for k in (100.0, 105.0):
                for ot in (OptionType.CALL, OptionType.PUT):
                    if not filter_side(ot, side):
                        continue
                    out.append(Contract(
                        underlying=symbol, occ_symbol=f"{symbol}{k}{ot.value}",
                        option_type=ot, strike=k, expiration=expiration, dte=30,
                        bid=3.0, ask=3.2, last=3.1, volume=500, open_interest=1000,
                    ))
            return out

        def get_history(self, symbol, days=60):
            return []

    prov, store = TwoSided(), Store(":memory:")
    with pytest.raises(UnknownSide):
        scan_symbol("TEST", prov, store, 100.0, 0.0, 0.30, ScanFilters(side="callz"))
    narrowed = scan_symbol("TEST", prov, store, 100.0, 0.0, 0.30,
                           ScanFilters(side="calls"))
    assert {sc.contract.option_type for sc in narrowed.scored} == {OptionType.CALL}


# --------------------------------------------------------------------------- #
# MAX_RESULTS: a documented ceiling that is now actually enforced.
#
# It was documented in .env.example ("Max scored contracts returned by a single
# scan") and assigned in config.py, but no code path read it -- /scan returned
# req.limit and the sweep returned its own limit, so turning the knob changed
# nothing. It is now the server-side ceiling on a request's returned contracts.
# --------------------------------------------------------------------------- #


def _many_calls_provider(n=150):
    """A provider with `n` tradeable calls on a single 30-DTE expiration."""

    class Many(OptionsDataProvider):
        supports_batch_quotes = False

        def get_quote(self, symbol):
            return Quote(symbol=symbol, last=100.0)

        def get_expirations(self, symbol):
            return [date.today() + timedelta(days=30)]

        def get_chain(self, symbol, expiration, side="both"):
            return [
                Contract(
                    underlying=symbol, occ_symbol=f"{symbol}{k}call",
                    option_type=OptionType.CALL, strike=k, expiration=expiration,
                    dte=30, bid=3.0, ask=3.2, last=3.1, volume=500, open_interest=1000,
                )
                for k in (90.0 + i for i in range(n))
            ]

        def get_history(self, symbol, days=60):
            return []

    return Many()


def _scan_client(monkeypatch, provider):
    from fastapi.testclient import TestClient

    import app.api as api

    monkeypatch.setattr(api, "get_provider", lambda: provider)
    monkeypatch.setattr(api, "get_store", lambda: Store(":memory:"))
    return TestClient(api.app)


def test_max_results_caps_a_scan_through_the_api(monkeypatch):
    """A request for 500 contracts is capped at MAX_RESULTS, and says so."""
    import app.api as api

    monkeypatch.setattr(api.settings, "max_results", 100)
    client = _scan_client(monkeypatch, _many_calls_provider(150))

    r = client.post("/scan", json={"ticker": "TEST", "side": "calls", "limit": 500})
    assert r.status_code == 200
    body = r.json()
    assert len(body["results"]) == 100                      # capped, not 150
    assert any("MAX_RESULTS caps" in n for n in body["meta"]["notes"])


def test_max_results_only_lowers_a_request_never_widens_it(monkeypatch):
    """A request below the ceiling is honored unchanged."""
    import app.api as api

    monkeypatch.setattr(api.settings, "max_results", 100)
    client = _scan_client(monkeypatch, _many_calls_provider(150))

    r = client.post("/scan", json={"ticker": "TEST", "side": "calls", "limit": 3})
    assert r.status_code == 200
    assert len(r.json()["results"]) == 3


def test_max_results_zero_disables_the_cap(monkeypatch):
    """0 means "no cap", matching the other 0-means-unlimited sweep knobs."""
    import app.api as api

    monkeypatch.setattr(api.settings, "max_results", 0)
    client = _scan_client(monkeypatch, _many_calls_provider(150))

    r = client.post("/scan", json={"ticker": "TEST", "side": "calls", "limit": 500})
    assert r.status_code == 200
    assert len(r.json()["results"]) == 150


class _SweepProvider(OptionsDataProvider):
    """Minimal offline provider for the sweep (price + history + a small chain)."""

    supports_batch_quotes = False

    def __init__(self, prices):
        self.prices = prices

    def get_quote(self, symbol):
        return Quote(symbol=symbol, last=self.prices.get(symbol, 0.0))

    def get_price_and_history(self, symbol, days=60):
        price = self.prices.get(symbol, 0.0)
        bars, p = [], price or 100.0
        for i in range(40):
            o = p
            c = p * (1.0 + 0.012 * (((i % 5) - 2) / 2.0))
            bars.append(OHLC(day=date.today() - timedelta(days=40 - i), open=o,
                             high=max(o, c) * 1.01, low=min(o, c) * 0.99, close=c, volume=1e6))
            p = c
        return price, bars

    def get_history(self, symbol, days=60):
        return self.get_price_and_history(symbol, days)[1]

    def get_expirations(self, symbol):
        return [date.today() + timedelta(days=30)]

    def get_chain(self, symbol, expiration, side="both"):
        price = self.prices.get(symbol, 100.0)
        mid = max(0.50, price * 0.03)
        out = []
        for k in (price * 0.95, price, price * 1.05):
            for ot in (OptionType.CALL, OptionType.PUT):
                out.append(Contract(
                    underlying=symbol.upper(), occ_symbol=f"{symbol}{int(k)}{ot.value}",
                    option_type=ot, strike=round(k, 1), expiration=expiration, dte=30,
                    bid=round(mid * 0.98, 2), ask=round(mid * 1.02, 2), last=round(mid, 2),
                    volume=500, open_interest=1000))
        return out


def test_max_results_caps_the_market_board(monkeypatch):
    """The sweep honors the same ceiling and surfaces it in the notes."""
    import app.market as market

    prices = {f"SYM{i:02d}": 50.0 + i for i in range(20)}
    monkeypatch.setattr(market, "_universe_cache", list(prices.keys()))
    monkeypatch.setattr(market.settings, "max_results", 5)

    top, notes = market.run_market_sweep(
        {"side": "both", "limit": 50}, _SweepProvider(prices), Store(":memory:"),
        lambda *a: None,
    )
    assert len(top) == 5
    assert any("MAX_RESULTS caps" in n for n in notes)


def test_scan_symbol_itself_stays_uncapped(monkeypatch):
    """The ceiling lives at the request boundary, not in the ranking core."""
    prov, store = _many_calls_provider(20), Store(":memory:")
    res = scan_symbol("TEST", prov, store, 100.0, 0.0, 0.30,
                      ScanFilters(side="calls"), settings=Settings(max_results=2))
    assert len(res.scored) == 20
