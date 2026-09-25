"""Print a report, as a terminal report or as JSON.

Both are built from the same objects, so they cannot drift apart. Neither can print a
prompt: everything here is built from InvocationRecord, which has no field able to hold
one. That is why this module needs no redaction step and no --show-content check.
"""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.table import Table
from rich.text import Text

from bedrock_log_lens.models import (
    Anomaly,
    AnomalyKind,
    GroupTotals,
    IssueKind,
    Report,
    TokenPercentiles,
)

_ANOMALY_STYLE = {
    AnomalyKind.BURST: "yellow",
    AnomalyKind.RATE_JUMP: "magenta",
    AnomalyKind.REPEATED_SHAPE: "bold red",
}

_ANOMALY_LABEL = {
    AnomalyKind.BURST: "burst",
    AnomalyKind.RATE_JUMP: "rate jump",
    AnomalyKind.REPEATED_SHAPE: "repeated shape",
}

#: Rows shown per section before the rest are summarised.
_ROWS = 10


def render_report(console: Console, report: Report, disclaimer: str) -> None:
    """Print the whole report."""
    _render_header(console, report)
    _render_totals(console, report)
    _render_groups(console, "By model", report.by_model, _ROWS)
    _render_groups(console, "By caller identity", report.by_identity, _ROWS)
    _render_groups(console, "By day", report.by_day, _ROWS)
    # Hours are what shows a spike; days are what shows a trend. Both were asked for, and
    # the hourly view is the one that makes an overnight runaway obvious.
    _render_groups(console, "By hour", report.by_hour, _ROWS)
    _render_percentiles(console, report)
    _render_anomalies(console, report)
    _render_expensive(console, report)
    _render_issues(console, report)

    console.print()
    console.print(Text(disclaimer, style="dim"))
    console.print(
        Text(
            "No prompt or response content is read into this report. Use --show-content "
            "with a request id to see one deliberately.",
            style="dim",
        )
    )


def _render_header(console: Console, report: Report) -> None:
    console.print()
    covered = "no records"
    if report.window_start and report.window_end:
        covered = f"{report.window_start:%Y-%m-%d %H:%M} to {report.window_end:%Y-%m-%d %H:%M} UTC"
    console.rule(
        Text.assemble(
            ("Bedrock invocation logs", "bold"),
            ("  ", ""),
            (covered, "dim"),
            ("  ", ""),
            (f"{report.sources_read} file(s)", "dim"),
        ),
        align="left",
    )


def _render_totals(console: Console, report: Report) -> None:
    totals = report.totals
    console.print()
    table = Table(box=None, pad_edge=False, show_header=False)
    table.add_column("", style="dim", no_wrap=True)
    table.add_column("", justify="right", no_wrap=True)
    table.add_row("requests", f"{totals.requests:,}")
    table.add_row("input tokens", f"{totals.input_tokens:,}")
    table.add_row("output tokens", f"{totals.output_tokens:,}")
    if totals.cache_read_tokens or totals.cache_write_tokens:
        table.add_row("cache read tokens", f"{totals.cache_read_tokens:,}")
        table.add_row("cache write tokens", f"{totals.cache_write_tokens:,}")
    table.add_row("est. cost", _money(totals.cost_usd))
    console.print(table)

    if report.unpriced_models:
        console.print(
            Text(
                f"  {totals.unpriced_requests:,} request(s) across "
                f"{len(report.unpriced_models)} model(s) had no published price, so the "
                f"total is a floor: {', '.join(report.unpriced_models[:4])}"
                + (" ..." if len(report.unpriced_models) > 4 else ""),
                style="yellow",
            )
        )


def _render_groups(
    console: Console, title: str, groups: tuple[GroupTotals, ...], limit: int
) -> None:
    if not groups:
        return
    console.print()
    console.print(Text(title, style="bold"))

    table = Table(box=None, pad_edge=False, header_style="dim")
    table.add_column("", overflow="fold", min_width=20)
    table.add_column("requests", justify="right", no_wrap=True)
    table.add_column("input", justify="right", no_wrap=True)
    table.add_column("output", justify="right", no_wrap=True)
    table.add_column("est. cost", justify="right", no_wrap=True)

    for group in groups[:limit]:
        table.add_row(
            _shorten(group.key),
            f"{group.requests:,}",
            f"{group.input_tokens:,}",
            f"{group.output_tokens:,}",
            _money(group.cost_usd) + ("+" if not group.cost_is_complete else ""),
        )
    console.print(table)
    if len(groups) > limit:
        console.print(Text(f"  ... and {len(groups) - limit} more", style="dim"))


