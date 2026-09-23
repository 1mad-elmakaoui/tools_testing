"""Load and validate ``limits.yaml``, the single source of truth for documented AWS limits.

This is the only module in the package that performs I/O. Everything it produces is plain
frozen data, so the rules and the engine stay pure and testable.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Generic, TypeVar

import yaml

from sagemaker_inference_picker.models import Option

SUPPORTED_SCHEMA_VERSION = 1
_PACKAGE = "sagemaker_inference_picker"
_DATA_FILE = "data/limits.yaml"

T = TypeVar("T")


class LimitsError(ValueError):
    """Raised when ``limits.yaml`` is missing, malformed or missing a source."""


@dataclass(frozen=True)
class Sourced(Generic[T]):
    """A value together with the AWS documentation page it was verified against."""

    value: T
    source: str
    note: str | None = None


@dataclass(frozen=True)
class Heuristic:
    """A judgement call made by this tool, justified by a rationale rather than a source."""

    value: float
    rationale: str


@dataclass(frozen=True)
class Pattern:
    """A secondary deployment pattern that modifies one of the four options."""

    key: str
    display_name: str
    applies_to: Option
    source: str
    note: str


@dataclass(frozen=True)
class OptionLimits:
    """Everything the rules need to know about one inference option.

    A ``None`` value on a limit means AWS documents no cap for that dimension.
    """

    option: Option
    display_name: str
    summary: str
    max_request_payload_mb: Sourced[float | None]
    max_response_payload_mb: Sourced[float | None]
    max_processing_seconds: Sourced[float | None]
    gpu_supported: Sourced[bool]
    scales_to_zero: Sourced[bool]
    returns_inline_response: Sourced[bool]
    native_completion_notification: Sourced[bool]
    supports_multi_model_endpoint: Sourced[bool]
    facts: Mapping[str, Sourced[float]]


@dataclass(frozen=True)
class LimitsData:
    """The parsed contents of ``limits.yaml``."""

    schema_version: int
    last_verified: str
    verification_note: str
    disclaimer: str
    options: Mapping[Option, OptionLimits]
    patterns: Mapping[str, Pattern]
    heuristics: Mapping[str, Heuristic]
    weights: Mapping[str, Heuristic]
    tie_break_order: tuple[Option, ...]
    tie_break_rationale: str
    origin: str

    def limits_for(self, option: Option) -> OptionLimits:
        """Return the limits for `option`."""
        try:
            return self.options[option]
        except KeyError as exc:  # pragma: no cover - parsing guarantees all four exist
            raise LimitsError(f"no limits defined for option {option.value!r}") from exc

    def heuristic(self, name: str) -> float:
        """Return the numeric value of a named heuristic."""
        try:
            return self.heuristics[name].value
        except KeyError as exc:
            raise LimitsError(f"unknown heuristic {name!r}") from exc

    def weight(self, name: str) -> float:
        """Return the numeric value of a named soft-preference weight."""
        try:
            return self.weights[name].value
        except KeyError as exc:
            raise LimitsError(f"unknown weight {name!r}") from exc

    def pattern(self, name: str) -> Pattern:
        """Return a named secondary deployment pattern."""
        try:
            return self.patterns[name]
        except KeyError as exc:
            raise LimitsError(f"unknown pattern {name!r}") from exc


def load_limits(path: Path | None = None) -> LimitsData:
    """Load limits from `path`, or from the data file bundled with the package."""
    if path is None:
        text = resources.files(_PACKAGE).joinpath(_DATA_FILE).read_text(encoding="utf-8")
        origin = f"{_PACKAGE}/{_DATA_FILE}"
    else:
        if not path.is_file():
            raise LimitsError(f"limits file not found: {path}")
        text = path.read_text(encoding="utf-8")
        origin = str(path)
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise LimitsError(f"{origin}: not valid YAML: {exc}") from exc
    return parse_limits(raw, origin=origin)


def parse_limits(raw: object, origin: str = "<memory>") -> LimitsData:
    """Validate an already-loaded YAML document and build a :class:`LimitsData`."""
    try:
        return _parse(raw, origin)
    except LimitsError as exc:
        raise LimitsError(f"{origin}: {exc}") from exc


def _parse(raw: object, origin: str) -> LimitsData:
    root = _mapping(raw, "document")
    schema_version = _int(root.get("schema_version"), "schema_version")
    if schema_version != SUPPORTED_SCHEMA_VERSION:
        raise LimitsError(
            f"unsupported schema_version {schema_version}; "
            f"this build understands {SUPPORTED_SCHEMA_VERSION}"
        )

    meta = _mapping(root.get("meta"), "meta")
    options = _parse_options(_mapping(root.get("options"), "options"))
    patterns = _parse_patterns(_mapping(root.get("patterns"), "patterns"))
    heuristics_raw = _mapping(root.get("heuristics"), "heuristics")
    weights = _parse_heuristics(_mapping(heuristics_raw.get("weights"), "heuristics.weights"))
    scalars = _parse_heuristics(
        {key: value for key, value in heuristics_raw.items() if key != "weights"}
    )
    tie_break = _mapping(root.get("tie_break_order"), "tie_break_order")

    return LimitsData(
        schema_version=schema_version,
        last_verified=_str(meta.get("last_verified"), "meta.last_verified"),
        verification_note=_str(meta.get("verification_note"), "meta.verification_note"),
        disclaimer=_str(meta.get("disclaimer"), "meta.disclaimer"),
        options=options,
        patterns=patterns,
        heuristics=scalars,
        weights=weights,
        tie_break_order=_parse_tie_break(tie_break.get("value")),
        tie_break_rationale=_str(tie_break.get("rationale"), "tie_break_order.rationale"),
        origin=origin,
    )


def _parse_options(raw: Mapping[str, object]) -> Mapping[Option, OptionLimits]:
    parsed: dict[Option, OptionLimits] = {}
    for option in Option:
        entry = raw.get(option.value)
        if entry is None:
            raise LimitsError(f"options.{option.value} is missing")
        parsed[option] = _parse_option(option, _mapping(entry, f"options.{option.value}"))
    unknown = sorted(set(raw) - {option.value for option in Option})
    if unknown:
        raise LimitsError(f"options contains unknown keys: {', '.join(unknown)}")
    return parsed


def _parse_option(option: Option, raw: Mapping[str, object]) -> OptionLimits:
    where = f"options.{option.value}"
    limits = _mapping(raw.get("limits"), f"{where}.limits")
    caps = _mapping(raw.get("capabilities"), f"{where}.capabilities")
    facts_raw = _mapping(raw.get("facts", {}), f"{where}.facts")
    return OptionLimits(
        option=option,
        display_name=_str(raw.get("display_name"), f"{where}.display_name"),
        summary=_str(raw.get("summary"), f"{where}.summary"),
        max_request_payload_mb=_sourced_optional_number(limits, "max_request_payload_mb", where),
        max_response_payload_mb=_sourced_optional_number(limits, "max_response_payload_mb", where),
        max_processing_seconds=_sourced_optional_number(limits, "max_processing_seconds", where),
        gpu_supported=_sourced_bool(caps, "gpu_supported", where),
        scales_to_zero=_sourced_bool(caps, "scales_to_zero", where),
        returns_inline_response=_sourced_bool(caps, "returns_inline_response", where),
        native_completion_notification=_sourced_bool(caps, "native_completion_notification", where),
        supports_multi_model_endpoint=_sourced_bool(caps, "supports_multi_model_endpoint", where),
        facts={
            name: _sourced_number(facts_raw, name, f"{where}.facts") for name in sorted(facts_raw)
        },
    )


def _parse_patterns(raw: Mapping[str, object]) -> Mapping[str, Pattern]:
    patterns: dict[str, Pattern] = {}
    for key in sorted(raw):
        entry = _mapping(raw[key], f"patterns.{key}")
        applies_to = _str(entry.get("applies_to"), f"patterns.{key}.applies_to")
        try:
            option = Option(applies_to)
        except ValueError as exc:
            raise LimitsError(
                f"patterns.{key}.applies_to is not a known option: {applies_to!r}"
            ) from exc
        patterns[key] = Pattern(
            key=key,
            display_name=_str(entry.get("display_name"), f"patterns.{key}.display_name"),
            applies_to=option,
            source=_source(entry, f"patterns.{key}"),
            note=_str(entry.get("note"), f"patterns.{key}.note"),
        )
    return patterns


def _parse_heuristics(raw: Mapping[str, object]) -> Mapping[str, Heuristic]:
    parsed: dict[str, Heuristic] = {}
    for key in sorted(raw):
        entry = _mapping(raw[key], f"heuristics.{key}")
        rationale = entry.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip():
            raise LimitsError(
                f"heuristics.{key} must carry a non-empty 'rationale': heuristics are this "
                f"tool's judgement calls, not documented AWS limits"
            )
        parsed[key] = Heuristic(
            value=_number(entry.get("value"), f"heuristics.{key}.value"),
            rationale=rationale,
        )
    return parsed


def _parse_tie_break(raw: object) -> tuple[Option, ...]:
    if not isinstance(raw, list):
        raise LimitsError("tie_break_order.value must be a list of option names")
    order: list[Option] = []
    for index, item in enumerate(raw):
        if not isinstance(item, str):
            raise LimitsError(f"tie_break_order.value[{index}] must be a string")
        try:
            order.append(Option(item))
        except ValueError as exc:
            raise LimitsError(
                f"tie_break_order.value[{index}] is not a known option: {item!r}"
            ) from exc
    if set(order) != set(Option):
        missing = sorted(option.value for option in Option if option not in order)
        raise LimitsError(f"tie_break_order.value must list every option; missing: {missing}")
    if len(order) != len(set(order)):
        raise LimitsError("tie_break_order.value must not repeat an option")
    return tuple(order)


def _mapping(raw: object, where: str) -> Mapping[str, object]:
    if not isinstance(raw, dict):
        raise LimitsError(f"{where} must be a mapping, got {type(raw).__name__}")
    for key in raw:
        if not isinstance(key, str):
            raise LimitsError(f"{where} has a non-string key: {key!r}")
    return {str(key): value for key, value in raw.items()}


def _str(raw: object, where: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise LimitsError(f"{where} must be a non-empty string")
    return raw.strip()


def _int(raw: object, where: str) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise LimitsError(f"{where} must be an integer")
    return raw


def _number(raw: object, where: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise LimitsError(f"{where} must be a number")
    return float(raw)


def _source(entry: Mapping[str, object], where: str) -> str:
    source = entry.get("source")
    if not isinstance(source, str) or not source.startswith("http"):
        raise LimitsError(
            f"{where} must carry a 'source' URL pointing at official AWS documentation"
        )
    return source


def _note(entry: Mapping[str, object], where: str) -> str | None:
    note = entry.get("note")
    if note is None:
        return None
    if not isinstance(note, str):
        raise LimitsError(f"{where}.note must be a string")
    return " ".join(note.split())


def _entry(parent: Mapping[str, object], key: str, where: str) -> Mapping[str, object]:
    raw = parent.get(key)
    if raw is None:
        raise LimitsError(f"{where}.{key} is missing")
    return _mapping(raw, f"{where}.{key}")


def _sourced_number(parent: Mapping[str, object], key: str, where: str) -> Sourced[float]:
    entry = _entry(parent, key, where)
    full = f"{where}.{key}"
    return Sourced(
        value=_number(entry.get("value"), f"{full}.value"),
        source=_source(entry, full),
        note=_note(entry, full),
    )


def _sourced_optional_number(
    parent: Mapping[str, object], key: str, where: str
) -> Sourced[float | None]:
    entry = _entry(parent, key, where)
    full = f"{where}.{key}"
    raw_value = entry.get("value")
    value = None if raw_value is None else _number(raw_value, f"{full}.value")
    if value is not None and value <= 0:
        raise LimitsError(f"{full}.value must be positive, got {value}")
    return Sourced(value=value, source=_source(entry, full), note=_note(entry, full))


def _sourced_bool(parent: Mapping[str, object], key: str, where: str) -> Sourced[bool]:
    entry = _entry(parent, key, where)
    full = f"{where}.{key}"
    value = entry.get("value")
    if not isinstance(value, bool):
        raise LimitsError(f"{full}.value must be true or false")
    return Sourced(value=value, source=_source(entry, full), note=_note(entry, full))
