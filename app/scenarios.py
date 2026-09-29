"""Offline long-option scenarios and reproducible reports using the pricing engine."""
from __future__ import annotations

import csv
import hashlib
import html
import io
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.engine.blackscholes import bs_price
from app.engine.scoring import score_contract
from app.models import Contract, OptionType

router = APIRouter()
Positive = Annotated[float, Field(gt=0, le=10_000_000)]
Nonnegative = Annotated[float, Field(ge=0, le=10_000_000)]


class ScenarioRequest(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")
    label: str = Field("Hypothetical contract", max_length=120)
    as_of: date = Field(default_factory=date.today)
    option_type: Literal["call", "put"] = "call"
    spot: Positive = 100
    strike: Positive = 100
    premium: Nonnegative = 3.1
    dte: int = Field(30, ge=0, le=3650)
    elapsed_days: float = Field(7, ge=0, le=3650)
    iv: float = Field(.25, ge=0, le=10)
    iv_change: float = Field(0, ge=-10, le=10)
    risk_free_rate: float = Field(.04, ge=-.2, le=.5)
    dividend_yield: float = Field(0, ge=0, le=.5)
    contracts: int = Field(1, ge=1, le=10000, strict=True)
    multiplier: int = Field(100, ge=1, le=10000, strict=True)
    round_trip_fee_per_contract: Nonnegative = 1.3
    move_min_pct: float = Field(-30, gt=-100, le=1000)
    move_max_pct: float = Field(30, gt=-100, le=1000)
    points: int = Field(61, ge=2, le=201, strict=True)
    bid: Nonnegative | None = None
    ask: Nonnegative | None = None
    open_interest: int = Field(0, ge=0, le=1_000_000_000)
    volume: int = Field(0, ge=0, le=1_000_000_000)
    historical_vol: float | None = Field(None, gt=0, le=10)
    iv_rank: float | None = Field(None, ge=0, le=100)

    @model_validator(mode="after")
    def consistent(self):
        if self.elapsed_days > self.dte:
            raise ValueError("elapsed_days cannot exceed days to expiry")
        if self.move_min_pct >= self.move_max_pct:
            raise ValueError("minimum spot move must be below maximum")
        if not 0 <= self.iv + self.iv_change <= 10:
            raise ValueError("scenario volatility must be between zero and ten")
        try:
            self.as_of + timedelta(days=self.dte)
        except OverflowError as exc:
            raise ValueError("expiry exceeds supported date range") from exc
        return self


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def simulate(req: ScenarioRequest) -> dict:
    option_type = OptionType(req.option_type)
    remaining = req.dte - req.elapsed_days
    sigma = req.iv + req.iv_change
    position_scale = req.multiplier * req.contracts
    fees = req.round_trip_fee_per_contract * req.contracts
    paid = req.premium * position_scale
    rows = []
    for index in range(req.points):
        move = req.move_min_pct + (req.move_max_pct - req.move_min_pct) * index / (req.points-1)
        spot = req.spot * (1+move/100)
        intrinsic = max(spot-req.strike, 0) if option_type == OptionType.CALL else max(req.strike-spot, 0)
        mark = max(0, bs_price(spot, req.strike, req.risk_free_rate, req.dividend_yield, sigma, remaining/365, option_type))
        rows.append({
            "spot_move_pct": move, "spot": spot, "remaining_days": remaining, "iv": sigma,
            "theoretical_per_share": mark, "expiry_intrinsic_per_share": intrinsic,
            "theoretical_pnl_usd": (mark-req.premium)*position_scale-fees,
            "expiry_pnl_usd": (intrinsic-req.premium)*position_scale-fees,
        })
    per_share_cost = req.premium + req.round_trip_fee_per_contract / req.multiplier
    break_even = req.strike+per_share_cost if option_type == OptionType.CALL else req.strike-per_share_cost
    contract = Contract(
        underlying=req.label, occ_symbol="HYPOTHETICAL", option_type=option_type, strike=req.strike,
        expiration=req.as_of+timedelta(days=req.dte), dte=req.dte,
        bid=req.bid or 0, ask=req.ask or 0, last=req.premium,
        open_interest=req.open_interest, volume=req.volume, provider_iv=req.iv or None,
    )
    grade = score_contract(contract,req.spot,req.risk_free_rate,req.dividend_yield,req.historical_vol,req.iv_rank).to_dict()
    # Scenario price is the explicit purchase price; grade uses the quoted mid.
    assumptions = [
        "Long single-leg position. Purchase premium is an input, not a promised executable quote.",
        "Black–Scholes–Merton European model with constant volatility/rates and continuous dividend yield; no early exercise, volatility smile, spread or slippage model.",
        "Theoretical P/L is a hypothetical mark before expiry; expiry P/L is intrinsic value less paid premium and the same round-trip fee assumption.",
        "Grade is a buyer-oriented heuristic. Missing inputs use disclosed fallback scores; it is not a prediction of profit.",
        "Scenario grading uses a quote midpoint when a valid bid/ask is supplied, otherwise the entered premium; the simulator always uses the entered purchase premium.",
        "A standard contract uses 100 shares; choose the actual multiplier for adjusted contracts. Rates and volatility are annual decimals; days are calendar days.",
        "The grader uses a half-day pricing floor for 0DTE. The scenario engine uses exact intrinsic payoff when remaining time is zero.",
    ]
    inputs = req.model_dump(mode="json")
    inputs_hash = hashlib.sha256(_canonical(inputs)).hexdigest()
    code = b"".join((Path(__file__).parent/"engine"/name).read_bytes() for name in ("blackscholes.py","scoring.py","grading.py"))
    return {
        "schema": "option-contract-grader.scenario", "version":1, "inputs":inputs,
        "provenance":{"inputs_sha256":inputs_hash,"engine_sha256":hashlib.sha256(code).hexdigest()},
        "assumptions":assumptions,"grade":grade,"rows":rows,
        "summary":{"premium_paid_usd":paid,"round_trip_fees_usd":fees,"max_loss_usd":paid+fees,
                   "expiry_break_even":break_even if break_even>=0 else None,
                   "break_even_reason":"Put cannot break even at a nonnegative spot under these costs." if break_even<0 else None,
                   "theoretical_best_on_grid_usd":max(r["theoretical_pnl_usd"] for r in rows),
                   "theoretical_worst_on_grid_usd":min(r["theoretical_pnl_usd"] for r in rows),
                   "remaining_days":remaining,"scenario_iv":sigma},
    }


def csv_report(doc):
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    columns = ["spot_move_pct","spot","remaining_days","iv","theoretical_per_share","expiry_intrinsic_per_share","theoretical_pnl_usd","expiry_pnl_usd","inputs_sha256","engine_sha256"]
    writer.writerow(columns)
    for row in doc["rows"]:
        writer.writerow([row.get(k,doc["provenance"].get(k,"")) for k in columns])
    return out.getvalue()


def html_report(doc):
    e = lambda value: html.escape(str(value))
    rows = "".join("<tr>"+"".join(f"<td>{e(row[k])}</td>" for k in ["spot","remaining_days","iv","theoretical_pnl_usd","expiry_pnl_usd"])+"</tr>" for row in doc["rows"])
    trace = doc["grade"]["score_trace"]
    contributions = "".join(f'<tr><td>{e(c["key"])}</td><td>{e(c["score"])}</td><td>{e(c["normalized_weight"])}</td><td>{e(c["contribution"])}</td><td>{e(c["basis"])}</td></tr>' for c in trace["components"])
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>Option scenario report</title><style>body{font:15px/1.6 system-ui;margin:3rem auto;max-width:72rem;padding:0 1rem;color:#17223a}table{border-collapse:collapse;width:100%;font-size:.85rem}td,th{border-bottom:1px solid #ddd;padding:.4rem;text-align:left}pre{white-space:pre-wrap;overflow-wrap:anywhere}h1{font-size:2.5rem}</style></head><body>'
        '<p>OPTION CONTRACT GRADER / SCENARIO REPORT</p>'
        f'<h1>{e(doc["inputs"]["label"])}</h1><p>Hypothetical {e(doc["inputs"]["option_type"])} · grade {e(doc["grade"]["overall_grade"])} · score {e(doc["grade"]["overall_score"])}</p>'
        f'<h2>Inputs and assumptions</h2><pre>{e(json.dumps(doc["inputs"],indent=2))}</pre><ul>'+ "".join(f"<li>{e(a)}</li>" for a in doc["assumptions"]) + '</ul>'
        f'<h2>Transparent grade</h2><p>Weighted score before liquidity cap: {e(trace["weighted_score"])}. Final score: {e(trace["final_score"])}. Liquidity gate: {e(trace["liquidity_gate"])}</p>'
        f'<table><tr><th>Dimension</th><th>Score</th><th>Weight</th><th>Contribution</th><th>Basis</th></tr>{contributions}</table>'
        f'<h2>Scenario P/L (USD)</h2><table><tr><th>Spot</th><th>Days remaining</th><th>IV</th><th>Theoretical P/L</th><th>Expiry P/L</th></tr>{rows}</table>'
        f'<h2>Provenance</h2><pre>{e(json.dumps(doc["provenance"],indent=2))}</pre></body></html>'
    )


@router.post("/scenario")
def scenario(req: ScenarioRequest):
    return simulate(req)


@router.post("/scenario/report")
def report(req: ScenarioRequest, format: Literal["json","csv","html"] = Query("json")):
    doc = simulate(req)
    body, media = (json.dumps(doc,indent=2,allow_nan=False)+"\n","application/json") if format=="json" else (csv_report(doc),"text/csv") if format=="csv" else (html_report(doc),"text/html")
    return Response(body,media_type=media,headers={
        "Content-Disposition":f'attachment; filename="option-scenario.{format}"',
        "X-Content-Type-Options":"nosniff",
        "Content-Security-Policy":"default-src 'none'; style-src 'unsafe-inline'; sandbox",
    })