def _render_percentiles(console: Console, report: Report) -> None:
    if not report.percentiles_input and not report.percentiles_output:
        return
    console.print()
    console.print(Text("Token distribution per model", style="bold"))

    table = Table(box=None, pad_edge=False, header_style="dim")
    table.add_column("", overflow="fold", min_width=20)
    table.add_column("", no_wrap=True)
    table.add_column("n", justify="right", no_wrap=True)
    table.add_column("p50", justify="right", no_wrap=True)
    table.add_column("p95", justify="right", no_wrap=True)
    table.add_column("p99", justify="right", no_wrap=True)
    table.add_column("max", justify="right", no_wrap=True)
    table.add_column("", no_wrap=True)

    for label, summaries in (
        ("input", report.percentiles_input),
        ("output", report.percentiles_output),
    ):
        for summary in summaries[:_ROWS]:
            table.add_row(
                _shorten(summary.model_id),
                label,
                f"{summary.count:,}",
                f"{summary.p50:,}",
                f"{summary.p95:,}",
                f"{summary.p99:,}",
                f"{summary.maximum:,}",
                _thin_note(summary),
            )
    console.print(table)

    if report.outliers:
        console.print(
            Text(
                f"  {len(report.outliers)} request(s) above their model's p99 — see the JSON "
                f"output for the full list",
                style="dim",
            )
        )


def _thin_note(summary: TokenPercentiles) -> Text:
    if summary.is_meaningful:
        return Text("")
    return Text("thin sample", style="dim")


def _render_anomalies(console: Console, report: Report) -> None:
    console.print()
    console.print(Text("Anomalies", style="bold"))
    if not report.anomalies:
        console.print(Text("  nothing matched the heuristics", style="green"))
        return

    console.print(
        Text(
            "  Heuristics, not proof. A scheduled batch job and a runaway agent look alike "
            "by call rate; these findings name the identity so a person can tell them apart.",
            style="dim",
        )
    )
    for finding in report.anomalies[:_ROWS]:
        _render_anomaly(console, finding)
    if len(report.anomalies) > _ROWS:
        console.print(Text(f"  ... and {len(report.anomalies) - _ROWS} more", style="dim"))


def _render_anomaly(console: Console, finding: Anomaly) -> None:
    console.print(
        Text.assemble(
            ("  ", ""),
            (_ANOMALY_LABEL[finding.kind], _ANOMALY_STYLE.get(finding.kind, "")),
            ("  ", ""),
            (_shorten(finding.identity_arn, 60), "bold"),
        )
    )
    console.print(
        Text(
            f"    {finding.window_start:%Y-%m-%d %H:%M:%S} to "
            f"{finding.window_end:%H:%M:%S} UTC  ·  {finding.request_count:,} requests  ·  "
            f"{_money(finding.cost_usd)}",
            style="dim",
        )
    )
    console.print(Text(f"    {finding.detail}", style="dim"))


def _render_expensive(console: Console, report: Report) -> None:
    if not report.most_expensive:
        return
    console.print()
    console.print(Text("Most expensive requests", style="bold"))

    table = Table(box=None, pad_edge=False, header_style="dim")
    table.add_column("request", overflow="fold", min_width=20)
    table.add_column("model", overflow="fold")
    table.add_column("in", justify="right", no_wrap=True)
    table.add_column("out", justify="right", no_wrap=True)
    table.add_column("est. cost", justify="right", no_wrap=True)

    for record, cost in report.most_expensive:
        table.add_row(
            record.request_id or "<no request id>",
            _shorten(record.model_id),
            f"{record.input_tokens or 0:,}",
            f"{record.output_tokens or 0:,}",
            _money(cost.usd or 0.0),
        )
    console.print(table)
    console.print(Text("  Metadata only. No prompt is read to build this list.", style="dim"))


