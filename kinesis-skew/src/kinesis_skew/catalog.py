"""Load and validate ``limits.yaml``.

The only module that reads it. It enforces the discipline the file documents: an AWS fact
must cite documentation, a judgement call must justify itself, and neither may arrive as a
bare number.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from importlib import resources
from pathlib import Path

import yaml

SUPPORTED_SCHEMA_VERSION = 1
_PACKAGE = "kinesis_skew"
_LIMITS_FILE = "data/limits.yaml"


class CatalogError(ValueError):
    """Raised when the data file is missing, malformed, or missing a source or rationale."""


@dataclass(frozen=True)
class AwsFacts:
    """What AWS documents. Changing any of these means AWS changed, not that we changed."""

    allowed_operations: Mapping[str, frozenset[str]]
    sampling_operations: Mapping[str, frozenset[str]]
    metric_namespace: str
    incoming_bytes_metric: str
    incoming_records_metric: str
    write_throttle_metric: str
    shard_level_metric_names: tuple[str, ...]
    shard_metric_period_seconds: int
    shard_write_bytes_per_second: int
    shard_write_records_per_second: int
    on_demand_peak_multiple: float
    on_demand_scaling_window_minutes: int
    get_records_calls_per_second_per_shard: int
    get_records_max_records_per_call: int
    cloudwatch_one_minute_retention_days: int

    @property
    def shard_write_bytes_per_minute(self) -> float:
        """The per-shard byte limit over one metric period."""
        return float(self.shard_write_bytes_per_second * self.shard_metric_period_seconds)

    @property
    def shard_write_records_per_minute(self) -> float:
        """The per-shard record limit over one metric period."""
        return float(self.shard_write_records_per_second * self.shard_metric_period_seconds)

    def operations_for(self, *, sampling: bool) -> Mapping[str, frozenset[str]]:
        """The allowlist for this run.

        The sampling operations are only in it when the caller asked to sample, so a run
        without ``--sample-keys`` cannot reach GetRecords at all.
        """
        if not sampling:
            return self.allowed_operations
        merged = {service: set(ops) for service, ops in self.allowed_operations.items()}
        for service, ops in self.sampling_operations.items():
            merged.setdefault(service, set()).update(ops)
        return {service: frozenset(ops) for service, ops in merged.items()}


@dataclass(frozen=True)
class Limits:
    """Documented CloudWatch quotas that shape how requests are batched."""

    max_metric_queries_per_call: int
    max_datapoints_per_call: int


@dataclass(frozen=True)
class Thresholds:
    """Where this tool draws its lines. None of these is an AWS figure."""

    default_lookback_hours: float
    hot_shard_utilisation: float
    skew_max_to_mean_ratio: float
    skew_gini: float
    skew_headroom_utilisation: float
    capacity_utilisation: float
    minimum_shards_for_skew_statistics: int
    minimum_window_coverage: float
    sample_max_get_records_calls: int
    sample_max_records: int
    sample_top_keys_shown: int
    sample_minimum_share_shown: float
    max_retries: int
    retry_base_delay_seconds: float

    def overridden(
        self,
        *,
        hot: float | None = None,
        skew_ratio: float | None = None,
        gini: float | None = None,
    ) -> Thresholds:
        """A copy with command-line overrides applied, or self when there are none."""
        if hot is None and skew_ratio is None and gini is None:
            return self
        return replace(
            self,
            hot_shard_utilisation=self.hot_shard_utilisation if hot is None else hot,
            skew_max_to_mean_ratio=(
                self.skew_max_to_mean_ratio if skew_ratio is None else skew_ratio
            ),
            skew_gini=self.skew_gini if gini is None else gini,
        )


@dataclass(frozen=True)
class Catalog:
    """The parsed contents of ``limits.yaml``."""

    schema_version: int
    last_verified: str
    verification_note: str
    disclaimer: str
    aws: AwsFacts
    limits: Limits
    thresholds: Thresholds
    origin: str


def load_catalog(path: Path | None = None) -> Catalog:
    """Load the catalogue from `path`, or from the file bundled with the package."""
    raw, origin = _read(path)
    try:
        return _parse(raw, origin)
    except CatalogError as exc:
        raise CatalogError(f"{origin}: {exc}") from exc


def _read(path: Path | None) -> tuple[object, str]:
    if path is None:
        text = resources.files(_PACKAGE).joinpath(_LIMITS_FILE).read_text(encoding="utf-8")
        origin = f"{_PACKAGE}/{_LIMITS_FILE}"
    else:
        if not path.is_file():
            raise CatalogError(f"file not found: {path}")
        text = path.read_text(encoding="utf-8")
        origin = str(path)
    try:
        return yaml.safe_load(text), origin
    except yaml.YAMLError as exc:
        raise CatalogError(f"{origin}: not valid YAML: {exc}") from exc


def _parse(raw: object, origin: str) -> Catalog:
    root = _mapping(raw, "document")
    _check_version(root)
    meta = _mapping(root.get("meta"), "meta")
    aws = _mapping(root.get("aws"), "aws")
    limits = _mapping(root.get("limits"), "limits")
    thresholds = _mapping(root.get("thresholds"), "thresholds")

    return Catalog(
        schema_version=SUPPORTED_SCHEMA_VERSION,
        last_verified=_str(meta.get("last_verified"), "meta.last_verified"),
        verification_note=_str(meta.get("verification_note"), "meta.verification_note"),
        disclaimer=_str(meta.get("disclaimer"), "meta.disclaimer"),
        aws=AwsFacts(
            allowed_operations=_parse_operations(aws, "allowed_operations"),
            sampling_operations=_parse_operations(aws, "sampling_operations"),
            metric_namespace=_fact_str(aws, "metric_namespace"),
            incoming_bytes_metric=_fact_str(aws, "incoming_bytes_metric"),
            incoming_records_metric=_fact_str(aws, "incoming_records_metric"),
            write_throttle_metric=_fact_str(aws, "write_throttle_metric"),
            shard_level_metric_names=_fact_str_list(aws, "shard_level_metric_names"),
            shard_metric_period_seconds=int(_fact_number(aws, "shard_metric_period_seconds")),
            shard_write_bytes_per_second=int(_fact_number(aws, "shard_write_bytes_per_second")),
            shard_write_records_per_second=int(_fact_number(aws, "shard_write_records_per_second")),
            on_demand_peak_multiple=_fact_number(aws, "on_demand_peak_multiple"),
            on_demand_scaling_window_minutes=int(
                _fact_number(aws, "on_demand_scaling_window_minutes")
            ),
            get_records_calls_per_second_per_shard=int(
                _fact_number(aws, "get_records_calls_per_second_per_shard")
            ),
            get_records_max_records_per_call=int(
                _fact_number(aws, "get_records_max_records_per_call")
            ),
            cloudwatch_one_minute_retention_days=int(
                _fact_number(aws, "cloudwatch_one_minute_retention_days")
            ),
        ),
        limits=Limits(
            max_metric_queries_per_call=int(
                _fact_number(limits, "max_metric_queries_per_call", "limits")
            ),
            max_datapoints_per_call=int(_fact_number(limits, "max_datapoints_per_call", "limits")),
        ),
        thresholds=Thresholds(
            default_lookback_hours=_judgement(thresholds, "default_lookback_hours"),
            hot_shard_utilisation=_judgement(thresholds, "hot_shard_utilisation"),
            skew_max_to_mean_ratio=_judgement(thresholds, "skew_max_to_mean_ratio"),
            skew_gini=_judgement(thresholds, "skew_gini"),
            skew_headroom_utilisation=_judgement(thresholds, "skew_headroom_utilisation"),
            capacity_utilisation=_judgement(thresholds, "capacity_utilisation"),
            minimum_shards_for_skew_statistics=int(
                _judgement(thresholds, "minimum_shards_for_skew_statistics")
            ),
            minimum_window_coverage=_judgement(thresholds, "minimum_window_coverage"),
            sample_max_get_records_calls=int(
                _judgement(thresholds, "sample_max_get_records_calls")
            ),
            sample_max_records=int(_judgement(thresholds, "sample_max_records")),
            sample_top_keys_shown=int(_judgement(thresholds, "sample_top_keys_shown")),
            sample_minimum_share_shown=_judgement(thresholds, "sample_minimum_share_shown"),
            max_retries=int(_judgement(thresholds, "max_retries")),
            retry_base_delay_seconds=_judgement(thresholds, "retry_base_delay_seconds"),
        ),
        origin=origin,
    )


def _parse_operations(aws: Mapping[str, object], key: str) -> Mapping[str, frozenset[str]]:
    where = f"aws.{key}"
    entry = _mapping(aws.get(key), where)
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


def _check_version(root: Mapping[str, object]) -> None:
    version = root.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise CatalogError("schema_version must be an integer")
    if version != SUPPORTED_SCHEMA_VERSION:
        raise CatalogError(
            f"unsupported schema_version {version}; "
            f"this build understands {SUPPORTED_SCHEMA_VERSION}"
        )


def _fact_str(parent: Mapping[str, object], key: str, where: str = "aws") -> str:
    """Read an AWS fact, which must cite documentation."""
    full = f"{where}.{key}"
    entry = _mapping(parent.get(key), full)
    _require_source(entry, full)
    return _str(entry.get("value"), f"{full}.value")


def _fact_str_list(parent: Mapping[str, object], key: str, where: str = "aws") -> tuple[str, ...]:
    full = f"{where}.{key}"
    entry = _mapping(parent.get(key), full)
    _require_source(entry, full)
    value = entry.get("value")
    if not isinstance(value, list) or not value:
        raise CatalogError(f"{full}.value must be a non-empty list")
    return tuple(_str(item, f"{full}.value") for item in value)


def _fact_number(parent: Mapping[str, object], key: str, where: str = "aws") -> float:
    full = f"{where}.{key}"
    entry = _mapping(parent.get(key), full)
    _require_source(entry, full)
    return _number(entry.get("value"), f"{full}.value")


def _judgement(parent: Mapping[str, object], key: str, where: str = "thresholds") -> float:
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
