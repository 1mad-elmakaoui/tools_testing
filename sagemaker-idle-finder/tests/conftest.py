"""Shared fixtures.

No test here reaches AWS. Clients are either botocore stubs with fake credentials or
in-memory fakes; nothing reads a real profile or opens a socket.
"""

from __future__ import annotations

import copy
import datetime as dt
from importlib import resources
from typing import Any

import pytest
import yaml

from sagemaker_idle_finder.catalog import Policy, Prices, load_policy, load_prices
from sagemaker_idle_finder.models import (
    Endpoint,
    EndpointStatus,
    MetricWindow,
    ScalableTarget,
    Variant,
)

#: A fixed "now", so every window and coverage figure in the tests is deterministic.
NOW = dt.datetime(2026, 9, 23, 12, 0, tzinfo=dt.UTC)
WINDOW_START = NOW - dt.timedelta(days=14)


@pytest.fixture(scope="session")
def policy() -> Policy:
    """The policy bundled with the package."""
    return load_policy()


@pytest.fixture(scope="session")
def prices() -> Prices:
    """The prices bundled with the package."""
    return load_prices()


@pytest.fixture
def raw_policy() -> dict[str, Any]:
    """A mutable copy of policy.yaml, for testing validation failures."""
    text = (
        resources.files("sagemaker_idle_finder")
        .joinpath("data/policy.yaml")
        .read_text(encoding="utf-8")
    )
    loaded = yaml.safe_load(text)
    assert isinstance(loaded, dict)
    return copy.deepcopy(loaded)


@pytest.fixture
def raw_prices() -> dict[str, Any]:
    """A mutable copy of prices.yaml."""
    text = (
        resources.files("sagemaker_idle_finder")
        .joinpath("data/prices.yaml")
        .read_text(encoding="utf-8")
    )
    loaded = yaml.safe_load(text)
    assert isinstance(loaded, dict)
    return copy.deepcopy(loaded)


def make_endpoint(
    name: str = "orders",
    status: EndpointStatus = EndpointStatus.IN_SERVICE,
    created_at: dt.datetime | None = None,
    variants: tuple[Variant, ...] = (),
    region: str = "eu-west-1",
) -> Endpoint:
    """An endpoint that existed long before the window unless told otherwise."""
    return Endpoint(
        name=name,
        region=region,
        status=status,
        created_at=created_at or dt.datetime(2025, 1, 1, tzinfo=dt.UTC),
        variants=variants,
    )


def make_variant(
    name: str = "AllTraffic",
    instance_type: str | None = "ml.m5.large",
    instance_count: int = 2,
    serverless_memory_mb: int | None = None,
    min_capacity: int | None = None,
    inference_components: tuple[str, ...] = (),
) -> Variant:
    """A variant, optionally serverless or with a registered scaling target."""
    target = None
    if min_capacity is not None:
        target = ScalableTarget(
            resource_id=f"endpoint/orders/variant/{name}",
            scalable_dimension="sagemaker:variant:DesiredInstanceCount",
            min_capacity=min_capacity,
            max_capacity=4,
        )
    return Variant(
        name=name,
        instance_type=None if serverless_memory_mb else instance_type,
        instance_count=0 if serverless_memory_mb else instance_count,
        serverless_memory_mb=serverless_memory_mb,
        inference_components=inference_components,
        scalable_target=target,
    )


def make_window(
    invocations: float | None = 0.0,
    start: dt.datetime = WINDOW_START,
    end: dt.datetime = NOW,
) -> MetricWindow:
    """A metric window over the standard fourteen days."""
    return MetricWindow(
        start=start,
        end=end,
        total_invocations=invocations,
        datapoints=0 if invocations is None else 336,
    )
