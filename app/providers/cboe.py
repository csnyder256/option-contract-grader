"""Free, no-account interim data provider: CBOE delayed quotes + Yahoo history.

While a brokerage data source (Tradier) is being set up, this provider lets the
whole engine run on REAL market data with zero credentials:

- **Option chain + underlying quote**: CBOE's public delayed-quotes JSON
  (``cdn.cboe.com/api/global/delayed_quotes/options/{SYMBOL}.json``). It returns
  every listed contract with bid/ask, IV, all five Greeks, open interest, volume,
  and the underlying price. ~15 minutes delayed. No key, no account.
- **Daily price history** (for realized volatility): Yahoo Finance's public chart
  endpoint. No key.

This is strictly better than guessing IV from a sector table: IV here is the real
market number for each specific strike/expiration. As with Tradier, the engine
still solves its own IV from the mid; CBOE's IV/Greeks are kept as a cross-check.

Both feeds are unofficial and can change without notice - hence the Tradier path
remains the supported production backend.
"""

from __future__ import annotations

import random
import time
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.config import settings
from app.models import Contract, OHLC, OptionType, Quote
from app.providers.base import FeedError, OptionsDataProvider, filter_side

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) OptionsFinder/0.1"
# Statuses worth retrying: rate limiting (429), Cloudflare bot-block (403), and
# transient upstream errors. Everything else (200, 404, ...) returns immediately.
_RETRYABLE_STATUS = {403, 429, 500, 502, 503, 504}


# --------------------------------------------------------------------------- #
# Pure parsers (no network) - tested offline.
# --------------------------------------------------------------------------- #

def parse_occ_symbol(occ: str) -> Tuple[str, date, OptionType, float]:
    """Parse an OCC option symbol like ``AAPL260629C00205000``.

    Layout: ROOT + YYMMDD + (C|P) + strike*1000 (8 digits). The right-hand 15
    characters are fixed-width, so the root can be any length.
    """
    suffix = occ[-15:]
    root = occ[:-15]
    yy = int(suffix[0:2])
    mm = int(suffix[2:4])
    dd = int(suffix[4:6])
    cp = suffix[6].upper()
    strike = int(suffix[7:15]) / 1000.0
    exp = date(2000 + yy, mm, dd)
    otype = OptionType.CALL if cp == "C" else OptionType.PUT
    return root, exp, otype, strike


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _underlying_price(data: Dict[str, Any]) -> float:
    for key in ("current_price", "close", "prev_day_close"):
        v = _to_float(data.get(key))
        if v > 0:
            return v
    return 0.0


def parse_cboe_quote(payload: Dict[str, Any], symbol: str, default_q: float = 0.0) -> Quote:
    data = payload.get("data") or {}
    return Quote(symbol=symbol.upper(), last=_underlying_price(data), dividend_yield=default_q)


def parse_cboe_expirations(payload: Dict[str, Any], today: Optional[date] = None) -> List[date]:
    today = today or date.today()
    data = payload.get("data") or {}
    exps = set()
    for o in data.get("options") or []:
        sym = o.get("option")
        if not sym:
            continue
        try:
            _, exp, _, _ = parse_occ_symbol(sym)
        except (ValueError, IndexError):
            continue
        if exp >= today:
            exps.add(exp)
    return sorted(exps)


def parse_cboe_chain(
    payload: Dict[str, Any], expiration: date, side: str = "both",
    today: Optional[date] = None,
) -> List[Contract]:
    today = today or date.today()
    data = payload.get("data") or {}
    underlying = (data.get("symbol") or "").upper()
    out: List[Contract] = []
    for o in data.get("options") or []:
        sym = o.get("option")
        if not sym:
            continue
        try:
            root, exp, otype, strike = parse_occ_symbol(sym)
        except (ValueError, IndexError):
            continue
        if exp != expiration:
            continue
        if not filter_side(otype, side):
            continue
        iv = _to_float(o.get("iv")) or None
        greeks = {
            "delta": _to_float(o.get("delta")),
            "gamma": _to_float(o.get("gamma")),
            "theta": _to_float(o.get("theta")),
            "vega": _to_float(o.get("vega")),
            "rho": _to_float(o.get("rho")),
            "mid_iv": iv,
        }
        out.append(
            Contract(
                underlying=underlying or root.upper(),
                occ_symbol=sym,
                option_type=otype,
                strike=strike,
                expiration=exp,
                dte=max(0, (exp - today).days),
                bid=_to_float(o.get("bid")),
                ask=_to_float(o.get("ask")),
                last=_to_float(o.get("last_trade_price")),
                volume=int(_to_float(o.get("volume"))),
                open_interest=int(_to_float(o.get("open_interest"))),
                provider_iv=iv,
                provider_greeks=greeks,
            )
        )
    return out


def parse_yahoo_history(payload: Dict[str, Any]) -> List[OHLC]:
    try:
        result = payload["chart"]["result"][0]
    except (KeyError, IndexError, TypeError):
        return []
    timestamps = result.get("timestamp") or []
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    out: List[OHLC] = []
    for i, ts in enumerate(timestamps):
        try:
            o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        except IndexError:
            continue
        if None in (o, h, l, c):
            continue
        d = datetime.fromtimestamp(ts, tz=timezone.utc).date()
        out.append(OHLC(day=d, open=float(o), high=float(h), low=float(l), close=float(c)))
    out.sort(key=lambda b: b.day)
    return out


def parse_yahoo_meta_price(payload: Dict[str, Any]) -> Optional[float]:
    """Underlying price from a Yahoo chart payload (meta.regularMarketPrice)."""
    try:
        meta = payload["chart"]["result"][0]["meta"]
    except (KeyError, IndexError, TypeError):
        return None
    for key in ("regularMarketPrice", "previousClose", "chartPreviousClose"):
        v = meta.get(key)
        if v:
            return float(v)
    return None


