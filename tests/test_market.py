"""Tests for the market-wide screener: universe loading, price-band pruning,
sweep orchestration (offline fake provider), and the background state machine."""

import time
from datetime import date, timedelta

from app import market
from app.config import settings
from app.models import Contract, OHLC, OptionType, Quote
from app.providers.base import OptionsDataProvider
from app.store import Store


class FakeProvider(OptionsDataProvider):
    """Deterministic, offline provider for sweep tests."""

    def __init__(self, prices):
        self.prices = prices  # symbol -> underlying price

    def get_quote(self, symbol):
        return Quote(symbol=symbol.upper(), last=self.prices.get(symbol, 0.0))

    def get_price_and_history(self, symbol, days=60):
        price = self.prices.get(symbol, 0.0)
        bars = []
        p = price or 100.0
        for i in range(40):
            o = p
            c = p * (1.0 + 0.012 * (((i % 5) - 2) / 2.0))  # deterministic wiggle -> some HV
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


def test_load_universe_parses(tmp_path, monkeypatch):
    f = tmp_path / "u.txt"
    f.write_text("# comment\n\nAAA\nbbb\nAAA\nCCC\n", encoding="utf-8")
    monkeypatch.setattr(market, "UNIVERSE_FILE", f)
    monkeypatch.setattr(market, "_universe_cache", None)
    assert market.load_universe() == ["AAA", "BBB", "CCC"]  # upper, deduped, no comments


def test_store_underlying_cache_roundtrip():
    s = Store(":memory:")
    assert s.get_underlying("AAA") is None
    s.save_underlying("AAA", 50.0, 0.30)
    price, hv = s.get_underlying("AAA")
    assert price == 50.0
    assert abs(hv - 0.30) < 1e-9


def test_sweep_prunes_by_price_band(monkeypatch):
    prices = {"AAA": 30.0, "BBB": 75.0, "CCC": 250.0, "DDD": 12.0}
    monkeypatch.setattr(market, "_universe_cache", list(prices.keys()))
    store = Store(":memory:")
    prov = FakeProvider(prices)
    progress = []

    top, notes = market.run_market_sweep(
        {"price_min": 20, "price_max": 100, "side": "both", "limit": 25},
        prov, store, lambda *a: progress.append(a),
    )

    unders = {r["underlying"] for r in top}
    assert unders  # produced results
    assert unders <= {"AAA", "BBB"}            # only in-band names
    assert "CCC" not in unders and "DDD" not in unders
    assert progress                            # progress callback fired
    assert any("price band" in n for n in notes)


def test_sweep_results_sorted_desc(monkeypatch):
    prices = {"AAA": 30.0, "BBB": 60.0}
    monkeypatch.setattr(market, "_universe_cache", list(prices.keys()))
    top, _ = market.run_market_sweep(
        {"price_min": 0, "price_max": 1000, "side": "both", "limit": 25},
        FakeProvider(prices), Store(":memory:"), lambda *a: None,
    )
    scores = [r["overall_score"] for r in top]
    assert scores == sorted(scores, reverse=True)


class FailingChainProvider(FakeProvider):
    """FakeProvider that raises when fetching chains for specific symbols."""

    def __init__(self, prices, fail):
        super().__init__(prices)
        self.fail = set(fail)

    def get_chain(self, symbol, expiration, side="both"):
        if symbol in self.fail:
            from app.providers.base import FeedError

            raise FeedError(symbol, "boom")
        return super().get_chain(symbol, expiration, side)


def test_board_has_many_distinct_names_not_a_few(monkeypatch):
    # The headline bug: one hot name used to flood the Top-N with its own ladder.
    prices = {f"SYM{i:02d}": 50.0 + i for i in range(20)}
    monkeypatch.setattr(market, "_universe_cache", list(prices.keys()))

    top, _ = market.run_market_sweep(
        {"side": "both", "limit": 50}, FakeProvider(prices), Store(":memory:"),
        lambda *a: None,
    )

    counts = {}
    for r in top:
        counts[r["underlying"]] = counts.get(r["underlying"], 0) + 1
    assert len(counts) >= 15                       # many distinct names, not ~5
    assert max(counts.values()) <= settings.per_name_board_cap   # board cap honored


