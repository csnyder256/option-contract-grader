"""Map a 0-100 score to a letter grade + plain-English meaning.

The grade key is the part the user actually reads: same scale for every
dimension and for the overall score.
"""

from __future__ import annotations

import math
from typing import Dict, List, Tuple

from app.config import GRADE_BANDS


def grade_for(score: float) -> Tuple[str, str]:
    """Return (letter, plain-English overall meaning) for a 0-100 score."""
    s = max(0.0, min(100.0, score))
    for threshold, letter, meaning in GRADE_BANDS:
        if s >= threshold:
            return letter, meaning
    return GRADE_BANDS[-1][1], GRADE_BANDS[-1][2]


def letter_only(score: float) -> str:
    return grade_for(score)[0]


def grade_key() -> List[Dict[str, object]]:
    """The full key, for the README and the frontend legend.

    Each band is **half-open**: `grade_for` returns a letter for every score
    from a band's `min` up to (but not including) the next band's `min`. There
    is therefore no largest score in a band to print, and the previous
    ``next_threshold - 0.1`` bound was wrong twice over -- it claimed every
    band stopped a tenth early, so the legend printed ``0-39.9`` for F while
    the grader still returned F for 39.95, and it contradicted the project's
    own documented bands (README and the docs site both say ``F | 0-39``).

    The UI shows whole numbers, so the key reports each band's **integer
    extent**: `max` is the highest whole number that still earns the letter.
    Consecutive bands therefore stay contiguous (``min == previous.max + 1``)
    and cover every whole number from 0 to 100, so the range the legend prints
    is exactly the range the grader implements for the numbers on screen.
    """
    key: List[Dict[str, object]] = []
    bands = GRADE_BANDS
    for i, (threshold, letter, meaning) in enumerate(bands):
        # The band runs up to the next (higher) band's threshold, exclusive;
        # its integer extent ends at the last whole number below that.
        upper = 100 if i == 0 else math.ceil(bands[i - 1][0]) - 1
        key.append(
            {
                "grade": letter,
                "min": math.ceil(threshold),
                "max": upper,
                "meaning": meaning,
            }
        )
    return key
