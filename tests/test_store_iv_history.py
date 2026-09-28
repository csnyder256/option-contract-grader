"""Regression tests: the daily ATM-IV series is append-only, and the price/HV
cache keeps the observation time it actually observed.

Both bugs share one root cause: `PRIMARY KEY (symbol, snap_date)` plus
`INSERT OR REPLACE`. Any later scan on the same day silently rewrote a day that
had already been observed, so the "historical" series IV Rank/Percentile read
was really "whichever scan ran last".

This module deliberately imports nothing added by the fix, so it can be run
unmodified against the pre-fix source to prove the tests fail for the right
reason: with `INSERT OR REPLACE` restored, the second write replaces the first,
the volatility spike disappears, and the sweep restamps an old price as fresh.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from app.engine.volatility import iv_rank
from app.models import Contract, OHLC, OptionType, Quote
from app.providers.base import OptionsDataProvider
from app.store import Store


def test_iv_snapshot_is_first_write_wins():
    s = Store(":memory:")
    d = date(2026, 8, 3)
    assert s.save_iv_snapshot("AAPL", 0.55, snap_date=d) is True    # first write records the day
    assert s.save_iv_snapshot("AAPL", 0.21, snap_date=d) is False   # later write is a no-op
    assert s.get_iv_history("AAPL") == [0.55]
    assert s.snapshot_count("AAPL") == 1


def test_intraday_rescan_cannot_rewrite_a_completed_days_iv():
    # 12 days of history with one genuine volatility spike on day 3. The user
    # re-scans on day 3; with OR REPLACE the spike is destroyed and IV Rank is
    # computed against a series where that event never happened.
    s = Store(":memory:")
    base = date(2026, 8, 1)
    hist = [0.20, 0.22, 0.19, 0.55, 0.21, 0.23, 0.18, 0.24, 0.20, 0.22, 0.19, 0.25]
    for i, iv in enumerate(hist):
        s.save_iv_snapshot("AAPL", iv, snap_date=base + timedelta(days=i))

    s.save_iv_snapshot("AAPL", 0.21, snap_date=base + timedelta(days=3))  # intraday re-scan

    assert s.snapshot_count("AAPL") == 12
    assert s.get_iv_history("AAPL") == hist          # the 0.55 spike survives
    assert max(s.get_iv_history("AAPL")) == 0.55

    # The consumer: a rank taken against the true series, not the rewritten one.
    assert iv_rank(0.21, s.get_iv_history("AAPL")) == pytest.approx(8.108, abs=1e-3)


def test_iv_snapshot_rejects_nonpositive_and_reports_first_write():
    s = Store(":memory:")
    assert s.save_iv_snapshot("AAPL", 0.0) is False
    assert s.save_iv_snapshot("AAPL", -0.2) is False
    assert s.snapshot_count("AAPL") == 0


def test_iv_snapshot_is_per_symbol_and_per_day():
    s = Store(":memory:")
    d1, d2 = date(2026, 8, 1), date(2026, 8, 2)
    s.save_iv_snapshot("AAPL", 0.20, snap_date=d1)
    s.save_iv_snapshot("MSFT", 0.30, snap_date=d1)   # different symbol, same day
    s.save_iv_snapshot("AAPL", 0.25, snap_date=d2)   # same symbol, new day
    assert s.get_iv_history("AAPL") == [0.20, 0.25]  # ascending by day
    assert s.get_iv_history("MSFT") == [0.30]


def test_underlying_cache_can_preserve_the_original_observation_time():
    # Re-caching a price the caller already held must NOT present it as freshly
    # observed, or the 12h TTL in the sweep never expires.
    s = Store(":memory:")
    s.save_underlying("AAA", 50.0, 0.30)

    price, hv, observed = s.get_underlying_fresh("AAA", 12.0, with_timestamp=True)
    assert (price, hv) == (50.0, 0.30)
    assert observed is not None

    # Re-save with the original timestamp -> the row's age is unchanged.
    s.save_underlying("AAA", 51.0, 0.31, observed_at=observed)
    _, _, again = s.get_underlying_fresh("AAA", 12.0, with_timestamp=True)
    assert again == observed

    # A genuine new fetch restamps it.
    s.save_underlying("AAA", 52.0, 0.32)
    _, _, restamped = s.get_underlying_fresh("AAA", 12.0, with_timestamp=True)
    assert restamped >= observed


def test_get_underlying_fresh_default_shape_is_unchanged():
    # The two-tuple return is the documented shape; with_timestamp is opt-in.
    s = Store(":memory:")
    s.save_underlying("AAA", 50.0, 0.30)
    assert s.get_underlying_fresh("AAA", 12.0) == (50.0, 0.30)
    assert s.get_underlying_fresh("MISSING", 12.0) is None
    assert s.get_underlying_fresh("AAA", 0.0) is None


def test_sweep_does_not_restamp_a_price_it_already_had(monkeypatch):
    """The sweep re-touching an in-band name must not refresh its cache age."""
    from app import market
    from app.models import Contract, OHLC, OptionType, Quote
    from app.providers.base import OptionsDataProvider

    class BatchProvider(OptionsDataProvider):
        """Batch-priced (price known, HV missing) -> forces Stage 1b to re-save."""

        supports_batch_quotes = True

        def __init__(self, prices):
            self.prices = prices

        def get_quote(self, symbol):
            return Quote(symbol=symbol.upper(), last=self.prices.get(symbol, 0.0))

        def get_quotes_batch(self, symbols):
            return {s.upper(): Quote(symbol=s.upper(), last=self.prices.get(s, 0.0))
                    for s in symbols}

        def get_history(self, symbol, days=60):
            p = self.prices.get(symbol, 100.0)
            c = p
            bars = []
            for i in range(40):
                o = c
                c2 = o * (1.0 + 0.012 * (((i % 5) - 2) / 2.0))
                bars.append(OHLC(day=date.today() - timedelta(days=40 - i), open=o,
                                 high=max(o, c2) * 1.01, low=min(o, c2) * 0.99,
                                 close=c2, volume=1e6))
                c = c2
            return bars

        def get_expirations(self, symbol):
            return [date.today() + timedelta(days=30)]

        def get_chain(self, symbol, expiration, side="both"):
            px = self.prices.get(symbol, 100.0)
            mid = max(0.5, px * 0.03)
            out = []
            for k in (px * 0.95, px, px * 1.05):
                for ot in (OptionType.CALL, OptionType.PUT):
                    out.append(Contract(
                        underlying=symbol.upper(), occ_symbol=f"{symbol}{int(k)}{ot.value}",
                        option_type=ot, strike=round(k, 1), expiration=expiration, dte=30,
                        bid=round(mid * 0.98, 2), ask=round(mid * 1.02, 2),
                        last=round(mid, 2), volume=500, open_interest=1000))
            return out

    prices = {"AAA": 50.0}
    monkeypatch.setattr(market, "_universe_cache", list(prices.keys()))
    store = Store(":memory:")

    market.run_market_sweep({"side": "both", "limit": 10}, BatchProvider(prices),
                            store, lambda *a: None)

    # Age the cached row to 11h (inside the 12h TTL) and record it.
    old = (datetime.now(timezone.utc) - timedelta(hours=11)).isoformat()
    store._conn.execute("UPDATE underlying_cache SET updated_at = ?", (old,))
    store._conn.commit()

    market.run_market_sweep({"side": "both", "limit": 10}, BatchProvider(prices),
                            store, lambda *a: None)

    _, _, after = store.get_underlying_fresh("AAA", 12.0, with_timestamp=True)
    assert after == old, "the sweep restamped a price it already had, resetting the TTL"


def test_repeated_scans_report_a_stable_iv_rank_through_the_api():
    """End-to-end: the rank the user sees must not drift with the last scan.

    Two scans on the same day, with the feed moving sharply in between, must
    report the SAME ripple because the day's observation is already recorded.
    With `INSERT OR REPLACE` the second scan overwrites today's row and the
    rank moves -- the series would be reactive to whatever ran last.
    """
    from fastapi.testclient import TestClient

    from app import api

    class MovingIvProvider(OptionsDataProvider):
        def __init__(self):
            self.provider_iv = 0.20

        def get_quote(self, symbol):
            return Quote(symbol=symbol.upper(), last=100.0)

        def get_history(self, symbol, days=60):
            out, c = [], 100.0
            for i in range(40):
                o = c
                c2 = o * (1.0 + 0.012 * (((i % 5) - 2) / 2.0))
                out.append(OHLC(day=date.today() - timedelta(days=40 - i), open=o,
                                high=max(o, c2) * 1.01, low=min(o, c2) * 0.99,
                                close=c2, volume=1e6))
                c = c2
            return out

        def get_expirations(self, symbol):
            return [date.today() + timedelta(days=30)]

        def get_chain(self, symbol, expiration, side="both"):
            mid = 3.0
            out = []
            for k in (95.0, 100.0, 105.0):
                for ot in (OptionType.CALL, OptionType.PUT):
                    out.append(Contract(
                        underlying=symbol.upper(), occ_symbol=f"{symbol}{int(k)}{ot.value}",
                        option_type=ot, strike=k, expiration=expiration, dte=30,
                        bid=mid - 0.05, ask=mid + 0.05, last=mid, volume=500,
                        open_interest=1000, provider_iv=self.provider_iv))
            return out

    store = Store(":memory:")
    # 11 prior days so IV Rank is live (threshold is 10 observations).
    for i in range(11):
        store.save_iv_snapshot("AAPL", 0.20 + 0.01 * i,
                               snap_date=date.today() - timedelta(days=11 - i))

    provider = MovingIvProvider()
    api._provider, api._store = provider, store
    client = TestClient(api.app)

    first = client.post("/scan", json={"ticker": "AAPL"})
    assert first.status_code == 200
    recorded = store.get_iv_history("AAPL")[-1]

    provider.provider_iv = 0.90          # the market moves sharply
    second = client.post("/scan", json={"ticker": "AAPL"})
    assert second.status_code == 200

    assert store.snapshot_count("AAPL") == 12                   # still one row per day
    assert store.get_iv_history("AAPL")[-1] == recorded         # day not rewritten
    assert second.json()["meta"]["iv_rank"] == first.json()["meta"]["iv_rank"]
