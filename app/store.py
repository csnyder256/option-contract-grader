"""Local SQLite store: daily ATM-IV snapshots + a daily underlying price/HV cache.

IV Rank / IV Percentile need ~52 weeks of past implied vol, which neither Tradier
nor CBOE serves directly. We build that history ourselves: each scan records the
underlying's current ATM IV (once per symbol per day). After enough days accrue,
IV Rank/Percentile become available; until then the engine falls back to the
IV-vs-HV signal and the UI labels rank as "warming up".

The ``underlying_cache`` table memoises each name's price + realized vol for the
day so a market-wide sweep can prune by price band without re-fetching every name
on every refresh.

All public methods are guarded by a re-entrant lock so the market sweep's worker
threads can share one connection safely.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import date, datetime, timezone
from typing import List, Optional, Tuple


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        if db_path != ":memory:":
            parent = os.path.dirname(os.path.abspath(db_path))
            os.makedirs(parent, exist_ok=True)
        # check_same_thread=False so the sweep's worker threads can share it;
        # _lock serialises access so concurrent writes don't collide.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.RLock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS iv_snapshots (
                    symbol    TEXT NOT NULL,
                    snap_date TEXT NOT NULL,
                    atm_iv    REAL NOT NULL,
                    PRIMARY KEY (symbol, snap_date)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS underlying_cache (
                    symbol     TEXT NOT NULL,
                    snap_date  TEXT NOT NULL,
                    price      REAL NOT NULL,
                    hv         REAL,
                    updated_at TEXT,
                    PRIMARY KEY (symbol, snap_date)
                )
                """
            )
            # Migrate existing DBs in place: add updated_at if an older schema
            # (without it) is already on disk.
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(underlying_cache)")}
            if "updated_at" not in cols:
                self._conn.execute("ALTER TABLE underlying_cache ADD COLUMN updated_at TEXT")
            self._conn.commit()

    # -- IV snapshots (for IV rank) ---------------------------------------
    def save_iv_snapshot(
        self, symbol: str, atm_iv: float, snap_date: Optional[date] = None
    ) -> None:
        if atm_iv is None or atm_iv <= 0:
            return
        d = (snap_date or date.today()).isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO iv_snapshots (symbol, snap_date, atm_iv) "
                "VALUES (?, ?, ?)",
                (symbol.upper(), d, float(atm_iv)),
            )
            self._conn.commit()

    def get_iv_history(self, symbol: str, lookback_days: int = 400) -> List[float]:
        """Most recent `lookback_days` ATM-IV snapshots, oldest first."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT atm_iv FROM iv_snapshots WHERE symbol = ? "
                "ORDER BY snap_date DESC LIMIT ?",
                (symbol.upper(), lookback_days),
            )
            rows = [r[0] for r in cur.fetchall()]
        rows.reverse()
        return rows

    def snapshot_count(self, symbol: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "SELECT COUNT(*) FROM iv_snapshots WHERE symbol = ?", (symbol.upper(),)
            )
            return int(cur.fetchone()[0])

    # -- Underlying price/HV cache (for the market sweep) -----------------
    def save_underlying(
        self, symbol: str, price: float, hv: Optional[float],
        snap_date: Optional[date] = None,
    ) -> None:
        if price is None or price <= 0:
            return
        d = (snap_date or date.today()).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO underlying_cache "
                "(symbol, snap_date, price, hv, updated_at) VALUES (?, ?, ?, ?, ?)",
                (symbol.upper(), d, float(price),
                 float(hv) if hv is not None else None, now),
            )
            self._conn.commit()

    def get_underlying(
        self, symbol: str, snap_date: Optional[date] = None
    ) -> Optional[Tuple[float, Optional[float]]]:
        """Return (price, hv) cached for `snap_date` (default today), or None."""
        d = (snap_date or date.today()).isoformat()
        with self._lock:
            cur = self._conn.execute(
                "SELECT price, hv FROM underlying_cache WHERE symbol = ? AND snap_date = ?",
                (symbol.upper(), d),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return (row[0], row[1])

    def get_underlying_fresh(
        self, symbol: str, max_age_hours: float
    ) -> Optional[Tuple[float, Optional[float]]]:
        """Return (price, hv) if the most recent snapshot is within `max_age_hours`.

        Keyed by `updated_at` (NOT snap_date) so a TTL that spans midnight still
        finds yesterday's row - the fix for the intermittent empty-board bug.
        """
        with self._lock:
            cur = self._conn.execute(
                "SELECT price, hv, updated_at FROM underlying_cache "
                "WHERE symbol = ? ORDER BY updated_at DESC LIMIT 1",
                (symbol.upper(),),
            )
            row = cur.fetchone()
        if row is None or row[2] is None:
            return None  # never cached, or legacy row with no timestamp -> stale
        try:
            ts = datetime.fromisoformat(row[2])
        except ValueError:
            return None
        age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0
        if age_h > max_age_hours:
            return None
        return (row[0], row[1])

    def close(self) -> None:
        with self._lock:
            self._conn.close()