def test_sweep_counts_chain_failures_in_notes(monkeypatch):
    prices = {"AAA": 30.0, "BBB": 40.0, "CCC": 50.0, "DDD": 60.0}
    monkeypatch.setattr(market, "_universe_cache", list(prices.keys()))
    prov = FailingChainProvider(prices, fail={"BBB", "DDD"})

    top, notes = market.run_market_sweep(
        {"side": "both", "limit": 50}, prov, Store(":memory:"), lambda *a: None,
    )

    unders = {r["underlying"] for r in top}
    assert unders == {"AAA", "CCC"}                       # failed names produced nothing
    assert any("failed to fetch" in n for n in notes)     # but the failure is surfaced
    assert any("BBB" in n or "DDD" in n for n in notes)   # with examples


def test_sweep_reports_scan_budget_truncation(monkeypatch):
    prices = {f"S{i}": 100.0 for i in range(5)}
    monkeypatch.setattr(market, "_universe_cache", list(prices.keys()))
    monkeypatch.setattr(market.settings, "max_chain_scans", 2)

    # A price filter forces the full universe to be priced, so in-band (5) > budget (2).
    _, notes = market.run_market_sweep(
        {"price_min": 0, "price_max": 1000, "side": "both", "limit": 50},
        FakeProvider(prices), Store(":memory:"), lambda *a: None,
    )
    assert any("top 2 of 5" in n for n in notes)


def test_in_band_order_does_not_drop_names(monkeypatch):
    # Names given in reverse order must all still reach the board (no alphabet bias).
    prices = {"ZZZ": 50.0, "MMM": 60.0, "AAA": 70.0}
    monkeypatch.setattr(market, "_universe_cache", list(prices.keys()))
    top, _ = market.run_market_sweep(
        {"side": "both", "limit": 50}, FakeProvider(prices), Store(":memory:"),
        lambda *a: None,
    )
    assert {r["underlying"] for r in top} == {"AAA", "MMM", "ZZZ"}


def test_underlying_cache_ttl_respects_age_and_ignores_snap_date():
    from datetime import datetime, timedelta, timezone

    s = Store(":memory:")
    s.save_underlying("AAA", 50.0, 0.30)
    assert s.get_underlying_fresh("AAA", 12.0) == (50.0, 0.30)   # fresh -> hit
    assert s.get_underlying_fresh("AAA", 0.0) is None            # any age > 0h -> stale
    assert s.get_underlying_fresh("MISSING", 12.0) is None

    # A row stamped yesterday's snap_date but within the TTL must still be found
    # (the cross-midnight fix for the intermittent empty-board bug).
    ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    s._conn.execute(
        "INSERT OR REPLACE INTO underlying_cache "
        "(symbol, snap_date, price, hv, updated_at) VALUES (?, ?, ?, ?, ?)",
        ("YST", yesterday, 42.0, 0.20, ts),
    )
    s._conn.commit()
    assert s.get_underlying_fresh("YST", 12.0) == (42.0, 0.20)


def test_background_sweep_completes(monkeypatch):
    prices = {"AAA": 30.0, "BBB": 75.0}
    monkeypatch.setattr(market, "_universe_cache", list(prices.keys()))
    started = market.start_market_sweep(
        {"price_min": 0, "price_max": 1000, "side": "both", "limit": 10},
        FakeProvider(prices), Store(":memory:"),
    )
    assert started["status"] == "running"

    state = started
    for _ in range(60):  # up to ~6s
        time.sleep(0.1)
        state = market.market_status()
        if state["status"] in ("done", "error"):
            break
    assert state["status"] == "done"
    assert state["results"]
