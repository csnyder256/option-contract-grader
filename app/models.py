"""Core data structures shared across the engine and providers.

Internal computation uses plain dataclasses (cheap, hashable-friendly, no
validation overhead). The API layer (app/api.py) defines its own pydantic
request/response models and converts at the boundary.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from datetime import date
from enum import Enum
from typing import List, Optional, Dict, Any


def _is_price(value: Any) -> bool:
    """True for a real, finite, positive number -- something that can be a price.

    ``NaN`` and the infinities are not prices, and they are not harmless here.
    Every guard in this module is a comparison, and every comparison against
    ``NaN`` is False, so a ``NaN`` bid slips through as if it were a quote:
    ``ask >= bid`` reads True, the mid becomes ``NaN``, and the bid/ask spread
    the hard liquidity gate exists to police becomes ``NaN`` too -- a value that
    gate cannot trip on, because ``NaN > MAX_SPREAD_PCT`` is also False. ``inf``
    is worse: an ``inf``/``inf`` quote looks two-sided, has a mid of ``inf``,
    and reports a cost per contract of ``inf``.

    Nothing that is not a finite positive number is a quote.
    """
    try:
        return math.isfinite(value) and value > 0
    except TypeError:  # None, a string, anything not number-like
        return False


class OptionType(str, Enum):
    CALL = "call"
    PUT = "put"


@dataclass
class Quote:
    """Underlying-symbol snapshot."""
    symbol: str
    last: float
    dividend_yield: float = 0.0  # annualized decimal (0.02 = 2%)


@dataclass
class OHLC:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class Greeks:
    delta: float
    gamma: float
    theta: float  # per calendar day
    vega: float   # per 1 percentage-point (1%) change in IV
    rho: float    # per 1 percentage-point (1%) change in rate
    d1: float
    d2: float


@dataclass
class Contract:
    """A single option contract as returned by a data provider."""
    underlying: str
    occ_symbol: str
    option_type: OptionType
    strike: float
    expiration: date
    dte: int                       # calendar days to expiration
    bid: float
    ask: float
    last: float
    volume: int = 0
    open_interest: int = 0
    provider_iv: Optional[float] = None     # vendor-supplied IV (cross-check)
    provider_greeks: Optional[Dict[str, float]] = None

    @property
    def mid(self) -> float:
        if _is_price(self.bid) and _is_price(self.ask) and self.ask >= self.bid:
            return (self.bid + self.ask) / 2.0
        # Fall back to last trade if we don't have a clean two-sided market.
        return self.last if _is_price(self.last) else 0.0

    @property
    def has_two_sided_market(self) -> bool:
        """A quotable market: a real bid, a real ask, and no cross.

        A crossed book (``ask < bid``) is a stale or corrupted quote, not a
        market: the two-sided path would compute a mid off the crossed pair and
        report a negative bid/ask spread. The ``ask >= bid`` guard was already
        here before this pass (so this property's behavior is unchanged); what
        was missing was regression coverage, which
        ``tests/test_failures_surface.py`` now pins alongside the one-sided and
        locked cases.

        A *non-finite* bid or ask is also not a market. ``NaN`` and ``inf`` are
        not prices (see ``_is_price``): ``ask >= bid`` reads True for them, so
        the crossed-book guard would wave them straight through and this
        property would report a quotable two-sided market where there is no
        quote at all.
        """
        return _is_price(self.bid) and _is_price(self.ask) and self.ask >= self.bid

    @property
    def spread(self) -> float:
        if self.has_two_sided_market:
            return self.ask - self.bid
        return 0.0

    @property
    def spread_pct(self) -> Optional[float]:
        m = self.mid
        if m > 0 and self.has_two_sided_market:
            return (self.ask - self.bid) / m
        return None


@dataclass
class SubScore:
    """One graded dimension shown to the user."""
    key: str             # internal id, e.g. "value"
    label: str           # UI label, e.g. "Value (Price Edge)"
    score: float         # 0-100
    grade: str           # A-F
    explanation: str     # plain-English, no jargon on the face of it
    metric: Optional[str] = None  # optional raw number/tooltip detail

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ScoredContract:
    contract: Contract
    iv: Optional[float]              # our computed implied volatility (decimal)
    greeks: Optional[Greeks]
    overall_score: float
    overall_grade: str
    overall_meaning: str
    sub_scores: List[SubScore]
    break_even: float
    cost_per_contract: float        # premium * 100
    flags: List[str] = field(default_factory=list)
    score_trace: Dict[str, Any] = field(default_factory=dict)
    scenario_context: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        c = self.contract
        return {
            "underlying": c.underlying,
            "occ_symbol": c.occ_symbol,
            "type": c.option_type.value,
            "strike": c.strike,
            "expiration": c.expiration.isoformat(),
            "dte": c.dte,
            "bid": c.bid,
            "ask": c.ask,
            "mid": round(c.mid, 4),
            "premium_per_share": round(c.mid, 4),
            "cost_per_contract": round(self.cost_per_contract, 2),
            "volume": c.volume,
            "open_interest": c.open_interest,
            "iv": round(self.iv, 4) if self.iv is not None else None,
            "break_even": round(self.break_even, 4),
            "overall_score": round(self.overall_score, 1),
            "overall_grade": self.overall_grade,
            "overall_meaning": self.overall_meaning,
            "sub_scores": [s.to_dict() for s in self.sub_scores],
            "flags": self.flags,
            "score_trace": self.score_trace,
            "scenario_context": self.scenario_context,
        }
