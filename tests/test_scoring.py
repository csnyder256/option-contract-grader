"""Scoring behavior: monotonicity, the liquidity gate, and score bounds."""

from datetime import date, timedelta

from app.config import settings
from app.engine.scoring import score_contract
from app.models import Contract, OptionType


def make_contract(strike=100.0, bid=3.0, ask=3.2, oi=1000, vol=500, dte=30,
                  otype=OptionType.CALL):
    exp = date.today() + timedelta(days=dte)
    return Contract(
        underlying="TEST",
        occ_symbol="TEST",
        option_type=otype,
        strike=strike,
        expiration=exp,
        dte=dte,
        bid=bid,
        ask=ask,
        last=(bid + ask) / 2,
        volume=vol,
        open_interest=oi,
    )


def sub(scored, key):
    return next(s for s in scored.sub_scores if s.key == key)


def test_scores_are_bounded():
    c = make_contract()
    sc = score_contract(c, underlying=100.0, risk_free_rate=0.04,
                        dividend_yield=0.0, hv=0.30)
    assert 0.0 <= sc.overall_score <= 100.0
    for s in sc.sub_scores:
        assert 0.0 <= s.score <= 100.0
        assert s.grade in {"A", "B", "C", "D", "F"}


def test_value_increases_with_realized_vol():
    # Same contract priced cheaper relative to a higher realized vol -> better value.
    c = make_contract()
    low = score_contract(c, 100.0, 0.04, 0.0, hv=0.10)
    high = score_contract(c, 100.0, 0.04, 0.0, hv=0.60)
    assert sub(high, "value").score > sub(low, "value").score


def test_liquidity_gate_caps_overall():
    # Wide spread + tiny open interest should trip the gate (overall <= 39 -> F).
    illiquid = make_contract(bid=0.10, ask=1.20, oi=5, vol=1)
    sc = score_contract(illiquid, 100.0, 0.04, 0.0, hv=0.30)
    assert sc.overall_score <= 39.0
    assert sc.overall_grade == "F"
    assert any("illiquid" in f.lower() for f in sc.flags)


def test_tighter_spread_scores_more_liquid():
    tight = make_contract(bid=3.05, ask=3.15, oi=2000, vol=1000)
    wide = make_contract(bid=2.50, ask=3.70, oi=2000, vol=1000)
    s_tight = score_contract(tight, 100.0, 0.04, 0.0, hv=0.30)
    s_wide = score_contract(wide, 100.0, 0.04, 0.0, hv=0.30)
    assert sub(s_tight, "liquidity").score > sub(s_wide, "liquidity").score


def test_no_price_is_bottom_grade():
    dead = make_contract(bid=0.0, ask=0.0)
    dead.last = 0.0
    sc = score_contract(dead, 100.0, 0.04, 0.0, hv=0.30)
    assert sc.overall_grade == "F"
    assert sc.overall_score == 0.0


def test_iv_rank_low_helps_buyer_volatility_score():
    c = make_contract()
    cheap_rank = score_contract(c, 100.0, 0.04, 0.0, hv=0.30, iv_rank=5.0)
    rich_rank = score_contract(c, 100.0, 0.04, 0.0, hv=0.30, iv_rank=95.0)
    assert sub(cheap_rank, "volatility").score > sub(rich_rank, "volatility").score
