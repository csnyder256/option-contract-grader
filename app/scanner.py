"""Reusable per-symbol scan core.

Shared by the single-ticker endpoint (`/scan`) and the market-wide sweep
(`app/market.py`). Kept provider-agnostic and free of FastAPI types so both
callers can use it without circular imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import List, Optional

from app.config import Settings, settings as default_settings
from app.engine import blackscholes as bs
from app.engine.scoring import score_contract
from app.engine.volatility import iv_rank
from app.models import Contract, ScoredContract


@dataclass
class ScanFilters:
    side: str = "both"
    premium_min: Optional[float] = None
    premium_max: Optional[float] = None
    expiration_from: Optional[date] = None
    expiration_to: Optional[date] = None


@dataclass
class SymbolScan:
    scored: List[ScoredContract]
    atm_iv: Optional[float]
    rank_value: Optional[float]
    exps: List[date]
    total_matching_exps: int
    truncated: bool
    snap_count: int


def premium_ok(c: Contract, premium_min: Optional[float], premium_max: Optional[float]) -> bool:
    m = c.mid
    if m <= 0:
        return False  # untradeable; no clean price
    if premium_min is not None and m < premium_min:
        return False
    if premium_max is not None and m > premium_max:
        return False
    return True


def select_expirations(all_exps, exp_from, exp_to, today, settings):
    """Honor an explicit date range, else default to the buyer DTE window; then cap.

    Returns (chosen_exps, total_matching, truncated).
    """
    has_range = exp_from is not None or exp_to is not None
    if has_range:
        pool = [
            e for e in all_exps
            if (exp_from is None or e >= exp_from) and (exp_to is None or e <= exp_to)
        ]
        hard_cap = 24
    else:
        band = [
            e for e in all_exps
            if settings.default_min_dte <= (e - today).days <= settings.default_max_dte
        ]
        pool = band if band else list(all_exps)
        hard_cap = settings.max_expirations
    return pool[:hard_cap], len(pool), len(pool) > hard_cap


def scan_symbol(
    symbol: str, provider, store, S: float, q: float, hv: Optional[float],
    filters: ScanFilters, today: Optional[date] = None, settings: Optional[Settings] = None,
) -> SymbolScan:
    """Fetch a symbol's chain, compute IV/Greeks, score, and rank.

    `S` (underlying price) and `hv` (realized vol) are passed in so the market
    sweep can reuse its cheap cached price/HV pass instead of re-fetching.
    """
    settings = settings or default_settings
    today = today or date.today()
    r = settings.risk_free_rate

    all_exps = provider.get_expirations(symbol)
    exps, total_matching, truncated = select_expirations(
        all_exps, filters.expiration_from, filters.expiration_to, today, settings
    )

    contracts: List[Contract] = []
    for e in exps:
        contracts.extend(provider.get_chain(symbol, e, side=filters.side))

    # Optional: keep only the k strikes nearest the money (per the config knob).
    # The biggest per-name speed lever for large market sweeps; 0 = no cap.
    k = settings.atm_strike_window
    if k and k > 0 and contracts:
        strikes = sorted({c.strike for c in contracts})
        atm_idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - S))
        lo, hi = max(0, atm_idx - k), min(len(strikes), atm_idx + k + 1)
        allowed = set(strikes[lo:hi])
        contracts = [c for c in contracts if c.strike in allowed]

    # ATM IV (nearest-expiration, nearest-strike solvable) -> snapshot -> rank.
    atm_iv = None
    for c in sorted(contracts, key=lambda c: (c.dte, abs(c.strike - S))):
        if c.mid <= 0:
            continue
        Tf = max(c.dte, 1) / 365.0
        solved = bs.implied_vol(c.mid, S, c.strike, r, q, Tf, c.option_type)
        if solved:
            atm_iv = solved
            break
    if atm_iv:
        store.save_iv_snapshot(symbol, atm_iv)
    rank_value = iv_rank(atm_iv, store.get_iv_history(symbol))
    snap_count = store.snapshot_count(symbol)

    # Pre-filter cheaply (premium) BEFORE the IV solve inside score_contract.
    candidates = [
        c for c in contracts if premium_ok(c, filters.premium_min, filters.premium_max)
    ]
    scored = [score_contract(c, S, r, q, hv, rank_value, settings) for c in candidates]
    scored.sort(key=lambda sc: sc.overall_score, reverse=True)

    return SymbolScan(
        scored=scored, atm_iv=atm_iv, rank_value=rank_value, exps=exps,
        total_matching_exps=total_matching, truncated=truncated, snap_count=snap_count,
    )
