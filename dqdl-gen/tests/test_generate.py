"""Rule generation, driven by hand-built profiles so no file is involved."""

from __future__ import annotations

import pytest

from dqdl_gen.catalog import Catalog
from dqdl_gen.emit import render_rule
from dqdl_gen.generate import generate
from dqdl_gen.models import (
    ColumnKind,
    ColumnProfile,
    DatasetProfile,
    Rule,
    Ruleset,
    Strictness,
)


def _column(**overrides: object) -> ColumnProfile:
    base: dict[str, object] = {
        "name": "col",
        "kind": ColumnKind.STRING,
        "source_type": "string",
        "row_count": 1000,
        "null_count": 0,
    }
    base.update(overrides)
    return ColumnProfile(**base)  # type: ignore[arg-type]


def _profile(*columns: ColumnProfile, **overrides: object) -> DatasetProfile:
    base: dict[str, object] = {
        "source_name": "fixture.csv",
        "source_format": "csv",
        "row_count": columns[0].row_count if columns else 1000,
        "columns": columns,
    }
    base.update(overrides)
    return DatasetProfile(**base)  # type: ignore[arg-type]


def _rules(ruleset: Ruleset, rule_type: str) -> list[Rule]:
    return [rule for rule in ruleset.rules if rule.rule_type == rule_type]


def _one(ruleset: Ruleset, rule_type: str) -> Rule:
    found = _rules(ruleset, rule_type)
    assert len(found) == 1, f"expected exactly one {rule_type}, got {len(found)}"
    return found[0]


# ------------------------------------------------------------------------- completeness


def test_a_complete_column_gets_is_complete(catalog: Catalog) -> None:
    ruleset = generate(_profile(_column(null_count=0)), catalog)
    assert _one(ruleset, "IsComplete").parameters == ("col",)
    assert not _rules(ruleset, "Completeness")


def test_the_documented_completeness_example(catalog: Catalog) -> None:
    """The brief's worked example: an observed 0.992 becomes a rule at 0.98."""
    column = _column(row_count=1000, null_count=8)
    assert column.completeness == pytest.approx(0.992)
    ruleset = generate(_profile(column), catalog, Strictness.BALANCED)
    assert render_rule(_one(ruleset, "Completeness")) == 'Completeness "col" >= 0.98'


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        (Strictness.STRICT, 'Completeness "col" >= 0.99'),
        (Strictness.BALANCED, 'Completeness "col" >= 0.98'),
        (Strictness.LENIENT, 'Completeness "col" >= 0.94'),
    ],
)
def test_completeness_loosens_with_strictness(
    catalog: Catalog, level: Strictness, expected: str
) -> None:
    ruleset = generate(_profile(_column(row_count=1000, null_count=8)), catalog, level)
    assert render_rule(_one(ruleset, "Completeness")) == expected


def test_a_threshold_that_rounds_to_zero_produces_no_rule(catalog: Catalog) -> None:
    """A column that is almost entirely null gets no completeness rule, not one at 0."""
    ruleset = generate(_profile(_column(row_count=1000, null_count=1000)), catalog)
    assert not _rules(ruleset, "Completeness")
    assert not _rules(ruleset, "IsComplete")


# ----------------------------------------------------------------------------- keys


def test_a_distinct_complete_integer_column_is_a_primary_key(catalog: Catalog) -> None:
    column = _column(kind=ColumnKind.INTEGER, source_type="int64", distinct_count=1000)
    assert _one(generate(_profile(column), catalog), "IsPrimaryKey").parameters == ("col",)


def test_a_distinct_column_with_nulls_is_unique_but_not_a_key(catalog: Catalog) -> None:
    column = _column(kind=ColumnKind.STRING, row_count=1000, null_count=10, distinct_count=990)
    ruleset = generate(_profile(column), catalog)
    assert _rules(ruleset, "IsUnique")
    assert not _rules(ruleset, "IsPrimaryKey")


def test_a_distinct_float_measure_is_not_treated_as_a_key(catalog: Catalog) -> None:
    """Distinctness alone is not evidence of a key: amounts often differ by accident."""
    column = _column(
        kind=ColumnKind.FLOAT,
        source_type="double",
        distinct_count=1000,
        minimum=1.0,
        maximum=99.0,
    )
    ruleset = generate(_profile(column), catalog)
    assert not _rules(ruleset, "IsPrimaryKey")
    assert not _rules(ruleset, "IsUnique")


def test_a_repeated_column_gets_no_key_rule(catalog: Catalog) -> None:
    column = _column(kind=ColumnKind.INTEGER, distinct_count=4)
    ruleset = generate(_profile(column), catalog)
    assert not _rules(ruleset, "IsPrimaryKey")
    assert not _rules(ruleset, "IsUnique")


# ------------------------------------------------------------------------ value sets


def _categorical(**overrides: object) -> ColumnProfile:
    base: dict[str, object] = {
        "kind": ColumnKind.STRING,
        "row_count": 1000,
        "null_count": 0,
        "distinct_count": 3,
        "top_values": (("NEW", 400), ("PAID", 400), ("VOID", 200)),
    }
    base.update(overrides)
    return _column(**base)


