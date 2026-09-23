"""Formatting helpers used in every explanation."""

from __future__ import annotations

import pytest

from sagemaker_inference_picker import units


@pytest.mark.parametrize(
    ("value", "expected"),
    [(4.0, "4"), (4, "4"), (0.5, "0.5"), (25.0, "25"), (1024.0, "1024"), (0.25, "0.25")],
)
def test_number_drops_pointless_decimals(value: float, expected: str) -> None:
    assert units.number(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (4.0, "4 MB"),
        (0.5, "0.5 MB"),
        (25.0, "25 MB"),
        (100.0, "100 MB"),
        (1024.0, "1024 MB (1 GB)"),
        (2048.0, "2048 MB (2 GB)"),
        (1500.0, "1500 MB"),
        (None, "no documented limit"),
    ],
)
def test_megabytes(value: float | None, expected: str) -> None:
    assert units.megabytes(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (60.0, "60 s"),
        (0.25, "0.25 s"),
        (120.0, "120 s (2 minutes)"),
        (900.0, "900 s (15 minutes)"),
        (3600.0, "3600 s (1 hour)"),
        (7200.0, "7200 s (2 hours)"),
        (240.0, "240 s (4 minutes)"),
        (95.0, "95 s"),
        (None, "no documented limit"),
    ],
)
def test_seconds(value: float | None, expected: str) -> None:
    assert units.seconds(value) == expected


def test_milliseconds() -> None:
    assert units.milliseconds(200.0) == "200 ms"
    assert units.milliseconds(2.5) == "2.5 ms"
