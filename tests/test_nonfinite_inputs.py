"""A non-finite number is not an observation.

Every guard in this engine is a comparison, and every comparison against ``NaN``
is False: ``NaN <= 0``, ``NaN > 0``, ``NaN >= NaN``, ``min(..., NaN) <= 0`` and
``NaN > MAX_SPREAD_PCT`` all read False. So a ``NaN`` (or an ``inf``) is not
merely tolerated by those guards, it walks through them as if it were data --
and then ``_clamp`` hands it back the TOP of the scale, because ``min(100.0,
nan)`` returns ``100.0`` for the same reason. Garbage in, perfect grade out.

That combination was reachable end to end. Python's ``json.loads`` accepts the
non-standard ``NaN`` / ``Infinity`` / ``-Infinity`` tokens and every provider
parser calls ``float()`` on the raw value, so one such number in a chart bar
made ``hv_from_bars`` return ``NaN`` -- and ``/scan`` then answered HTTP 500
(Starlette encodes JSON with ``allow_nan=False``) instead of grading a board. On
the paths that did serialize, the grade was *inflated*: a ``NaN`` realized vol
scored value 100/A and volatility 100/A and lifted the overall grade from
C (62.9) to B (77.4).

These tests are written against the public surface (models, engine, store, the
HTTP app) so they can be run unmodified against the pre-fix source to prove they
fail for the right reason.
"""

from datetime import date, timedelta
import math

import pytest
from fastapi.testclient import TestClient

import app.api as api
from app.api import app
from app.engine.scoring import score_contract, _clamp
from app.engine.volatility import hv_from_bars
from app.models import Contract, OHLC, OptionType, Quote
from app.providers.base import OptionsDataProvider
from app.store import Store

NAN = float("nan")
INF = float("inf")


def make_contract(strike=100.0, bid=3.0, ask=3.2, last=3.1, oi=1000, vol=500, dte=30,
                  otype=OptionType.CALL):
    return Contract(
        underlying="TEST", occ_symbol="TEST", option_type=otype, strike=strike,
        expiration=date.today() + timedelta(days=dte), dte=dte,
        bid=bid, ask=ask, last=last, volume=vol, open_interest=oi,
    )


def sub(scored, key):
    return next(s for s in scored.sub_scores if s.key == key)


def walk_floats(node, path=""):
    """Yield (path, value) for every float anywhere in a JSON-ish document."""
    if isinstance(node, float):
        yield path, node
    elif isinstance(node, dict):
        for k, v in node.items():
            yield from walk_floats(v, f"{path}.{k}")
    elif isinstance(node, (list, tuple)):
        for i, v in enumerate(node):
            yield from walk_floats(v, f"{path}[{i}]")


# --- the quote ------------------------------------------------------------- #

@pytest.mark.parametrize("bad", [NAN, INF, -INF])
def test_a_nonfinite_quote_is_not_a_two_sided_market(bad):
    """``inf``/``inf`` used to read as a quotable market with a mid of ``inf``.

    ``ask >= bid`` is True for the infinities, so the crossed-book guard waved
    them through: the contract reported a two-sided market, a mid of ``inf``, a
    spread of ``NaN`` (``inf - inf``), and a cost per contract of ``inf``.
    """
    c = make_contract(bid=bad, ask=bad, last=3.0)
    assert c.has_two_sided_market is False
    assert math.isfinite(c.mid)
    assert c.spread_pct is None          # no measurable spread -> the gate applies


def test_a_nonfinite_quote_cannot_escape_the_hard_liquidity_gate():
    """The gate is a cap; it must fire on a spread it cannot measure.

    With a ``NaN`` spread, ``spread_pct > MAX_SPREAD_PCT`` is False, so the gate
    stayed open and the spread dimension -- 60% of the liquidity score -- scored
    a perfect 100 via ``_clamp(NaN)``. The contract graded D 43.2 instead of the
    documented F, and its cost per contract was ``inf``.
    """
    sc = score_contract(make_contract(bid=INF, ask=INF, last=3.0), 100.0, 0.04, 0.0, 0.30, None)
    assert sc.overall_score <= 39.0
    assert sc.overall_grade == "F"
    assert any("illiquid" in f.lower() for f in sc.flags)
    assert sub(sc, "liquidity").score < 60.0
    assert math.isfinite(sc.cost_per_contract)


