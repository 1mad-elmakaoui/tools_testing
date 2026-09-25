"""Shared fixtures. Nothing here builds an AWS client."""

from __future__ import annotations

import pytest

from kinesis_skew.catalog import AwsFacts, Catalog, Thresholds, load_catalog
from kinesis_skew.models import ShardTraffic

#: A day at one-minute resolution, matching the default lookback.
WINDOW_MINUTES = 24 * 60


@pytest.fixture(scope="session")
def catalog() -> Catalog:
    """The catalogue that ships with the package."""
    return load_catalog()


@pytest.fixture(scope="session")
def aws(catalog: Catalog) -> AwsFacts:
    return catalog.aws


@pytest.fixture(scope="session")
def thresholds(catalog: Catalog) -> Thresholds:
    return catalog.thresholds


def make_traffic(
    shard_id: str,
    *,
    peak_fraction: float = 0.1,
    throttled: float = 0.0,
    total_bytes: float | None = None,
    observed_minutes: int = WINDOW_MINUTES,
    window_minutes: int = WINDOW_MINUTES,
    is_open: bool = True,
    bytes_limit_per_minute: float = 1048576 * 60,
    records_limit_per_minute: float = 1000 * 60,
    records_peak_fraction: float | None = None,
) -> ShardTraffic:
    """A shard whose busiest minute reached `peak_fraction` of its byte limit.

    `total_bytes` defaults to the peak sustained for the whole window, which keeps the
    share-of-traffic figures in step with the peaks unless a test wants them apart.
    """
    # A shard CloudWatch reported nothing for has no peak either: there is no datapoint to
    # take a maximum over.
    silent = observed_minutes == 0
    peak_bytes = 0.0 if silent else bytes_limit_per_minute * peak_fraction
    records_fraction = peak_fraction if records_peak_fraction is None else records_peak_fraction
    if silent:
        records_fraction = 0.0
    return ShardTraffic(
        shard_id=shard_id,
        total_bytes=peak_bytes * observed_minutes if total_bytes is None else total_bytes,
        total_records=records_limit_per_minute * records_fraction * observed_minutes,
        throttled_records=throttled,
        peak_bytes_per_minute=peak_bytes,
        peak_records_per_minute=records_limit_per_minute * records_fraction,
        observed_minutes=observed_minutes,
        window_minutes=window_minutes,
        is_open=is_open,
    )
