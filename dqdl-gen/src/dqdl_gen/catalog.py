"""Load and validate ``dqdl.yaml``, the single source of truth for this tool.

The only module that reads that file. It enforces the discipline the file documents: a DQDL
fact must cite AWS documentation, and a judgement call must carry a rationale.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from importlib import resources
from pathlib import Path

import yaml

from dqdl_gen.models import ColumnKind, Strictness

SUPPORTED_SCHEMA_VERSION = 1
_PACKAGE = "dqdl_gen"
_DATA_FILE = "data/dqdl.yaml"


class CatalogError(ValueError):
    """Raised when ``dqdl.yaml`` is missing, malformed, or missing a source or rationale."""


class ConditionKind(StrEnum):
    """What sort of condition a rule type accepts after its parameters."""

    NONE = "none"
    NUMBER = "number"
    STRING = "string"
    ANY = "any"


@dataclass(frozen=True)
class RuleTypeSpec:
    """One DQDL rule type this tool is allowed to emit."""

    name: str
    parameters: int
    condition: ConditionKind
    scope: str
    description: str
    source: str

    def accepts(self, condition: ConditionKind) -> bool:
        """Whether a condition of this kind may follow this rule type."""
        if self.condition is ConditionKind.NONE:
            return condition is ConditionKind.NONE
        if self.condition is ConditionKind.ANY:
            return condition is not ConditionKind.NONE
        return condition is self.condition


@dataclass(frozen=True)
class Tolerances:
    """How much room one strictness level leaves for normal variation."""

    level: Strictness
    description: str
    completeness_margin: float
    uniqueness_margin: float
    numeric_padding: float
    length_padding: int
    row_count_tolerance: float


@dataclass(frozen=True)
class Shape:
    """Thresholds deciding whether a column looks like a category, a key, or free text."""

    max_allowed_value_set: int
    max_distinct_ratio: float
    min_rows_for_value_set: int
    top_values_shown: int
    min_uniqueness_for_rule: float
    key_candidate_kinds: frozenset[ColumnKind]


@dataclass(frozen=True)
class Catalog:
    """The parsed contents of ``dqdl.yaml``."""

    schema_version: int
    last_verified: str
    verification_note: str
    disclaimer: str
    grammar_source: str
    reference_source: str
    comment_prefix: str
    rule_types: Mapping[str, RuleTypeSpec]
    data_types: tuple[str, ...]
    shape: Shape
    tolerances: Mapping[Strictness, Tolerances]
    origin: str

    def rule_type(self, name: str) -> RuleTypeSpec:
        """Look up a rule type, refusing anything not in the catalogue."""
        try:
            return self.rule_types[name]
        except KeyError as exc:
            known = ", ".join(sorted(self.rule_types))
            raise CatalogError(
                f"{name!r} is not a DQDL rule type this tool emits. Known: {known}"
            ) from exc

    def tolerance(self, level: Strictness) -> Tolerances:
        """Return the tolerances for a strictness level."""
        try:
            return self.tolerances[level]
        except KeyError as exc:  # pragma: no cover - parsing guarantees all three exist
            raise CatalogError(f"no tolerances defined for {level.value!r}") from exc


def load_catalog(path: Path | None = None) -> Catalog:
    """Load the catalogue from `path`, or from the file bundled with the package."""
    if path is None:
        text = resources.files(_PACKAGE).joinpath(_DATA_FILE).read_text(encoding="utf-8")
        origin = f"{_PACKAGE}/{_DATA_FILE}"
    else:
        if not path.is_file():
            raise CatalogError(f"catalog file not found: {path}")
        text = path.read_text(encoding="utf-8")
        origin = str(path)
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise CatalogError(f"{origin}: not valid YAML: {exc}") from exc
    return parse_catalog(raw, origin=origin)


def parse_catalog(raw: object, origin: str = "<memory>") -> Catalog:
    """Validate an already-loaded YAML document and build a :class:`Catalog`."""
    try:
        return _parse(raw, origin)
    except CatalogError as exc:
        raise CatalogError(f"{origin}: {exc}") from exc


def _parse(raw: object, origin: str) -> Catalog:
    root = _mapping(raw, "document")
    version = _int(root.get("schema_version"), "schema_version")
    if version != SUPPORTED_SCHEMA_VERSION:
        raise CatalogError(
            f"unsupported schema_version {version}; "
            f"this build understands {SUPPORTED_SCHEMA_VERSION}"
        )

    meta = _mapping(root.get("meta"), "meta")
    syntax = _mapping(root.get("syntax"), "syntax")
    comment = _mapping(syntax.get("comment_prefix"), "syntax.comment_prefix")
    _require_source(comment, "syntax.comment_prefix")

    data_types = _mapping(root.get("data_types"), "data_types")
    _require_source(data_types, "data_types")

    return Catalog(
        schema_version=version,
        last_verified=_str(meta.get("last_verified"), "meta.last_verified"),
        verification_note=_str(meta.get("verification_note"), "meta.verification_note"),
        disclaimer=_str(meta.get("disclaimer"), "meta.disclaimer"),
        grammar_source=_str(meta.get("grammar_source"), "meta.grammar_source"),
        reference_source=_str(meta.get("reference_source"), "meta.reference_source"),
        comment_prefix=_str(comment.get("value"), "syntax.comment_prefix.value"),
        rule_types=_parse_rule_types(_mapping(root.get("rule_types"), "rule_types")),
        data_types=_parse_data_types(data_types),
        shape=_parse_shape(_mapping(root.get("shape"), "shape")),
        tolerances=_parse_strictness(_mapping(root.get("strictness"), "strictness")),
        origin=origin,
    )


def _parse_rule_types(raw: Mapping[str, object]) -> Mapping[str, RuleTypeSpec]:
    if not raw:
        raise CatalogError("rule_types must not be empty")
    specs: dict[str, RuleTypeSpec] = {}
    for name in sorted(raw):
        where = f"rule_types.{name}"
        entry = _mapping(raw[name], where)
        condition = _str(entry.get("condition"), f"{where}.condition")
        try:
            kind = ConditionKind(condition)
        except ValueError as exc:
            allowed = ", ".join(item.value for item in ConditionKind)
            raise CatalogError(
                f"{where}.condition must be one of {allowed}, got {condition!r}"
            ) from exc
        parameters = _int(entry.get("parameters"), f"{where}.parameters")
        if parameters < 0:
            raise CatalogError(f"{where}.parameters must not be negative")
        specs[name] = RuleTypeSpec(
            name=name,
            parameters=parameters,
            condition=kind,
            scope=_str(entry.get("scope"), f"{where}.scope"),
            description=_str(entry.get("description"), f"{where}.description"),
            source=_require_source(entry, where),
        )
    return specs


def _parse_data_types(raw: Mapping[str, object]) -> tuple[str, ...]:
    values = raw.get("value")
    if not isinstance(values, list) or not values:
        raise CatalogError("data_types.value must be a non-empty list")
    parsed: list[str] = []
    for index, item in enumerate(values):
        if not isinstance(item, str) or not item.strip():
            raise CatalogError(f"data_types.value[{index}] must be a non-empty string")
        parsed.append(item)
    return tuple(parsed)


def _parse_shape(raw: Mapping[str, object]) -> Shape:
    return Shape(
        max_allowed_value_set=int(_judgement(raw, "max_allowed_value_set", "shape")),
        max_distinct_ratio=_judgement(raw, "max_distinct_ratio", "shape"),
        min_rows_for_value_set=int(_judgement(raw, "min_rows_for_value_set", "shape")),
        top_values_shown=int(_judgement(raw, "top_values_shown", "shape")),
        min_uniqueness_for_rule=_judgement(raw, "min_uniqueness_for_rule", "shape"),
        key_candidate_kinds=_parse_kinds(raw, "key_candidate_kinds", "shape"),
    )


def _parse_kinds(parent: Mapping[str, object], key: str, where: str) -> frozenset[ColumnKind]:
    """Read a judgement call whose value is a list of column kinds."""
    full = f"{where}.{key}"
    entry = _mapping(parent.get(key), full)
    rationale = entry.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        raise CatalogError(f"{full} must carry a non-empty 'rationale'")
    values = entry.get("value")
    if not isinstance(values, list) or not values:
        raise CatalogError(f"{full}.value must be a non-empty list")
    kinds: set[ColumnKind] = set()
    for index, item in enumerate(values):
        if not isinstance(item, str):
            raise CatalogError(f"{full}.value[{index}] must be a string")
        try:
            kinds.add(ColumnKind(item))
        except ValueError as exc:
            allowed = ", ".join(kind.value for kind in ColumnKind)
            raise CatalogError(
                f"{full}.value[{index}] is not a column kind: {item!r}. Allowed: {allowed}"
            ) from exc
    return frozenset(kinds)


def _parse_strictness(raw: Mapping[str, object]) -> Mapping[Strictness, Tolerances]:
    parsed: dict[Strictness, Tolerances] = {}
    for level in Strictness:
        entry = raw.get(level.value)
        if entry is None:
            raise CatalogError(f"strictness.{level.value} is missing")
        where = f"strictness.{level.value}"
        values = _mapping(entry, where)
        parsed[level] = Tolerances(
            level=level,
            description=_str(values.get("description"), f"{where}.description"),
            completeness_margin=_judgement(values, "completeness_margin", where),
            uniqueness_margin=_judgement(values, "uniqueness_margin", where),
            numeric_padding=_judgement(values, "numeric_padding", where),
            length_padding=int(_judgement(values, "length_padding", where)),
            row_count_tolerance=_judgement(values, "row_count_tolerance", where),
        )
    unknown = sorted(set(raw) - {level.value for level in Strictness})
    if unknown:
        raise CatalogError(f"strictness contains unknown levels: {', '.join(unknown)}")
    return parsed


def _judgement(parent: Mapping[str, object], key: str, where: str) -> float:
    """Read a tool judgement call, which must justify itself with a rationale."""
    full = f"{where}.{key}"
    entry = parent.get(key)
    if entry is None:
        raise CatalogError(f"{full} is missing")
    values = _mapping(entry, full)
    rationale = values.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        raise CatalogError(
            f"{full} must carry a non-empty 'rationale': it is this tool's judgement call, "
            f"not something AWS documents"
        )
    return _number(values.get("value"), f"{full}.value")


def _require_source(entry: Mapping[str, object], where: str) -> str:
    source = entry.get("source")
    if not isinstance(source, str) or not source.startswith("http"):
        raise CatalogError(f"{where} must carry a 'source' URL pointing at documentation")
    return source


def _mapping(raw: object, where: str) -> Mapping[str, object]:
    if not isinstance(raw, dict):
        raise CatalogError(f"{where} must be a mapping, got {type(raw).__name__}")
    for key in raw:
        if not isinstance(key, str):
            raise CatalogError(f"{where} has a non-string key: {key!r}")
    return {str(key): value for key, value in raw.items()}


def _str(raw: object, where: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise CatalogError(f"{where} must be a non-empty string")
    return raw.strip()


def _int(raw: object, where: str) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise CatalogError(f"{where} must be an integer")
    return raw


def _number(raw: object, where: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise CatalogError(f"{where} must be a number")
    return float(raw)
