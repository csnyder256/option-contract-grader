"""Tradier market-data provider.

Tradier returns the *entire* option chain for an expiration in a single REST
call (unlike Robinhood's one-request-per-contract fan-out), which keeps us
comfortably under the documented 120 requests/minute market-data cap. Greeks/IV
are included (courtesy of ORATS, refreshed ~hourly); we keep them only as a
cross-check and compute our own from the real-time bid/ask.

The JSON-parsing logic is split into pure module functions so it can be tested
offline against recorded fixtures (see tests/test_tradier.py).

Failure contract: HTTP 429 is retried with the server's ``Retry-After`` honored
(delta-seconds or HTTP-date) and clamped to ``FEED_BACKOFF_CAP``; when the
retries are exhausted the call raises the same typed ``FeedError`` the CBOE
provider raises, naming the symbol. Other HTTP, transport, and malformed JSON failures also surface as typed
``FeedError`` values, so sweeps can count them without treating them as empty data.

Docs: https://docs.tradier.com/reference (markets/options/chains, expirations,
quotes, history) and https://docs.tradier.com/docs/rate-limiting
"""

from __future__ import annotations

import time
import math
from collections import deque
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Dict, List, Optional

import httpx

from app.config import settings
from app.models import Contract, OHLC, OptionType, Quote
from app.providers.base import FeedError, OptionsDataProvider, filter_side


# --------------------------------------------------------------------------- #
# Pure helpers / parsers (no network) - tested offline.
# --------------------------------------------------------------------------- #

def _as_list(value: Any) -> List[Any]:
    """Tradier collapses single-element arrays to a scalar/object; normalize."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def parse_retry_after(value: Optional[str], now: Optional[datetime] = None) -> Optional[float]:
    """A ``Retry-After`` header as a non-negative delay in seconds, or None.

    RFC 9110 allows the header in two forms and both are legal in the wild:
    delta-seconds (``"120"``) or an HTTP-date
    (``"Wed, 21 Oct 2026 07:28:00 GMT"``). Only the first used to be handled -
    the date form reached ``float()``, raised ``ValueError``, and killed the
    caller's retry loop instead of backing off. A value that parses as neither
    returns None so the caller can use its own schedule, and a date already in
    the past returns 0.0 (``"retry now"``) rather than None.
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


def _symbol_hint(params: Optional[Dict[str, Any]]) -> str:
    """Best-effort symbol label for a failed market-data call.

    ``/markets/quotes`` takes either ``symbol`` (one) or ``symbols`` (a
    comma-separated list); a rate-limit error should name the first ticker
    rather than the URL, because that is what the sweep reports and counts.
    """
    for key in ("symbol", "symbols"):
        raw = (params or {}).get(key)
        if raw:
            first = str(raw).split(",")[0].strip()
            if first:
                return first
    return "tradier"


def parse_quote(payload: Dict[str, Any], symbol: str, default_q: float = 0.0) -> Quote:
    quotes = _as_list((payload.get("quotes") or {}).get("quote"))
    if not quotes:
        raise ValueError(f"No quote returned for {symbol}")
    q = quotes[0]
    last = _to_float(q.get("last"))
    if last <= 0:
        # Fall back to close / prevclose if last is missing pre/post market.
        last = _to_float(q.get("close")) or _to_float(q.get("prevclose"))
    # Tradier quotes don't reliably carry a dividend yield; use the fallback.
    div_yield = _to_float(q.get("dividend_yield"), default_q)
    return Quote(symbol=symbol.upper(), last=last, dividend_yield=div_yield)


def parse_quotes_batch(payload: Dict[str, Any], default_q: float = 0.0) -> Dict[str, Quote]:
    """Parse a multi-symbol /markets/quotes response into {SYMBOL: Quote}.

    Tradier collapses a single-element ``quote`` array to one object and may
    return an ``unmatched_symbols`` block for unknown tickers; both are handled.
    """
    quotes = _as_list((payload.get("quotes") or {}).get("quote"))
    out: Dict[str, Quote] = {}
    for q in quotes:
        sym = (q.get("symbol") or "").upper()
        if not sym:
            continue
        last = _to_float(q.get("last"))
        if last <= 0:
            last = _to_float(q.get("close")) or _to_float(q.get("prevclose"))
        out[sym] = Quote(
            symbol=sym, last=last,
            dividend_yield=_to_float(q.get("dividend_yield"), default_q),
        )
    return out


def parse_expirations(payload: Dict[str, Any]) -> List[date]:
    raw = (payload.get("expirations") or {}).get("date")
    dates = [_parse_date(d) for d in _as_list(raw) if d]
    return sorted(dates)


