"""Render a scan as a table, a JSON document or CSV.

All three read the same :class:`~sagemaker_idle_finder.models.ScanResult`, so they cannot
disagree. Every money column is labelled as an estimate, in the header rather than on each
row, and the footer says which price publication the figures came from.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from rich.console import Console
from rich.table import Table
from rich.text import Text

from sagemaker_idle_finder.models import Finding, Remedy, ScanResult, Verdict

#: Minimum widths the layout reasons about, in characters. They are display choices, not
#: AWS figures, so they live here next to the renderer rather than in the policy file.
_ENDPOINT_MIN_WIDTH = 20
_REMEDY_MIN_WIDTH = 11
_INSTANCES_WIDTH = 17
_VERDICT_WIDTH = 9
_VARIANT_WIDTH = 10
_REGION_WIDTH = 12
_MONEY_WIDTH = 12
#: Rich distributes leftover space its own way, so the estimate above is treated as a floor
#: with a little room to spare rather than an exact prediction.
_LAYOUT_SAFETY_MARGIN = 6

_VERDICT_STYLE = {
    Verdict.IDLE: "bold red",
    Verdict.UNDERUSED: "yellow",
    Verdict.HEALTHY: "green",
    Verdict.NOT_APPLICABLE: "dim",
    Verdict.TOO_NEW: "cyan",
    Verdict.NOT_BILLING: "dim",
}

CSV_COLUMNS = (
    "region",
    "endpoint",
    "variant",
    "status",
    "verdict",
    "instance_type",
    "instance_count",
    "invocations",
    "invocations_per_instance_hour",
    "estimated_monthly_usd",
    "estimated_wasted_monthly_usd",
    "remedy",
    "evidence",
)


def finding_to_dict(finding: Finding) -> dict[str, Any]:
    """Serialise one finding."""
    density = finding.invocations_per_instance_hour
    return {
        "region": finding.endpoint.region,
        "endpoint": finding.endpoint.name,
        "variant": finding.variant.name,
        "status": finding.endpoint.status.value,
        "verdict": finding.verdict.value,
        "is_waste": finding.verdict.is_waste,
        "instance_type": finding.variant.instance_type,
        "instance_count": finding.variant.instance_count,
        "serverless_memory_mb": finding.variant.serverless_memory_mb,
        "uses_inference_components": finding.variant.uses_inference_components,
        "can_scale_to_zero": finding.variant.can_scale_to_zero,
        "created_at": finding.endpoint.created_at.isoformat(),
        "window_coverage": round(finding.window_coverage, 4),
        "invocations": None if finding.window is None else finding.window.total_invocations,
        "invocations_per_instance_hour": None if density is None else round(density, 6),
        "estimated_monthly_usd": finding.cost.monthly_usd,
        "estimated_wasted_monthly_usd": finding.cost.wasted_monthly_usd,
        "instance_hour_usd": finding.cost.instance_hour_usd,
        "no_estimate_reason": finding.cost.no_estimate_reason,
        "estimated_serverless_monthly_usd": finding.cost.serverless_monthly_usd,
        "remedy": finding.remedy.value,
        "evidence": finding.evidence,
    }


def result_to_dict(result: ScanResult) -> dict[str, Any]:
    """Serialise a whole scan."""
    return {
        "scanned_at": result.started_at.isoformat(),
        "regions": list(result.regions),
        "lookback_days": result.lookback_days,
        "estimates": {
            "note": (
                "All money figures are estimates from published on-demand list prices. "
                "They ignore savings plans, reserved capacity, private pricing, data "
                "transfer and storage."
            ),
            "prices_published": result.prices_published,
            "total_estimated_monthly_usd": round(result.total_monthly_usd, 2),
            "total_estimated_wasted_monthly_usd": round(result.total_wasted_monthly_usd, 2),
            "findings_without_a_waste_estimate": len(result.unquantified_waste),
            "totals_are_complete": not result.unquantified_waste,
        },
        "counts": {verdict.value: len(result.with_verdict(verdict)) for verdict in Verdict},
        "findings": [finding_to_dict(finding) for finding in result.findings],
        "errors": [{"region": region, "error": message} for region, message in result.errors],
    }


def result_to_csv(result: ScanResult) -> str:
    """Serialise a scan as CSV, one row per finding."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(CSV_COLUMNS), lineterminator="\n")
    writer.writeheader()
    for finding in result.findings:
        row = finding_to_dict(finding)
        writer.writerow({column: row.get(column, "") for column in CSV_COLUMNS})
    return buffer.getvalue()


