"""Rendering DQDL literals: the two places it is easy to emit something invalid."""

from __future__ import annotations

import pytest

from dqdl_gen import syntax


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("plain", '"plain"'),
        ('has "quotes"', '"has \\"quotes\\""'),
        ("back\\slash", '"back\\\\slash"'),
        ("", '""'),
        ("hyphen-and space", '"hyphen-and space"'),
    ],
)
def test_quote_escapes_only_the_two_recognised_sequences(value: str, expected: str) -> None:
    assert syntax.quote(value) == expected


@pytest.mark.parametrize("value", [1e-07, 1e20, 0.000001, 123456789.0, -1e-09])
def test_numbers_never_use_exponent_notation(value: float) -> None:
    """DQDL has no exponent form, so repr() is not a safe way to render a float."""
    rendered = syntax.number(value)
    assert "e" not in rendered.lower()


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0.982, "0.982"), (100.0, "100"), (0.0, "0"), (-4.5, "-4.5"), (1e-07, "0")],
)
def test_number_rendering(value: float, expected: str) -> None:
    assert syntax.number(value) == expected


def test_bounds_round_outwards_so_observed_values_stay_inside() -> None:
    assert syntax.lower_bound(1.23456789) == "1.234567"
    assert syntax.upper_bound(1.23456781) == "1.234568"


def test_bounds_ignore_float_arithmetic_noise() -> None:
    """1.5 - 14.85 is -13.350000000000001; the bound should read -13.35, not -13.350001."""
    assert syntax.lower_bound(1.5 - 14.85) == "-13.35"
    assert syntax.upper_bound(0.1 + 0.2) == "0.3"


def test_negative_places_are_rejected() -> None:
    with pytest.raises(ValueError, match="places must not be negative"):
        syntax.number(1.0, places=-1)


def test_integer_rendering() -> None:
    assert syntax.integer(42) == "42"
    assert syntax.integer(-1) == "-1"