def test_a_nonfinite_last_trade_is_not_a_price():
    c = make_contract(bid=0.0, ask=0.0, last=INF)
    assert c.mid == 0.0
    sc = score_contract(c, 100.0, 0.04, 0.0, 0.30, None)
    assert sc.overall_score == 0.0
    assert sc.overall_grade == "F"
    assert any("No price" in f for f in sc.flags)


# --- the realized-vol estimate --------------------------------------------- #

def _bars(n=40, bad_index=None):
    out, c = [], 100.0
    for i in range(n):
        o = c
        c2 = o * (1.0 + 0.012 * (((i % 5) - 2) / 2.0))
        out.append(OHLC(day=date.today() - timedelta(days=n - i), open=o,
                        high=max(o, c2) * 1.01, low=min(o, c2) * 0.99,
                        close=c2, volume=1e6))
        c = c2
    if bad_index is not None:
        out[bad_index] = OHLC(day=out[bad_index].day, open=NAN, high=NAN, low=NAN, close=NAN)
    return out


def _zeroed(n=40, bad_index=39):
    """The same series with the bad bar's prices set to zero, not NaN."""
    out = _bars(n)
    out[bad_index] = OHLC(day=out[bad_index].day, open=0.0, high=0.0, low=0.0, close=0.0)
    return out


def test_a_nonfinite_bar_is_skipped_exactly_like_a_nonpositive_one():
    """One non-finite bar used to poison the whole annualized estimate.

    The Yang-Zhang guard skipped non-positive bars (``min(...) <= 0``) but not
    non-finite ones, because that comparison is False for ``NaN`` too. The
    variance went ``NaN``, the ``yz_var <= 0`` fallback check was False for
    ``NaN``, and the estimator returned ``NaN`` as an annualized realized
    volatility -- which then reached the response body and the grader.

    The exact invariant: a non-finite bar contributes nothing, which is what a
    non-positive bar already did. So the estimate over a series with one NaN bar
    must equal the estimate over the same series with that bar zeroed out.
    """
    holed = hv_from_bars(_bars(bad_index=39), 30)
    zeroed = hv_from_bars(_zeroed(bad_index=39), 30)
    assert holed is not None and math.isfinite(holed)
    assert holed == pytest.approx(zeroed, rel=1e-12)
    # And the usable bars still produce an estimate, rather than nothing.
    assert holed == pytest.approx(hv_from_bars(_bars(), 30), rel=0.02)


@pytest.mark.parametrize("bad", [NAN, INF, -INF])
def test_an_unusable_series_has_no_estimate_at_all(bad):
    """``hv_from_bars`` never hands a non-finite value to its callers."""
    bar = OHLC(day=date.today(), open=bad, high=bad, low=bad, close=bad)
    assert hv_from_bars([bar] * 5, 30) is None


# --- the grader ------------------------------------------------------------ #

@pytest.mark.parametrize("bad", [NAN, INF, -INF])
def test_a_nonfinite_realized_vol_scores_as_missing_data(bad):
    """It used to score as PERFECT data: value 100/A and volatility 100/A.

    ``if hv and iv`` reads a ``NaN`` as truthy, ``bs_price`` returns ``NaN``,
    and ``_clamp(NaN)`` returns 100.0 -- so the two volatility-driven dimensions
    (30 and 12 of the 100 weight) were handed the top of the scale on an input
    the engine cannot use at all.
    """
    c = make_contract()
    broken = score_contract(c, 100.0, 0.04, 0.0, bad, None)
    missing = score_contract(c, 100.0, 0.04, 0.0, None, None)
    assert sub(broken, "value").score == sub(missing, "value").score == 50.0
    assert sub(broken, "volatility").score == sub(missing, "volatility").score == 50.0
    assert sub(broken, "value").metric == "n/a"
    assert broken.overall_score == pytest.approx(missing.overall_score)
    assert broken.scenario_context["historical_vol"] is None


def test_a_nonfinite_realized_vol_does_not_inflate_the_grade():
    # The honest comparison: a NaN HV must not out-grade a real, calm one.
    c = make_contract()
    real = score_contract(c, 100.0, 0.04, 0.0, 0.30, None)
    broken = score_contract(c, 100.0, 0.04, 0.0, NAN, None)
    assert broken.overall_score <= real.overall_score


def test_a_nonfinite_iv_rank_is_not_a_rank():
    c = make_contract()
    broken = score_contract(c, 100.0, 0.04, 0.0, 0.30, NAN)
    missing = score_contract(c, 100.0, 0.04, 0.0, 0.30, None)
    assert sub(broken, "volatility").score == sub(missing, "volatility").score


