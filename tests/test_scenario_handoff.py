"""The graded-contract -> scenario-form handoff must produce a submittable form.

``frontend/app.js`` renders each graded contract with an "Explore scenarios"
button that hands the whole contract dict to ``frontend/scenario.js``'s
``open()``, which copies its numbers straight into the Scenario Simulator form.
``scenario.js``'s ``run()`` then starts with ``if (!form.reportValidity()) return;``.

So any value the page itself writes into a field has to satisfy that field's own
HTML constraints, or the browser silently refuses to submit the form the page
just filled in -- the user clicks "Simulate scenarios" and nothing happens.

It did happen. Three fields carried a `step` finer than the value the API emits:

- ``premium`` (``step=".01"``) received ``premium_per_share``, i.e.
  ``round(contract.mid, 4)``. ``mid`` is ``(bid + ask) / 2``, so an odd-cent
  quote pair yields a half-cent price. Measured against the live free CBOE
  delayed-quote feed for AAPL (3,508 contracts, 2,979 two-sided): bid and ask
  carry at most 2 decimals, and **1,366 of those 2,979 quotes (45.9%)** produce a
  mid off the 0.01 grid -- 0.015, 0.025, 0.035, ...
- ``iv`` (``step=".1"``) received ``round(contract.iv, 4) * 100`` -- an IV solved
  from the mid, so essentially never a multiple of 0.1 (e.g. 11.41).
- ``iv_rank`` (``step="1"``) received the *raw* rank from ``scenario_context``
  (e.g. 23.42258022260396); the API rounds it to 1 decimal for ``meta``, which is
  still off an integer grid.

Reproduced in a real browser: ``open()`` on a graded contract left
``form.reportValidity() === false`` with three invalid fields, and clicking
"Simulate scenarios" issued zero ``fetch`` calls and rendered nothing.

The fields that already receive API decimals -- ``spot``, ``strike``,
``historical_vol`` -- already used ``step="any"``. These tests pin that contract
for every value the handoff writes, so the fix cannot be undone field by field.
"""

from datetime import date, timedelta
import re
from pathlib import Path

import pytest

from app.engine.scoring import score_contract
from app.models import Contract, OptionType

INDEX_HTML = Path(__file__).resolve().parents[1] / "frontend" / "index.html"


# --------------------------------------------------------------------------- #
# The form's own constraints, read out of the page the browser is given.
# --------------------------------------------------------------------------- #

