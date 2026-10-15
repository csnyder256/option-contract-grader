"""The grade key must describe the bands the grader actually implements.

`grade_for` returns a letter for every score from a band's `min` up to (but not
including) the next band's `min`, and the UI shows whole numbers. So the legend
has to report each band's integer extent, and the whole number printed beside a
letter has to lie inside that letter's band -- otherwise a card reads "F 40",
which its own legend cannot explain.

Both were wrong on `main`:

- `grade_key()` reported `next_threshold - 0.1`, so the legend printed
  ``0-39.9`` for F while `grade_for(39.95)` still returned F, and it disagreed
  with the project's own documented bands (README and the docs site both say
  ``F | 0-39``).
- `frontend/app.js` rendered `Math.round(score)` beside the letter, so a
  contract scoring 39.6 rendered as "F 40" (74.6 as "C 75", 59.6 as "D 60",
  89.6 as "B 90"): a number contradicting its own letter.

Against the pre-fix source the band tests fail three ways: 89.9 is not an
integer, ``90 != 89.9 + 1`` leaves a tenth-wide hole between bands, and the
sliver above the printed max is real (39.9 + 0.5 is graded D, so ``F | 0-39.9``
was simply wrong).
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Dict, List, Tuple

from fastapi.testclient import TestClient

from app.api import app
from app.config import GRADE_BANDS
from app.engine.grading import grade_for, grade_key, letter_only

APP_JS = Path(__file__).resolve().parents[1] / "frontend" / "app.js"


def _bands() -> List[Dict[str, object]]:
    return grade_key()


def _raw(band: Dict[str, object]) -> Tuple[float, float]:
    """The bounds exactly as served -- the numbers the legend prints."""
    return float(band["min"]), float(band["max"])  # type: ignore[arg-type]


def _grade_of(band: Dict[str, object]) -> str:
    return str(band["grade"])


def test_legend_reports_the_integer_extent_of_each_band():
    for band in _bands():
        lo, hi = _raw(band)
        # pre-fix the maxima were 39.9 / 59.9 / 74.9 / 89.9, which no score can
        # equal -- a band has no largest score, so the printed bound must be a
        # whole number (the last one the band still earns).
        assert lo.is_integer() and hi.is_integer(), band


def test_legend_bands_are_contiguous_and_cover_every_whole_number():
    bands = _bands()  # descending: A, B, C, D, F
    assert _raw(bands[0])[1] == 100.0
    assert _raw(bands[-1])[0] == 0.0
    for higher, lower in zip(bands, bands[1:]):
        lo_higher = _raw(higher)[0]
        hi_lower = _raw(lower)[1]
        # pre-fix this read 90.0 != 89.9 + 1 -- a tenth-wide hole between bands
        assert lo_higher == hi_lower + 1, (higher, lower)
    for band in bands:
        lo, hi = _raw(band)
        for whole in range(int(lo), int(hi) + 1):
            assert grade_for(whole)[0] == _grade_of(band), (whole, band)


def test_legend_max_is_the_last_whole_score_the_band_still_earns():
    bands = _bands()
    # bands[0] is the top band: the grader clamps above 100, so it has no
    # "next score up" to test against.
    for band in bands[1:]:
        hi = _raw(band)[1]
        assert grade_for(hi)[0] == _grade_of(band)
        assert grade_for(hi + 1)[0] != _grade_of(band)
    top = bands[0]
    assert _raw(top)[1] == 100.0 and grade_for(100)[0] == _grade_of(top)


def test_a_score_just_above_the_printed_max_is_still_in_the_band():
    """The old bound stopped a tenth short, and the sliver it left out was real.

    A user reading ``F: 0-39.9`` who then saw a 39.95 graded F had been told the
    legend was wrong by the app itself.
    """
    for band in _bands():
        hi = _raw(band)[1]
        # pre-fix, 39.9 + 0.5 is graded D -- the F band did not end at 39.9
        assert grade_for(hi + 0.5)[0] == _grade_of(band), band


def test_legend_still_describes_every_configured_band_in_order():
    bands = _bands()
    assert [_grade_of(b) for b in bands] == [b[1] for b in GRADE_BANDS]
    assert [b["meaning"] for b in bands] == [b[2] for b in GRADE_BANDS]


def test_the_printed_number_is_inside_the_band_its_letter_names():
    """The display rule, mirrored: floor(score) is always in score's own band.

    The frontend has no test harness (a gap the README names), so the rule it
    applies is pinned here as a property, and the expression itself is guarded
    by the next test. Rounding up is what broke it: `round(39.6)` is 40, which
    the grader calls D while the card said F.
    """
    for score in (0.0, 39.6, 39.95, 40.0, 59.99, 60.0, 74.6, 74.99, 75.0, 89.99,
                  90.0, 99.999, 100.0):
        shown = math.floor(score)
        assert grade_for(shown)[0] == grade_for(score)[0], (score, shown)
    # the pre-fix rule does not hold, which is why the expression had to change
    assert grade_for(round(39.6))[0] != grade_for(39.6)[0]
    assert grade_for(round(74.6))[0] != grade_for(74.6)[0]


def test_frontend_floors_the_score_instead_of_rounding_it():
    """Lock the one expression that regresses the badge into a contradiction."""
    src = APP_JS.read_text(encoding="utf-8")
    assert "String(Math.floor(r.overall_score))" in src
    assert "String(Math.floor(score.score))" in src
    assert "String(Math.round(r.overall_score))" not in src
    assert "String(Math.round(score.score))" not in src


def test_key_endpoint_serves_the_same_contiguous_integer_bands():
    """The legend the browser actually fetches, through the real app."""
    with TestClient(app) as client:
        key = client.get("/key").json()["grade_key"]
    assert [_grade_of(b) for b in key] == ["A", "B", "C", "D", "F"]
    for band in key:
        lo, hi = _raw(band)
        assert lo.is_integer() and hi.is_integer(), band
    for higher, lower in zip(key, key[1:]):
        assert _raw(higher)[0] == _raw(lower)[1] + 1
    assert all(letter_only(_raw(b)[0]) == _grade_of(b) for b in key)