def test_clamp_never_launders_a_nonfinite_value_into_the_top_of_the_scale():
    # min(100.0, nan) is 100.0, because nan < 100.0 is False.
    assert _clamp(NAN) == 0.0
    assert _clamp(INF) == 0.0
    assert _clamp(-INF) == 0.0
    assert _clamp(NAN) != 100.0
    assert _clamp(100.0) == 100.0          # the finite path is untouched
    assert _clamp(-5.0) == 0.0


# --- the store ------------------------------------------------------------- #

def test_a_nonfinite_hv_is_not_cached_as_a_realized_vol():
    """SQLite turns ``nan`` into NULL, but ``inf`` round-trips as ``inf``.

    So an infinite realized vol survived the cache and came back out of it in
    the sweep's Stage 1, where no comparison-based guard downstream could reject
    it. The price is still worth caching; the unusable HV is not.
    """
    s = Store(":memory:")
    s.save_underlying("AAA", 50.0, INF)
    assert s.get_underlying("AAA") == (50.0, None)
    assert s.get_underlying_fresh("AAA", 12.0) == (50.0, None)

    s.save_underlying("BBB", 50.0, NAN)
    assert s.get_underlying("BBB") == (50.0, None)


def test_a_nonfinite_price_is_still_refused_by_the_store():
    s = Store(":memory:")
    for bad in (NAN, INF, -INF):
        s.save_underlying("AAA", bad, 0.30)
        assert s.get_underlying("AAA") is None


# --- the endpoint ---------------------------------------------------------- #

class NonFiniteHistoryProvider(OptionsDataProvider):
    """A feed whose chart payload carried one non-finite number.

    Python's ``json.loads`` accepts the non-standard ``NaN`` / ``Infinity`` /
    ``-Infinity`` tokens, and every provider parser calls ``float()`` on the raw
    value, so one such number in a bar is what reaches the estimators.
    """

    def __init__(self, price=100.0, dividend_yield=0.0, history="clean"):
        self.price = price
        self.dividend_yield = dividend_yield
        self.history = history

    def get_quote(self, symbol):
        return Quote(symbol=symbol.upper(), last=self.price,
                     dividend_yield=self.dividend_yield)

    def get_history(self, symbol, days=60):
        if self.history == "all_bad_bars":
            bar = OHLC(day=date.today(), open=NAN, high=NAN, low=NAN, close=NAN)
            return [bar] * 5
        return _bars(40, bad_index=39 if self.history == "one_bad_bar" else None)

    def get_expirations(self, symbol):
        return [date.today() + timedelta(days=30)]

    def get_chain(self, symbol, expiration, side="both"):
        mid = 3.0
        out = []
        for k in (95.0, 100.0, 105.0):
            for ot in (OptionType.CALL, OptionType.PUT):
                out.append(Contract(
                    underlying=symbol.upper(), occ_symbol=f"{symbol}{k}{ot.value}",
                    option_type=ot, strike=k, expiration=expiration, dte=30,
                    bid=mid - 0.05, ask=mid + 0.05, last=mid, volume=500,
                    open_interest=1000))
        return out


@pytest.fixture
def client(monkeypatch):
    def install(provider):
        monkeypatch.setattr(api, "_provider", provider)
        monkeypatch.setattr(api, "_store", Store(":memory:"))
        return TestClient(app, raise_server_exceptions=False)
    return install


