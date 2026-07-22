"""FastAPI app: single-ticker scan + market-wide screener.

Endpoints
  POST /scan          - one ticker + filters -> graded, ranked list
  POST /market/scan   - start a background sweep of the curated universe
  GET  /market/status - poll sweep progress + top-N results
  GET  /health        - liveness + config sanity
  GET  /key           - the grade key + sub-score labels (for the UI legend)
  GET  /              - serves the frontend (static files)
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import market
from app.config import settings
from app.engine.grading import grade_key
from app.engine.scoring import LABELS
from app.engine.volatility import hv_from_bars
from app.providers.base import FeedError, OptionsDataProvider
from app.providers.cboe import CboeProvider
from app.providers.tradier import TradierProvider
from app.scanner import ScanFilters, scan_symbol
from app.store import Store

app = FastAPI(title="Deterministic Options Finder", version="0.2.0")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

# Lazily-created singletons (so importing the app never requires a token).
_provider: Optional[OptionsDataProvider] = None
_cboe_provider: Optional[CboeProvider] = None
_realtime_provider: Optional[TradierProvider] = None
_store: Optional[Store] = None


def get_provider() -> OptionsDataProvider:
    global _provider
    if _provider is None:
        if settings.data_provider.lower() == "tradier":
            _provider = TradierProvider()
        else:
            _provider = CboeProvider()
    return _provider


def get_cboe_provider() -> CboeProvider:
    """CBOE provider for the sweep's bulk delayed-chain path (large sweeps)."""
    global _cboe_provider
    prov = get_provider()
    if isinstance(prov, CboeProvider):
        return prov  # reuse the primary instance
    if _cboe_provider is None:
        _cboe_provider = CboeProvider()
    return _cboe_provider


def get_realtime_provider() -> Optional[TradierProvider]:
    """Tradier provider for real-time chains on small sweeps, or None if no token."""
    global _realtime_provider
    prov = get_provider()
    if isinstance(prov, TradierProvider):
        return prov  # reuse the primary instance
    if not settings.tradier_token:
        return None
    if _realtime_provider is None:
        try:
            _realtime_provider = TradierProvider()
        except Exception:
            _realtime_provider = None
    return _realtime_provider


def get_store() -> Store:
    global _store
    if _store is None:
        _store = Store(settings.db_path)
    return _store


def _delayed_note() -> Optional[str]:
    if settings.data_provider.lower() == "cboe":
        return "Free CBOE data (no account): quotes are ~15 minutes delayed."
    if settings.is_sandbox:
        return "Sandbox environment: option data is ~15 minutes delayed."
    return None


# --------------------------------------------------------------------------- #
# Single-ticker scan.
# --------------------------------------------------------------------------- #

class ScanRequest(BaseModel):
    ticker: str = Field(..., description="Underlying symbol, e.g. AAPL")
    expiration_from: Optional[date] = Field(None, description="Earliest expiration (inclusive)")
    expiration_to: Optional[date] = Field(None, description="Latest expiration (inclusive)")
    premium_min: Optional[float] = Field(None, description="Min premium PER SHARE")
    premium_max: Optional[float] = Field(None, description="Max premium PER SHARE")
    side: str = Field("both", description='"calls", "puts", or "both"')
    limit: int = Field(50, ge=1, le=500)


