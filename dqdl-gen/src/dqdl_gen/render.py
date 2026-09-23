"""Rich tables and JSON documents, rendered from the same objects.

Both views read the same profile and ruleset, so ``--output json`` cannot drift away from
what the terminal shows.
"""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.table import Table
from rich.text import Text

from dqdl_gen.catalog import Catalog
from dqdl_gen.emit import render_rule
from dqdl_gen.models import ColumnProfile, DatasetProfile, Ruleset


def profile_to_dict(profile: DatasetProfile) -> dict[str, Any]:
    """Serialise a profile."""
    return {
        "source_name": profile.source_name,
        "source_format": profile.source_format,
        "row_count": profile.row_count,
        "column_count": profile.column_count,
        "sampled": profile.sampled,
        "total_row_count": profile.total_row_count,
        "row_count_is_known": profile.row_count_is_known,
        "columns": [_column_to_dict(column) for column in profile.columns],
    }


def _column_to_dict(column: ColumnProfile) -> dict[str, Any]:
    return {
        "name": column.name,
        "kind": column.kind.value,
        "source_type": column.source_type,
        "row_count": column.row_count,
        "null_count": column.null_count,
        "null_rate": round(1 - column.completeness, 6),
        "completeness": round(column.completeness, 6),
        "distinct_count": column.distinct_count,
        "minimum": column.minimum,
        "maximum": column.maximum,
        "mean": column.mean,
        "stddev": column.stddev,
        "min_length": column.min_length,
        "max_length": column.max_length,
        "top_values": [{"value": value, "count": count} for value, count in column.top_values],
    }


def ruleset_to_dict(ruleset: Ruleset, catalog: Catalog, dqdl: str) -> dict[str, Any]:
    """Serialise a ruleset, including the rendered DQDL."""
    return {
        "strictness": ruleset.strictness.value,
        "rule_count": ruleset.rule_count,
        "catalog": {
            "last_verified": catalog.last_verified,
            "grammar_source": catalog.grammar_source,
            "reference_source": catalog.reference_source,
            "origin": catalog.origin,
        },
        "profile": profile_to_dict(ruleset.profile),
        "rules": [
            {
                "rule_type": rule.rule_type,
                "parameters": list(rule.parameters),
                "condition": rule.condition,
                "evidence": rule.comment,
                "dqdl": render_rule(rule),
            }
            for rule in ruleset.rules
        ],
        "dqdl": dqdl,
    }


def catalog_to_dict(catalog: Catalog) -> dict[str, Any]:
    """Serialise the catalogue: what may be emitted, and how tolerant each level is."""
    return {
        "schema_version": catalog.schema_version,
        "last_verified": catalog.last_verified,
        "verification_note": catalog.verification_note,
        "disclaimer": catalog.disclaimer,
        "grammar_source": catalog.grammar_source,
        "reference_source": catalog.reference_source,
        "origin": catalog.origin,
        "rule_types": {
            name: {
                "parameters": spec.parameters,
                "condition": spec.condition.value,
                "scope": spec.scope,
                "description": spec.description,
                "source": spec.source,
            }
            for name, spec in catalog.rule_types.items()
        },
        "data_types": list(catalog.data_types),
        "shape": {
            "max_allowed_value_set": catalog.shape.max_allowed_value_set,
            "max_distinct_ratio": catalog.shape.max_distinct_ratio,
            "min_rows_for_value_set": catalog.shape.min_rows_for_value_set,
            "top_values_shown": catalog.shape.top_values_shown,
            "key_candidate_kinds": sorted(kind.value for kind in catalog.shape.key_candidate_kinds),
        },
        "strictness": {
            level.value: {
                "description": tolerance.description,
                "completeness_margin": tolerance.completeness_margin,
                "uniqueness_margin": tolerance.uniqueness_margin,
                "numeric_padding": tolerance.numeric_padding,
                "length_padding": tolerance.length_padding,
                "row_count_tolerance": tolerance.row_count_tolerance,
            }
            for level, tolerance in catalog.tolerances.items()
        },
    }


