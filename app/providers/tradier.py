"""Tradier market-data provider.

Tradier returns the *entire* option chain for an expiration in a single REST
call (unlike Robinhood's one-request-per-contract fan-out), which keeps us
comfortably under the documented 120 requests/minute market-data cap. Greeks/IV
are included (courtesy of ORATS, refreshed ~hourly); we keep them only as a
cross-check and compute our own from the real-time bid/ask.

The JSON-parsing logic is split into pure module functions so it can be tested
offline against recorded fixtures (see tests/test_tradier.py).

Docs: https://docs.tradier.com/reference (markets/options/chains, expirations,
quotes, history) and https://docs.tradier.com/docs/rate-limiting
"""

from __future__ import annotations

import time
from collections import deque
from datetime import date, datetime
from typing import Any, Dict, List, Optional

import httpx

from app.config import settings
from app.models import Contract, OHLC, OptionType, Quote
from app.providers.base import OptionsDataProvider, filter_side


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

    def _get(self, path: str, params: Dict[str, Any], retries: int = 3) -> Dict[str, Any]:
        for attempt in range(retries + 1):
            self._throttle()
            resp = self._client.get(path, params=params)
            if resp.status_code == 429:
                # Honor the server's hint if present, else exponential backoff.
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else min(30.0, 2.0 ** attempt)
                if attempt < retries:
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError("Unreachable")

    def _post(self, path: str, data: Dict[str, Any], retries: int = 3) -> Dict[str, Any]:
        """POST with the same throttle + 429 backoff as _get (used for bulk quotes)."""
        for attempt in range(retries + 1):
            self._throttle()
            resp = self._client.post(path, data=data)
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else min(30.0, 2.0 ** attempt)
                if attempt < retries:
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError("Unreachable")

    # -- provider interface -----------------------------------------------
    def get_quote(self, symbol: str) -> Quote:
        payload = self._get("/v1/markets/quotes", {"symbols": symbol, "greeks": "false"})
        return parse_quote(payload, symbol, self.default_q)

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
                "/v1/markets/quotes", {"symbols": ",".join(chunk), "greeks": "false"}
            )
            out.update(parse_quotes_batch(payload, self.default_q))
        return out

    def get_expirations(self, symbol: str) -> List[date]:
        payload = self._get(
            "/v1/markets/options/expirations",
            {"symbol": symbol, "includeAllRoots": "true", "strikes": "false"},
        )
        return parse_expirations(payload)

    def get_chain(self, symbol: str, expiration: date, side: str = "both") -> List[Contract]:
        payload = self._get(
            "/v1/markets/options/chains",
            {"symbol": symbol, "expiration": expiration.isoformat(), "greeks": "true"},
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
        )
        return parse_history(payload)

    def close(self) -> None:
        self._client.close()