def _inputs() -> dict:
    """{name: {attr: value}} for every named <input> in frontend/index.html."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    out = {}
    for tag in re.findall(r"<input\b[^>]*>", html):
        name = re.search(r'name="([^"]+)"', tag)
        if not name:
            continue
        out[name.group(1)] = dict(re.findall(r'([\w-]+)="([^"]*)"', tag))
    return out


def _step_mismatch(attrs: dict, value) -> bool:
    """True when the browser would report a step mismatch for `value`.

    Mirrors the HTML rule: the step base is the field's ``min`` (default 0), and
    an empty or ``any`` step accepts every value.
    """
    step = (attrs.get("step") or "").strip()
    if step in ("", "any"):
        return False
    try:
        step_f = float(step)
        base = float(attrs.get("min") or 0.0)
    except ValueError:
        return False
    if step_f <= 0:
        return False
    n = (float(value) - base) / step_f
    return abs(n - round(n)) > 1e-9


def _out_of_range(attrs: dict, value) -> str:
    if attrs.get("min") not in (None, ""):
        if float(value) < float(attrs["min"]):
            return f"{value} < min {attrs['min']}"
    if attrs.get("max") not in (None, ""):
        if float(value) > float(attrs["max"]):
            return f"{value} > max {attrs['max']}"
    return ""


def test_the_constraint_checker_actually_rejects_an_off_grid_value():
    """Guard against a vacuous pass: the checker must catch the original bug.

    1.145 against the pre-fix ``step=".01"`` is exactly what the browser refused.
    """
    assert _step_mismatch({"step": ".01", "min": "0"}, 1.145) is True
    assert _step_mismatch({"step": ".01", "min": "0"}, 1.15) is False
    assert _step_mismatch({"step": "1", "min": "0", "max": "100"}, 23.42258022260396) is True
    assert _step_mismatch({"step": "any"}, 23.42258022260396) is False
    assert _out_of_range({"max": "100"}, 120.0) != ""


# --------------------------------------------------------------------------- #
# The handoff, mirrored from frontend/scenario.js open().
# --------------------------------------------------------------------------- #

def _handoff_values(graded: dict) -> dict:
    """The values ``open()`` writes into the form, for one graded contract."""
    ctx = graded.get("scenario_context") or {}
    values = {
        "spot": ctx.get("spot") or graded["strike"],
        "strike": graded["strike"],
        "premium": graded["premium_per_share"],
        "dte": graded["dte"],
        "elapsed_days": min(7, graded["dte"]),
        "iv": (graded["iv"] if graded["iv"] is not None else 0.25) * 100,
        "risk_free_rate": (ctx.get("risk_free_rate") if ctx.get("risk_free_rate") is not None else 0.04) * 100,
        "dividend_yield": (ctx.get("dividend_yield") if ctx.get("dividend_yield") is not None else 0.0) * 100,
        "bid": graded["bid"],
        "ask": graded["ask"],
        "volume": graded["volume"],
        "open_interest": graded["open_interest"],
    }
    # open() omits these two when the grader had nothing to record, so the field
    # keeps its HTML default (empty) rather than being filled with a value.
    if ctx.get("historical_vol") is not None:
        values["historical_vol"] = ctx["historical_vol"] * 100
    if ctx.get("iv_rank") is not None:
        values["iv_rank"] = ctx["iv_rank"]
    return values


def _graded(bid: float, ask: float, strike: float = 100.0, dte: int = 30,
            hv=None, iv_rank=None, option_type=OptionType.CALL) -> dict:
    contract = Contract(
        underlying="TEST", occ_symbol="TEST", option_type=option_type,
        strike=strike, expiration=date.today() + timedelta(days=dte), dte=dte,
        bid=bid, ask=ask, last=(bid + ask) / 2.0, volume=500, open_interest=1000,
    )
    return score_contract(contract, 100.0, 0.04, 0.0, hv, iv_rank).to_dict()


# The measured repro: a real offline /scan over an odd-cent put quote. Its top
# contract reported premium_per_share 1.145, iv 0.1141 and a raw rank of
# 23.42258022260396 -- the three values the browser rejected.
HALF_CENT = _graded(1.13, 1.16, hv=0.25, iv_rank=23.42258022260396,
                    option_type=OptionType.PUT)
# A cheap contract whose mid is still off the cent grid (0.645), the other
# half of the same live-feed measurement.
CHEAP_CALL = _graded(0.63, 0.66, hv=0.28, iv_rank=61.7)
# No realized vol and no rank: the two optional fields stay empty.
NO_CONTEXT = _graded(3.0, 3.2)
# A 0DTE contract: elapsed_days collapses to 0.
ZERO_DTE = _graded(1.13, 1.16, dte=0, hv=0.25, iv_rank=0.0)


@pytest.mark.parametrize("graded", [HALF_CENT, CHEAP_CALL, NO_CONTEXT, ZERO_DTE],
                         ids=["half_cent", "cheap_call", "no_context", "zero_dte"])
def test_every_value_the_handoff_writes_satisfies_the_forms_own_constraints(graded):
    """The page must not fill the form with values its own validity check rejects."""
    inputs = _inputs()
    values = _handoff_values(graded)
    assert values, "the handoff wrote nothing; the mirror is out of date"
    for name, value in values.items():
        assert name in inputs, f"scenario.js writes {name!r}, which the form does not have"
        attrs = inputs[name]
        assert not _step_mismatch(attrs, value), (
            f"{name}={value!r} is off the step grid of "
            f"step={attrs.get('step')!r} min={attrs.get('min')!r}; the browser "
            f"refuses to submit the form (reportValidity() -> false)"
        )
        assert not _out_of_range(attrs, value), f"{name}={value!r} is out of range"


def test_the_measured_values_from_the_reported_bug_are_accepted():
    """The exact numbers a real scan produced, pinned as accepted values.

    ``premium_per_share`` 1.145, ``iv`` 11.41 and a raw ``iv_rank`` of
    23.42258022260396 all failed the form's constraints before this fix.
    """
    values = _handoff_values(HALF_CENT)
    assert values["premium"] == 1.145
    assert values["iv"] == pytest.approx(11.41)
    assert values["iv_rank"] == pytest.approx(23.42258022260396)

    inputs = _inputs()
    for name in ("premium", "iv", "iv_rank"):
        assert not _step_mismatch(inputs[name], values[name]), name
        assert inputs[name]["step"] == "any", name


def test_the_fields_holding_whole_counts_keep_an_integer_step():
    """The fix is scoped: counts and integer fields still use a real step."""
    inputs = _inputs()
    for name, step in (("contracts", "1"), ("multiplier", "1"), ("dte", "1"),
                       ("open_interest", "1"), ("volume", "1")):
        assert inputs[name]["step"] == step, name
    # bid/ask are copied verbatim from the feed; measured at most 2 decimals
    # across 3,508 live CBOE contracts, so the 0.01 grid is still honest there.
    for name in ("bid", "ask"):
        assert inputs[name]["step"] == ".01", name
