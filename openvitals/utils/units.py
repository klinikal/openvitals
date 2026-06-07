"""
Unit conversion utilities shared across connectors.

Single source of truth for physical conversions — prevents divergent constants
across connectors for the same conversion (e.g. kJ → kcal used by WHOOP and Strava).
"""

from __future__ import annotations

_KJ_TO_KCAL = 0.239006  # thermochemical (1 kJ = 0.239006 kcal = 1 / 4.184 kcal)


def kj_to_kcal(kj: float | None) -> float | None:
    """Convert kilojoules → kilocalories. Returns None if input is None."""
    if kj is None:
        return None
    return round(kj * _KJ_TO_KCAL, 1)