def _yahoo_range(days: int) -> str:
    if days <= 25:
        return "2mo"
    if days <= 60:
        return "3mo"
    if days <= 150:
        return "6mo"
    return "1y"


# --------------------------------------------------------------------------- #
# Live provider.
# --------------------------------------------------------------------------- #

class CboeProvider(OptionsDataProvider):
    OPTIONS_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"
    YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"

    def __init__(self, default_dividend_yield: Optional[float] = None, timeout: float = 20.0,
                 cache_ttl: float = 30.0):
        self.default_q = (
            default_dividend_yield if default_dividend_yield is not None
            else settings.default_dividend_yield
        )
        self._client = httpx.Client(
            headers={
                "User-Agent": _UA,
                "Accept": "application/json",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://finance.yahoo.com/",
            },
            timeout=timeout, follow_redirects=True,
        )
        self._cache_ttl = cache_ttl
        self._chain_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._retries = settings.feed_retries
        self._backoff_base = settings.feed_backoff_base
        self._backoff_cap = settings.feed_backoff_cap

    def _sleep_backoff(self, attempt: int, retry_after: Optional[str]) -> None:
        if retry_after:
            try:
                wait = float(retry_after)
            except ValueError:
                wait = self._backoff_base * (2 ** attempt)
        else:
            wait = self._backoff_base * (2 ** attempt)
        wait = min(wait, self._backoff_cap)
        wait += random.uniform(0.0, 0.25 * wait)  # jitter: de-synchronize workers
        time.sleep(wait)

    def _request_with_retry(self, url: str, params: Optional[dict] = None,
                            symbol: str = "") -> httpx.Response:
        """GET with exponential backoff on 429/403/5xx and network errors.

        Returns the response for any non-retryable status (incl. 200 and 404).
        Raises FeedError once retries are exhausted so the caller can COUNT the
        failure instead of silently dropping the name.
        """
        last_exc: Optional[Exception] = None
        for attempt in range(self._retries + 1):
            try:
                resp = self._client.get(url, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_exc = e
                if attempt < self._retries:
                    self._sleep_backoff(attempt, None)
                    continue
                raise FeedError(symbol or url, f"network error: {type(e).__name__}")
            if resp.status_code in _RETRYABLE_STATUS:
                if attempt < self._retries:
                    self._sleep_backoff(attempt, resp.headers.get("Retry-After"))
                    continue
                raise FeedError(symbol or url,
                                f"HTTP {resp.status_code} after {attempt + 1} tries")
            return resp
        raise FeedError(symbol or url, f"network error: {last_exc}")

    def _fetch_options(self, symbol: str) -> Dict[str, Any]:
        """Fetch (and briefly cache) the full CBOE chain JSON for a symbol.

        Cached so get_expirations() + per-expiration get_chain() calls within one
        scan reuse a single HTTP request.
        """
        key = symbol.upper()
        now = time.monotonic()
        hit = self._chain_cache.get(key)
        if hit and now - hit[0] < self._cache_ttl:
            return hit[1]
        payload = self._get_options_json(key)
        self._chain_cache[key] = (now, payload)
        return payload

    def _get_options_json(self, symbol: str) -> Dict[str, Any]:
        # Equities/ETFs use the plain symbol; indices use a leading underscore;
        # dotted/dashed tickers (BRK.B / BRK-B) use the stripped root (BRKB).
        stripped = symbol.replace(".", "").replace("-", "")
        candidates = [symbol, "_" + symbol]
        if stripped != symbol:
            candidates.append(stripped)
        last_status = None
        for sym in candidates:
            resp = self._request_with_retry(self.OPTIONS_URL.format(sym=sym), symbol=symbol)
            if resp.status_code == 200:
                return resp.json()
            last_status = resp.status_code  # 404 -> try the next candidate spelling
        raise FeedError(symbol, f"no CBOE delayed-quote data (last HTTP {last_status})")

    def get_quote(self, symbol: str) -> Quote:
        return parse_cboe_quote(self._fetch_options(symbol), symbol, self.default_q)

    def get_expirations(self, symbol: str) -> List[date]:
        return parse_cboe_expirations(self._fetch_options(symbol))

    def get_chain(self, symbol: str, expiration: date, side: str = "both") -> List[Contract]:
        return parse_cboe_chain(self._fetch_options(symbol), expiration, side=side)

    def _chart_json(self, symbol: str, days: int) -> Dict[str, Any]:
        # Yahoo uses a dash for class shares (BRK.B -> BRK-B).
        url = self.YAHOO_URL.format(sym=symbol.upper().replace(".", "-"))
        resp = self._request_with_retry(
            url, params={"range": _yahoo_range(days), "interval": "1d"}, symbol=symbol
        )
        if resp.status_code != 200:  # e.g. 404 for a delisted/invalid ticker
            raise FeedError(symbol, f"Yahoo chart HTTP {resp.status_code}")
        return resp.json()

    def get_history(self, symbol: str, days: int = 60) -> List[OHLC]:
        return parse_yahoo_history(self._chart_json(symbol, days))

    def get_price_and_history(self, symbol: str, days: int = 60):
        """One Yahoo chart call yields BOTH the price and the daily history -
        no need to download the 1.5 MB CBOE chain just to read the price.

        Raises FeedError on persistent feed failure so the sweep can count it.
        """
        payload = self._chart_json(symbol, days)
        price = parse_yahoo_meta_price(payload) or 0.0
        bars = parse_yahoo_history(payload)
        if price <= 0 and bars:
            price = bars[-1].close
        return price, bars

    def close(self) -> None:
        self._client.close()
