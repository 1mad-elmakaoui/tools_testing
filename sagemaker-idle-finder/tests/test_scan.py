"""A whole scan, driven by in-memory clients.

Covers the cases the brief called out: an endpoint created inside the lookback, Creating
and Failed statuses, inference-component variants, and missing metrics.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from sagemaker_idle_finder.aws import CallRecorder, ReadOnlyClient, RetryPolicy
from sagemaker_idle_finder.catalog import Policy, Prices
from sagemaker_idle_finder.models import Remedy, ServerlessWhatIf, Verdict
from sagemaker_idle_finder.scan import scan, scan_region
from tests.conftest import NOW, WINDOW_START
from tests.test_aws import FakeClient

REGION = "eu-west-1"


def _endpoint(
    name: str,
    *,
    status: str = "InService",
    created: dt.datetime | None = None,
    instance_type: str | None = "ml.m5.large",
    count: int = 2,
    serverless_mb: int | None = None,
    model_name: str | None = "a-model",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the DescribeEndpoint and DescribeEndpointConfig pair for one endpoint."""
    runtime: dict[str, Any] = {"VariantName": "AllTraffic", "CurrentInstanceCount": count}
    configured: dict[str, Any] = {"VariantName": "AllTraffic"}
    if serverless_mb:
        runtime = {
            "VariantName": "AllTraffic",
            "CurrentServerlessConfig": {"MemorySizeInMB": serverless_mb, "MaxConcurrency": 5},
        }
        configured["ServerlessConfig"] = {"MemorySizeInMB": serverless_mb, "MaxConcurrency": 5}
    else:
        configured["InstanceType"] = instance_type
    if model_name:
        configured["ModelName"] = model_name

    described = {
        "EndpointName": name,
        "EndpointStatus": status,
        "EndpointConfigName": f"{name}-config",
        "CreationTime": created or dt.datetime(2025, 1, 1, tzinfo=dt.UTC),
        "ProductionVariants": [runtime],
    }
    config = {"EndpointConfigName": f"{name}-config", "ProductionVariants": [configured]}
    return described, config


def _clients(
    endpoints: list[tuple[dict[str, Any], dict[str, Any]]],
    invocations: dict[str, float | None],
    scalable_targets: list[dict[str, Any]] | None = None,
    recorder: CallRecorder | None = None,
) -> dict[str, ReadOnlyClient]:
    """Wire up fake SageMaker, CloudWatch and autoscaling clients for one region."""
    described = {name["EndpointName"]: (name, config) for name, config in endpoints}
    retry = RetryPolicy(max_retries=1, base_delay_seconds=0.0, sleep=lambda _: None)

    sagemaker_fake = FakeClient(
        {
            "list_endpoints": {"Endpoints": [{"EndpointName": key} for key in described]},
        }
    )

    def describe_endpoint(**kwargs: Any) -> Any:
        sagemaker_fake.calls.append(("describe_endpoint", kwargs))
        return described[kwargs["EndpointName"]][0]

    def describe_config(**kwargs: Any) -> Any:
        sagemaker_fake.calls.append(("describe_endpoint_config", kwargs))
        name = kwargs["EndpointConfigName"].removesuffix("-config")
        return described[name][1]

    sagemaker_fake.describe_endpoint = describe_endpoint  # type: ignore[attr-defined]
    sagemaker_fake.describe_endpoint_config = describe_config  # type: ignore[attr-defined]

    # One MetricDataResult per query, in the order the queries were built.
    results = [
        {
            "Id": f"m{index}",
            "Values": [] if invocations.get(key) is None else [invocations[key]],
        }
        for index, key in enumerate(key for key in described if key in invocations)
    ]
    cloudwatch_fake = FakeClient({"get_metric_data": {"MetricDataResults": results}})
    autoscaling_fake = FakeClient(
        {"describe_scalable_targets": {"ScalableTargets": scalable_targets or []}}
    )

    allowed = {
        "sagemaker": frozenset({"ListEndpoints", "DescribeEndpoint", "DescribeEndpointConfig"}),
        "cloudwatch": frozenset({"GetMetricData"}),
        "application-autoscaling": frozenset({"DescribeScalableTargets"}),
    }
    fakes = {
        "sagemaker": sagemaker_fake,
        "cloudwatch": cloudwatch_fake,
        "application-autoscaling": autoscaling_fake,
    }
    return {
        service: ReadOnlyClient(fake, service, allowed[service], retry, recorder)  # type: ignore[arg-type]
        for service, fake in fakes.items()
    }


