"""Pluggable data-provider interface.

The engine only ever talks to this abstraction, so swapping Tradier for a
Robinhood (open-stocks-mcp / custom FastMCP) or ThetaData backend later is a
drop-in: implement the four methods, no engine changes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Dict, List

from app.models import Contract, OHLC, OptionType, Quote


class FeedError(Exception):
    """A data-feed call failed after exhausting retries.

    Carries the symbol and a short reason so the market sweep can COUNT the
    failure and surface it in notes instead of silently dropping the name.
    """

    def __init__(self, symbol: str, reason: str = ""):
        self.symbol = symbol
        self.reason = reason
        super().__init__(f"{symbol}: {reason}" if reason else symbol)


class OptionsDataProvider(ABC):
    # Providers that can fetch many quotes in one call set this True and override
    # get_quotes_batch(); the market sweep uses it for a cheap Stage-1 pre-pass.
    supports_batch_quotes: bool = False

    @abstractmethod
    def get_quote(self, symbol: str) -> Quote:
        """Latest underlying price (and dividend yield if available)."""

    @abstractmethod
    def get_expirations(self, symbol: str) -> List[date]:
        """All listed expiration dates for the underlying, ascending."""

    @abstractmethod
    def get_chain(
        self, symbol: str, expiration: date, side: str = "both"
    ) -> List[Contract]:
        """Full option chain for one expiration.

        `side` is one of "calls", "puts", "both".
        """

    @abstractmethod
    def get_history(self, symbol: str, days: int = 60) -> List[OHLC]:
        """Daily OHLC bars for realized-volatility estimation, oldest first."""

    def get_price_and_history(self, symbol: str, days: int = 60):
        """Return (underlying_price, [OHLC]) for the cheap market-sweep pre-pass.

        Default = two calls (quote + history). Providers that can serve both in a
        single cheap call (e.g. CBOE via Yahoo's chart endpoint) should override
        this so the sweep doesn't pay for a heavy chain fetch just to read a price.
        """
        price = self.get_quote(symbol).last
        return price, self.get_history(symbol, days)

    def get_quotes_batch(self, symbols: List[str]) -> Dict[str, Quote]:
        """Return {SYMBOL: Quote} for many symbols.

        Default = one get_quote() per symbol (works for every provider, but is
        no faster than the naive path). Providers whose API takes a symbol LIST
        in a single call (e.g. Tradier POST /markets/quotes) should override this
        and set ``supports_batch_quotes = True`` so the sweep can prune the whole
        universe in a handful of requests.
        """
        out: Dict[str, Quote] = {}
        for s in symbols:
            try:
                out[s.upper()] = self.get_quote(s)
            except Exception:
                continue
        return out


class UnknownSide(ValueError):
    """A `side` value that is not one of calls / puts / both.

    Callers must not treat an unrecognized side as "both": a typo like
    "callz" would then silently return the whole board and the user would
    read a plausible-looking result for a request that was never made.
    """

    def __init__(self, side: object):
        self.side = side
        super().__init__(
            f"side must be one of 'calls', 'puts', or 'both' (got {side!r})"
        )


# Canonical side spellings accepted from callers, mapped to the canonical form.
_SIDE_ALIASES = {
    "calls": "calls",
    "call": "calls",
    "puts": "puts",
    "put": "puts",
    "both": "both",
    "all": "both",
}


def normalize_side(side: object) -> str:
    """Return the canonical side ('calls' | 'puts' | 'both').

    Raises UnknownSide for anything else, including None. Blank/None is treated
    as the documented default ('both') only when the caller omitted it entirely;
    callers that want that default should pass 'both' explicitly.
    """
    if side is None or (isinstance(side, str) and side.strip() == ""):
        return "both"
    key = str(side).strip().lower()
    try:
        return _SIDE_ALIASES[key]
    except KeyError:
        raise UnknownSide(side) from None


def filter_side(option_type: OptionType, side: str) -> bool:
    """True if `option_type` is in the requested `side`.

    An unknown side raises UnknownSide rather than widening to "both" (see
    normalize_side). Accepts the alias spellings ("call"/"put"/"all").
    """
    canonical = normalize_side(side)
    if canonical == "both":
        return True
    if canonical == "calls":
        return option_type == OptionType.CALL
    return option_type == OptionType.PUT

