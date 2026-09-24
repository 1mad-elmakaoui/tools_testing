"""Load and validate ``policy.yaml`` and ``prices.yaml``.

The only module that reads those files. It enforces the discipline they document: an AWS
fact must cite documentation, and a judgement call must carry a rationale.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import yaml

SUPPORTED_SCHEMA_VERSION = 1
_PACKAGE = "sagemaker_idle_finder"
_POLICY_FILE = "data/policy.yaml"
_PRICES_FILE = "data/prices.yaml"


class CatalogError(ValueError):
    """Raised when a data file is missing, malformed, or missing a source or rationale."""


@dataclass(frozen=True)
class Thresholds:
    """Where this tool draws its lines. None of these is an AWS figure."""

    underused_invocations_per_instance_hour: float
    default_lookback_days: int
    minimum_window_coverage: float
    serverless_candidate_max_instance_memory_gb: float
    metric_period_seconds: int
    hours_per_month: float
    max_retries: int
    retry_base_delay_seconds: float


@dataclass(frozen=True)
class AwsFacts:
    """API identifiers, and the complete set of operations this tool may call."""

    allowed_operations: Mapping[str, frozenset[str]]
    metric_namespace: str
    invocations_metric: str
    variant_scalable_dimension: str
    inference_component_scalable_dimension: str
    autoscaling_service_namespace: str
    serverless_max_memory_gb: int


@dataclass(frozen=True)
class Limits:
    """Documented CloudWatch quotas that shape how requests are batched."""

    max_metric_queries_per_call: int
    max_datapoints_per_call: int


@dataclass(frozen=True)
class Policy:
    """The parsed contents of ``policy.yaml``."""

    schema_version: int
    last_verified: str
    verification_note: str
    disclaimer: str
    aws: AwsFacts
    limits: Limits
    thresholds: Thresholds
    origin: str


@dataclass(frozen=True)
class Prices:
    """The parsed contents of ``prices.yaml``."""

    publication_date: str
    source: str
    note: str
    hosting_instance_hour_usd: Mapping[str, Mapping[str, float]]
    serverless_second_usd_by_memory_gb: Mapping[str, Mapping[int, float]]
    origin: str

    def instance_hour(self, region: str, instance_type: str) -> float | None:
        """The hourly price of one instance, or None when it is not in the price file."""
        return self.hosting_instance_hour_usd.get(region, {}).get(instance_type)

    def serverless_second(self, region: str, memory_gb: int) -> float | None:
        """The per-second price of a serverless endpoint at a memory size."""
        return self.serverless_second_usd_by_memory_gb.get(region, {}).get(memory_gb)

    @property
    def regions(self) -> tuple[str, ...]:
        """Regions the price file covers."""
        return tuple(sorted(self.hosting_instance_hour_usd))


def load_policy(path: Path | None = None) -> Policy:
    """Load the policy from `path`, or from the file bundled with the package."""
    raw, origin = _read(path, _POLICY_FILE)
    try:
        return _parse_policy(raw, origin)
    except CatalogError as exc:
        raise CatalogError(f"{origin}: {exc}") from exc


def load_prices(path: Path | None = None) -> Prices:
    """Load prices from `path`, or from the file bundled with the package."""
    raw, origin = _read(path, _PRICES_FILE)
    try:
        return _parse_prices(raw, origin)
    except CatalogError as exc:
        raise CatalogError(f"{origin}: {exc}") from exc


def _read(path: Path | None, default: str) -> tuple[object, str]:
    if path is None:
        text = resources.files(_PACKAGE).joinpath(default).read_text(encoding="utf-8")
        origin = f"{_PACKAGE}/{default}"
    else:
        if not path.is_file():
            raise CatalogError(f"file not found: {path}")
        text = path.read_text(encoding="utf-8")
        origin = str(path)
    try:
        return yaml.safe_load(text), origin
    except yaml.YAMLError as exc:
        raise CatalogError(f"{origin}: not valid YAML: {exc}") from exc


def _parse_policy(raw: object, origin: str) -> Policy:
    root = _mapping(raw, "document")
    _check_version(root)
    meta = _mapping(root.get("meta"), "meta")
    aws = _mapping(root.get("aws"), "aws")
    limits = _mapping(root.get("limits"), "limits")
    thresholds = _mapping(root.get("thresholds"), "thresholds")

    return Policy(
        schema_version=SUPPORTED_SCHEMA_VERSION,
        last_verified=_str(meta.get("last_verified"), "meta.last_verified"),
        verification_note=_str(meta.get("verification_note"), "meta.verification_note"),
        disclaimer=_str(meta.get("disclaimer"), "meta.disclaimer"),
        aws=AwsFacts(
            allowed_operations=_parse_operations(aws),
            metric_namespace=_fact_str(aws, "metric_namespace", "aws"),
            invocations_metric=_fact_str(aws, "invocations_metric", "aws"),
            variant_scalable_dimension=_fact_str(aws, "variant_scalable_dimension", "aws"),
            inference_component_scalable_dimension=_fact_str(
                aws, "inference_component_scalable_dimension", "aws"
            ),
            autoscaling_service_namespace=_fact_str(aws, "autoscaling_service_namespace", "aws"),
            serverless_max_memory_gb=int(_fact_number(aws, "serverless_max_memory_gb", "aws")),
        ),
        limits=Limits(
            max_metric_queries_per_call=int(
                _fact_number(limits, "max_metric_queries_per_call", "limits")
            ),
            max_datapoints_per_call=int(_fact_number(limits, "max_datapoints_per_call", "limits")),
        ),
        thresholds=Thresholds(
            underused_invocations_per_instance_hour=_judgement(
                thresholds, "underused_invocations_per_instance_hour", "thresholds"
            ),
            default_lookback_days=int(
                _judgement(thresholds, "default_lookback_days", "thresholds")
            ),
            minimum_window_coverage=_judgement(thresholds, "minimum_window_coverage", "thresholds"),
            serverless_candidate_max_instance_memory_gb=_judgement(
                thresholds, "serverless_candidate_max_instance_memory_gb", "thresholds"
            ),
            metric_period_seconds=int(
                _judgement(thresholds, "metric_period_seconds", "thresholds")
            ),
            hours_per_month=_judgement(thresholds, "hours_per_month", "thresholds"),
            max_retries=int(_judgement(thresholds, "max_retries", "thresholds")),
            retry_base_delay_seconds=_judgement(
                thresholds, "retry_base_delay_seconds", "thresholds"
            ),
        ),
        origin=origin,
    )


def _parse_operations(aws: Mapping[str, object]) -> Mapping[str, frozenset[str]]:
    where = "aws.allowed_operations"
    entry = _mapping(aws.get("allowed_operations"), where)
    _require_source(entry, where)
    services = _mapping(entry.get("value"), f"{where}.value")
    if not services:
        raise CatalogError(f"{where}.value must not be empty")
    parsed: dict[str, frozenset[str]] = {}
    for service, operations in services.items():
        if not isinstance(operations, list) or not operations:
            raise CatalogError(f"{where}.value.{service} must be a non-empty list")
        for operation in operations:
            if not isinstance(operation, str) or not operation:
                raise CatalogError(f"{where}.value.{service} contains a non-string operation")
        parsed[service] = frozenset(str(item) for item in operations)
    return parsed


def _parse_prices(raw: object, origin: str) -> Prices:
    root = _mapping(raw, "document")
    _check_version(root)
    meta = _mapping(root.get("meta"), "meta")
    hosting_raw = _mapping(root.get("hosting_instance_hour_usd"), "hosting_instance_hour_usd")
    if not hosting_raw:
        raise CatalogError("hosting_instance_hour_usd must not be empty")

    hosting: dict[str, dict[str, float]] = {}
    for region, entries in hosting_raw.items():
        values = _mapping(entries, f"hosting_instance_hour_usd.{region}")
        hosting[region] = {
            str(name): _number(price, f"hosting_instance_hour_usd.{region}.{name}")
            for name, price in values.items()
        }

    serverless: dict[str, dict[int, float]] = {}
    serverless_raw = root.get("serverless_on_demand_second_usd_by_memory_gb") or {}
    for region, entries in _mapping(
        serverless_raw, "serverless_on_demand_second_usd_by_memory_gb"
    ).items():
        where = f"serverless_on_demand_second_usd_by_memory_gb.{region}"
        values = _mapping(entries, where)
        serverless[region] = {
            int(memory): _number(price, f"{where}.{memory}") for memory, price in values.items()
        }

    return Prices(
        publication_date=_str(meta.get("publication_date"), "meta.publication_date"),
        source=_str(meta.get("source"), "meta.source"),
        note=_str(meta.get("note"), "meta.note"),
        hosting_instance_hour_usd=hosting,
        serverless_second_usd_by_memory_gb=serverless,
        origin=origin,
    )


def _check_version(root: Mapping[str, object]) -> None:
    version = root.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise CatalogError("schema_version must be an integer")
    if version != SUPPORTED_SCHEMA_VERSION:
        raise CatalogError(
            f"unsupported schema_version {version}; "
            f"this build understands {SUPPORTED_SCHEMA_VERSION}"
        )


def _fact_str(parent: Mapping[str, object], key: str, where: str) -> str:
    """Read an AWS fact, which must cite documentation."""
    full = f"{where}.{key}"
    entry = _mapping(parent.get(key), full)
    _require_source(entry, full)
    return _str(entry.get("value"), f"{full}.value")


def _fact_number(parent: Mapping[str, object], key: str, where: str) -> float:
    full = f"{where}.{key}"
    entry = _mapping(parent.get(key), full)
    _require_source(entry, full)
    return _number(entry.get("value"), f"{full}.value")


def _judgement(parent: Mapping[str, object], key: str, where: str) -> float:
    """Read a judgement call, which must justify itself with a rationale."""
    full = f"{where}.{key}"
    entry = _mapping(parent.get(key), full)
    rationale = entry.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        raise CatalogError(
            f"{full} must carry a non-empty 'rationale': it is this tool's judgement call, "
            f"not something AWS documents"
        )
    return _number(entry.get("value"), f"{full}.value")


def _require_source(entry: Mapping[str, object], where: str) -> str:
    source = entry.get("source")
    if not isinstance(source, str) or not source.startswith("http"):
        raise CatalogError(f"{where} must carry a 'source' URL pointing at documentation")
    return source


def _mapping(raw: object, where: str) -> Mapping[str, object]:
    if raw is None:
        raise CatalogError(f"{where} is missing")
    if not isinstance(raw, dict):
        raise CatalogError(f"{where} must be a mapping, got {type(raw).__name__}")
    return {str(key): value for key, value in raw.items()}


def _str(raw: object, where: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise CatalogError(f"{where} must be a non-empty string")
    return raw.strip()


def _number(raw: object, where: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise CatalogError(f"{where} must be a number")
    return float(raw)