def _scan(clients: dict[str, ReadOnlyClient], policy: Policy, prices: Prices) -> Any:
    return scan_region(clients, REGION, policy, prices, WINDOW_START, NOW)


# ------------------------------------------------------------------------ happy paths


def test_an_idle_endpoint_is_found_and_costed(policy: Policy, prices: Prices) -> None:
    clients = _clients([_endpoint("idle-one")], {"idle-one": 0.0})
    findings = _scan(clients, policy, prices)
    assert len(findings) == 1
    assert findings[0].verdict is Verdict.IDLE
    assert findings[0].remedy is Remedy.DELETE
    assert findings[0].wasted_monthly_usd > 0


def test_a_busy_endpoint_is_healthy(policy: Policy, prices: Prices) -> None:
    clients = _clients([_endpoint("busy")], {"busy": 900_000.0})
    findings = _scan(clients, policy, prices)
    assert findings[0].verdict is Verdict.HEALTHY
    assert findings[0].wasted_monthly_usd == 0


# ---------------------------------------------------------------- the awkward cases


def test_an_endpoint_created_inside_the_lookback_is_not_called_idle(
    policy: Policy, prices: Prices
) -> None:
    clients = _clients(
        [_endpoint("brand-new", created=NOW - dt.timedelta(days=1))], {"brand-new": 0.0}
    )
    findings = _scan(clients, policy, prices)
    assert findings[0].verdict is Verdict.TOO_NEW
    assert findings[0].wasted_monthly_usd == 0


@pytest.mark.parametrize("status", ["Creating", "Failed", "Deleting", "OutOfService"])
def test_an_endpoint_that_is_not_billing_is_reported_without_waste(
    policy: Policy, prices: Prices, status: str
) -> None:
    clients = _clients([_endpoint("odd", status=status)], {})
    findings = _scan(clients, policy, prices)
    assert findings[0].verdict is Verdict.NOT_BILLING
    assert findings[0].wasted_monthly_usd == 0


def test_a_serverless_variant_is_not_applicable(policy: Policy, prices: Prices) -> None:
    clients = _clients([_endpoint("sls", serverless_mb=2048)], {})
    findings = _scan(clients, policy, prices)
    assert findings[0].verdict is Verdict.NOT_APPLICABLE
    assert findings[0].variant.is_serverless


def test_a_scale_to_zero_variant_is_not_applicable(policy: Policy, prices: Prices) -> None:
    targets = [
        {
            "ResourceId": "endpoint/scaler/variant/AllTraffic",
            "ScalableDimension": "sagemaker:variant:DesiredInstanceCount",
            "MinCapacity": 0,
            "MaxCapacity": 4,
        }
    ]
    clients = _clients([_endpoint("scaler")], {}, scalable_targets=targets)
    findings = _scan(clients, policy, prices)
    assert findings[0].verdict is Verdict.NOT_APPLICABLE
    assert findings[0].variant.can_scale_to_zero


def test_an_inference_component_variant_is_flagged_as_unverifiable(
    policy: Policy, prices: Prices
) -> None:
    """A variant with no model name hosts components, whose scaling we cannot read."""
    clients = _clients([_endpoint("components", model_name=None)], {"components": 0.0})
    findings = _scan(clients, policy, prices)
    assert findings[0].variant.uses_inference_components
    assert findings[0].verdict is Verdict.IDLE
    assert "ListInferenceComponents" in findings[0].evidence


def test_missing_metrics_are_distinguished_from_a_measured_zero(
    policy: Policy, prices: Prices
) -> None:
    clients = _clients([_endpoint("no-metrics")], {"no-metrics": None})
    findings = _scan(clients, policy, prices)
    assert findings[0].window is not None
    assert findings[0].window.total_invocations is None
    assert "no data at all" in findings[0].evidence


# ------------------------------------------------------------------ call discipline


def test_a_scan_calls_only_read_operations(policy: Policy, prices: Prices) -> None:
    """The guarantee the README makes, asserted over a whole scan."""
    recorder = CallRecorder()
    clients = _clients(
        [_endpoint("one"), _endpoint("two")], {"one": 0.0, "two": 5.0}, recorder=recorder
    )
    _scan(clients, policy, prices)
    allowed = {
        ("sagemaker", "ListEndpoints"),
        ("sagemaker", "DescribeEndpoint"),
        ("sagemaker", "DescribeEndpointConfig"),
        ("cloudwatch", "GetMetricData"),
        ("application-autoscaling", "DescribeScalableTargets"),
    }
    assert set(recorder.calls) <= allowed
    assert recorder.calls, "the scan should have called something"


