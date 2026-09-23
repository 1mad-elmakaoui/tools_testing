"""Every DQDL rule this tool can emit, in one module.

Keeping the templates together means the set of rule shapes can be read in one sitting, and
that adding a rule type is a single, visible change. Each function returns a
:class:`~dqdl_gen.models.Rule` carrying the evidence comment that justifies it; nothing here
decides *whether* a rule applies, only how it is written. That decision lives in
:mod:`dqdl_gen.generate`.

Rule type names, parameter counts and condition kinds are checked against the catalogue by
:mod:`dqdl_gen.validate` before anything is written out.
"""

from __future__ import annotations

from collections.abc import Sequence

from dqdl_gen import syntax
from dqdl_gen.models import Rule


def row_count_between(low: int, high: int, evidence: str) -> Rule:
    """Rows in the dataset must stay within a range of the profiled count."""
    return Rule(
        rule_type="RowCount",
        condition=f"between {syntax.integer(low)} and {syntax.integer(high)}",
        comment=evidence,
    )


def column_count(count: int, evidence: str) -> Rule:
    """The dataset must keep the same number of columns."""
    return Rule(
        rule_type="ColumnCount",
        condition=f"= {syntax.integer(count)}",
        comment=evidence,
    )


def is_complete(column: str, evidence: str) -> Rule:
    """The column must contain no nulls."""
    return Rule(rule_type="IsComplete", parameters=(column,), comment=evidence)


def completeness_at_least(column: str, threshold: float, evidence: str) -> Rule:
    """The column's non-null fraction must stay at or above a threshold."""
    return Rule(
        rule_type="Completeness",
        parameters=(column,),
        condition=f">= {syntax.number(threshold)}",
        comment=evidence,
    )


def is_primary_key(column: str, evidence: str) -> Rule:
    """The column must stay unique and complete."""
    return Rule(rule_type="IsPrimaryKey", parameters=(column,), comment=evidence)


def is_unique(column: str, evidence: str) -> Rule:
    """Every value in the column must be distinct."""
    return Rule(rule_type="IsUnique", parameters=(column,), comment=evidence)


def uniqueness_at_least(column: str, threshold: float, evidence: str) -> Rule:
    """The column's distinct fraction must stay at or above a threshold."""
    return Rule(
        rule_type="Uniqueness",
        parameters=(column,),
        condition=f">= {syntax.number(threshold)}",
        comment=evidence,
    )


def column_values_in(column: str, values: Sequence[str], evidence: str) -> Rule:
    """The column may only hold values from a fixed set."""
    if not values:
        raise ValueError("an allowed-value rule needs at least one value")
    rendered = ", ".join(syntax.quote(value) for value in values)
    return Rule(
        rule_type="ColumnValues",
        parameters=(column,),
        condition=f"in [{rendered}]",
        comment=evidence,
    )


def column_values_between(column: str, low: float, high: float, evidence: str) -> Rule:
    """The column's values must fall within a numeric range."""
    return Rule(
        rule_type="ColumnValues",
        parameters=(column,),
        condition=f"between {syntax.lower_bound(low)} and {syntax.upper_bound(high)}",
        comment=evidence,
    )


def column_length_between(column: str, low: int, high: int, evidence: str) -> Rule:
    """The column's string lengths must fall within a range."""
    return Rule(
        rule_type="ColumnLength",
        parameters=(column,),
        condition=f"between {syntax.integer(low)} and {syntax.integer(high)}",
        comment=evidence,
    )


def column_data_type(column: str, data_type: str, evidence: str) -> Rule:
    """The column's values must cast to a Spark type.

    The caller is responsible for passing a type DQDL accepts; the catalogue holds the list
    and the validator rejects anything else. Notably "String" is not one of them.
    """
    return Rule(
        rule_type="ColumnDataType",
        parameters=(column,),
        condition=f"= {syntax.quote(data_type)}",
        comment=evidence,
    )


def distinct_values_count_between(column: str, low: int, high: int, evidence: str) -> Rule:
    """The number of distinct values in the column must stay within a range."""
    return Rule(
        rule_type="DistinctValuesCount",
        parameters=(column,),
        condition=f"between {syntax.integer(low)} and {syntax.integer(high)}",
        comment=evidence,
    )
