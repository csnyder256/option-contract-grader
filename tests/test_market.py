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


def _isolate_universe(tmp_path, monkeypatch, legacy_syms=("AAA", "BBB")):
    """Point the universe at tmp files and clear both caches.

    ``optionable.txt`` deliberately does not exist yet: the point of these
    tests is what happens when it appears (or is rewritten) *after* import.
    ``UNIVERSE_FILE`` is left alone on purpose -- it is the module's own
    ``None``-means-resolve-it-now default that is under test.
    """
    legacy = tmp_path / "sp500_etfs.txt"
    legacy.write_text("\n".join(legacy_syms) + "\n", encoding="utf-8")
    generated = tmp_path / "optionable.txt"
    monkeypatch.setattr(market, "LEGACY_UNIVERSE_FILE", legacy)
    monkeypatch.setattr(market, "OPTIONABLE_FILE", generated)
    monkeypatch.setattr(market, "_universe_cache", None)
    monkeypatch.setattr(market, "_universe_memo", None, raising=False)
    return legacy, generated


def test_universe_picks_up_a_generated_list_without_a_restart(tmp_path, monkeypatch):
    """Running the generator against a live server must change what it scans.

    The preferred file was resolved once, at import, so
    ``scripts/update_universe.py`` had no effect on a running process: the
    sweep kept scanning the 565-name fallback and ``GET /health`` -- the
    documented way to confirm which list is loaded -- kept reporting that count
    as if it were current. A silent coverage shortfall is exactly the failure
    this repo keeps closing.
    """
    _, generated = _isolate_universe(tmp_path, monkeypatch)

    assert market.load_universe() == ["AAA", "BBB"]          # the fallback
    assert market.universe_info()["full_optionable_list"] is False
    assert market.universe_info()["file"] == "sp500_etfs.txt"

    generated.write_text("# generated\nCCC\nDDD\nEEE\n", encoding="utf-8")

    assert market.load_universe() == ["CCC", "DDD", "EEE"]   # picked up, no restart
    assert market.universe_size() == 3
    info = market.universe_info()
    assert info["file"] == "optionable.txt"
    assert info["full_optionable_list"] is True
    assert info["file_missing"] is False


def test_universe_rereads_a_regenerated_file(tmp_path, monkeypatch):
    """A universe that changes on disk is re-read, not served from the old memo."""
    _, generated = _isolate_universe(tmp_path, monkeypatch)
    generated.write_text("AAA\n", encoding="utf-8")
    assert market.load_universe() == ["AAA"]

    generated.write_text("AAA\nBBB\n", encoding="utf-8")
    assert market.load_universe() == ["AAA", "BBB"]


def test_universe_info_names_a_missing_file(tmp_path, monkeypatch):
    """No list at all is reported as such, instead of a bare zero."""
    legacy, generated = _isolate_universe(tmp_path, monkeypatch)
    legacy.unlink()
    info = market.universe_info()
    assert info["size"] == 0
    assert info["file_missing"] is True


def test_universe_cache_override_still_wins(monkeypatch):
    """An explicitly injected list is authoritative (unchanged contract)."""
    monkeypatch.setattr(market, "_universe_cache", ["ZZZ"])
    assert market.load_universe() == ["ZZZ"]
    assert market.universe_size() == 1


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


def test_sweep_reports_a_failed_batch_quote_pass(monkeypatch):
    # A batch quote request that fails is not the same fact as "these names have
    # no quote". Pre-fix the reason was swallowed and the note guessed.
    from app.providers.base import FeedError

    class BatchQuoteFailProvider(FakeProvider):
        supports_batch_quotes = True

        def get_quotes_batch(self, symbols):
            raise FeedError("MSFT", "HTTP 429 after 4 tries (rate limited)")

    prices = {f"B{i}": 50.0 + i for i in range(4)}
    monkeypatch.setattr(market, "_universe_cache", list(prices.keys()))

    top, notes = market.run_market_sweep(
        {"side": "both", "limit": 50}, BatchQuoteFailProvider(prices),
        Store(":memory:"), lambda *a: None,
    )

    assert top == []                                        # nothing was priced
    assert any("Batched quote pass failed" in n for n in notes)
    assert any("HTTP 429 after 4 tries" in n for n in notes)  # the feed's own reason
    assert any("its 4 name(s)" in n for n in notes)           # and how much it cost


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
