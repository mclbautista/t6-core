"""Fuzzy scorers for lookups."""

from __future__ import annotations

__all__ = ["wratio"]


def wratio(a: str, b: str) -> float:
    """rapidfuzz's WRatio on normalized text, scaled to 0 to 1. The studio's fuzzy score for names.

    Needs the `lookup` extra (`pip install 't6[lookup]'`).
    """
    try:
        from rapidfuzz import fuzz
    except ImportError:
        raise ImportError("fuzzy contact matching needs rapidfuzz: pip install 't6[lookup]'") from None
    return float(fuzz.WRatio(a, b)) / 100
