"""Decide what one variant's traffic means.

Pure: takes plain data, returns a verdict and the evidence for it. No AWS, no clock, no
files — the current time is passed in so a test can pin it.

The order of the checks matters more than any single one. A variant that cannot waste idle
money (serverless, or autoscaling down to zero) is settled before traffic is considered,
and an endpoint too young for the lookback is settled before silence is read as idleness.
That second check is the main guard against the tool's worst failure: calling a two-day-old
endpoint idle on the strength of twelve days when it did not exist.
"""

from __future__ import annotations

import datetime as dt

from sagemaker_idle_finder.catalog import Thresholds
from sagemaker_idle_finder.models import (
    Endpoint,
    MetricWindow,
    Variant,
    Verdict,
)


def window_coverage(
    endpoint: Endpoint, window_start: dt.datetime, window_end: dt.datetime
) -> float:
    """What fraction of the requested window the endpoint actually existed for."""
    total = (window_end - window_start).total_seconds()
    if total <= 0:
        return 0.0
    existed_from = max(endpoint.created_at, window_start)
    existed = (window_end - existed_from).total_seconds()
    return max(0.0, min(1.0, existed / total))


def classify(
    endpoint: Endpoint,
    variant: Variant,
    window: MetricWindow | None,
    thresholds: Thresholds,
    coverage: float,
) -> tuple[Verdict, str]:
    """Return a verdict for `variant` and a sentence explaining it."""
    if not endpoint.status.bills_for_instances:
        return (
            Verdict.NOT_BILLING,
            f"endpoint status is {endpoint.status.value}, so its instances are not "
            f"billing normally and no waste is attributed",
        )

    if variant.is_serverless:
        memory = variant.serverless_memory_mb
        return (
            Verdict.NOT_APPLICABLE,
            f"serverless variant ({memory} MB), which costs nothing while idle",
        )

    target = variant.scalable_target
    if target is not None and target.can_scale_to_zero:
        return (
            Verdict.NOT_APPLICABLE,
            f"autoscaling is registered on {target.resource_id} with a minimum capacity of "
            f"0, so it can scale to zero and costs nothing while idle",
        )

    if coverage < thresholds.minimum_window_coverage:
        return (
            Verdict.TOO_NEW,
            f"created {_date(endpoint.created_at)}, covering only {coverage:.0%} of the "
            f"lookback, so there is not enough history to judge it",
        )

    if window is None:
        return (
            Verdict.TOO_NEW,
            "no metric window was collected, so there is nothing to judge",
        )

    if variant.instance_count <= 0:
        return (
            Verdict.NOT_APPLICABLE,
            "the variant reports no instances, so there is no idle instance cost",
        )

    invocations = window.observed
    if invocations <= 0:
        measured = (
            "CloudWatch returned no data at all"
            if window.total_invocations is None
            else "CloudWatch reported zero invocations"
        )
        return (
            Verdict.IDLE,
            f"{measured} across {window.hours:.0f} hours on "
            f"{variant.instance_count} x {variant.instance_type}",
        )

    per_instance_hour = invocations / (window.hours * variant.instance_count)
    threshold = thresholds.underused_invocations_per_instance_hour
    if per_instance_hour < threshold:
        return (
            Verdict.UNDERUSED,
            f"{invocations:,.0f} invocations over {window.hours:.0f} hours is "
            f"{per_instance_hour:.3f} per instance-hour across "
            f"{_plural(variant.instance_count, 'instance')}, "
            f"below the threshold of {threshold:g}",
        )

    return (
        Verdict.HEALTHY,
        f"{invocations:,.0f} invocations over {window.hours:.0f} hours is "
        f"{per_instance_hour:.2f} per instance-hour, at or above the threshold of {threshold:g}",
    )


def justified_instances(window: MetricWindow, instance_count: int, thresholds: Thresholds) -> int:
    """How many instances the observed traffic would justify at the threshold.

    Never more than the variant actually has, and never fewer than one: an endpoint that
    exists at all needs an instance, so the surplus is what sits above that.
    """
    threshold = thresholds.underused_invocations_per_instance_hour
    if threshold <= 0 or window.hours <= 0:
        return max(1, instance_count)
    needed = window.observed / (threshold * window.hours)
    return max(1, min(instance_count, _ceil(needed)))


def _ceil(value: float) -> int:
    whole = int(value)
    return whole if value == whole else whole + 1


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _date(moment: dt.datetime) -> str:
    return moment.date().isoformat()
