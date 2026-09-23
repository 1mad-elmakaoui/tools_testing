"""The plain data types: validation and derived properties."""

from __future__ import annotations

import pytest

from dqdl_gen.models import ColumnKind, ColumnProfile, DatasetProfile, Rule, Ruleset, Strictness


def _column(**overrides: object) -> ColumnProfile:
    base: dict[str, object] = {
        "name": "col",
        "kind": ColumnKind.STRING,
        "source_type": "string",
        "row_count": 100,
        "null_count": 0,
    }
    base.update(overrides)
    return ColumnProfile(**base)  # type: ignore[arg-type]


def test_a_negative_row_count_is_rejected() -> None:
    with pytest.raises(ValueError, match="row_count must not be negative"):
        _column(row_count=-1)


def test_more_nulls_than_rows_is_rejected() -> None:
    with pytest.raises(ValueError, match="must be between 0 and 100"):
        _column(null_count=101)


def test_a_negative_null_count_is_rejected() -> None:
    with pytest.raises(ValueError, match="must be between 0 and 100"):
        _column(null_count=-1)


def test_completeness_of_an_empty_column() -> None:
    assert _column(row_count=0, null_count=0).completeness == 1.0


def test_completeness_and_non_null_count() -> None:
    column = _column(row_count=100, null_count=25)
    assert column.non_null_count == 75
    assert column.completeness == 0.75
    assert column.is_complete is False


def test_uniqueness_is_none_without_a_distinct_count() -> None:
    assert _column(distinct_count=None).uniqueness is None


def test_uniqueness_is_none_when_everything_is_null() -> None:
    assert _column(row_count=10, null_count=10, distinct_count=0).uniqueness is None


def test_uniqueness_ratio() -> None:
    assert _column(row_count=100, null_count=0, distinct_count=50).uniqueness == 0.5


def test_is_distinct_requires_a_counted_column() -> None:
    assert _column(distinct_count=100).is_distinct is True
    assert _column(distinct_count=99).is_distinct is False
    assert _column(distinct_count=None).is_distinct is False


def test_numeric_kinds() -> None:
    assert ColumnKind.INTEGER.is_numeric
    assert ColumnKind.FLOAT.is_numeric
    assert not ColumnKind.STRING.is_numeric
    assert not ColumnKind.BOOLEAN.is_numeric


def test_duplicate_column_names_are_rejected() -> None:
    with pytest.raises(ValueError, match="column names must be unique"):
        DatasetProfile(
            source_name="x.csv",
            source_format="csv",
            row_count=1,
            columns=(_column(name="a"), _column(name="a")),
        )


def test_a_negative_dataset_row_count_is_rejected() -> None:
    with pytest.raises(ValueError, match="row_count must not be negative"):
        DatasetProfile(source_name="x", source_format="csv", row_count=-1, columns=())


def test_row_count_knowledge() -> None:
    full = DatasetProfile(source_name="x", source_format="csv", row_count=10, columns=())
    assert full.row_count_is_known is True
    assert full.known_row_count == 10

    sampled = DatasetProfile(
        source_name="x", source_format="csv", row_count=10, columns=(), sampled=True
    )
    assert sampled.row_count_is_known is False
    assert sampled.known_row_count is None

    known = DatasetProfile(
        source_name="x",
        source_format="csv",
        row_count=10,
        columns=(),
        sampled=True,
        total_row_count=500,
    )
    assert known.row_count_is_known is True
    assert known.known_row_count == 500


def test_an_empty_rule_type_is_rejected() -> None:
    with pytest.raises(ValueError, match="rule_type must not be empty"):
        Rule(rule_type="")


def test_rules_for_a_column() -> None:
    ruleset = Ruleset(
        profile=DatasetProfile(source_name="x", source_format="csv", row_count=1, columns=()),
        strictness=Strictness.BALANCED,
        rules=(
            Rule("IsComplete", ("a",)),
            Rule("IsComplete", ("b",)),
            Rule("RowCount", (), "> 0"),
        ),
    )
    assert ruleset.rule_count == 3
    assert [rule.parameters for rule in ruleset.rules_for("a")] == [("a",)]
    assert ruleset.rules_for("zzz") == ()


def test_profiles_are_frozen() -> None:
    column = _column()
    with pytest.raises(AttributeError):
        column.name = "other"  # type: ignore[misc]
