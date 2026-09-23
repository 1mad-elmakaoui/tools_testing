"""Decide which rules a profile justifies.

Pure: given the same :class:`~dqdl_gen.models.DatasetProfile`, catalogue and strictness, this
always produces the same :class:`~dqdl_gen.models.Ruleset`, and it reads no files.

Two principles run through it. A rule is only generated from a statistic that was actually
measured, so a missing statistic means a missing rule rather than a guessed one. And every
rule is loosened from what was observed by the tolerance for the chosen strictness, because
a ruleset that encodes one file exactly will fail on the next one.
"""

from __future__ import annotations

import math

from dqdl_gen import templates
from dqdl_gen.catalog import Catalog, Tolerances
from dqdl_gen.models import (
    ColumnKind,
    ColumnProfile,
    DatasetProfile,
    Rule,
    Ruleset,
    Strictness,
)

#: Arrow column kinds that map onto a ColumnDataType value DQDL accepts. String is absent
#: on purpose: it is not one of the values the rule type takes.
_DATA_TYPES = {
    ColumnKind.BOOLEAN: "Boolean",
    ColumnKind.INTEGER: "Long",
    ColumnKind.FLOAT: "Double",
    ColumnKind.DATE: "Date",
    ColumnKind.TIMESTAMP: "Timestamp",
}


def generate(
    profile: DatasetProfile,
    catalog: Catalog,
    strictness: Strictness = Strictness.BALANCED,
) -> Ruleset:
    """Build a ruleset from a profile."""
    tolerances = catalog.tolerance(strictness)
    rules: list[Rule] = []

    row_count_rule = _row_count(profile, tolerances)
    if row_count_rule is not None:
        rules.append(row_count_rule)
    rules.append(
        templates.column_count(
            profile.column_count, f"{profile.column_count} columns in the profiled file"
        )
    )

    for column in profile.columns:
        rules.extend(_column_rules(column, profile, catalog, tolerances))

    return Ruleset(profile=profile, strictness=strictness, rules=tuple(rules))


def _row_count(profile: DatasetProfile, tolerances: Tolerances) -> Rule | None:
    """A row-count rule, or None when the file's true row count is unknown.

    After sampling a CSV there is no cheap way to know how many rows the file holds, and a
    rule derived from the sample size would be wrong rather than approximate.
    """
    total = profile.known_row_count
    if total is None or total == 0:
        return None
    tolerance = tolerances.row_count_tolerance
    low = max(0, math.floor(total * (1 - tolerance)))
    high = math.ceil(total * (1 + tolerance))
    return templates.row_count_between(
        low,
        high,
        f"{total:,} rows in the file, allowing {_percent(tolerance)} either way "
        f"at {tolerances.level.value} strictness",
    )


def _column_rules(
    column: ColumnProfile,
    profile: DatasetProfile,
    catalog: Catalog,
    tolerances: Tolerances,
) -> list[Rule]:
    rules: list[Rule] = []
    suffix = _sample_suffix(profile)

    completeness = _completeness(column, tolerances, suffix)
    if completeness is not None:
        rules.append(completeness)

    key = _key_rule(column, catalog, tolerances, suffix)
    if key is not None:
        rules.append(key)

    value_set = _value_set(column, profile, catalog, suffix)
    if value_set is not None:
        rules.extend(value_set)

    numeric = _numeric_range(column, tolerances, suffix)
    if numeric is not None:
        rules.append(numeric)

    length = _length_range(column, tolerances, suffix)
    if length is not None:
        rules.append(length)

    data_type = _data_type(column, catalog, suffix)
    if data_type is not None:
        rules.append(data_type)

    return rules


def _completeness(column: ColumnProfile, tolerances: Tolerances, suffix: str) -> Rule | None:
    if column.row_count == 0:
        return None
    if column.is_complete:
        return templates.is_complete(column.name, f"no nulls in {column.row_count:,} rows{suffix}")
    observed = column.completeness
    threshold = _floor_to_hundredths(observed - tolerances.completeness_margin)
    if threshold <= 0:
        return None
    return templates.completeness_at_least(
        column.name,
        threshold,
        f"{column.non_null_count:,} of {column.row_count:,} rows non-null "
        f"({observed:.3f} observed), less a {tolerances.completeness_margin} margin "
        f"at {tolerances.level.value} strictness{suffix}",
    )


def _key_rule(
    column: ColumnProfile, catalog: Catalog, tolerances: Tolerances, suffix: str
) -> Rule | None:
    """A key rule, only for a column that is plausibly a candidate key.

    Distinctness alone is not enough. In a small file a float measure often has no repeated
    value, and claiming it is unique would fail the first time two rows shared an amount, so
    only the column kinds listed in the catalogue are eligible.
    """
    if column.kind not in catalog.shape.key_candidate_kinds:
        return None
    if not column.is_distinct:
        return _near_unique_rule(column, catalog, tolerances, suffix)
    if column.is_complete:
        return templates.is_primary_key(
            column.name,
            f"all {column.row_count:,} values distinct and non-null, so this looks like "
            f"a key{suffix}",
        )
    return templates.is_unique(
        column.name,
        f"all {column.non_null_count:,} non-null values distinct, but "
        f"{column.null_count:,} rows are null, so this is unique without being a key{suffix}",
    )


