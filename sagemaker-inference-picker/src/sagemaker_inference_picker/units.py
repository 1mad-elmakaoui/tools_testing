"""Human-readable formatting for the sizes and durations quoted in explanations."""

from __future__ import annotations

_MB_PER_GB = 1024.0
_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600


def number(value: float) -> str:
    """Render a float without a pointless trailing ``.0``."""
    if value == int(value):
        return str(int(value))
    return f"{value:g}"


def megabytes(value: float | None) -> str:
    """Render a size in MB, adding a GB gloss once it is easier to read that way."""
    if value is None:
        return "no documented limit"
    if value >= _MB_PER_GB and value % _MB_PER_GB == 0:
        return f"{number(value)} MB ({number(value / _MB_PER_GB)} GB)"
    return f"{number(value)} MB"


def seconds(value: float | None) -> str:
    """Render a duration in seconds, adding a minutes or hours gloss where it helps."""
    if value is None:
        return "no documented limit"
    gloss = _duration_gloss(value)
    if gloss is None:
        return f"{number(value)} s"
    return f"{number(value)} s ({gloss})"


def milliseconds(value: float) -> str:
    """Render a latency target in milliseconds."""
    return f"{number(value)} ms"


def _duration_gloss(value: float) -> str | None:
    if value >= _SECONDS_PER_HOUR and value % _SECONDS_PER_HOUR == 0:
        hours = value / _SECONDS_PER_HOUR
        return f"{number(hours)} hour" if hours == 1 else f"{number(hours)} hours"
    if value >= 2 * _SECONDS_PER_MINUTE and value % _SECONDS_PER_MINUTE == 0:
        return f"{number(value / _SECONDS_PER_MINUTE)} minutes"
    return None
