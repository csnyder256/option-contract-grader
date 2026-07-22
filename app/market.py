"""Market-wide screener: sweep a curated universe (S&P 500 + ETFs) and return the
Top N contracts across the market.

Pipeline (the README's "The market sweep" section documents the same four stages):
  1. Cheap pass: price + realized vol per name (one Yahoo chart call, cached daily).
  2. Prune to names whose price is inside the requested band.
  3. Expensive pass: fetch + score chains ONLY for in-band names (bounded concurrency).
  4. Rank everything, return Top N.

Runs in a background thread with a pollable progress/state object so the HTTP
request never blocks.
"""

from __future__ import annotations

import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app.config import settings
from app.engine.volatility import hv_from_bars
from app.scanner import ScanFilters, scan_symbol

UNIVERSE_DIR = Path(__file__).resolve().parent / "universe"
OPTIONABLE_FILE = UNIVERSE_DIR / "optionable.txt"
LEGACY_UNIVERSE_FILE = UNIVERSE_DIR / "sp500_etfs.txt"
# Prefer the full optionable universe once generated (scripts/update_universe.py);
# fall back to the curated S&P+ETF list so the app still runs out of the box.
UNIVERSE_FILE = OPTIONABLE_FILE if OPTIONABLE_FILE.exists() else LEGACY_UNIVERSE_FILE

_universe_cache: Optional[List[str]] = None


def load_universe() -> List[str]:
    global _universe_cache
    if _universe_cache is None:
        out: List[str] = []
        seen = set()
        try:
            for line in UNIVERSE_FILE.read_text(encoding="utf-8").splitlines():
                s = line.strip().upper()
                if not s or s.startswith("#") or s in seen:
                    continue
                seen.add(s)
                out.append(s)
        except FileNotFoundError:
            out = []
        if settings.max_universe and settings.max_universe > 0:
            out = out[: settings.max_universe]
        _universe_cache = out
    return _universe_cache


def universe_size() -> int:
    return len(load_universe())


# --------------------------------------------------------------------------- #
# Background state.
# --------------------------------------------------------------------------- #

@dataclass
class _State:
    status: str = "idle"          # idle | running | done | error
    phase: str = ""               # "prices" | "chains" | ...
    done: int = 0
    total: int = 0
    params: dict = field(default_factory=dict)
    results: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    error: Optional[str] = None
    started_on: Optional[str] = None
    finished_at: Optional[str] = None


_state = _State()
_state_lock = threading.Lock()


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _snapshot() -> dict:
    with _state_lock:
        return {
            "status": _state.status,
            "phase": _state.phase,
            "done": _state.done,
            "total": _state.total,
            "params": dict(_state.params),
            "results": list(_state.results),
            "notes": list(_state.notes),
            "error": _state.error,
            "started_on": _state.started_on,
            "finished_at": _state.finished_at,
        }


def _set_progress(phase: str, done: int, total: int) -> None:
    with _state_lock:
        _state.phase = phase
        _state.done = done
        _state.total = total


def market_status() -> dict:
    return _snapshot()


def start_market_sweep(params: dict, provider, store,
                       cboe_provider=None, realtime_provider=None) -> dict:
    """Kick off a background sweep (or return the in-flight state if one runs).

    `provider` prices the universe (batched when it supports it). `cboe_provider`
    fetches bulk delayed chains for large sweeps; `realtime_provider` (Tradier)
    fetches real-time chains when the in-band set is small. Both are optional;
    when omitted the sweep uses `provider` for everything (single-provider mode).
    """
    with _state_lock:
        if _state.status == "running":
            return _snapshot_locked()
        _state.status = "running"
        _state.phase = "starting"
        _state.done = 0
        _state.total = universe_size()
        _state.params = dict(params)
        _state.results = []
        _state.notes = []
        _state.error = None
        _state.started_on = _now()
        _state.finished_at = None
    threading.Thread(
        target=_run_sweep,
        args=(params, provider, store, cboe_provider, realtime_provider),
        daemon=True,
    ).start()
    return _snapshot()


def _snapshot_locked() -> dict:
    # caller already holds _state_lock
    return {
        "status": _state.status, "phase": _state.phase, "done": _state.done,
        "total": _state.total, "params": dict(_state.params),
        "results": list(_state.results), "notes": list(_state.notes),
        "error": _state.error, "started_on": _state.started_on,
        "finished_at": _state.finished_at,
    }


