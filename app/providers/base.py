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


def filter_side(option_type: OptionType, side: str) -> bool:
    side = (side or "both").lower()
    if side in ("both", "all"):
        return True
    if side in ("calls", "call"):
        return option_type == OptionType.CALL
    if side in ("puts", "put"):
        return option_type == OptionType.PUT
    return True
