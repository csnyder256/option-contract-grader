"""Map a 0-100 score to a letter grade + plain-English meaning.

The grade key is the part the user actually reads: same scale for every
dimension and for the overall score.
"""

from __future__ import annotations

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
    """The full key, for the README and the frontend legend."""
    key: List[Dict[str, object]] = []
    bands = GRADE_BANDS
    for i, (threshold, letter, meaning) in enumerate(bands):
        upper = 100.0 if i == 0 else bands[i - 1][0] - 0.1
        key.append(
            {
                "grade": letter,
                "min": threshold,
                "max": round(upper, 1),
                "meaning": meaning,
            }
        )
    return key
