"""Central configuration and tunables.

Everything the engine treats as a "knob" lives here so it can be adjusted
without touching logic: API credentials, the risk-free rate, scoring weights,
grade thresholds, and the liquidity gate. Values come from environment
variables (optionally a local .env file) with sensible defaults.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is optional at runtime
    pass


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# --- Scoring weights (buyer-tuned; long single-leg). Must sum to 100. ---------
# These map 1:1 to the seven displayed sub-scores.
DEFAULT_WEIGHTS: Dict[str, float] = {
    "value": 30.0,      # price edge vs. fair value (realized-vol based)
    "odds": 20.0,       # probability of profit at expiration
    "liquidity": 20.0,  # spread / open interest / volume (also a hard gate)
    "volatility": 12.0, # IV vs HV (cheap/expensive) + IV rank when available
    "move": 10.0,       # required break-even move vs. typical move
    "decay": 4.0,       # time-decay risk for a buyer
    "leverage": 4.0,    # bang-for-buck risk balance
}

# --- Grade bands. (min_inclusive, letter, plain-English overall meaning). -----
GRADE_BANDS = [
    (90.0, "A", "As close to free money as options legally get - the odds, the price, "
                "the liquidity, and the math all line up."),
    (75.0, "B", "Strong setup. A couple of things aren't perfect, but the edge is real."),
    (60.0, "C", "Playable but mediocre. You're not getting robbed, but you're not getting "
                "an edge either."),
    (40.0, "D", "Weak. The price or the odds are working against you."),
    (0.0,  "F", "Put your money on a race horse named Tubby McTubberson instead - "
                "overpriced, illiquid, or the odds are ugly."),
]


@dataclass
class Settings:
    # Which data backend to use: "cboe" (free, no account) or "tradier".
    data_provider: str = field(default_factory=lambda: os.getenv("DATA_PROVIDER", "cboe"))

    # Tradier
    tradier_token: str = field(default_factory=lambda: os.getenv("TRADIER_TOKEN", ""))
    tradier_env: str = field(default_factory=lambda: os.getenv("TRADIER_ENV", "production"))

    # Engine inputs
    risk_free_rate: float = field(default_factory=lambda: _get_float("RISK_FREE_RATE", 0.04))
    default_dividend_yield: float = field(
        default_factory=lambda: _get_float("DEFAULT_DIVIDEND_YIELD", 0.0)
    )

    # Volatility / history
    hv_window: int = field(default_factory=lambda: _get_int("HV_WINDOW", 30))

    # Scan limits (surfaced to the user, never silent)
    max_expirations: int = field(default_factory=lambda: _get_int("MAX_EXPIRATIONS", 6))
    max_results: int = field(default_factory=lambda: _get_int("MAX_RESULTS", 100))

    # Per-name option of strikes to scan around the money (0 = no cap, scan all
    # strikes). A small window (e.g. 12) is the biggest per-name speed lever for
    # large sweeps; it keeps only the ~k strikes nearest the underlying.
    atm_strike_window: int = field(default_factory=lambda: _get_int("ATM_STRIKE_WINDOW", 0))

    # Market sweep: cap how many universe names to scan (0 = all). Lower = faster.
    max_universe: int = field(default_factory=lambda: _get_int("MAX_UNIVERSE", 0))

    # Market sweep tuning -------------------------------------------------------
    # Concurrency for the sweep's worker pools. 4 matches the maintained
    # yahoo-finance client default and avoids tripping Yahoo's 429 limiter.
    sweep_max_workers: int = field(default_factory=lambda: _get_int("SWEEP_MAX_WORKERS", 4))
    # Best contracts kept PER NAME before the global merge (pre-merge cap).
    per_symbol_cap: int = field(default_factory=lambda: _get_int("PER_SYMBOL_CAP", 25))
    # Best contracts allowed PER UNDERLYING on the final Top-N board. This is the
    # knob that keeps one hot name from flooding the board with its strike ladder.
    per_name_board_cap: int = field(default_factory=lambda: _get_int("PER_NAME_BOARD_CAP", 2))
    # How fresh a cached underlying price/HV must be (hours) to be reused. A TTL
    # (not a calendar-day key) so a half-failed cold sweep can't lock in a tiny
    # pool for the rest of the day.
    cache_ttl_hours: float = field(default_factory=lambda: _get_float("CACHE_TTL_HOURS", 12.0))
    # Free-feed retry/backoff (deterministic given inputs aside from jitter).
    feed_retries: int = field(default_factory=lambda: _get_int("FEED_RETRIES", 4))
    feed_backoff_base: float = field(default_factory=lambda: _get_float("FEED_BACKOFF_BASE", 1.0))
    feed_backoff_cap: float = field(default_factory=lambda: _get_float("FEED_BACKOFF_CAP", 30.0))
    # Symbols per batched quote request (Tradier POST /markets/quotes, Yahoo v7).
    quote_batch_size: int = field(default_factory=lambda: _get_int("QUOTE_BATCH_SIZE", 100))
    # Hybrid chain routing: if the in-band set is at/below this, fetch chains from
    # the real-time provider (Tradier); above it, use CBOE's bulk delayed chains.
    realtime_chain_threshold: int = field(
        default_factory=lambda: _get_int("REALTIME_CHAIN_THRESHOLD", 300)
    )
    # Hard cap on how many in-band names a single sweep fetches chains for. Names
    # are liquidity-ordered, so this scans the most-liquid slice; coverage is
    # reported in notes (never silently truncated). 0 = no cap.
    max_chain_scans: int = field(default_factory=lambda: _get_int("MAX_CHAIN_SCANS", 2000))

    # Default expiration window (DTE) when the user gives no date range.
    # Avoids surfacing 0DTE/weekly junk by default; the buyer sweet spot is ~3-6 wks.
    default_min_dte: int = field(default_factory=lambda: _get_int("DEFAULT_MIN_DTE", 14))
    default_max_dte: int = field(default_factory=lambda: _get_int("DEFAULT_MAX_DTE", 60))

    # Liquidity gate
    max_spread_pct: float = field(default_factory=lambda: _get_float("MAX_SPREAD_PCT", 0.15))
    min_open_interest: int = field(default_factory=lambda: _get_int("MIN_OPEN_INTEREST", 50))

    # Storage
    db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", "data/options.db"))

    # Weights (copied so callers can mutate per-request without global effects)
    weights: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))

    @property
    def tradier_base_url(self) -> str:
        if self.tradier_env.lower().startswith("sand"):
            return "https://sandbox.tradier.com"
        return "https://api.tradier.com"

    @property
    def is_sandbox(self) -> bool:
        return self.tradier_env.lower().startswith("sand")


settings = Settings()