def _run_sweep(params: dict, provider, store,
               cboe_provider=None, realtime_provider=None) -> None:
    try:
        results, notes = run_market_sweep(
            params, provider, store, _set_progress,
            cboe_provider=cboe_provider, realtime_provider=realtime_provider,
        )
        with _state_lock:
            _state.results = results
            _state.notes = notes
            _state.status = "done"
            _state.finished_at = _now()
    except Exception as e:  # never leave the state stuck on "running"
        with _state_lock:
            _state.status = "error"
            _state.error = str(e)
            _state.finished_at = _now()


# --------------------------------------------------------------------------- #
# The sweep itself (pure-ish; takes a progress callback).
# --------------------------------------------------------------------------- #

def run_market_sweep(params: dict, provider, store, progress_cb,
                     cboe_provider=None, realtime_provider=None) -> Tuple[list, list]:
    today = date.today()
    universe = load_universe()
    price_min = params.get("price_min")
    price_max = params.get("price_max")
    has_price_filter = price_min is not None or price_max is not None
    ttl = settings.cache_ttl_hours
    workers = max(1, settings.sweep_max_workers)
    budget = settings.max_chain_scans
    hv_days = settings.hv_window + 5

    # When there's no price filter the in-band set is just the universe order, so
    # there's no point pricing past the scan budget - bound the price pass too.
    if not has_price_filter and budget and budget > 0:
        candidates = universe[:budget]
    else:
        candidates = universe

    # --- Stage 1: prices (TTL cache -> batch or concurrent) ---------------
    progress_cb("prices", 0, len(candidates))
    priced: Dict[str, Tuple[float, Optional[float]]] = {}
    price_failed: List[str] = []
    uncached: List[str] = []
    for sym in candidates:
        cached = store.get_underlying_fresh(sym, ttl)
        if cached is not None and cached[0] and cached[0] > 0:
            priced[sym] = cached
        else:
            uncached.append(sym)
    done = len(priced)
    progress_cb("prices", done, len(candidates))

    if uncached and getattr(provider, "supports_batch_quotes", False):
        # Cheap batched price pass (e.g. Tradier POST /markets/quotes). HV is
        # filled in for the survivors only, in Stage 1b.
        try:
            quotes = provider.get_quotes_batch(uncached)
        except Exception:
            quotes = {}
        for sym in uncached:
            q = quotes.get(sym.upper())
            if q is not None and q.last and q.last > 0:
                priced[sym] = (q.last, None)
                store.save_underlying(sym, q.last, None, today)
            else:
                price_failed.append(sym)
            done += 1
        progress_cb("prices", done, len(candidates))
    elif uncached:
        # One cheap call per name yields BOTH price and history (Yahoo chart).
        def fetch_ph(sym: str):
            try:
                price, bars = provider.get_price_and_history(sym, days=hv_days)
            except Exception:
                return sym, 0.0, None, True
            hv = hv_from_bars(bars, settings.hv_window) if bars else None
            if price and price > 0:
                store.save_underlying(sym, price, hv, today)
            return sym, price, hv, False

        with ThreadPoolExecutor(max_workers=workers) as ex:
            for fut in as_completed([ex.submit(fetch_ph, s) for s in uncached]):
                sym, price, hv, failed = fut.result()
                if price and price > 0:
                    priced[sym] = (price, hv)
                elif failed:
                    price_failed.append(sym)
                done += 1
                progress_cb("prices", done, len(candidates))

    # --- Stage 2: prune by price band (preserve liquidity order) ----------
    in_band = [
        s for s in candidates
        if s in priced
        and (price_min is None or priced[s][0] >= price_min)
        and (price_max is None or priced[s][0] <= price_max)
    ]
    total_in_band = len(in_band)
    budget_note: Optional[str] = None
    if budget and budget > 0 and total_in_band > budget:
        in_band = in_band[:budget]
        budget_note = (
            f"Scanned the top {len(in_band)} of {total_in_band} in-band names by "
            f"liquidity; narrow the price/premium filters to reach deeper."
        )

    # --- Hybrid chain provider: real-time when the set is small ------------
    bulk_provider = cboe_provider or provider
    if realtime_provider is not None and len(in_band) <= settings.realtime_chain_threshold:
        chain_provider = realtime_provider
        chain_mode = "real-time"
    else:
        chain_provider = bulk_provider
        chain_mode = "CBOE delayed (~15 min)"

    # --- Stage 1b: HV for survivors that were batch-priced without it ------
    need_hv = [s for s in in_band if priced[s][1] is None]
    if need_hv:
        def fetch_hv(sym: str):
            try:
                bars = provider.get_history(sym, days=hv_days)
            except Exception:
                return sym, None
            return sym, (hv_from_bars(bars, settings.hv_window) if bars else None)

        with ThreadPoolExecutor(max_workers=workers) as ex:
            for fut in as_completed([ex.submit(fetch_hv, s) for s in need_hv]):
                sym, hv = fut.result()
                price = priced[sym][0]
                priced[sym] = (price, hv)
                if hv is not None:
                    store.save_underlying(sym, price, hv, today)

    # --- Stage 3: fetch + score chains for survivors ----------------------
    filters = ScanFilters(
        side=params.get("side", "both"),
        premium_min=params.get("premium_min"),
        premium_max=params.get("premium_max"),
        expiration_from=(today + timedelta(days=params["dte_from"]))
        if params.get("dte_from") is not None else None,
        expiration_to=(today + timedelta(days=params["dte_to"]))
        if params.get("dte_to") is not None else None,
    )
    all_scored = []
    chain_failed: List[str] = []
    done = 0
    progress_cb("chains", 0, len(in_band))

    def score_one(sym: str):
        price, hv = priced[sym]
        time.sleep(random.uniform(0.0, 0.12))  # jitter: be polite to the feed
        try:
            res = scan_symbol(
                sym, chain_provider, store, price, settings.default_dividend_yield,
                hv, filters, today=today,
            )
            return sym, res.scored[: settings.per_symbol_cap], False
        except Exception:
            return sym, [], True

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for fut in as_completed([ex.submit(score_one, s) for s in in_band]):
            sym, scored, failed = fut.result()
            all_scored.extend(scored)
            if failed:
                chain_failed.append(sym)
            done += 1
            progress_cb("chains", done, len(in_band))

    # --- Stage 4: global rank + per-name board cap -> Top N ---------------
    all_scored.sort(key=lambda sc: sc.overall_score, reverse=True)
    limit = int(params.get("limit") or 50)
    board_cap = max(1, settings.per_name_board_cap)
    top: list = []
    per_name: Dict[str, int] = {}
    for sc in all_scored:
        u = sc.contract.underlying.upper()
        if per_name.get(u, 0) >= board_cap:
            continue  # keep one hot name from flooding the board with its ladder
        per_name[u] = per_name.get(u, 0) + 1
        top.append(sc.to_dict())
        if len(top) >= limit:
            break

    # --- Notes (surface coverage + drops; never silent) -------------------
    notes: List[str] = []
    notes.append(
        f"Universe {len(universe)} names; priced {len(priced)}/{len(candidates)}"
        + (f" ({len(price_failed)} unpriced - rate-limited or no data)" if price_failed else "")
        + "."
    )
    if budget_note:
        notes.append(budget_note)
    scanned_desc = f"{len(in_band)} names scanned" + (
        " in your price band" if has_price_filter else ""
    )
    notes.append(
        f"{scanned_desc}; chains via {chain_mode}"
        + (f"; {len(chain_failed)} failed to fetch" if chain_failed else "")
        + "."
    )
    if chain_mode.startswith("CBOE") and realtime_provider is not None and not has_price_filter:
        notes.append("Narrow the price/premium filters for real-time (Tradier) chains.")
    notes.append(
        f"Scored {len(all_scored)} contracts; showing top {len(top)} across "
        f"{len(per_name)} names (max {board_cap} per name)."
    )
    if price_failed:
        notes.append("Unpriced examples: " + ", ".join(price_failed[:6]) + " ...")
    if chain_failed:
        notes.append("Chain-fetch failures: " + ", ".join(chain_failed[:6]) + " ...")
    notes.append(f"Data as of {_now()}.")
    return top, notes