def _near_unique_rule(
    column: ColumnProfile, catalog: Catalog, tolerances: Tolerances, suffix: str
) -> Rule | None:
    """A uniqueness rule for a column that is almost, but not quite, a key.

    Usually a key with a handful of duplicates, which is worth watching. A column that is
    not close to unique gets nothing, because a low uniqueness threshold says nothing.
    """
    observed = column.uniqueness
    if observed is None or observed < catalog.shape.min_uniqueness_for_rule:
        return None
    threshold = _floor_to_hundredths(observed - tolerances.uniqueness_margin)
    if threshold <= 0:
        return None
    return templates.uniqueness_at_least(
        column.name,
        threshold,
        f"{column.distinct_count:,} distinct values across {column.non_null_count:,} "
        f"non-null rows ({observed:.3f} observed), so nearly a key; less a "
        f"{tolerances.uniqueness_margin} margin at {tolerances.level.value} "
        f"strictness{suffix}",
    )


def _value_set(
    column: ColumnProfile,
    profile: DatasetProfile,
    catalog: Catalog,
    suffix: str,
) -> list[Rule] | None:
    """Allowed-value and distinct-count rules for a column that looks like a category."""
    shape = catalog.shape
    if column.kind is not ColumnKind.STRING or column.distinct_count is None:
        return None
    if profile.row_count < shape.min_rows_for_value_set:
        return None
    if column.distinct_count > shape.max_allowed_value_set:
        return None
    if column.non_null_count == 0:
        return None
    if column.distinct_count / column.non_null_count > shape.max_distinct_ratio:
        return None
    # Only act when the profiler captured the complete set, not just the frequent values.
    if len(column.top_values) != column.distinct_count:
        return None

    values = [value for value, _ in column.top_values]
    shown = ", ".join(
        f"{value} ({count:,})" for value, count in column.top_values[: shape.top_values_shown]
    )
    more = (
        ""
        if len(column.top_values) <= shape.top_values_shown
        else f", and {len(column.top_values) - shape.top_values_shown} more"
    )
    evidence = (
        f"{column.distinct_count} distinct values across {column.non_null_count:,} "
        f"non-null rows: {shown}{more}{suffix}"
    )
    return [
        templates.column_values_in(column.name, sorted(values), evidence),
        templates.distinct_values_count_between(
            column.name,
            1,
            column.distinct_count,
            f"{column.distinct_count} distinct values observed; this guards against the "
            f"set collapsing, while the allowed-value rule guards against it growing{suffix}",
        ),
    ]


def _numeric_range(column: ColumnProfile, tolerances: Tolerances, suffix: str) -> Rule | None:
    if not column.kind.is_numeric or column.minimum is None or column.maximum is None:
        return None
    span = column.maximum - column.minimum
    # A constant column has no span to pad, so fall back to a share of the value itself;
    # otherwise every constant column would get an immovable rule.
    reference = span if span > 0 else abs(column.minimum)
    padding = reference * tolerances.numeric_padding
    low = column.minimum - padding
    high = column.maximum + padding
    if column.kind is ColumnKind.INTEGER:
        low = math.floor(low)
        high = math.ceil(high)
    described = (
        f"observed {_trim(column.minimum)} to {_trim(column.maximum)}"
        if span > 0
        else f"constant at {_trim(column.minimum)}"
    )
    return templates.column_values_between(
        column.name,
        low,
        high,
        f"{described}, widened by {_percent(tolerances.numeric_padding)} "
        f"at {tolerances.level.value} strictness{suffix}",
    )


def _length_range(column: ColumnProfile, tolerances: Tolerances, suffix: str) -> Rule | None:
    if column.kind is not ColumnKind.STRING:
        return None
    if column.min_length is None or column.max_length is None:
        return None
    padding = tolerances.length_padding
    low = max(0, column.min_length - padding)
    high = column.max_length + padding
    return templates.column_length_between(
        column.name,
        low,
        high,
        f"observed lengths {column.min_length} to {column.max_length} characters, "
        f"padded by {padding} at {tolerances.level.value} strictness{suffix}",
    )


def _data_type(column: ColumnProfile, catalog: Catalog, suffix: str) -> Rule | None:
    data_type = _DATA_TYPES.get(column.kind)
    if data_type is None or data_type not in catalog.data_types:
        return None
    return templates.column_data_type(
        column.name,
        data_type,
        f"read as {column.source_type}{suffix}",
    )


def _sample_suffix(profile: DatasetProfile) -> str:
    if not profile.sampled:
        return ""
    if profile.total_row_count is None:
        return f"; from a sample of {profile.row_count:,} rows"
    return f"; from a sample of {profile.row_count:,} of {profile.total_row_count:,} rows"


def _floor_to_hundredths(value: float) -> float:
    """Round down to two decimals, so a threshold never lands above what was observed."""
    return math.floor(value * 100) / 100


def _percent(fraction: float) -> str:
    return f"{_trim(fraction * 100)}%"


def _trim(value: float) -> str:
    return str(int(value)) if value == int(value) else f"{value:g}"
