"""Classification: the decisions that decide whether money is called wasted.

These run on hand-built values with no AWS anywhere, which is the point of keeping the
logic pure.
"""

from __future__ import annotations

import datetime as dt

import pytest

from sagemaker_idle_finder.catalog import Policy
from sagemaker_idle_finder.classify import classify, justified_instances, window_coverage
from sagemaker_idle_finder.models import (
    Endpoint,
    EndpointStatus,
    MetricWindow,
    Variant,
    Verdict,
)
from tests.conftest import NOW, WINDOW_START, make_endpoint, make_variant, make_window


def _classify(
    policy: Policy,
    endpoint: Endpoint,
    variant: Variant,
    window: MetricWindow | None,
) -> tuple[Verdict, str]:
    coverage = window_coverage(endpoint, WINDOW_START, NOW)
    return classify(endpoint, variant, window, policy.thresholds, coverage)


# ------------------------------------------------------------------------------- idle


def test_zero_invocations_is_idle(policy: Policy) -> None:
    verdict, evidence = _classify(policy, make_endpoint(), make_variant(), make_window(0.0))
    assert verdict is Verdict.IDLE
    assert "zero invocations" in evidence
    assert "2 x ml.m5.large" in evidence


def test_no_metric_data_at_all_is_still_idle_but_says_so(policy: Policy) -> None:
    """CloudWatch returning nothing and reporting zero are different facts."""
    verdict, evidence = _classify(policy, make_endpoint(), make_variant(), make_window(None))
    assert verdict is Verdict.IDLE
    assert "no data at all" in evidence


# ------------------------------------------------------------------------- underused


def test_traffic_below_the_threshold_is_underused(policy: Policy) -> None:
    # 336 hours x 2 instances = 672 instance-hours; 100 invocations is well under 1/hour.
    verdict, evidence = _classify(policy, make_endpoint(), make_variant(), make_window(100.0))
    assert verdict is Verdict.UNDERUSED
    assert "per instance-hour" in evidence
    assert "2 instances" in evidence


def test_traffic_at_the_threshold_is_healthy(policy: Policy) -> None:
    """Exactly at the threshold counts as healthy: the rule is 'below', not 'at or below'."""
    exactly = 336.0 * 2 * policy.thresholds.underused_invocations_per_instance_hour
    verdict, _ = _classify(policy, make_endpoint(), make_variant(), make_window(exactly))
    assert verdict is Verdict.HEALTHY


def test_traffic_just_below_the_threshold_is_underused(policy: Policy) -> None:
    almost = 336.0 * 2 * policy.thresholds.underused_invocations_per_instance_hour - 1
    verdict, _ = _classify(policy, make_endpoint(), make_variant(), make_window(almost))
    assert verdict is Verdict.UNDERUSED


def test_plenty_of_traffic_is_healthy(policy: Policy) -> None:
    verdict, evidence = _classify(policy, make_endpoint(), make_variant(), make_window(500_000.0))
    assert verdict is Verdict.HEALTHY
    assert "at or above" in evidence


# -------------------------------------------------------------- nothing to waste here


def test_a_serverless_variant_is_not_applicable(policy: Policy) -> None:
    variant = make_variant(serverless_memory_mb=2048)
    verdict, evidence = _classify(policy, make_endpoint(), variant, make_window(0.0))
    assert verdict is Verdict.NOT_APPLICABLE
    assert "costs nothing while idle" in evidence


def test_a_scale_to_zero_variant_is_not_applicable(policy: Policy) -> None:
    """A minimum capacity of zero means the idle cost is already gone."""
    variant = make_variant(min_capacity=0)
    verdict, evidence = _classify(policy, make_endpoint(), variant, make_window(0.0))
    assert verdict is Verdict.NOT_APPLICABLE
    assert "minimum capacity of 0" in evidence


def test_a_scale_to_one_variant_is_still_judged(policy: Policy) -> None:
    """Autoscaling that never goes below one instance still leaves an idle cost."""
    variant = make_variant(min_capacity=1)
    verdict, _ = _classify(policy, make_endpoint(), variant, make_window(0.0))
    assert verdict is Verdict.IDLE


