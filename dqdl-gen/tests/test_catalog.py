"""Integrity of dqdl.yaml and the loader that enforces its rules.

Two disciplines are asserted here: a DQDL fact must cite documentation, and a judgement call
must justify itself with a rationale.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
import yaml

from dqdl_gen.catalog import Catalog, CatalogError, ConditionKind, load_catalog, parse_catalog
from dqdl_gen.models import Strictness


def test_every_rule_type_cites_a_source(raw_catalog: dict[str, Any]) -> None:
    for name, entry in raw_catalog["rule_types"].items():
        assert entry["source"].startswith("https://"), f"rule_types.{name} has no source URL"


def test_the_data_type_list_cites_a_source(raw_catalog: dict[str, Any]) -> None:
    assert raw_catalog["data_types"]["source"].startswith("https://")


def test_every_judgement_call_carries_a_rationale(raw_catalog: dict[str, Any]) -> None:
    for key, entry in raw_catalog["shape"].items():
        assert entry.get("rationale", "").strip(), f"shape.{key} has no rationale"
    for level, values in raw_catalog["strictness"].items():
        for key, entry in values.items():
            if key == "description":
                continue
            assert entry.get("rationale", "").strip(), f"strictness.{level}.{key} lacks one"


def test_last_verified_is_an_iso_date(catalog: Catalog) -> None:
    assert dt.date.fromisoformat(catalog.last_verified).year >= 2024


def test_string_is_not_an_accepted_column_data_type(catalog: Catalog) -> None:
    """AWS's rule config lists seven types and String is not among them."""
    assert "String" not in catalog.data_types
    assert set(catalog.data_types) == {
        "Boolean",
        "Date",
        "Timestamp",
        "Integer",
        "Double",
        "Float",
        "Long",
    }


def test_every_strictness_level_is_defined(catalog: Catalog) -> None:
    assert set(catalog.tolerances) == set(Strictness)


def test_strictness_levels_are_ordered_from_tight_to_loose(catalog: Catalog) -> None:
    """A lenient rule must never be tighter than a strict one."""
    strict = catalog.tolerance(Strictness.STRICT)
    balanced = catalog.tolerance(Strictness.BALANCED)
    lenient = catalog.tolerance(Strictness.LENIENT)
    for attribute in (
        "completeness_margin",
        "numeric_padding",
        "length_padding",
        "row_count_tolerance",
    ):
        values = [getattr(level, attribute) for level in (strict, balanced, lenient)]
        assert values == sorted(values), f"{attribute} is not ordered: {values}"


def test_condition_kinds_are_enforced(catalog: Catalog) -> None:
    assert catalog.rule_type("IsComplete").accepts(ConditionKind.NONE)
    assert not catalog.rule_type("IsComplete").accepts(ConditionKind.NUMBER)
    assert catalog.rule_type("RowCount").accepts(ConditionKind.NUMBER)
    assert not catalog.rule_type("RowCount").accepts(ConditionKind.STRING)
    assert catalog.rule_type("ColumnValues").accepts(ConditionKind.STRING)
    assert catalog.rule_type("ColumnValues").accepts(ConditionKind.NUMBER)
    assert not catalog.rule_type("ColumnValues").accepts(ConditionKind.NONE)


def test_an_unknown_rule_type_is_refused(catalog: Catalog) -> None:
    with pytest.raises(CatalogError, match="is not a DQDL rule type this tool emits"):
        catalog.rule_type("Frobnicate")


def test_load_from_an_explicit_path(tmp_path: Path, raw_catalog: dict[str, Any]) -> None:
    target = tmp_path / "dqdl.yaml"
    target.write_text(yaml.safe_dump(raw_catalog), encoding="utf-8")
    loaded = load_catalog(target)
    assert loaded.origin == str(target)


def test_a_missing_file_is_reported_clearly(tmp_path: Path) -> None:
    with pytest.raises(CatalogError, match="catalog file not found"):
        load_catalog(tmp_path / "nope.yaml")


def test_invalid_yaml_is_reported_clearly(tmp_path: Path) -> None:
    target = tmp_path / "dqdl.yaml"
    target.write_text("key: [unclosed\n", encoding="utf-8")
    with pytest.raises(CatalogError, match="not valid YAML"):
        load_catalog(target)


def test_unsupported_schema_version_is_rejected(raw_catalog: dict[str, Any]) -> None:
    raw_catalog["schema_version"] = 99
    with pytest.raises(CatalogError, match="unsupported schema_version"):
        parse_catalog(raw_catalog)


def test_a_rule_type_without_a_source_is_rejected(raw_catalog: dict[str, Any]) -> None:
    del raw_catalog["rule_types"]["RowCount"]["source"]
    with pytest.raises(CatalogError, match="must carry a 'source' URL"):
        parse_catalog(raw_catalog)


def test_a_judgement_without_a_rationale_is_rejected(raw_catalog: dict[str, Any]) -> None:
    del raw_catalog["strictness"]["balanced"]["completeness_margin"]["rationale"]
    with pytest.raises(CatalogError, match="must carry a non-empty 'rationale'"):
        parse_catalog(raw_catalog)


def test_a_shape_judgement_without_a_rationale_is_rejected(
    raw_catalog: dict[str, Any],
) -> None:
    del raw_catalog["shape"]["key_candidate_kinds"]["rationale"]
    with pytest.raises(CatalogError, match="must carry a non-empty 'rationale'"):
        parse_catalog(raw_catalog)


def test_an_unknown_column_kind_is_rejected(raw_catalog: dict[str, Any]) -> None:
    raw_catalog["shape"]["key_candidate_kinds"]["value"] = ["integer", "telepathic"]
    with pytest.raises(CatalogError, match="is not a column kind"):
        parse_catalog(raw_catalog)


def test_an_unknown_condition_kind_is_rejected(raw_catalog: dict[str, Any]) -> None:
    raw_catalog["rule_types"]["RowCount"]["condition"] = "vibes"
    with pytest.raises(CatalogError, match="condition must be one of"):
        parse_catalog(raw_catalog)


def test_a_missing_strictness_level_is_rejected(raw_catalog: dict[str, Any]) -> None:
    del raw_catalog["strictness"]["lenient"]
    with pytest.raises(CatalogError, match=r"strictness\.lenient is missing"):
        parse_catalog(raw_catalog)


def test_an_unknown_strictness_level_is_rejected(raw_catalog: dict[str, Any]) -> None:
    raw_catalog["strictness"]["paranoid"] = raw_catalog["strictness"]["strict"]
    with pytest.raises(CatalogError, match="unknown levels"):
        parse_catalog(raw_catalog)


def test_empty_rule_types_are_rejected(raw_catalog: dict[str, Any]) -> None:
    raw_catalog["rule_types"] = {}
    with pytest.raises(CatalogError, match="rule_types must not be empty"):
        parse_catalog(raw_catalog)


def test_a_non_mapping_document_is_rejected() -> None:
    with pytest.raises(CatalogError, match="must be a mapping"):
        parse_catalog(["not", "a", "mapping"])


def test_errors_name_the_file_they_came_from(raw_catalog: dict[str, Any]) -> None:
    raw_catalog["schema_version"] = 42
    with pytest.raises(CatalogError, match=r"^my-catalog\.yaml: "):
        parse_catalog(raw_catalog, origin="my-catalog.yaml")