def test_a_nonfinite_history_grades_a_board_instead_of_a_500(client):
    """The headline failure: one non-finite bar took the whole endpoint down.

    ``meta.historical_vol`` carried the ``NaN`` into the response body, and
    Starlette encodes JSON with ``allow_nan=False``, so the request raised
    ``ValueError: Out of range float values are not JSON compliant`` inside the
    response renderer -- after the endpoint had already returned -- and the
    client got a bare HTTP 500 with no explanation and no board.
    """
    c = client(NonFiniteHistoryProvider(history="one_bad_bar"))
    r = c.post("/scan", json={"ticker": "AAPL", "side": "calls", "limit": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["results"]                              # a real board, not an error
    assert body["meta"]["historical_vol"] is not None   # estimated from the usable bars
    assert all(math.isfinite(v) for _, v in walk_floats(body))


def test_an_unusable_bar_is_dropped_not_amplified(client):
    """The bad bar must not make the board look better than the clean board.

    Dropping one bar from a 40-bar series moves the estimate by a hair; grading
    the ``NaN`` as data used to move it by a grade, because the two
    volatility-driven sub-scores were handed the top of the scale.
    """
    holed = client(NonFiniteHistoryProvider(history="one_bad_bar")).post(
        "/scan", json={"ticker": "AAPL", "side": "calls", "limit": 5}).json()
    clean = client(NonFiniteHistoryProvider(history="clean")).post(
        "/scan", json={"ticker": "AAPL", "side": "calls", "limit": 5}).json()
    assert holed["meta"]["historical_vol"] == pytest.approx(
        clean["meta"]["historical_vol"], rel=0.02)
    for a, b in zip(holed["results"], clean["results"]):
        assert a["overall_grade"] == b["overall_grade"]
        assert a["overall_score"] <= b["overall_score"] + 0.5


def test_a_wholly_unusable_history_still_grades(client):
    """No usable bars is a missing HV -- the documented fallback, not an error."""
    c = client(NonFiniteHistoryProvider(history="all_bad_bars"))
    r = c.post("/scan", json={"ticker": "AAPL", "side": "calls", "limit": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["meta"]["historical_vol"] is None
    assert body["results"]
    assert all(math.isfinite(v) for _, v in walk_floats(body))
    assert all(s["score"] == 50.0 for c_ in body["results"]
               for s in c_["sub_scores"] if s["key"] == "value")


def test_a_nonfinite_underlying_price_is_answered_not_crashed(client):
    """``inf <= 0`` is False, so the price guard waved an infinite price through."""
    c = client(NonFiniteHistoryProvider(price=INF))
    r = c.post("/scan", json={"ticker": "AAPL"})
    assert r.status_code == 502
    assert "No valid price" in r.json()["detail"]


def test_a_nonfinite_dividend_yield_falls_back_to_the_configured_default(client):
    from app.config import settings

    c = client(NonFiniteHistoryProvider(dividend_yield=INF))
    r = c.post("/scan", json={"ticker": "AAPL", "side": "calls", "limit": 2})
    assert r.status_code == 200, r.text
    assert r.json()["meta"]["dividend_yield"] == settings.default_dividend_yield


# --- the market sweep ------------------------------------------------------ #

class SweepProvider(OptionsDataProvider):
    """Prices every name with one chart call, one of which carries a bad bar."""

    def __init__(self, bad):
        self.bad = bad

    def get_quote(self, symbol):
        return Quote(symbol=symbol.upper(), last=50.0)

    def get_price_and_history(self, symbol, days=60):
        bars = _bars(40, bad_index=39 if self.bad else None)
        return 50.0, bars

    def get_history(self, symbol, days=60):
        return self.get_price_and_history(symbol, days)[1]

    def get_expirations(self, symbol):
        return [date.today() + timedelta(days=30)]

    def get_chain(self, symbol, expiration, side="both"):
        mid = 1.5
        out = []
        for k in (47.5, 50.0, 52.5):
            for ot in (OptionType.CALL, OptionType.PUT):
                out.append(Contract(
                    underlying=symbol.upper(), occ_symbol=f"{symbol}{k}{ot.value}",
                    option_type=ot, strike=k, expiration=expiration, dte=30,
                    bid=mid - 0.05, ask=mid + 0.05, last=mid, volume=500,
                    open_interest=1000))
        return out


def test_a_nonfinite_bar_cannot_inflate_a_sweep_board(monkeypatch):
    """The board-level version of the same bug, measured.

    The sweep prices each name with one chart call and caches the realized vol
    it derives from it, so a single non-finite bar reached the grader through
    the same route. Measured on the pre-fix source: every contract on the board
    was given a value sub-score of exactly ``100.0`` -- the top of the scale --
    and the board moved from 60.9/57.2 to 78.1/77.3, a grade-crossing +17
    points on an input the engine cannot use at all.
    """
    from app import market

    def board(bad):
        monkeypatch.setattr(market, "_universe_cache", ["AAA", "BBB", "CCC"])
        top, _ = market.run_market_sweep(
            {"side": "both", "limit": 10}, SweepProvider(bad), Store(":memory:"),
            lambda *a: None,
        )
        return top

    holed = board(True)
    clean = board(False)

    assert len(holed) == len(clean)
    assert all(math.isfinite(v) for r in holed for _, v in walk_floats(r))
    for a, b in zip(holed, clean):
        assert a["overall_grade"] == b["overall_grade"]
        assert a["overall_score"] == pytest.approx(b["overall_score"], abs=1.0)
        value = next(s for s in a["sub_scores"] if s["key"] == "value")
        assert value["score"] < 90.0            # not the top of the scale on garbage
