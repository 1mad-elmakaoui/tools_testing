"""Rendering primitives for DQDL literals.

Small, but the place where two easy mistakes are prevented:

* DQDL numbers have no exponent form, so ``repr(1e-07)`` is not a valid literal;
* only ``\\"`` and ``\\\\`` are recognised escapes inside a quoted string.

Range bounds are rounded outwards — a lower bound down, an upper bound up — so that
reducing the precision of a profiled value can never produce a rule that excludes a value
the profiler actually saw.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal, localcontext

#: Decimal places kept when rendering a profiled float.
DEFAULT_PLACES = 6


def quote(value: str) -> str:
    """Render a string as a DQDL quoted string, escaping the two recognised sequences."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def number(value: float, places: int = DEFAULT_PLACES) -> str:
    """Render a number in plain decimal notation, never in exponent form."""
    return _render(value, places, ROUND_HALF_EVEN)


def lower_bound(value: float, places: int = DEFAULT_PLACES) -> str:
    """Render a range's lower bound, rounding down so it never excludes observed data."""
    return _render(value, places, ROUND_FLOOR)


def upper_bound(value: float, places: int = DEFAULT_PLACES) -> str:
    """Render a range's upper bound, rounding up so it never excludes observed data."""
    return _render(value, places, ROUND_CEILING)


def integer(value: int) -> str:
    """Render an integer."""
    return str(int(value))


def _render(value: float, places: int, rounding: str) -> str:
    if places < 0:
        raise ValueError(f"places must not be negative, got {places}")
    exact = Decimal(repr(float(value)))
    # quantize() raises InvalidOperation when the result needs more significant digits than
    # the context allows, which a large magnitude plus several decimal places easily does.
    # Give it room for every digit either side of the point.
    with localcontext() as context:
        context.prec = max(len(exact.as_tuple().digits) + places + 8, 32)
        # Arithmetic on floats leaves noise in the low digits: 1.5 - 14.85 is
        # -13.350000000000001, and rounding that outwards would emit -13.350001. Clear the
        # noise a few places below what we keep, then round outwards from the clean value.
        settled = exact.quantize(Decimal(1).scaleb(-(places + 3)), rounding=ROUND_HALF_EVEN)
        rounded = settled.quantize(Decimal(1).scaleb(-places), rounding=rounding)
    # normalize() strips trailing zeros but can yield an exponent form such as 1E+2;
    # formatting with "f" forces plain notation back out.
    text = format(rounded.normalize(), "f")
    return "-0" if text == "-0" and value == 0 else text
