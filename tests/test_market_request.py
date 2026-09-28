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
from app.models import Contract, OptionType, Quote
from app.providers.base import OptionsDataProvider, filter_side
from app.scanner import ScanFilters, scan_symbol
from app.store import Store


# --------------------------------------------------------------------------- #
# filter_side still widens an unknown value -- which is exactly why the API
# boundary has to reject one before it ever reaches a provider.
# --------------------------------------------------------------------------- #

def test_filter_side_widens_an_unknown_side():
    """Documents the provider-level behavior the API guards against.

    If a future change makes filter_side raise instead, this test fails on
    purpose: the guard in app/api.py becomes redundant and should be revisited
    rather than left to rot.
    """
    for unknown in ("callz", "putss", "", "X", "ccalls"):
        assert filter_side(OptionType.CALL, unknown) is True
        assert filter_side(OptionType.PUT, unknown) is True


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
    assert "side must be one of" in r.json()["detail"]


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
    assert MarketScanRequest(side=good).side == good


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

    Reaching scan_symbol with a typo returns calls AND puts; the API guards
    exist so that request never gets that far. This pins the behavior the guard
    is protecting the user from.
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
    widened = scan_symbol("TEST", prov, store, 100.0, 0.0, 0.30,
                          ScanFilters(side="callz"))
    narrowed = scan_symbol("TEST", prov, store, 100.0, 0.0, 0.30,
                           ScanFilters(side="calls"))
    assert {sc.contract.option_type for sc in widened.scored} == {
        OptionType.CALL, OptionType.PUT
    }
    assert {sc.contract.option_type for sc in narrowed.scored} == {OptionType.CALL}


# --------------------------------------------------------------------------- #
# MAX_RESULTS: documented in .env.example, assigned in config.py, read nowhere.
# --------------------------------------------------------------------------- #

def test_max_results_is_not_consulted_by_a_scan(monkeypatch):
    """Turning MAX_RESULTS down must not change a scan's returned count.

    The README now lists this as a known rough edge. If someone wires the knob
    up for real, this test fails and the README note should be removed with it.
    """
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
                for k in (90.0 + i for i in range(20))
            ]

        def get_history(self, symbol, days=60):
            return []

    prov, store = Many(), Store(":memory:")
    capped = Settings(max_results=2)
    res = scan_symbol("TEST", prov, store, 100.0, 0.0, 0.30,
                      ScanFilters(side="calls"), settings=capped)
    assert len(res.scored) == 20, (
        "MAX_RESULTS is documented as a cap but scan_symbol ignores it; "
        "if this now equals 2, the knob was wired up - update the README caveat"
    )
