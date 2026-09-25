"""Load and validate policy.yaml and prices.yaml.

The only module that reads them. It enforces the discipline they document: an AWS fact must
cite documentation, a judgement call must justify itself, and neither may arrive as a bare
number in the code.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from importlib import resources
from pathlib import Path

import yaml

from bedrock_log_lens.match import InferenceScope, resolve_service_name

SUPPORTED_SCHEMA_VERSION = 1
_PACKAGE = "bedrock_log_lens"
_POLICY_FILE = "data/policy.yaml"
_PRICES_FILE = "data/prices.yaml"

#: A rate is quoted per this many tokens.
TOKENS_PER_PRICE_UNIT = 1_000_000


class CatalogError(ValueError):
    """Raised when a data file is missing, malformed, or missing a source or rationale."""


@dataclass(frozen=True)
class AwsFacts:
    """What AWS documents about the log format and the calls this tool may make."""

    schema_type: str
    supported_schema_major: str
    content_fields: tuple[str, ...]
    s3_log_prefix_template: str
    read_only_operations: Mapping[str, frozenset[str]]
    iam_actions: Mapping[str, str]


@dataclass(frozen=True)
class Thresholds:
    """Where this tool draws its lines. None of these is an AWS figure."""

    outlier_percentile: int
    minimum_requests_for_percentiles: int
    burst_window_seconds: int
    burst_request_count: int
    rate_jump_multiple: float
    rate_jump_minimum_requests: int
    repeated_shape_count: int
    baseline_minimum_windows: int
    top_expensive_requests: int

    def overridden(
        self,
        *,
        burst_window: int | None = None,
        burst_count: int | None = None,
        top: int | None = None,
    ) -> Thresholds:
        """A copy with command-line overrides applied, or self when there are none."""
        if burst_window is None and burst_count is None and top is None:
            return self
        return replace(
            self,
            burst_window_seconds=(
                self.burst_window_seconds if burst_window is None else burst_window
            ),
            burst_request_count=(self.burst_request_count if burst_count is None else burst_count),
            top_expensive_requests=self.top_expensive_requests if top is None else top,
        )


@dataclass(frozen=True)
class Policy:
    """The parsed contents of policy.yaml."""

    schema_version: int
    last_verified: str
    verification_note: str
    disclaimer: str
    aws: AwsFacts
    thresholds: Thresholds
    origin: str


@dataclass(frozen=True)
class ModelRates:
    """What one model costs, per million tokens."""

    service_name: str
    input: float | None = None
    output: float | None = None
    cache_read: float | None = None
    cache_write: float | None = None

    def rate_for(self, kind: str) -> float | None:
        """The rate for one token kind, or None when AWS publishes none."""
        return {
            "input": self.input,
            "output": self.output,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
        }.get(kind)


@dataclass(frozen=True)
class Prices:
    """The parsed contents of prices.yaml."""

    publication_date: str
    source: str
    note: str
    #: region -> service name -> rate kind -> USD per million tokens
    rates: Mapping[str, Mapping[str, Mapping[str, float]]]
    #: Model IDs the matching rule cannot reach, mapped by hand.
    model_name_overrides: Mapping[str, str]
    origin: str

    @property
    def regions(self) -> tuple[str, ...]:
        """Regions the price file covers."""
        return tuple(sorted(self.rates))

    def service_names(self, region: str) -> tuple[str, ...]:
        """Every priced model name in a region."""
        return tuple(sorted(self.rates.get(region, {})))

    def rates_for(self, model_id: str, region: str, scope: InferenceScope) -> ModelRates | None:
        """The rates that apply to a model in a region, or None when it is not priced."""
        available = self.rates.get(region)
        if not available:
            return None
        name = resolve_service_name(model_id, tuple(available), self.model_name_overrides)
        if name is None:
            return None

        entry = available[name]
        suffix = "_global" if scope is InferenceScope.GLOBAL else ""

        def pick(kind: str) -> float | None:
            # A global inference profile is billed at the global rate where AWS publishes
            # one; where it does not, the regional rate is what the account is charged.
            value = entry.get(f"{kind}{suffix}")
            return value if value is not None else entry.get(kind)

        return ModelRates(
            service_name=name,
            input=pick("input"),
            output=pick("output"),
            cache_read=pick("cache_read"),
            cache_write=pick("cache_write"),
        )


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
    thresholds = _mapping(root.get("thresholds"), "thresholds")

    return Policy(
        schema_version=SUPPORTED_SCHEMA_VERSION,
        last_verified=_str(meta.get("last_verified"), "meta.last_verified"),
        verification_note=_str(meta.get("verification_note"), "meta.verification_note"),
        disclaimer=_str(meta.get("disclaimer"), "meta.disclaimer"),
        aws=AwsFacts(
            schema_type=_fact_str(aws, "schema_type"),
            supported_schema_major=_fact_str(aws, "supported_schema_major"),
            content_fields=_fact_str_list(aws, "content_fields"),
            s3_log_prefix_template=_fact_str(aws, "s3_log_prefix_template"),
            read_only_operations=_parse_operations(aws),
            iam_actions=_parse_iam_actions(aws),
        ),
        thresholds=Thresholds(
            outlier_percentile=int(_judgement(thresholds, "outlier_percentile")),
            minimum_requests_for_percentiles=int(
                _judgement(thresholds, "minimum_requests_for_percentiles")
            ),
            burst_window_seconds=int(_judgement(thresholds, "burst_window_seconds")),
            burst_request_count=int(_judgement(thresholds, "burst_request_count")),
            rate_jump_multiple=_judgement(thresholds, "rate_jump_multiple"),
            rate_jump_minimum_requests=int(_judgement(thresholds, "rate_jump_minimum_requests")),
            repeated_shape_count=int(_judgement(thresholds, "repeated_shape_count")),
            baseline_minimum_windows=int(_judgement(thresholds, "baseline_minimum_windows")),
            top_expensive_requests=int(_judgement(thresholds, "top_expensive_requests")),
        ),
        origin=origin,
    )


def _parse_operations(aws: Mapping[str, object]) -> Mapping[str, frozenset[str]]:
    where = "aws.read_only_operations"
    entry = _mapping(aws.get("read_only_operations"), where)
    _require_source(entry, where)
    services = _mapping(entry.get("value"), f"{where}.value")
    if not services:
        raise CatalogError(f"{where}.value must not be empty")
    parsed: dict[str, frozenset[str]] = {}
    for service, operations in services.items():
        if not isinstance(operations, list) or not operations:
            raise CatalogError(f"{where}.value.{service} must be a non-empty list")
        parsed[service] = frozenset(str(item) for item in operations)
    return parsed


def _parse_iam_actions(aws: Mapping[str, object]) -> Mapping[str, str]:
    """The IAM action authorising each API operation."""
    where = "aws.iam_actions"
    entry = _mapping(aws.get("iam_actions"), where)
    _require_source(entry, where)
    values = _mapping(entry.get("value"), f"{where}.value")
    if not values:
        raise CatalogError(f"{where}.value must not be empty")
    return {
        str(operation): _str(action, f"{where}.value.{operation}")
        for operation, action in values.items()
    }


def _parse_prices(raw: object, origin: str) -> Prices:
    root = _mapping(raw, "document")
    _check_version(root)
    meta = _mapping(root.get("meta"), "meta")

    rates_raw = _mapping(root.get("rates_usd_per_million_tokens"), "rates_usd_per_million_tokens")
    if not rates_raw:
        raise CatalogError("rates_usd_per_million_tokens must not be empty")

    rates: dict[str, dict[str, dict[str, float]]] = {}
    for region, models in rates_raw.items():
        region_entry = _mapping(models, f"rates_usd_per_million_tokens.{region}")
        rates[str(region)] = {
            str(name): {
                str(kind): _number(price, f"rates.{region}.{name}.{kind}")
                for kind, price in _mapping(values, f"rates.{region}.{name}").items()
            }
            for name, values in region_entry.items()
        }

    overrides_raw = root.get("model_name_overrides") or {}
    overrides: dict[str, str] = {}
    for model_id, entry in _mapping(overrides_raw, "model_name_overrides").items():
        where = f"model_name_overrides.{model_id}"
        mapping = _mapping(entry, where)
        _require_source(mapping, where)
        overrides[str(model_id)] = _str(mapping.get("value"), f"{where}.value")

    return Prices(
        publication_date=_str(meta.get("publication_date"), "meta.publication_date"),
        source=_str(meta.get("source"), "meta.source"),
        note=_str(meta.get("note"), "meta.note"),
        rates=rates,
        model_name_overrides=overrides,
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


def _fact_str(parent: Mapping[str, object], key: str, where: str = "aws") -> str:
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
