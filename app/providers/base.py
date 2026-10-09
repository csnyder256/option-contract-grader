"""Pluggable data-provider interface.

The engine only ever talks to this abstraction, so swapping Tradier for a
Robinhood (open-stocks-mcp / custom FastMCP) or ThetaData backend later is a
drop-in: implement the four methods, no engine changes.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Dict, List, Optional

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


def parse_retry_after(
    value: Optional[str], now: Optional[datetime] = None
) -> Optional[float]:
    """A ``Retry-After`` header as a non-negative, finite delay in seconds, or None.

    RFC 9110 allows the header in two forms and both are legal in the wild:
    delta-seconds (``"120"``) or an HTTP-date
    (``"Wed, 21 Oct 2026 07:28:00 GMT"``). A value that parses as neither
    returns None so the caller can use its own schedule, and a date already in
    the past returns 0.0 (``"retry now"``) rather than None.

    This is shared by both providers, because a response header is untrusted
    input and must never be handed straight to ``time.sleep``: ``time.sleep``
    raises ``ValueError`` on a negative or non-finite delay, so a single
    ``Retry-After: -1`` (or ``NaN``) would otherwise kill a retry loop with an
    untyped error instead of the typed ``FeedError`` the providers promise. A
    non-finite value (``inf``/``nan``) is therefore rejected as unparseable, and
    every returned delay is clamped to ``>= 0``.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        seconds = float(text)
        return max(0.0, seconds) if math.isfinite(seconds) else None
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:  # an HTTP-date without a zone is UTC by convention
        when = when.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return max(0.0, (when - now).total_seconds())


class OptionsDataProvider(ABC):
    # Providers that can fetch many quotes in one call set this True and override
    # get_quotes_batch(); the market sweep uses it for a cheap Stage-1 pre-pass.
    supports_batch_quotes: bool = False

    def check_status(self, resp, symbol: str, what: str = "request") -> None:
        """Raise FeedError unless `resp` is a usable HTTP 200.

        Shared HTTP contract for every provider. Before this existed each
        provider decided for itself whether a non-200 mattered, and the ones
        that did not check quietly handed a JSON error body to their own parser.
        That parser then found no ``data`` / ``chart`` / ``quotes`` key, returned
        an empty result, and the caller could not tell "this symbol has no
        options" from "the feed rejected us". This keeps the distinction: a
        non-200 is a counted, surfaced failure.

        Statuses already exhausted by the provider's own retry policy arrive
        here as-is; a 404 stays a "no such symbol" report rather than an empty
        chain. This helper is a new shared contract, and it is currently used by
        CBOE only. Tradier keeps its own equivalent check inside
        ``TradierProvider._parse_json`` (it raises ``FeedError`` there rather
        than calling this method), so the two providers agree on the *outcome*
        -- a typed ``FeedError`` for a non-200 -- while still differing in
        transport and retry policy.
        """
        code = getattr(resp, "status_code", None)
        if code == 200:
            return
        raise FeedError(symbol or "?", f"{what}: HTTP {code}")

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

    Accepts call/put/all aliases and case-insensitive spellings. None and blank
    strings retain the helper's existing 'both' default; other unknown values
    raise UnknownSide. HTTP request fields are strings, so JSON null is rejected
    by request validation before this helper runs.
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