def render_result(console: Console, result: ScanResult, show_all: bool) -> None:
    """Print the findings table, sorted by estimated waste, with a total.

    The table adapts to the terminal instead of overflowing it. A narrow window drops the
    columns that can be recovered elsewhere - never the waste figure or the remedy, which
    are the reason to run the command - and says which ones it dropped.
    """
    findings = result.findings if show_all else [f for f in result.findings if f.verdict.is_waste]
    layout = _layout(console, result, findings)

    table = Table(
        title=(
            f"SageMaker endpoints across {', '.join(result.regions)} "
            f"over the last {result.lookback_days} days"
        ),
        title_justify="left",
        title_style="bold",
        header_style="bold",
        show_lines=False,
    )
    table.add_column("Endpoint", overflow="fold", min_width=layout.endpoint_min_width or None)
    if layout.variant:
        table.add_column("Variant", overflow="fold")
    if layout.region:
        table.add_column("Region", no_wrap=True)
    if layout.instances:
        table.add_column("Instances", justify="right", no_wrap=True)
    if layout.invocations:
        table.add_column("Invocations", justify="right", no_wrap=True)
    table.add_column("Verdict", no_wrap=True)
    if layout.monthly:
        table.add_column("est. $/month", justify="right", no_wrap=True)
    table.add_column("est. wasted", justify="right", no_wrap=True)
    table.add_column("Remedy", min_width=_REMEDY_MIN_WIDTH)

    for finding in findings:
        row: list[str | Text] = [finding.endpoint.name]
        if layout.variant:
            row.append(finding.variant.name)
        if layout.region:
            row.append(finding.endpoint.region)
        if layout.instances:
            row.append(_instances(finding))
        if layout.invocations:
            row.append(_invocations(finding))
        row.append(Text(finding.verdict.value, style=_VERDICT_STYLE.get(finding.verdict, "")))
        if layout.monthly:
            row.append(_money(finding.cost.monthly_usd))
        row.append(_money(finding.cost.wasted_monthly_usd))
        row.append(_remedy(finding))
        table.add_row(*row)

    if findings:
        table.add_section()
        total: list[str | Text] = [Text("TOTAL", style="bold")]
        total += [""] * (int(layout.variant) + int(layout.region) + int(layout.instances))
        if layout.invocations:
            total.append("")
        total.append(Text(f"{len(findings)} shown", style="dim"))
        if layout.monthly:
            total.append(Text(_money(result.total_monthly_usd), style="bold"))
        # No "incomplete" marker here: the note under the table says the same thing with
        # room to name which findings it means.
        total.append(Text(_money(result.total_wasted_monthly_usd), style="bold red"))
        total.append("")
        table.add_row(*total)

    console.print()
    console.print(table)
    if not findings:
        console.print("[green]No idle or underused endpoints found.[/green]")

    _render_notes(console, result, layout)


@dataclass(frozen=True)
class _Layout:
    """Which optional columns this terminal has room for."""

    variant: bool
    region: bool
    invocations: bool
    monthly: bool
    instances: bool = True
    #: Zero lets the endpoint name fold on a very narrow terminal. Folding a name is worth
    #: it to keep the waste figure and the remedy on screen.
    endpoint_min_width: int = _ENDPOINT_MIN_WIDTH

    @property
    def dropped(self) -> tuple[str, ...]:
        """Columns left out that a reader might go looking for."""
        names = []
        if not self.invocations:
            names.append("invocations")
        if not self.monthly:
            names.append("estimated monthly cost")
        if not self.instances:
            names.append("instances")
        return tuple(names)