def _render_issues(console: Console, report: Report) -> None:
    counts = report.issue_counts
    if not counts:
        return
    total = sum(counts.values())
    console.print()
    style = "red" if total > report.totals.requests else "yellow"
    console.print(
        Text(
            f"{total:,} record(s) could not be used and are not in any total above:",
            style=style,
        )
    )
    for kind, count in sorted(counts.items(), key=lambda item: -item[1]):
        console.print(Text(f"  {count:>8,}  {_issue_label(kind)}", style="dim"))


def _issue_label(kind: IssueKind) -> str:
    return kind.value.replace("_", " ")


def _money(amount: float) -> str:
    if amount and abs(amount) < 0.01:
        return f"${amount:,.4f}"
    return f"${amount:,.2f}"


def _shorten(text: str, width: int = 44) -> str:
    """Keep the end of a long identifier: an ARN's tail says who it is."""
    if len(text) <= width:
        return text
    return "…" + text[-(width - 1) :]


# ------------------------------------------------------------------------------- JSON


def report_to_dict(report: Report, disclaimer: str) -> dict[str, Any]:
    """The whole report as plain data.

    Built from the same records as the terminal view, so it carries no content either.
    """
    return {
        "note": disclaimer,
        "privacy": (
            "This report is built from log metadata only. Prompt and response bodies are "
            "discarded at parse time and never reach any output."
        ),
        "window": {
            "start": report.window_start.isoformat() if report.window_start else None,
            "end": report.window_end.isoformat() if report.window_end else None,
        },
        "sources_read": report.sources_read,
        "prices_published": report.prices_published,
        "totals": _group_to_dict(report.totals),
        "cost_is_complete": report.cost_is_complete,
        "unpriced_models": list(report.unpriced_models),
        "by_model": [_group_to_dict(group) for group in report.by_model],
        "by_identity": [_group_to_dict(group) for group in report.by_identity],
        "by_hour": [_group_to_dict(group) for group in report.by_hour],
        "by_day": [_group_to_dict(group) for group in report.by_day],
        "percentiles": {
            "input": [_percentiles_to_dict(item) for item in report.percentiles_input],
            "output": [_percentiles_to_dict(item) for item in report.percentiles_output],
        },
        "outliers": [
            {
                "request_id": item.record.request_id,
                "model_id": item.record.model_id,
                "identity_arn": item.record.identity_arn,
                "timestamp": item.record.timestamp.isoformat(),
                "kind": item.kind,
                "tokens": item.value,
                "model_p99": item.threshold,
                "source": item.record.source,
            }
            for item in report.outliers
        ],
        "anomalies": [
            {
                "kind": item.kind.value,
                "heuristic": True,
                "identity_arn": item.identity_arn,
                "model_id": item.model_id,
                "window_start": item.window_start.isoformat(),
                "window_end": item.window_end.isoformat(),
                "request_count": item.request_count,
                "calls_per_minute": round(item.calls_per_minute, 2),
                "estimated_cost_usd": item.cost_usd,
                "detail": item.detail,
            }
            for item in report.anomalies
        ],
        "most_expensive": [
            {
                "request_id": record.request_id,
                "model_id": record.model_id,
                "identity_arn": record.identity_arn,
                "timestamp": record.timestamp.isoformat(),
                "input_tokens": record.input_tokens,
                "output_tokens": record.output_tokens,
                "estimated_cost_usd": cost.usd,
                "source": record.source,
            }
            for record, cost in report.most_expensive
        ],
        "skipped_records": {
            "total": sum(report.issue_counts.values()),
            "by_reason": {kind.value: count for kind, count in report.issue_counts.items()},
        },
    }


def _group_to_dict(group: GroupTotals) -> dict[str, Any]:
    return {
        "key": group.key,
        "requests": group.requests,
        "input_tokens": group.input_tokens,
        "output_tokens": group.output_tokens,
        "cache_read_tokens": group.cache_read_tokens,
        "cache_write_tokens": group.cache_write_tokens,
        "total_tokens": group.total_tokens,
        "estimated_cost_usd": group.cost_usd,
        "unpriced_requests": group.unpriced_requests,
        "cost_is_complete": group.cost_is_complete,
    }


def _percentiles_to_dict(item: TokenPercentiles) -> dict[str, Any]:
    return {
        "model_id": item.model_id,
        "requests": item.count,
        "p50": item.p50,
        "p95": item.p95,
        "p99": item.p99,
        "max": item.maximum,
        "sample_is_meaningful": item.is_meaningful,
    }