def test_cloudwatch_is_not_queried_for_variants_that_cannot_waste(
    policy: Policy, prices: Prices
) -> None:
    """Asking about a serverless variant costs a request and cannot change the verdict."""
    recorder = CallRecorder()
    clients = _clients([_endpoint("sls", serverless_mb=2048)], {}, recorder=recorder)
    _scan(clients, policy, prices)
    assert ("cloudwatch", "GetMetricData") not in recorder.calls


# -------------------------------------------------------------------- multi-region


def test_a_failing_region_is_reported_without_abandoning_the_others(
    policy: Policy, prices: Prices
) -> None:
    good = _clients([_endpoint("idle-one")], {"idle-one": 0.0})

    def factory(region: str) -> dict[str, ReadOnlyClient]:
        if region == "bad-region":
            raise OSError("no credentials for bad-region")
        return good

    result = scan(factory, ["eu-west-1", "bad-region"], policy, prices, NOW, 14)
    assert len(result.findings) == 1
    assert result.errors == (("bad-region", "no credentials for bad-region"),)


def test_findings_are_sorted_by_waste(policy: Policy, prices: Prices) -> None:
    clients = _clients(
        [_endpoint("small", count=1), _endpoint("big", count=8)],
        {"small": 0.0, "big": 0.0},
    )
    findings = _scan(clients, policy, prices)
    from sagemaker_idle_finder.scan import _sort_key

    ordered = sorted(findings, key=_sort_key)
    assert ordered[0].endpoint.name == "big"
    assert ordered[0].wasted_monthly_usd > ordered[1].wasted_monthly_usd


def test_a_threshold_override_changes_the_verdict(policy: Policy, prices: Prices) -> None:
    clients = _clients([_endpoint("thin")], {"thin": 5_000.0})
    relaxed = scan_region(clients, REGION, policy, prices, WINDOW_START, NOW, threshold=0.001)
    assert relaxed[0].verdict is Verdict.HEALTHY

    clients = _clients([_endpoint("thin")], {"thin": 5_000.0})
    strict = scan_region(clients, REGION, policy, prices, WINDOW_START, NOW, threshold=1000.0)
    assert strict[0].verdict is Verdict.UNDERUSED


def test_the_serverless_what_if_reaches_the_finding(policy: Policy, prices: Prices) -> None:
    """A single thinly-used instance gets a dollar figure once assumptions are supplied."""
    endpoint = _endpoint("nlp-sandbox", instance_type="ml.m5.large", count=1)
    clients = _clients([endpoint], {"nlp-sandbox": 40.0})
    what_if = ServerlessWhatIf(seconds_per_invocation=0.5, memory_gb=2)

    plain = scan_region(clients, REGION, policy, prices, WINDOW_START, NOW)
    assert plain[0].remedy is Remedy.MOVE_TO_SERVERLESS
    assert plain[0].cost.wasted_monthly_usd is None

    clients = _clients([endpoint], {"nlp-sandbox": 40.0})
    costed = scan_region(clients, REGION, policy, prices, WINDOW_START, NOW, None, what_if)
    assert costed[0].cost.serverless_monthly_usd is not None
    assert costed[0].cost.wasted_monthly_usd is not None
    assert "you supplied" in costed[0].evidence


def test_the_what_if_is_ignored_where_serverless_is_not_the_remedy(
    policy: Policy, prices: Prices
) -> None:
    """A GPU box cannot move to serverless, so no serverless figure is invented for it."""
    endpoint = _endpoint("vision", instance_type="ml.g5.xlarge", count=1)
    clients = _clients([endpoint], {"vision": 40.0})
    findings = scan_region(
        clients,
        REGION,
        policy,
        prices,
        WINDOW_START,
        NOW,
        None,
        ServerlessWhatIf(seconds_per_invocation=0.5, memory_gb=2),
    )
    assert findings[0].remedy is Remedy.MOVE_TO_ASYNC_SCALE_TO_ZERO
    assert findings[0].cost.serverless_monthly_usd is None
