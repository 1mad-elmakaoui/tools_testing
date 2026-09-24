"""Estimate what a variant costs per month, and how much of that is wasted.

Pure. Every figure is an estimate derived from published on-demand list prices, so a price
that is not in the file produces ``None`` and a stated reason rather than a zero that would
quietly understate a total.
"""

from __future__ import annotations

from dataclasses import replace

from sagemaker_idle_finder.catalog import Prices, Thresholds
from sagemaker_idle_finder.classify import justified_instances
from sagemaker_idle_finder.models import (
    CostEstimate,
    MetricWindow,
    ServerlessWhatIf,
    Variant,
    Verdict,
)


def estimate(
    region: str,
    variant: Variant,
    verdict: Verdict,
    window: MetricWindow | None,
    prices: Prices,
    thresholds: Thresholds,
) -> CostEstimate:
    """Estimate the monthly cost of `variant` and the share of it judged wasted.

    Waste depends on the verdict:

    * ``idle`` — the whole cost, because nothing is being served for it.
    * ``underused`` — only the instances the observed traffic does not justify. An endpoint
      that exists needs at least one instance, so the surplus is what sits above that. When
      the traffic does not even justify that one instance there is no surplus to remove,
      and the answer is ``None``: something is being wasted, but how much of it can be
      recovered depends on what replaces the endpoint, which this tool cannot see.
    * anything else — nothing.
    """
    if variant.is_serverless:
        return CostEstimate(
            monthly_usd=None,
            wasted_monthly_usd=0.0,
            no_estimate_reason=(
                "serverless is billed per request, not per idle hour, so there is no "
                "standing monthly cost to estimate"
            ),
        )

    if not variant.instance_type or variant.instance_count <= 0:
        return CostEstimate(
            monthly_usd=None,
            wasted_monthly_usd=0.0,
            no_estimate_reason="the variant reports no instances to price",
        )

    hourly = prices.instance_hour(region, variant.instance_type)
    if hourly is None:
        return CostEstimate(
            monthly_usd=None,
            wasted_monthly_usd=None,
            no_estimate_reason=f"no published price for {variant.instance_type} in {region}",
        )

    monthly = hourly * variant.instance_count * thresholds.hours_per_month
    wasted = _wasted(variant, verdict, window, hourly, thresholds)
    return CostEstimate(
        monthly_usd=round(monthly, 2),
        wasted_monthly_usd=None if wasted is None else round(wasted, 2),
        instance_hour_usd=hourly,
        no_estimate_reason=None
        if wasted is not None
        else (
            "the traffic does not justify even one instance, and an endpoint cannot run on "
            "fewer, so the recoverable amount depends on what replaces it rather than on "
            "removing instances"
        ),
    )


def _wasted(
    variant: Variant,
    verdict: Verdict,
    window: MetricWindow | None,
    hourly: float,
    thresholds: Thresholds,
) -> float | None:
    if verdict is Verdict.IDLE:
        return hourly * variant.instance_count * thresholds.hours_per_month
    if verdict is Verdict.UNDERUSED and window is not None:
        justified = justified_instances(window, variant.instance_count, thresholds)
        surplus = max(0, variant.instance_count - justified)
        if surplus == 0:
            return None
        return hourly * surplus * thresholds.hours_per_month
    return 0.0


def serverless_monthly_estimate(
    region: str,
    invocations: float,
    seconds_per_invocation: float,
    memory_gb: int,
    prices: Prices,
    lookback_hours: float,
    thresholds: Thresholds,
) -> float | None:
    """Estimate what the same traffic would cost on serverless inference.

    Lets the "move to serverless" remedy say what it is worth rather than only that it is
    possible. Returns None when the region has no published serverless price.
    """
    per_second = prices.serverless_second(region, memory_gb)
    if per_second is None or lookback_hours <= 0:
        return None
    monthly_invocations = invocations * (thresholds.hours_per_month / lookback_hours)
    return round(monthly_invocations * seconds_per_invocation * per_second, 2)


def with_serverless_what_if(
    estimate: CostEstimate,
    region: str,
    window: MetricWindow | None,
    what_if: ServerlessWhatIf,
    prices: Prices,
    thresholds: Thresholds,
) -> CostEstimate:
    """Add a serverless comparison to `estimate`, using the caller's assumptions.

    Where the waste could not be quantified because there was no surplus instance to
    remove, the comparison supplies it: the standing instance cost less what the same
    traffic would cost billed per request. The figure is only as good as the assumptions it
    was given, which is why it is never derived without them.
    """
    if window is None:
        return estimate
    serverless = serverless_monthly_estimate(
        region,
        window.observed,
        what_if.seconds_per_invocation,
        what_if.memory_gb,
        prices,
        window.hours,
        thresholds,
    )
    if serverless is None or estimate.monthly_usd is None:
        return replace(estimate, serverless_monthly_usd=serverless)
    if estimate.wasted_monthly_usd is not None:
        return replace(estimate, serverless_monthly_usd=serverless)
    return replace(
        estimate,
        serverless_monthly_usd=serverless,
        wasted_monthly_usd=round(max(0.0, estimate.monthly_usd - serverless), 2),
        no_estimate_reason=None,
    )