def test_a_variant_with_no_instances_is_not_applicable(policy: Policy) -> None:
    variant = make_variant(instance_count=0)
    verdict, _ = _classify(policy, make_endpoint(), variant, make_window(0.0))
    assert verdict is Verdict.NOT_APPLICABLE


# ------------------------------------------------------------------------- too new


def test_an_endpoint_created_inside_the_window_is_not_called_idle(policy: Policy) -> None:
    """The guard that matters most: silence during time the endpoint did not exist."""
    endpoint = make_endpoint(created_at=NOW - dt.timedelta(days=2))
    verdict, evidence = _classify(policy, endpoint, make_variant(), make_window(0.0))
    assert verdict is Verdict.TOO_NEW
    assert "14%" in evidence
    assert "not enough history" in evidence


def test_an_endpoint_older_than_the_coverage_floor_is_judged(policy: Policy) -> None:
    endpoint = make_endpoint(created_at=NOW - dt.timedelta(days=13))
    verdict, _ = _classify(policy, endpoint, make_variant(), make_window(0.0))
    assert verdict is Verdict.IDLE


def test_coverage_is_clamped_between_zero_and_one() -> None:
    ancient = make_endpoint(created_at=dt.datetime(2020, 1, 1, tzinfo=dt.UTC))
    assert window_coverage(ancient, WINDOW_START, NOW) == 1.0
    future = make_endpoint(created_at=NOW + dt.timedelta(days=5))
    assert window_coverage(future, WINDOW_START, NOW) == 0.0


def test_an_empty_window_has_no_coverage() -> None:
    assert window_coverage(make_endpoint(), NOW, NOW) == 0.0


def test_a_missing_window_is_too_new_rather_than_idle(policy: Policy) -> None:
    verdict, evidence = _classify(policy, make_endpoint(), make_variant(), None)
    assert verdict is Verdict.TOO_NEW
    assert "nothing to judge" in evidence


# ---------------------------------------------------------------------- not billing


@pytest.mark.parametrize(
    "status",
    [
        EndpointStatus.CREATING,
        EndpointStatus.FAILED,
        EndpointStatus.DELETING,
        EndpointStatus.OUT_OF_SERVICE,
        EndpointStatus.UNKNOWN,
    ],
)
def test_statuses_that_do_not_bill_normally(policy: Policy, status: EndpointStatus) -> None:
    verdict, evidence = _classify(
        policy, make_endpoint(status=status), make_variant(), make_window(0.0)
    )
    assert verdict is Verdict.NOT_BILLING
    assert status.value in evidence


@pytest.mark.parametrize(
    "status",
    [
        EndpointStatus.IN_SERVICE,
        EndpointStatus.UPDATING,
        EndpointStatus.SYSTEM_UPDATING,
        EndpointStatus.ROLLING_BACK,
    ],
)
def test_statuses_that_do_bill(policy: Policy, status: EndpointStatus) -> None:
    verdict, _ = _classify(policy, make_endpoint(status=status), make_variant(), make_window(0.0))
    assert verdict is Verdict.IDLE


# ------------------------------------------------------------- justified instances


def test_justified_instances_never_drops_below_one(policy: Policy) -> None:
    assert justified_instances(make_window(0.0), 4, policy.thresholds) == 1


def test_justified_instances_never_exceeds_what_exists(policy: Policy) -> None:
    assert justified_instances(make_window(10_000_000.0), 3, policy.thresholds) == 3


def test_justified_instances_rounds_up(policy: Policy) -> None:
    # 400 invocations over 336 hours at 1/hour justifies 1.19 instances, so two.
    assert justified_instances(make_window(400.0), 8, policy.thresholds) == 2


def test_justified_instances_handles_a_zero_threshold(policy: Policy) -> None:
    from dataclasses import replace

    loosened = replace(policy.thresholds, underused_invocations_per_instance_hour=0.0)
    assert justified_instances(make_window(5.0), 3, loosened) == 3