def test_a_low_cardinality_column_gets_an_allowed_value_set(catalog: Catalog) -> None:
    ruleset = generate(_profile(_categorical()), catalog)
    assert render_rule(_one(ruleset, "ColumnValues")) == (
        'ColumnValues "col" in ["NEW", "PAID", "VOID"]'
    )


def test_the_value_set_is_sorted_so_output_is_stable(catalog: Catalog) -> None:
    unsorted = _categorical(top_values=(("VOID", 400), ("NEW", 400), ("PAID", 200)))
    assert render_rule(_one(generate(_profile(unsorted), catalog), "ColumnValues")) == (
        'ColumnValues "col" in ["NEW", "PAID", "VOID"]'
    )


def test_a_value_set_is_paired_with_a_distinct_count_rule(catalog: Catalog) -> None:
    ruleset = generate(_profile(_categorical()), catalog)
    assert render_rule(_one(ruleset, "DistinctValuesCount")) == (
        'DistinctValuesCount "col" between 1 and 3'
    )


def test_too_few_rows_means_no_value_set(catalog: Catalog) -> None:
    """Below the row threshold there is not enough evidence the set is complete."""
    small = _categorical(row_count=10, top_values=(("A", 5), ("B", 5)), distinct_count=2)
    ruleset = generate(_profile(small), catalog)
    assert not _rules(ruleset, "ColumnValues")


def test_too_many_distinct_values_means_no_value_set(catalog: Catalog) -> None:
    many = catalog.shape.max_allowed_value_set + 1
    column = _categorical(distinct_count=many, top_values=tuple((f"v{i}", 1) for i in range(many)))
    assert not _rules(generate(_profile(column), catalog), "ColumnValues")


def test_a_high_distinct_ratio_means_no_value_set(catalog: Catalog) -> None:
    """Few rows and almost as many distinct values is not a category."""
    column = _categorical(
        row_count=60, distinct_count=20, top_values=tuple((f"v{i}", 3) for i in range(20))
    )
    assert not _rules(generate(_profile(column), catalog), "ColumnValues")


def test_a_partial_value_set_is_never_used(catalog: Catalog) -> None:
    """If the profiler only captured frequent values, no allowed-value rule is generated."""
    column = _categorical(distinct_count=5, top_values=(("NEW", 400), ("PAID", 400)))
    assert not _rules(generate(_profile(column), catalog), "ColumnValues")


# --------------------------------------------------------------------- numeric ranges


def test_a_numeric_range_is_padded(catalog: Catalog) -> None:
    column = _column(kind=ColumnKind.FLOAT, source_type="double", minimum=0.0, maximum=100.0)
    ruleset = generate(_profile(column), catalog, Strictness.BALANCED)
    assert render_rule(_one(ruleset, "ColumnValues")) == ('ColumnValues "col" between -5 and 105')


def test_a_strict_numeric_range_is_not_padded(catalog: Catalog) -> None:
    column = _column(kind=ColumnKind.FLOAT, source_type="double", minimum=2.5, maximum=7.5)
    ruleset = generate(_profile(column), catalog, Strictness.STRICT)
    assert render_rule(_one(ruleset, "ColumnValues")) == 'ColumnValues "col" between 2.5 and 7.5'


def test_an_integer_range_stays_integral(catalog: Catalog) -> None:
    column = _column(kind=ColumnKind.INTEGER, source_type="int64", minimum=10.0, maximum=20.0)
    condition = _one(generate(_profile(column), catalog), "ColumnValues").condition
    assert condition is not None
    assert "." not in condition


def test_a_constant_numeric_column_still_gets_room(catalog: Catalog) -> None:
    """With no span to pad, the padding is taken from the value itself."""
    column = _column(kind=ColumnKind.FLOAT, source_type="double", minimum=100.0, maximum=100.0)
    ruleset = generate(_profile(column), catalog, Strictness.BALANCED)
    assert render_rule(_one(ruleset, "ColumnValues")) == ('ColumnValues "col" between 95 and 105')


def test_no_range_without_measured_bounds(catalog: Catalog) -> None:
    column = _column(kind=ColumnKind.FLOAT, source_type="double", minimum=None, maximum=None)
    assert not _rules(generate(_profile(column), catalog), "ColumnValues")


# ---------------------------------------------------------------------- string lengths


def test_string_lengths_are_padded(catalog: Catalog) -> None:
    column = _column(min_length=3, max_length=10)
    ruleset = generate(_profile(column), catalog, Strictness.BALANCED)
    assert render_rule(_one(ruleset, "ColumnLength")) == 'ColumnLength "col" between 2 and 11'


def test_a_length_lower_bound_never_goes_negative(catalog: Catalog) -> None:
    column = _column(min_length=0, max_length=4)
    ruleset = generate(_profile(column), catalog, Strictness.LENIENT)
    assert render_rule(_one(ruleset, "ColumnLength")) == 'ColumnLength "col" between 0 and 9'