def parse_chain(
    payload: Dict[str, Any], side: str = "both", today: Optional[date] = None
) -> List[Contract]:
    today = today or date.today()
    options = _as_list((payload.get("options") or {}).get("option"))
    out: List[Contract] = []
    for o in options:
        otype_raw = (o.get("option_type") or "").lower()
        if otype_raw not in ("call", "put"):
            continue
        option_type = OptionType(otype_raw)
        if not filter_side(option_type, side):
            continue
        exp_raw = o.get("expiration_date")
        if not exp_raw:
            continue
        exp = _parse_date(exp_raw)
        greeks = o.get("greeks") or None
        provider_iv = None
        if isinstance(greeks, dict):
            provider_iv = _to_float(greeks.get("mid_iv"), 0.0) or None
        out.append(
            Contract(
                underlying=(o.get("underlying") or o.get("root_symbol") or "").upper(),
                occ_symbol=o.get("symbol", ""),
                option_type=option_type,
                strike=_to_float(o.get("strike")),
                expiration=exp,
                dte=max(0, (exp - today).days),
                bid=_to_float(o.get("bid")),
                ask=_to_float(o.get("ask")),
                last=_to_float(o.get("last")),
                volume=_to_int(o.get("volume")),
                open_interest=_to_int(o.get("open_interest")),
                provider_iv=provider_iv,
                provider_greeks=greeks if isinstance(greeks, dict) else None,
            )
        )
    return out


def parse_history(payload: Dict[str, Any]) -> List[OHLC]:
    days = _as_list((payload.get("history") or {}).get("day"))
    out: List[OHLC] = []
    for d in days:
        try:
            out.append(
                OHLC(
                    day=_parse_date(d["date"]),
                    open=_to_float(d.get("open")),
                    high=_to_float(d.get("high")),
                    low=_to_float(d.get("low")),
                    close=_to_float(d.get("close")),
                    volume=_to_float(d.get("volume")),
                )
            )
        except (KeyError, ValueError):
            continue
    out.sort(key=lambda b: b.day)
    return out


# --------------------------------------------------------------------------- #
# Live provider.
# --------------------------------------------------------------------------- #

