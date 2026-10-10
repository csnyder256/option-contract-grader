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
from app.providers.base import filter_side, normalize_side


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


def _strike_window(
    contracts: List[Contract], underlying: float, settings: Settings
) -> List[Contract]:
    """Keep only the k strikes nearest the money (0 = no cap).

    The biggest per-name speed lever for large market sweeps. Applied to
    whatever list it is given, so a side-filtered board and the unfiltered
    chain each prune against their own strikes.
    """
    k = settings.atm_strike_window
    if not k or k <= 0 or not contracts:
        return contracts
    strikes = sorted({c.strike for c in contracts})
    atm_idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - underlying))
    lo, hi = max(0, atm_idx - k), min(len(strikes), atm_idx + k + 1)
    allowed = set(strikes[lo:hi])
    return [c for c in contracts if c.strike in allowed]


def _solve_atm_iv(
    contracts: List[Contract], S: float, r: float, q: float
) -> Optional[float]:
    """The nearest-expiration, nearest-strike solvable IV: the symbol's ATM IV."""
    for c in sorted(contracts, key=lambda c: (c.dte, abs(c.strike - S))):
        if c.mid <= 0:
            continue
        Tf = max(c.dte, 1) / 365.0
        solved = bs.implied_vol(c.mid, S, c.strike, r, q, Tf, c.option_type)
        if solved:
            return solved
    return None


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

    # Normalize the side up front: a typo raises before any state change (and
    # before a snapshot is recorded), rather than widening silently to both.
    side = normalize_side(filters.side)

    all_exps = provider.get_expirations(symbol)
    exps, total_matching, truncated = select_expirations(
        all_exps, filters.expiration_from, filters.expiration_to, today, settings
    )

    # Fetch BOTH sides in one pass per expiration, then narrow for the board.
    # Both providers apply `side` client-side inside their chain parsers (they
    # always walk the whole payload), so asking for both costs the same request
    # and parse work as asking for one -- and it lets the ATM-IV observation
    # below be a property of the symbol instead of a property of this request.
    chain: List[Contract] = []
    for e in exps:
        chain.extend(provider.get_chain(symbol, e, side="both"))

    contracts = _strike_window(
        [c for c in chain if filter_side(c.option_type, side)], S, settings
    )

    # ATM IV (nearest-expiration, nearest-strike solvable) -> snapshot -> rank.
    # Solved from the UNFILTERED chain: the snapshot is shared per symbol per
    # day and first-write-wins, so a calls-only and a puts-only scan of the same
    # symbol must record the SAME day-IV. Solving from the requested side made
    # the series a mix of call-side and put-side IVs, so IV Rank measured the
    # skew rather than the symbol's own IV history.
    atm_iv = _solve_atm_iv(_strike_window(chain, S, settings), S, r, q)
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
