"""Tie the pieces together: collect, measure, judge, cost, recommend.

The AWS-facing part is a thin shell around :mod:`collect` and :mod:`metrics`; everything
that decides anything is a pure call into :mod:`classify`, :mod:`cost` and
:mod:`recommend`, so a scan can be reconstructed in a test from stubbed clients alone.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace

from sagemaker_idle_finder import classify, cost, recommend
from sagemaker_idle_finder.aws import AwsError, ReadOnlyClient
from sagemaker_idle_finder.catalog import Policy, Prices, Thresholds
from sagemaker_idle_finder.collect import collect_endpoints
from sagemaker_idle_finder.metrics import MetricTarget, fetch_invocations
from sagemaker_idle_finder.models import (
    Endpoint,
    Finding,
    MetricWindow,
    Remedy,
    ScanResult,
    ServerlessWhatIf,
    Variant,
    Verdict,
)

#: Appended to the evidence when component-level scaling could not be checked.
_COMPONENT_CAVEAT = (
    "this variant hosts inference components, whose own scale-to-zero policy cannot be "
    "read without sagemaker:ListInferenceComponents, which this tool does not request"
)


def scan_region(
    clients: Mapping[str, ReadOnlyClient],
    region: str,
    policy: Policy,
    prices: Prices,
    start: dt.datetime,
    end: dt.datetime,
    threshold: float | None = None,
    what_if: ServerlessWhatIf | None = None,
) -> list[Finding]:
    """Scan one region and return a finding per variant."""
    thresholds = _with_threshold(policy, threshold)
    endpoints = collect_endpoints(
        clients["sagemaker"], clients["application-autoscaling"], region, policy.aws
    )

    targets = [
        MetricTarget(endpoint.name, variant.name)
        for endpoint in endpoints
        for variant in endpoint.variants
        if _needs_metrics(endpoint, variant)
    ]
    windows = fetch_invocations(
        clients["cloudwatch"],
        targets,
        start,
        end,
        policy.aws,
        policy.limits,
        thresholds.metric_period_seconds,
    )

    return [
        _finding(
            endpoint,
            variant,
            windows.get((endpoint.name, variant.name)),
            region,
            policy,
            prices,
            start,
            end,
            threshold,
            what_if,
        )
        for endpoint in endpoints
        for variant in endpoint.variants
    ]


def _needs_metrics(endpoint: Endpoint, variant: Variant) -> bool:
    """Skip the CloudWatch query where the answer cannot change the verdict.

    A serverless variant, one that can scale to zero, or an endpoint that is not billing
    normally is settled before traffic is considered, so asking costs a request and buys
    nothing.
    """
    if not endpoint.status.bills_for_instances:
        return False
    return not (variant.is_serverless or variant.can_scale_to_zero)


def _finding(
    endpoint: Endpoint,
    variant: Variant,
    window: MetricWindow | None,
    region: str,
    policy: Policy,
    prices: Prices,
    start: dt.datetime,
    end: dt.datetime,
    threshold: float | None,
    what_if: ServerlessWhatIf | None,
) -> Finding:
    thresholds = _with_threshold(policy, threshold)
    coverage = classify.window_coverage(endpoint, start, end)
    verdict, evidence = classify.classify(endpoint, variant, window, thresholds, coverage)

    if variant.uses_inference_components and verdict.is_waste:
        evidence = f"{evidence}; {_COMPONENT_CAVEAT}"

    estimate = cost.estimate(region, variant, verdict, window, prices, thresholds)
    remedy, why = recommend.recommend(variant, verdict, window, thresholds, policy.aws)
    if why:
        evidence = f"{evidence}. Suggested: {why}"

    if what_if is not None and remedy is Remedy.MOVE_TO_SERVERLESS:
        estimate = cost.with_serverless_what_if(
            estimate, region, window, what_if, prices, thresholds
        )
        if estimate.serverless_monthly_usd is not None:
            evidence = (
                f"{evidence}. At the {what_if.seconds_per_invocation:g}s / "
                f"{what_if.memory_gb} GB you supplied, the same traffic on serverless is "
                f"about ${estimate.serverless_monthly_usd:,.2f} a month"
            )

    return Finding(
        endpoint=endpoint,
        variant=variant,
        verdict=verdict,
        window=window,
        cost=estimate,
        remedy=remedy,
        evidence=evidence,
        window_coverage=coverage,
    )


def scan(
    client_factory: Callable[[str], Mapping[str, ReadOnlyClient]],
    regions: Sequence[str],
    policy: Policy,
    prices: Prices,
    now: dt.datetime,
    lookback_days: int,
    threshold: float | None = None,
    what_if: ServerlessWhatIf | None = None,
) -> ScanResult:
    """Scan every region, collecting errors rather than abandoning the run.

    `client_factory` is called with a region and returns the guarded clients for it. A
    region that fails is reported alongside the findings, because a scan that dies on one
    bad region is far less useful than one that says which region it could not reach.
    """
    start = now - dt.timedelta(days=lookback_days)
    findings: list[Finding] = []
    errors: list[tuple[str, str]] = []

    for region in regions:
        try:
            clients = client_factory(region)
            findings.extend(
                scan_region(clients, region, policy, prices, start, now, threshold, what_if)
            )
        except (AwsError, OSError) as exc:
            errors.append((region, str(exc)))

    findings.sort(key=_sort_key)
    return ScanResult(
        findings=tuple(findings),
        regions=tuple(regions),
        lookback_days=lookback_days,
        started_at=now,
        prices_published=prices.publication_date,
        errors=tuple(errors),
    )


def _sort_key(finding: Finding) -> tuple[float, str, str]:
    """Most waste first, then by name so equal rows keep a stable order."""
    return (-finding.wasted_monthly_usd, finding.endpoint.name, finding.variant.name)


def _with_threshold(policy: Policy, threshold: float | None) -> Thresholds:
    """Apply a command-line threshold override to the policy's thresholds."""
    if threshold is None:
        return policy.thresholds
    return replace(policy.thresholds, underused_invocations_per_instance_hour=threshold)


__all__ = ["Verdict", "scan", "scan_region"]
