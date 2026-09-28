"""Offline HTTP-level tests for the five FastAPI endpoints (`TestClient`).

The README listed "no HTTP-level tests of the five FastAPI endpoints" as the
number-one testing gap. These tests exercise the real ASGI app and stub the
provider/store singletons, so nothing here touches the network or a real
database. They also lock down the `side` contract at the HTTP boundary: an
unknown side must be rejected (422), never silently widened to "both".
"""

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

import app.api as api
from app.api import app
from app.config import settings
from app.models import Contract, OHLC, OptionType, Quote
from app.providers.base import FeedError, OptionsDataProvider, UnknownSide, filter_side, normalize_side
from app.store import Store


class FakeProvider(OptionsDataProvider):
    """Deterministic offline provider with a couple of names and one bad symbol."""

    def __init__(self, bad=None):
        self.bad = set(bad or [])

    def get_quote(self, symbol):
        if symbol in self.bad:
            raise FeedError(symbol, "no data")
        return Quote(symbol=symbol.upper(), last=100.0)

    def get_history(self, symbol, days=60):
        return [
            OHLC(day=date.today() - timedelta(days=i), open=100.0, high=101.0,
                 low=99.0, close=100.0 + (i % 3))
            for i in range(40)
        ]

    def get_expirations(self, symbol):
        return [date.today() + timedelta(days=30)]

    def get_chain(self, symbol, expiration, side="both"):
        out = []
        for k in (95.0, 100.0, 105.0):
            for ot in (OptionType.CALL, OptionType.PUT):
                if not filter_side(ot, side):
                    continue
                out.append(Contract(
                    underlying=symbol.upper(), occ_symbol=f"{symbol}{k}{ot.value}",
                    option_type=ot, strike=k, expiration=expiration, dte=30,
                    bid=2.98, ask=3.02, last=3.0, volume=500, open_interest=1000))
        return out


@pytest.fixture
def client(monkeypatch):
    """A TestClient whose provider/store singletons are offline fakes."""
    provider = FakeProvider(bad={"NOPE"})
    monkeypatch.setattr(api, "_provider", provider)
    monkeypatch.setattr(api, "_cboe_provider", provider)
    monkeypatch.setattr(api, "_realtime_provider", provider)
    monkeypatch.setattr(api, "_store", Store(":memory:"))
    return TestClient(app)


# --- /health --------------------------------------------------------------- #

def test_health_reports_provider_and_universe():
    with TestClient(app) as c:
        r = c.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["data_provider"] == settings.data_provider
    assert "realtime_note" in body
    assert body["universe_size"] > 0


# --- /key ------------------------------------------------------------------ #

def test_key_exposes_grade_and_subscore_labels():
    with TestClient(app) as c:
        r = c.get("/key")
    assert r.status_code == 200
    body = r.json()
    assert [b["grade"] for b in body["grade_key"]] == ["A", "B", "C", "D", "F"]
    assert set(body["sub_score_labels"]) == {
        "value", "odds", "move", "liquidity", "volatility", "decay", "leverage",
    }


# --- /scan ----------------------------------------------------------------- #

def test_scan_returns_ranked_results(client):
    r = client.post("/scan", json={"ticker": "aapl", "side": "both", "limit": 10})
    assert r.status_code == 200
    body = r.json()
    assert body["meta"]["ticker"] == "AAPL"          # normalized to upper
    assert body["results"]
    scores = [c["overall_score"] for c in body["results"]]
    assert scores == sorted(scores, reverse=True)     # descending rank
    assert all(c["underlying"] == "AAPL" for c in body["results"])


def test_scan_side_filter_excludes_the_other_side(client):
    calls = client.post("/scan", json={"ticker": "AAPL", "side": "calls"}).json()
    assert {c["type"] for c in calls["results"]} == {"call"}
    puts = client.post("/scan", json={"ticker": "AAPL", "side": "puts"}).json()
    assert {c["type"] for c in puts["results"]} == {"put"}


def test_scan_rejects_unknown_side(client):
    # A typo must not silently return both sides (the pre-fix behavior).
    for junk in ("garbage", "callz", "putss", "X"):
        r = client.post("/scan", json={"ticker": "AAPL", "side": junk})
        assert r.status_code == 422, junk


def test_scan_rejects_blank_ticker(client):
    r = client.post("/scan", json={"ticker": "   "})
    assert r.status_code == 422


def test_scan_maps_feed_error_to_502(client):
    r = client.post("/scan", json={"ticker": "NOPE"})
    assert r.status_code == 502
    assert "NOPE" in r.json()["detail"]


def test_scan_limit_is_honored(client):
    r = client.post("/scan", json={"ticker": "AAPL", "limit": 2})
    assert r.status_code == 200
    assert len(r.json()["results"]) == 2


def test_scan_notes_flag_default_dte_window(client):
    r = client.post("/scan", json={"ticker": "AAPL"})
    notes = r.json()["meta"]["notes"]
    assert any("defaulted to expirations" in n for n in notes)


# --- /market/scan + /market/status ----------------------------------------- #

def test_market_scan_starts_and_status_polls(monkeypatch, client):
    monkeypatch.setattr(api.market, "_universe_cache", ["AAA", "BBB"])
    started = client.post("/market/scan", json={"side": "both", "limit": 10})
    assert started.status_code == 200
    assert started.json()["status"] == "running"

    state = started.json()
    for _ in range(80):
        state = client.get("/market/status").json()
        if state["status"] in ("done", "error"):
            break
    assert state["status"] == "done"
    assert state["results"]


def test_market_scan_rejects_unknown_side(client):
    r = client.post("/market/scan", json={"side": "nope"})
    assert r.status_code == 422


# --- / (static frontend) --------------------------------------------------- #

def test_root_serves_the_frontend(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


# --- the side contract itself (unit level) --------------------------------- #

def test_normalize_side_accepts_aliases_and_defaults_blank():
    assert normalize_side("calls") == "calls"
    assert normalize_side("CALL") == "calls"
    assert normalize_side(" Put ") == "puts"
    assert normalize_side("all") == "both"
    assert normalize_side(None) == "both"
    assert normalize_side("  ") == "both"


def test_normalize_side_rejects_unknown():
    for junk in ("garbage", "callz", "X", 3):
        with pytest.raises(UnknownSide):
            normalize_side(junk)


def test_filter_side_raises_instead_of_widening():
    with pytest.raises(UnknownSide):
        filter_side(OptionType.CALL, "callz")
    assert filter_side(OptionType.CALL, "calls") is True
    assert filter_side(OptionType.PUT, "calls") is False