def render_profile(console: Console, profile: DatasetProfile, catalog: Catalog) -> None:
    """Print the profile table that ``--explain`` shows."""
    heading = f"{profile.source_name} · {profile.row_count:,} rows · {profile.column_count} columns"
    if profile.sampled:
        total = (
            "unknown total"
            if profile.total_row_count is None
            else f"of {profile.total_row_count:,}"
        )
        heading += f" · sampled ({total})"

    table = Table(
        title=heading,
        title_justify="left",
        title_style="bold",
        header_style="bold",
        show_lines=False,
    )
    # Eight columns is about as many as stays readable in an 80-column terminal, so range
    # and the two moments are paired up rather than given a column each.
    table.add_column("Column", no_wrap=True)
    table.add_column("Type", no_wrap=True)
    table.add_column("Nulls", justify="right", no_wrap=True)
    table.add_column("Distinct", justify="right", no_wrap=True)
    table.add_column("Range", justify="right")
    table.add_column("Mean / sd", justify="right")
    table.add_column("Len", justify="right", no_wrap=True)
    table.add_column("Top values", max_width=34, overflow="ellipsis")

    shown = catalog.shape.top_values_shown
    for column in profile.columns:
        table.add_row(
            column.name,
            column.kind.value,
            _null_cell(column),
            _maybe(column.distinct_count),
            _range_cell(column),
            _moments_cell(column),
            _length_cell(column),
            _top_cell(column, shown),
        )
    console.print()
    console.print(table)


def _null_cell(column: ColumnProfile) -> Text:
    if column.null_count == 0:
        return Text("0", style="green")
    rate = 1 - column.completeness
    return Text(f"{column.null_count:,} ({rate:.1%})", style="yellow")


def _range_cell(column: ColumnProfile) -> str:
    if column.minimum is None or column.maximum is None:
        return "-"
    return f"{_maybe(column.minimum)} to {_maybe(column.maximum)}"


def _moments_cell(column: ColumnProfile) -> str:
    if column.mean is None:
        return "-"
    if column.stddev is None:
        return _maybe(column.mean)
    return f"{_maybe(column.mean)} / {_maybe(column.stddev)}"


def _length_cell(column: ColumnProfile) -> str:
    if column.min_length is None or column.max_length is None:
        return "-"
    return f"{column.min_length}-{column.max_length}"


def _top_cell(column: ColumnProfile, shown: int) -> str:
    if not column.top_values:
        return "-"
    parts = [f"{value} ({count:,})" for value, count in column.top_values[:shown]]
    if len(column.top_values) > shown:
        parts.append(f"+{len(column.top_values) - shown} more")
    return ", ".join(parts)


def _maybe(value: float | int | None) -> str:
    """Render a statistic, avoiding scientific notation, which reads badly in a table."""
    if value is None:
        return "-"
    if isinstance(value, int) or value == int(value):
        return f"{int(value):,}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def render_catalog(console: Console, catalog: Catalog) -> None:
    """Print the rule types this tool may emit, with their sources."""
    table = Table(
        title=f"DQDL rule types dqdl-gen emits · verified {catalog.last_verified}",
        title_justify="left",
        title_style="bold",
        header_style="bold",
        show_lines=True,
    )
    table.add_column("Rule type", no_wrap=True)
    table.add_column("Params", justify="center")
    table.add_column("Condition", no_wrap=True)
    table.add_column("Scope", no_wrap=True)
    table.add_column("What it checks", overflow="fold")
    for name in sorted(catalog.rule_types):
        spec = catalog.rule_types[name]
        table.add_row(
            name,
            str(spec.parameters),
            spec.condition.value,
            spec.scope,
            f"{spec.description}\n[dim]{spec.source}[/dim]",
        )
    console.print()
    console.print(table)

    console.print()
    console.print(Text("ColumnDataType accepts", style="bold"))
    console.print(f"  {', '.join(catalog.data_types)}")
    console.print(
        "  [dim]Note that String is not among them, so string columns get no "
        "ColumnDataType rule.[/dim]"
    )

    console.print()
    console.print(Text("Strictness levels", style="bold"))
    for level, tolerance in catalog.tolerances.items():
        console.print(f"  [bold]{level.value}[/bold] — {tolerance.description}")
        console.print(
            f"    [dim]completeness margin {tolerance.completeness_margin}, "
            f"numeric padding {tolerance.numeric_padding}, "
            f"length padding {tolerance.length_padding}, "
            f"row count tolerance {tolerance.row_count_tolerance}[/dim]"
        )

    console.print()
    console.print(Text(catalog.verification_note, style="dim"))


def render_summary(console: Console, ruleset: Ruleset, destination: str | None) -> None:
    """Print what was generated and where it went."""
    counts: dict[str, int] = {}
    for rule in ruleset.rules:
        counts[rule.rule_type] = counts.get(rule.rule_type, 0) + 1
    breakdown = ", ".join(f"{name} x{count}" for name, count in sorted(counts.items()))
    console.print()
    console.print(
        Text(
            f"{ruleset.rule_count} rules at {ruleset.strictness.value} strictness",
            style="bold green",
        )
    )
    console.print(f"  {breakdown}")
    if destination is not None:
        console.print(f"  written to [bold]{destination}[/bold]")
    if not ruleset.profile.row_count_is_known:
        console.print(
            "  [yellow]No RowCount rule: the file was sampled and its true row count is "
            "not known, so one would have been a guess.[/yellow]"
        )