# ------------------------------------------------------------------------- data types


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (ColumnKind.INTEGER, "Long"),
        (ColumnKind.FLOAT, "Double"),
        (ColumnKind.BOOLEAN, "Boolean"),
        (ColumnKind.DATE, "Date"),
        (ColumnKind.TIMESTAMP, "Timestamp"),
    ],
)
def test_data_type_rules(catalog: Catalog, kind: ColumnKind, expected: str) -> None:
    ruleset = generate(_profile(_column(kind=kind)), catalog)
    assert render_rule(_one(ruleset, "ColumnDataType")) == f'ColumnDataType "col" = "{expected}"'


def test_string_columns_get_no_data_type_rule(catalog: Catalog) -> None:
    """DQDL's ColumnDataType does not accept "String", so emitting one would be invalid."""
    ruleset = generate(_profile(_column(kind=ColumnKind.STRING)), catalog)
    assert not _rules(ruleset, "ColumnDataType")


def test_an_unclassifiable_column_gets_no_data_type_rule(catalog: Catalog) -> None:
    ruleset = generate(_profile(_column(kind=ColumnKind.OTHER)), catalog)
    assert not _rules(ruleset, "ColumnDataType")


# ------------------------------------------------------------------------- row counts


def test_row_count_is_padded_by_the_tolerance(catalog: Catalog) -> None:
    ruleset = generate(_profile(_column(row_count=1000)), catalog, Strictness.BALANCED)
    assert render_rule(_one(ruleset, "RowCount")) == "RowCount between 800 and 1200"


def test_no_row_count_rule_when_the_total_is_unknown(catalog: Catalog) -> None:
    """After sampling a CSV the real total is unknown, so a rule would be a guess."""
    profile = _profile(_column(row_count=100), sampled=True, total_row_count=None)
    ruleset = generate(profile, catalog)
    assert not _rules(ruleset, "RowCount")
    assert profile.row_count_is_known is False


def test_a_sampled_parquet_still_gets_a_row_count_rule(catalog: Catalog) -> None:
    profile = _profile(_column(row_count=100), sampled=True, total_row_count=5000)
    ruleset = generate(profile, catalog, Strictness.BALANCED)
    assert render_rule(_one(ruleset, "RowCount")) == "RowCount between 4000 and 6000"


def test_column_count_is_always_generated(catalog: Catalog) -> None:
    ruleset = generate(_profile(_column(name="a"), _column(name="b")), catalog)
    assert render_rule(_one(ruleset, "ColumnCount")) == "ColumnCount = 2"


# --------------------------------------------------------------------------- evidence


def test_every_rule_carries_evidence(catalog: Catalog, orders_profile: DatasetProfile) -> None:
    ruleset = generate(orders_profile, catalog)
    assert ruleset.rules
    for rule in ruleset.rules:
        assert rule.comment.strip(), f"{rule.rule_type} has no evidence comment"


def test_evidence_names_the_sample_when_one_was_used(catalog: Catalog) -> None:
    profile = _profile(_column(row_count=100), sampled=True, total_row_count=5000)
    ruleset = generate(profile, catalog)
    completeness = _one(ruleset, "IsComplete")
    assert "sample of 100 of 5,000 rows" in completeness.comment


def test_generation_is_deterministic(catalog: Catalog, orders_profile: DatasetProfile) -> None:
    first = generate(orders_profile, catalog)
    second = generate(orders_profile, catalog)
    assert [render_rule(rule) for rule in first.rules] == [
        render_rule(rule) for rule in second.rules
    ]


# ------------------------------------------------------------------ near-unique columns


def test_a_nearly_unique_column_gets_a_uniqueness_rule(catalog: Catalog) -> None:
    """A key with a handful of duplicates is worth watching."""
    column = _column(kind=ColumnKind.STRING, row_count=1000, null_count=0, distinct_count=990)
    ruleset = generate(_profile(column), catalog, Strictness.BALANCED)
    assert render_rule(_one(ruleset, "Uniqueness")) == 'Uniqueness "col" >= 0.98'


def test_a_column_far_from_unique_gets_no_uniqueness_rule(catalog: Catalog) -> None:
    column = _column(kind=ColumnKind.STRING, row_count=1000, distinct_count=4)
    assert not _rules(generate(_profile(column), catalog), "Uniqueness")


def test_a_near_unique_float_is_still_not_a_key(catalog: Catalog) -> None:
    column = _column(
        kind=ColumnKind.FLOAT, source_type="double", row_count=1000, distinct_count=990
    )
    assert not _rules(generate(_profile(column), catalog), "Uniqueness")


def test_uniqueness_is_not_emitted_alongside_a_key_rule(catalog: Catalog) -> None:
    column = _column(kind=ColumnKind.INTEGER, source_type="int64", distinct_count=1000)
    ruleset = generate(_profile(column), catalog)
    assert _rules(ruleset, "IsPrimaryKey")
    assert not _rules(ruleset, "Uniqueness")