def _layout(console: Console, result: ScanResult, findings: Sequence[Finding]) -> _Layout:
    """Choose the columns, widest first, dropping until the table fits.

    Variant and region come off first because they are often the same value on every row;
    invocations and the monthly cost follow, because both survive in ``--explain`` and in
    the JSON and CSV output. The endpoint, verdict, waste and remedy always stay.
    """
    seen: dict[str, int] = {}
    for finding in findings:
        seen[finding.endpoint.name] = seen.get(finding.endpoint.name, 0) + 1

    layout = _Layout(
        variant=any(count > 1 for count in seen.values()),
        region=len(result.regions) > 1,
        invocations=True,
        monthly=True,
    )
    for candidate in (
        layout,
        replace(layout, invocations=False),
        replace(layout, invocations=False, monthly=False),
        replace(layout, variant=False, region=False, invocations=False, monthly=False),
        _Layout(
            variant=False, region=False, invocations=False, monthly=False, endpoint_min_width=0
        ),
    ):
        if _width_of(candidate) <= console.width:
            return candidate
    return _Layout(
        variant=False,
        region=False,
        invocations=False,
        monthly=False,
        instances=False,
        endpoint_min_width=0,
    )


def _width_of(layout: _Layout) -> int:
    """The width the table needs at its minimum column sizes."""
    columns = [layout.endpoint_min_width, _VERDICT_WIDTH, _MONEY_WIDTH, _REMEDY_MIN_WIDTH]
    if layout.instances:
        columns.append(_INSTANCES_WIDTH)
    if layout.variant:
        columns.append(_VARIANT_WIDTH)
    if layout.region:
        columns.append(_REGION_WIDTH)
    if layout.invocations:
        columns.append(_MONEY_WIDTH)
    if layout.monthly:
        columns.append(_MONEY_WIDTH)
    # Rich pads each column by one space on each side and draws a border between them.
    return sum(columns) + 3 * len(columns) + 1 + _LAYOUT_SAFETY_MARGIN


def _render_notes(console: Console, result: ScanResult, layout: _Layout) -> None:
    counts = {v: len(result.with_verdict(v)) for v in Verdict}
    summary = ", ".join(
        f"{count} {verdict.value.replace('_', ' ')}" for verdict, count in counts.items() if count
    )
    console.print()
    console.print(Text(summary or "nothing found", style="dim"))
    console.print(
        Text(
            "Every money figure is an estimate from on-demand list prices published "
            f"{result.prices_published}. It ignores savings plans, reserved capacity, "
            "private pricing, data transfer and storage.",
            style="dim",
        )
    )
    if result.unquantified_waste:
        names = ", ".join(
            f"{finding.endpoint.name}/{finding.variant.name}"
            for finding in result.unquantified_waste
        )
        console.print(
            Text(
                f"{len(result.unquantified_waste)} wasteful finding(s) have no dollar figure, "
                f"so the total is a floor rather than the whole picture: {names}",
                style="yellow",
            )
        )
    if layout.dropped:
        console.print(
            Text(
                f"This terminal is too narrow for the {' and '.join(layout.dropped)} "
                f"column(s); widen it, or use --explain or --output json for the full detail.",
                style="dim",
            )
        )
    for region, message in result.errors:
        console.print(f"[red]{region} could not be scanned:[/red] {message}")


def render_detail(console: Console, result: ScanResult) -> None:
    """Print the evidence behind every finding that costs money."""
    wasteful = [finding for finding in result.findings if finding.verdict.is_waste]
    if not wasteful:
        return
    console.print()
    console.print(Text("Why", style="bold"))
    for finding in wasteful:
        console.print(
            f"  [bold]{finding.endpoint.name}[/bold] / {finding.variant.name} "
            f"[dim]({finding.endpoint.region})[/dim]"
        )
        console.print(f"    {finding.evidence}")


def _instances(finding: Finding) -> str:
    variant = finding.variant
    if variant.is_serverless:
        return f"serverless {variant.serverless_memory_mb} MB"
    if not variant.instance_type:
        return str(variant.instance_count)
    return f"{variant.instance_count} x {variant.instance_type}"


def _invocations(finding: Finding) -> str:
    if finding.window is None:
        return "-"
    if finding.window.total_invocations is None:
        return "no data"
    return f"{finding.window.total_invocations:,.0f}"


def _money(value: float | None) -> str:
    if value is None:
        return "-"
    return f"${value:,.2f}"


def _remedy(finding: Finding) -> str:
    """The remedy in words, or a dash where there is nothing to do."""
    if finding.remedy is Remedy.NONE:
        return "-"
    return finding.remedy.value.replace("_", " ")
