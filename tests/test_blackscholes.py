"""BSM engine validation against textbook values and internal consistency."""

import math

import pytest

from app.engine import blackscholes as bs
from app.models import OptionType


def test_call_put_textbook():
    # Hull, Options Futures and Other Derivatives: S=42,K=40,r=10%,sigma=20%,T=0.5
    S, K, r, q, sigma, T = 42, 40, 0.10, 0.0, 0.20, 0.5
    call = bs.bs_price(S, K, r, q, sigma, T, OptionType.CALL)
    put = bs.bs_price(S, K, r, q, sigma, T, OptionType.PUT)
    assert call == pytest.approx(4.76, abs=0.01)
    assert put == pytest.approx(0.81, abs=0.01)


def test_put_call_parity():
    S, K, r, q, sigma, T = 100, 105, 0.03, 0.01, 0.25, 0.75
    call = bs.bs_price(S, K, r, q, sigma, T, OptionType.CALL)
    put = bs.bs_price(S, K, r, q, sigma, T, OptionType.PUT)
    lhs = call - put
    rhs = S * math.exp(-q * T) - K * math.exp(-r * T)
    assert lhs == pytest.approx(rhs, abs=1e-9)


@pytest.mark.parametrize("sigma", [0.08, 0.15, 0.30, 0.65, 1.2])
@pytest.mark.parametrize("otype", [OptionType.CALL, OptionType.PUT])
def test_iv_roundtrip(sigma, otype):
    S, K, r, q, T = 100, 110, 0.04, 0.0, 0.4
    price = bs.bs_price(S, K, r, q, sigma, T, otype)
    iv = bs.implied_vol(price, S, K, r, q, T, otype)
    assert iv is not None
    assert iv == pytest.approx(sigma, abs=1e-4)


def test_iv_below_intrinsic_returns_none():
    # A call can't be worth less than its intrinsic value.
    S, K, r, q, T = 100, 80, 0.04, 0.0, 0.5
    assert bs.implied_vol(1.0, S, K, r, q, T, OptionType.CALL) is None


def test_greek_signs():
    S, K, r, q, sigma, T = 100, 100, 0.04, 0.0, 0.25, 0.5
    call = bs.greeks(S, K, r, q, sigma, T, OptionType.CALL)
    put = bs.greeks(S, K, r, q, sigma, T, OptionType.PUT)
    assert 0 < call.delta < 1
    assert -1 < put.delta < 0
    assert call.gamma > 0 and put.gamma > 0
    assert call.gamma == pytest.approx(put.gamma, abs=1e-12)  # gamma is side-agnostic
    assert call.vega > 0 and put.vega > 0
    assert call.theta < 0  # long ATM call bleeds time value
    assert call.rho > 0 and put.rho < 0


def test_prob_itm_monotonic_in_strike():
    # Higher strike -> lower probability a call finishes ITM.
    S, r, q, sigma, T = 100, 0.04, 0.0, 0.3, 0.5
    p_low = bs.prob_itm(S, 90, r, q, sigma, T, OptionType.CALL)
    p_high = bs.prob_itm(S, 120, r, q, sigma, T, OptionType.CALL)
    assert 0 <= p_high < p_low <= 1