def run_scan(req: ScanRequest, provider: OptionsDataProvider, store: Store) -> dict:
    symbol = req.ticker.strip().upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="ticker is required")

    quote = provider.get_quote(symbol)
    S = quote.last
    if S <= 0:
        raise HTTPException(status_code=502, detail=f"No valid price for {symbol}")
    q = quote.dividend_yield or settings.default_dividend_yield

    history = provider.get_history(symbol, days=settings.hv_window + 5)
    hv = hv_from_bars(history, settings.hv_window)

    filters = ScanFilters(
        side=req.side, premium_min=req.premium_min, premium_max=req.premium_max,
        expiration_from=req.expiration_from, expiration_to=req.expiration_to,
    )
    res = scan_symbol(symbol, provider, store, S, q, hv, filters)
    results = [sc.to_dict() for sc in res.scored][: req.limit]

    notes = []
    dn = _delayed_note()
    if dn:
        notes.append(dn)
    if req.expiration_from is None and req.expiration_to is None:
        notes.append(
            f"No date range given - defaulted to expirations {settings.default_min_dte}-"
            f"{settings.default_max_dte} DTE (the buyer sweet spot). Set a range to override."
        )
    if res.truncated:
        notes.append(
            f"Scanned the nearest of {res.total_matching_exps} matching expirations; "
            f"narrow the date range to cover others."
        )
    if res.rank_value is None:
        notes.append(
            f"IV Rank is warming up ({res.snap_count} day(s) of history; needs ~10). "
            f"Using IV-vs-HV for volatility value in the meantime."
        )

    return {
        "meta": {
            "ticker": symbol,
            "underlying_price": round(S, 4),
            "risk_free_rate": settings.risk_free_rate,
            "dividend_yield": q,
            "historical_vol": round(hv, 4) if hv is not None else None,
            "atm_iv": round(res.atm_iv, 4) if res.atm_iv else None,
            "iv_rank": round(res.rank_value, 1) if res.rank_value is not None else None,
            "scanned_expirations": [e.isoformat() for e in res.exps],
            "matching_expirations": res.total_matching_exps,
            "truncated": res.truncated,
            "contracts_scored": len(res.scored),
            "generated_on": date.today().isoformat(),
            "environment": settings.data_provider.lower(),
            "notes": notes,
            "grade_key": grade_key(),
            "sub_score_labels": LABELS,
        },
        "results": results,
    }


@app.post("/scan")
def scan(req: ScanRequest):
    try:
        provider = get_provider()
    except ValueError as e:
        raise HTTPException(status_code=503, detail=str(e))
    try:
        return run_scan(req, provider, get_store())
    except HTTPException:
        raise
    except FeedError as e:
        raise HTTPException(status_code=502, detail=f"Data feed unavailable for {e.symbol}: {e.reason}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Scan failed: {e}")


# --------------------------------------------------------------------------- #
# Market-wide screener.
# --------------------------------------------------------------------------- #

class MarketScanRequest(BaseModel):
    price_min: Optional[float] = Field(None, description="Min underlying STOCK price")
    price_max: Optional[float] = Field(None, description="Max underlying STOCK price")
    premium_min: Optional[float] = Field(None, description="Min premium PER SHARE")
    premium_max: Optional[float] = Field(None, description="Max premium PER SHARE")
    side: str = Field("both", description='"calls", "puts", or "both"')
    dte_from: Optional[int] = Field(None, description="Min days to expiration")
    dte_to: Optional[int] = Field(None, description="Max days to expiration")
    limit: int = Field(50, ge=1, le=200)


@app.post("/market/scan")
def market_scan(req: MarketScanRequest):
    try:
        provider = get_provider()
    except ValueError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return market.start_market_sweep(
        req.model_dump(), provider, get_store(),
        cboe_provider=get_cboe_provider(),
        realtime_provider=get_realtime_provider(),
    )


@app.get("/market/status")
def market_status():
    return market.market_status()


# --------------------------------------------------------------------------- #
# Misc.
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    is_tradier = settings.data_provider.lower() == "tradier"
    if is_tradier and settings.is_sandbox:
        realtime_note = "Tradier sandbox: data is ~15 min delayed (no Greeks)."
    elif is_tradier and settings.tradier_token:
        realtime_note = (
            "Tradier production: real-time quotes (free on an individual brokerage "
            "account; entity/professional accounts incur exchange fees)."
        )
    else:
        realtime_note = "Free CBOE/Yahoo: quotes are ~15 min delayed (no account needed)."
    return {
        "status": "ok",
        "data_provider": settings.data_provider,
        "tradier_token_configured": bool(settings.tradier_token),
        "tradier_env": settings.tradier_env,
        "realtime_note": realtime_note,
        "risk_free_rate": settings.risk_free_rate,
        "universe_size": market.universe_size(),
    }


@app.get("/key")
def key():
    return {"grade_key": grade_key(), "sub_score_labels": LABELS}


# Serve the frontend last so API routes take precedence.
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
else:  # pragma: no cover
    @app.get("/")
    def root():
        return JSONResponse({"detail": "frontend not found", "see": "/health"})
