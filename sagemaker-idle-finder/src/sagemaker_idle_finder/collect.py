"""Turn AWS responses into the plain data the rest of the tool works on.

Thin on purpose: it reads fields and builds dataclasses, so the decisions downstream can be
tested without a client. Where a field is absent it stays absent rather than being defaulted
to something that would change a verdict.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Any

from sagemaker_idle_finder.aws import ReadOnlyClient
from sagemaker_idle_finder.catalog import AwsFacts
from sagemaker_idle_finder.models import (
    Endpoint,
    EndpointStatus,
    ScalableTarget,
    Variant,
)


def collect_endpoints(
    sagemaker: ReadOnlyClient,
    autoscaling: ReadOnlyClient,
    region: str,
    aws: AwsFacts,
) -> list[Endpoint]:
    """List every endpoint in `region` and describe it."""
    targets = _scalable_targets(autoscaling, aws)
    endpoints: list[Endpoint] = []
    for summary in sagemaker.paginate("ListEndpoints", "Endpoints"):
        name = str(summary.get("EndpointName", ""))
        if not name:
            continue
        endpoints.append(_describe(sagemaker, name, region, aws, targets))
    return endpoints


def _describe(
    sagemaker: ReadOnlyClient,
    name: str,
    region: str,
    aws: AwsFacts,
    targets: Mapping[str, ScalableTarget],
) -> Endpoint:
    described = sagemaker.call("DescribeEndpoint", EndpointName=name)
    status = _status(described.get("EndpointStatus"))
    config_name = described.get("EndpointConfigName")

    configured: Mapping[str, Mapping[str, Any]] = {}
    if config_name:
        configured = _config_variants(sagemaker, str(config_name))

    variants = tuple(
        _variant(runtime, configured.get(str(runtime.get("VariantName", ""))), name, targets, aws)
        for runtime in described.get("ProductionVariants") or []
    )
    return Endpoint(
        name=name,
        region=region,
        status=status,
        created_at=_moment(described.get("CreationTime")),
        variants=variants,
        config_name=str(config_name) if config_name else None,
        failure_reason=_optional_str(described.get("FailureReason")),
    )


def _config_variants(
    sagemaker: ReadOnlyClient, config_name: str
) -> Mapping[str, Mapping[str, Any]]:
    """Index an endpoint config's variants by name.

    The config is where the instance *type* lives; DescribeEndpoint only reports counts.
    """
    config = sagemaker.call("DescribeEndpointConfig", EndpointConfigName=config_name)
    return {
        str(variant.get("VariantName", "")): variant
        for variant in config.get("ProductionVariants") or []
    }


def _variant(
    runtime: Mapping[str, Any],
    configured: Mapping[str, Any] | None,
    endpoint_name: str,
    targets: Mapping[str, ScalableTarget],
    aws: AwsFacts,
) -> Variant:
    name = str(runtime.get("VariantName", ""))
    serverless = runtime.get("CurrentServerlessConfig") or (
        configured.get("ServerlessConfig") if configured else None
    )

    memory_mb = None
    max_concurrency = None
    if isinstance(serverless, dict):
        memory_mb = _optional_int(serverless.get("MemorySizeInMB"))
        max_concurrency = _optional_int(serverless.get("MaxConcurrency"))

    instance_type = None
    if configured is not None:
        instance_type = _optional_str(configured.get("InstanceType"))

    resource_id = f"endpoint/{endpoint_name}/variant/{name}"
    del aws  # the dimension is already encoded in the targets mapping's keys

    return Variant(
        name=name,
        instance_type=instance_type,
        instance_count=_optional_int(runtime.get("CurrentInstanceCount")) or 0,
        serverless_memory_mb=memory_mb,
        serverless_max_concurrency=max_concurrency,
        inference_components=_inference_components(configured),
        scalable_target=targets.get(resource_id),
    )


def _inference_components(configured: Mapping[str, Any] | None) -> tuple[str, ...]:
    """Detect a variant whose models are deployed as inference components.

    An endpoint config variant that names no model is hosting inference components rather
    than a model directly. The component *names* cannot be read without
    ``sagemaker:ListInferenceComponents``, which this tool does not request, so the fact is
    recorded without pretending to enumerate them — and the renderer says so, because
    component-level scale-to-zero would change the verdict.
    """
    if configured is None:
        return ()
    if configured.get("ModelName"):
        return ()
    return ("<not enumerated>",)


def _scalable_targets(autoscaling: ReadOnlyClient, aws: AwsFacts) -> dict[str, ScalableTarget]:
    """Every SageMaker scaling target in the region, indexed by resource id."""
    found: dict[str, ScalableTarget] = {}
    for raw in autoscaling.paginate(
        "DescribeScalableTargets",
        "ScalableTargets",
        ServiceNamespace=aws.autoscaling_service_namespace,
    ):
        resource_id = _optional_str(raw.get("ResourceId"))
        if not resource_id:
            continue
        found[resource_id] = ScalableTarget(
            resource_id=resource_id,
            scalable_dimension=str(raw.get("ScalableDimension", "")),
            min_capacity=_optional_int(raw.get("MinCapacity")) or 0,
            max_capacity=_optional_int(raw.get("MaxCapacity")) or 0,
        )
    return found


def _status(value: object) -> EndpointStatus:
    try:
        return EndpointStatus(str(value))
    except ValueError:
        return EndpointStatus.UNKNOWN


def _moment(value: object) -> dt.datetime:
    """Read a timestamp, treating a naive one as UTC so comparisons are safe."""
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    return dt.datetime.now(tz=dt.UTC)


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _optional_str(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value