class TradierProvider(OptionsDataProvider):
    """REST client with a 120/min rate-limit guard and 429 backoff."""

    MAX_PER_MINUTE = 118  # stay just under the documented 120/min cap
    supports_batch_quotes = True  # /markets/quotes takes a comma-separated list

    def __init__(
        self,
        token: Optional[str] = None,
        base_url: Optional[str] = None,
        default_dividend_yield: Optional[float] = None,
        timeout: float = 15.0,
    ):
        self.token = token if token is not None else settings.tradier_token
        self.base_url = base_url or settings.tradier_base_url
        self.default_q = (
            default_dividend_yield
            if default_dividend_yield is not None
            else settings.default_dividend_yield
        )
        if not self.token:
            raise ValueError(
                "TRADIER_TOKEN is not set. Add it to your environment or .env file."
            )
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
            },
            timeout=timeout,
        )
        self._req_times: deque = deque()

    # -- rate limiting -----------------------------------------------------
    def _throttle(self) -> None:
        now = time.monotonic()
        while self._req_times and now - self._req_times[0] > 60.0:
            self._req_times.popleft()
        if len(self._req_times) >= self.MAX_PER_MINUTE:
            sleep_for = 60.0 - (now - self._req_times[0]) + 0.05
            if sleep_for > 0:
                time.sleep(sleep_for)
        self._req_times.append(time.monotonic())

    def _retry_delay(self, attempt: int, retry_after: Optional[str]) -> float:
        """Honor delta-seconds and HTTP-date, bounded by the feed backoff cap."""
        wait = settings.feed_backoff_base * (2 ** attempt)
        parsed = parse_retry_after(retry_after)
        if parsed is not None:
            wait = parsed
        return max(0.0, min(wait, settings.feed_backoff_cap))

    def _request_raw(
        self, send: Callable[[], httpx.Response], params: Dict[str, Any], retries: int,
    ) -> httpx.Response:
        """Shared transport; callers retain the typed JSON failure contract."""
        for attempt in range(retries + 1):
            self._throttle()
            try:
                resp = send()
            except httpx.RequestError as exc:
                raise FeedError(_symbol_hint(params), "market-data transport failed") from exc
            if resp.status_code == 429:
                if attempt < retries:
                    time.sleep(self._retry_delay(attempt, resp.headers.get("Retry-After")))
                    continue
                raise FeedError(_symbol_hint(params),
                                f"HTTP 429 after {attempt + 1} tries (rate limited)")
            return resp
        raise ValueError("retries must be non-negative")

    def _get_raw(self, path: str, params: Dict[str, Any], retries: int = 3):
        return self._request_raw(lambda: self._client.get(path, params=params), params, retries)

    def _post_raw(self, path: str, data: Dict[str, Any], retries: int = 3):
        return self._request_raw(lambda: self._client.post(path, data=data), data, retries)

    def _get(self, path: str, params: Dict[str, Any], retries: int = 3,
             symbol: str = "") -> Dict[str, Any]:
        return self._parse_json(self._get_raw(path, params, retries),
                                symbol or _symbol_hint(params), path)

    def _post(self, path: str, data: Dict[str, Any], retries: int = 3,
              symbol: str = "") -> Dict[str, Any]:
        return self._parse_json(self._post_raw(path, data, retries),
                                symbol or _symbol_hint(data), path)

    # -- provider interface -----------------------------------------------
    def _parse_json(self, resp, symbol: str = "", what: str = "request") -> Dict[str, Any]:
        """JSON body, or a counted FeedError -- never an empty result.

        Tradier answers a bad symbol with ``{"quotes": "null"}`` (or an
        ``errors`` block) at HTTP 200. ``parse_quote`` already raises on that,
        but the chain/history/expiration parsers returned an empty list, and an
        empty list is indistinguishable from "no options listed". A 429 or 5xx
        that survived the retry loop used to arrive as a bare
        ``httpx.HTTPStatusError``, which callers catching ``FeedError`` never
        saw -- the README lists that as a documented rough edge.

        Returns the parsed object on success. A non-200, an unparseable body,
        an ``errors`` block, or a JSON *non-object* (array/string/number/null)
        all raise ``FeedError``. Only a well-formed object passes through, so a
        caller can still receive an empty chain from a legitimate
        ``{"options": null}`` without that being confused for a dead feed.
        """
        status = getattr(resp, "status_code", 200)
        if status != 200:
            raise FeedError(
                symbol or "?",
                f"{what}: HTTP {status}{'' if status != 429 else ' (rate limit, retries exhausted)'}",
            )
        try:
            payload = resp.json()
        except ValueError as e:
            raise FeedError(symbol or "?", f"{what}: unparseable JSON response") from e
        if not isinstance(payload, dict):
            # A JSON array, string, number, or null at HTTP 200 used to collapse
            # to {} and be parsed as an empty chain/history -- a malformed feed
            # wearing a normal empty result's clothes, which is exactly what this
            # module's error contract exists to prevent. Only an OBJECT can carry
            # ``options`` / ``history``; anything else is a broken response.
            raise FeedError(symbol or "?", f"{what}: unexpected JSON shape (not an object)")
        if payload.get("errors"):
            errs = payload["errors"]
            if isinstance(errs, dict):
                errs = errs.get("error") or errs
            raise FeedError(symbol or "?", f"{what}: {errs}")
        return payload

    def get_quote(self, symbol: str) -> Quote:
        resp = self._get_raw("/v1/markets/quotes", {"symbols": symbol, "greeks": "false"})
        return parse_quote(self._parse_json(resp, symbol, "quote"), symbol, self.default_q)

    def get_quotes_batch(self, symbols: List[str]) -> Dict[str, Quote]:
        """Fetch many underlying quotes in a few POST calls (chunked).

        Collapses a whole-universe Stage-1 price pass from N requests to
        ~N/quote_batch_size, staying well under the 120/min market-data cap.
        """
        out: Dict[str, Quote] = {}
        batch = max(1, settings.quote_batch_size)
        syms = [s.upper() for s in symbols]
        for i in range(0, len(syms), batch):
            chunk = syms[i:i + batch]
            payload = self._post(
                "/v1/markets/quotes", {"symbols": ",".join(chunk), "greeks": "false"},
                symbol=",".join(chunk),
            )
            out.update(parse_quotes_batch(payload, self.default_q))
        return out

    def get_expirations(self, symbol: str) -> List[date]:
        payload = self._get(
            "/v1/markets/options/expirations",
            {"symbol": symbol, "includeAllRoots": "true", "strikes": "false"},
            symbol=symbol,
        )
        return parse_expirations(payload)

    def get_chain(self, symbol: str, expiration: date, side: str = "both") -> List[Contract]:
        payload = self._get(
            "/v1/markets/options/chains",
            {"symbol": symbol, "expiration": expiration.isoformat(), "greeks": "true"},
            symbol=symbol,
        )
        return parse_chain(payload, side=side)

    def get_history(self, symbol: str, days: int = 60) -> List[OHLC]:
        from datetime import timedelta

        end = date.today()
        # Pull extra calendar days to cover weekends/holidays.
        start = end - timedelta(days=int(days * 1.6) + 10)
        payload = self._get(
            "/v1/markets/history",
            {
                "symbol": symbol,
                "interval": "daily",
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
            symbol=symbol,
        )
        return parse_history(payload)

    def close(self) -> None:
        self._client.close()
