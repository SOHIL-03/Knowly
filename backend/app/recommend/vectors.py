"""Safe vector helpers: embeddings can be missing, the wrong size, non-finite or all zeros."""

from __future__ import annotations

import math
from math import sumprod
from typing import Any


def clean_vector(value: Any, dim: int | None = None) -> list[float] | None:
    """The vector as floats, or None when it is missing, malformed, non-finite, zero or the wrong size."""
    if value is None:
        return None
    try:
        v = [float(x) for x in value]
    except (TypeError, ValueError):
        return None
    if not v or (dim is not None and len(v) != dim):
        return None
    if not all(map(math.isfinite, v)) or sumprod(v, v) <= 0.0:
        return None
    return v


def unit(v: list[float] | None) -> list[float] | None:
    """The vector scaled to length 1, or None when it is missing or has no length."""
    if not v:
        return None
    n = math.sqrt(sumprod(v, v))
    return [x / n for x in v] if n > 0.0 else None


def dot(a: list[float] | None, b: list[float] | None) -> float:
    """Dot product; for unit vectors this is the cosine. 0.0 when either is missing or the sizes differ."""
    if not a or not b or len(a) != len(b):
        return 0.0
    return sumprod(a, b)


def cos(a: list[float] | None, b: list[float] | None) -> float:
    """Cosine similarity in [-1, 1]; 0.0 when either vector is unusable or the sizes differ."""
    if not a or not b or len(a) != len(b):
        return 0.0
    na, nb = sumprod(a, a), sumprod(b, b)
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return max(-1.0, min(1.0, sumprod(a, b) / math.sqrt(na * nb)))
