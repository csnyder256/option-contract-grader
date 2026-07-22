"""Deterministic scoring: seven buyer-tuned sub-scores -> one composite grade.

Designed for LONG single-leg contracts (buying calls/puts). Every sub-score is a
pure function of the contract + a handful of context inputs (underlying price,
risk-free rate, dividend yield, realized vol, optional IV rank), so the same
chain always produces the same grades.

Sub-scores (each 0-100):
  value       - price edge vs. fair value computed at realized volatility
  odds        - probability of profit at expiration (N(d2) at break-even)
  move        - required break-even move vs. the typical (1-SD) move
  liquidity   - spread / open interest / volume  (also a hard gate)
  volatility  - IV vs HV (cheap/expensive) + IV rank when available
  decay       - time-decay risk for a buyer
  leverage    - bang-for-buck risk balance (moneyness)
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional

from app.config import Settings, settings as default_settings
from app.engine import blackscholes as bs
from app.engine.grading import grade_for, letter_only
from app.engine.volatility import iv_vs_hv_ratio
from app.models import Contract, Greeks, OptionType, ScoredContract, SubScore

# Display labels for each sub-score (jargon stays out of the user's face).
LABELS: Dict[str, str] = {
    "value": "Value (Price Edge)",
    "odds": "Odds of Profit",
    "move": "Move Needed",
    "liquidity": "Liquidity",
    "volatility": "Volatility Value",
    "decay": "Time-Decay Risk",
    "leverage": "Leverage & Risk",
}


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _sub(key: str, score: float, explanation: str, metric: Optional[str] = None) -> SubScore:
    score = _clamp(score)
    return SubScore(
        key=key,
        label=LABELS[key],
        score=score,
        grade=letter_only(score),
        explanation=explanation,
        metric=metric,
    )


def score_contract(
    contract: Contract,
    underlying: float,
    risk_free_rate: float,
    dividend_yield: float,
    hv: Optional[float],
    iv_rank: Optional[float] = None,
    config: Optional[Settings] = None,
) -> ScoredContract:
    cfg = config or default_settings
    weights = cfg.weights
    S = underlying
    K = contract.strike
    r = risk_free_rate
    q = dividend_yield
    otype = contract.option_type
    mid = contract.mid
    flags: List[str] = []

    # Effective time to expiry in years (floor avoids div-by-zero on 0DTE).
    dte = max(contract.dte, 0)
    T = dte / 365.0
    if T <= 0:
        T = 0.5 / 365.0
        flags.append("Expires today (0DTE) - extreme gamma risk")

    cost_per_contract = mid * 100.0
    break_even = (K + mid) if otype == OptionType.CALL else (K - mid)

    # No tradeable price -> bottom grade, nothing else is meaningful.
    if mid <= 0:
        flags.append("No price (no bid/ask/last)")
        return ScoredContract(
            contract=contract, iv=None, greeks=None,
            overall_score=0.0, overall_grade="F",
            overall_meaning=grade_for(0.0)[1],
            sub_scores=[], break_even=break_even,
            cost_per_contract=cost_per_contract, flags=flags,
        )

    # --- Implied volatility (compute our own; fall back to vendor) ----------
    iv = bs.implied_vol(mid, S, K, r, q, T, otype)
    if iv is None:
        iv = contract.provider_iv
        if iv:
            flags.append("Using vendor IV (couldn't solve from price)")
    greeks_obj: Optional[Greeks] = None
    if iv and iv > 0:
        try:
            greeks_obj = bs.greeks(S, K, r, q, iv, T, otype)
        except ValueError:
            greeks_obj = None
    if iv is None:
        flags.append("No clean IV (illiquid / one-sided market)")

    sub_scores: List[SubScore] = []

    # --- 1. Value / price edge --------------------------------------------
    if hv and iv:
        fair = bs.bs_price(S, K, r, q, hv, T, otype)
        edge = (fair - mid) / mid if mid > 0 else 0.0
        value_score = 50.0 + edge * 100.0
        if value_score >= 60:
            expl = "Looks cheap vs. how much the stock has actually been moving."
        elif value_score <= 40:
            expl = "Looks expensive vs. how much the stock has actually been moving."
        else:
            expl = "Fairly priced vs. the stock's recent real movement."
        sub_scores.append(_sub("value", value_score, expl,
                               f"fair ${fair:.2f} vs ${mid:.2f} ({edge:+.0%})"))
    else:
        sub_scores.append(_sub("value", 50.0,
                               "Not enough data to judge price fairness.", "n/a"))

    # --- 2. Odds of profit (POP at break-even) -----------------------------
    if iv:
        be = break_even
        if otype == OptionType.PUT and be <= 0:
            pop = 0.0
        else:
            pop = bs.prob_itm(S, max(be, 1e-6), r, q, iv, T, otype)
        odds_score = pop * 100.0
        sub_scores.append(_sub("odds", odds_score,
                               f"About a {pop:.0%} chance of being profitable by expiration.",
                               f"break-even ${be:.2f}"))
    else:
        pop = None
        sub_scores.append(_sub("odds", 0.0,
                               "Can't estimate the odds (no implied volatility).", "n/a"))

    # --- 3. Move needed vs. typical move -----------------------------------
    if iv:
        if otype == OptionType.CALL:
            required_frac = (break_even - S) / S
        else:
            required_frac = (S - break_even) / S
        expected_frac = iv * math.sqrt(T)  # 1-SD move as a fraction of spot
        ratio = required_frac / expected_frac if expected_frac > 0 else 99.0
        move_score = _clamp(100.0 * (1.0 - ratio / 2.0))
        if required_frac <= 0:
            expl = "Already past break-even territory - little to no move needed."
        elif ratio <= 1.0:
            expl = "Needs less than its typical move to pay off - reasonable ask."
        else:
            expl = "Needs more than its typical move to pay off - a stretch."
        sub_scores.append(_sub("move", move_score, expl,
                               f"needs {required_frac:+.1%}; typical ~{expected_frac:.1%}"))
    else:
        sub_scores.append(_sub("move", 0.0,
                               "Can't estimate the required move.", "n/a"))

    # --- 4. Liquidity (and the hard gate) ----------------------------------
    spread_pct = contract.spread_pct
    oi = contract.open_interest
    vol = contract.volume
    log5k = math.log10(5000.0)
    oi_score = _clamp(100.0 * math.log10(oi + 1) / log5k)
    vol_score = _clamp(100.0 * math.log10(vol + 1) / log5k)
    if spread_pct is None:
        spread_score = 0.0
    else:
        spread_score = _clamp(100.0 * (1.0 - spread_pct / 0.20))
    liquidity_score = 0.6 * spread_score + 0.25 * oi_score + 0.15 * vol_score

    gated = (
        spread_pct is None
        or spread_pct > cfg.max_spread_pct
        or oi < cfg.min_open_interest
    )
    if liquidity_score >= 60 and not gated:
        liq_expl = "Tight spread and healthy interest - easy to get in and out."
    else:
        liq_expl = "Wide spread or thin interest - hard to trade without giving up edge."
    spread_disp = f"{spread_pct:.0%}" if spread_pct is not None else "n/a"
    sub_scores.append(_sub("liquidity", liquidity_score, liq_expl,
                           f"spread {spread_disp}, OI {oi}, vol {vol}"))

    # --- 5. Volatility value (IV vs HV + IV rank) --------------------------
    if iv and hv:
        ratio = iv_vs_hv_ratio(iv, hv) or 1.0
        ivhv_score = _clamp(100.0 * (1.5 - ratio))  # 0.5x ->100, 1x ->50, 1.5x ->0
        if iv_rank is not None:
            rank_component = 100.0 - iv_rank  # low rank = cheap = good for a buyer
            vol_value = 0.6 * ivhv_score + 0.4 * rank_component
            metric = f"IV {iv:.0%} vs HV {hv:.0%}; IV rank {iv_rank:.0f}"
        else:
            vol_value = ivhv_score
            metric = f"IV {iv:.0%} vs HV {hv:.0%}; IV rank warming up"
        if vol_value >= 60:
            v_expl = "Options are cheap relative to the stock's real volatility - buyer's friend."
        elif vol_value <= 40:
            v_expl = "Options are pricey relative to the stock's real volatility."
        else:
            v_expl = "Implied volatility is roughly in line with realized."
        sub_scores.append(_sub("volatility", vol_value, v_expl, metric))
    else:
        sub_scores.append(_sub("volatility", 50.0,
                               "Not enough data to judge volatility pricing.", "n/a"))

    # --- 6. Time-decay risk (buyer) ----------------------------------------
    if greeks_obj:
        theta_yield = abs(greeks_obj.theta) / mid if mid > 0 else 1.0
        decay_score = _clamp(100.0 * (1.0 - theta_yield / 0.05))  # 5%/day -> 0
        if dte <= 7:
            decay_score *= 0.5
            flags.append("Under 7 DTE - decay & gamma risk are high")
        if decay_score >= 60:
            d_expl = "Time decay is mild relative to the premium."
        else:
            d_expl = "Time is eating this premium quickly - it needs to move soon."
        sub_scores.append(_sub("decay", decay_score, d_expl,
                               f"loses ~{theta_yield:.1%}/day to time"))
    else:
        sub_scores.append(_sub("decay", 50.0,
                               "Can't estimate time decay.", "n/a"))

    # --- 7. Leverage & risk balance (moneyness) ----------------------------
    if greeks_obj:
        ad = abs(greeks_obj.delta)
        lev_score = 100.0 * math.exp(-(((ad - 0.45) / 0.30) ** 2))
        if ad < 0.20:
            l_expl = "Long-shot lottery ticket - cheap, but low odds."
        elif ad > 0.75:
            l_expl = "Deep in the money - capital-heavy with little leverage."
        else:
            l_expl = "Balanced exposure - real leverage without being a pure gamble."
        sub_scores.append(_sub("leverage", lev_score, l_expl,
                               f"delta {greeks_obj.delta:+.2f}"))
    else:
        sub_scores.append(_sub("leverage", 50.0,
                               "Can't estimate leverage balance.", "n/a"))

    # --- Composite ---------------------------------------------------------
    by_key = {s.key: s.score for s in sub_scores}
    total_w = sum(weights.values()) or 1.0
    composite = sum(weights.get(k, 0.0) * by_key.get(k, 0.0) for k in weights) / total_w

    if gated:
        composite = min(composite, 39.0)
        flags.append("Illiquid - overall capped; other scores unreliable")

    grade, meaning = grade_for(composite)
    return ScoredContract(
        contract=contract,
        iv=iv,
        greeks=greeks_obj,
        overall_score=composite,
        overall_grade=grade,
        overall_meaning=meaning,
        sub_scores=sub_scores,
        break_even=break_even,
        cost_per_contract=cost_per_contract,
        flags=flags,
    )
