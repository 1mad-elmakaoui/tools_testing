"""Print a scan, as a terminal report or as JSON.

Both come from the same objects, so the machine-readable output cannot drift away from what
the table says. The bar chart is the point of the terminal view: skew is a shape, and a
column of bars shows it faster than any statistic.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from typing import Any

from rich.console import Console
from rich.table import Table
from rich.text import Text

from kinesis_skew.models import (
    KeySample,
    ScanResult,
    ShardUtilisation,
    SkewStatistics,
    StreamReport,
    Verdict,
)

_VERDICT_STYLE = {
    Verdict.SKEW: "bold red",
    Verdict.CAPACITY: "bold yellow",
    Verdict.MIXED: "bold magenta",
    Verdict.BURSTY: "bold cyan",
    Verdict.HEALTHY: "green",
    Verdict.NO_SHARD_METRICS: "dim",
    Verdict.NO_TRAFFIC: "dim",
}

#: Widest the bar column is allowed to get, and the narrowest still worth drawing.
_MAX_BAR = 40
_MIN_BAR = 10
#: Shards listed before the rest are summarised. A hot shard is at the top or nowhere.
_SHARDS_SHOWN = 12


def render_result(console: Console, result: ScanResult, show_all: bool) -> None:
    """Print every stream in the scan."""
    reports = result.reports if show_all else list(result.problems)
    if not reports and not result.errors:
        console.print()
        console.print(
            f"[green]Nothing throttled across {len(result.reports)} stream(s) in "
            f"{result.region} over the last {result.lookback_hours:g} hours.[/green]"
        )
        return

    for report in reports:
        render_stream(console, report)

    for stream_name, message in result.errors:
        console.print(f"[red]{stream_name} could not be read:[/red] {message}")


def render_stream(console: Console, report: StreamReport) -> None:
    """Print one stream: the verdict, the evidence, the shards and what to do."""
    console.print()
    console.rule(
        Text.assemble(
            (report.stream_name, "bold"),
            ("  ", ""),
            (report.capacity_mode.value.lower().replace("_", "-"), "dim"),
            ("  ", ""),
            (f"{report.open_shard_count} open shard(s)", "dim"),
        ),
        align="left",
    )

    verdict = report.diagnosis.verdict
    console.print(
        Text.assemble(
            (f"{verdict.value.replace('_', ' ')}", _VERDICT_STYLE.get(verdict, "")),
            ("  ", ""),
            (report.diagnosis.headline, "bold"),
        )
    )
    for line in report.diagnosis.evidence:
        console.print(Text(f"  - {line}", style="dim"))

    if report.utilisation:
        console.print()
        _render_shards(console, report)

    if report.samples:
        console.print()
        _render_samples(console, report.samples, report.sampled_shard_id)

    console.print()
    console.print(Text("What to do", style="bold"))
    for line in report.diagnosis.remedy.splitlines():
        console.print(f"  {line}" if not line.startswith("    ") else line)


def _render_shards(console: Console, report: StreamReport) -> None:
    """A bar per shard, busiest first, so the shape of the traffic is visible at a glance."""
    ordered = sorted(report.utilisation, key=lambda item: item.utilisation, reverse=True)
    shown = ordered[:_SHARDS_SHOWN]
    width = max(_MIN_BAR, min(_MAX_BAR, console.width - 58))

    table = Table(box=None, pad_edge=False, header_style="bold")
    table.add_column("Shard", overflow="fold", min_width=18)
    table.add_column("Busiest minute", width=width)
    table.add_column("of limit", justify="right", no_wrap=True)
    table.add_column("share", justify="right", no_wrap=True)
    table.add_column("throttled", justify="right", no_wrap=True)
    table.add_column("", no_wrap=True)

    for shard in shown:
        table.add_row(
            _short_shard_id(shard.shard_id),
            _bar(shard.utilisation, width),
            f"{shard.utilisation:.0%}",
            f"{shard.share_of_bytes:.0%}",
            f"{shard.throttled_records:,.0f}" if shard.throttled_records else "-",
            _note(shard),
        )
    console.print(table)

    if len(ordered) > len(shown):
        console.print(Text(f"  ... and {len(ordered) - len(shown)} more shard(s)", style="dim"))

    excluded = [shard for shard in ordered if not shard.counted_in_statistics]
    if excluded and report.statistics is not None:
        console.print(
            Text(
                f"  Statistics cover the {report.statistics.shard_count} shard(s) open across "
                f"the whole window; {len(excluded)} other(s) are shown but not ranked.",
                style="dim",
            )
        )
    if report.statistics is not None:
        console.print(f"  {_statistics_line(report.statistics)}")


def _bar(fraction: float, width: int) -> Text:
    """A bar whose colour says how close to the limit the shard came."""
    filled = max(0, min(width, round(fraction * width)))
    style = "red" if fraction >= 0.8 else "yellow" if fraction >= 0.5 else "green"
    bar = Text("█" * filled, style=style)
    bar.append("·" * (width - filled), style="dim")
    return bar


def _note(shard: ShardUtilisation) -> Text:
    if not shard.is_open:
        return Text("closed", style="dim")
    if not shard.counted_in_statistics:
        return Text("not ranked", style="dim")
    if shard.binding_dimension == "records":
        return Text("record-bound", style="dim")
    return Text("")


def _statistics_line(stats: SkewStatistics) -> str:
    return (
        f"[dim]busiest/mean [/dim]{stats.max_to_mean_ratio:.1f}x   "
        f"[dim]hottest share [/dim]{stats.hottest_share:.0%}   "
        f"[dim]Gini [/dim]{stats.gini:.2f}   "
        f"[dim]CV [/dim]{stats.coefficient_of_variation:.2f}"
    )


def _render_samples(console: Console, samples: Sequence[KeySample], shard_id: str | None) -> None:
    console.print(Text(f"Most frequent partition keys sampled from {shard_id}", style="bold"))
    for entry in samples:
        console.print(f"  {entry.share:>6.1%}  {entry.count:>7,}  {entry.partition_key}")
    listed = sum(entry.share for entry in samples)
    if listed < 0.999:
        console.print(Text(f"  {1 - listed:>6.1%}  spread across the remaining keys", style="dim"))
    console.print(
        Text(
            "  Sampled from the records in the shard now, which are not the records that "
            "were rejected earlier. Evidence about which keys dominate, not proof of which "
            "one caused a throttle.",
            style="dim",
        )
    )


def _short_shard_id(shard_id: str) -> str:
    """Drop the constant ``shardId-`` prefix and keep the digits.

    Stripping the leading zeros too would be tidier and would also stop the result being
    something you can paste back into the AWS CLI, which is most of what it is for.
    """
    return shard_id.removeprefix("shardId-")


# ------------------------------------------------------------------------------- JSON


def stream_to_dict(report: StreamReport) -> dict[str, Any]:
    """One stream as plain data."""
    return {
        "stream_name": report.stream_name,
        "region": report.region,
        "capacity_mode": report.capacity_mode.value,
        "open_shard_count": report.open_shard_count,
        "window": {
            "start": report.window_start.isoformat(),
            "end": report.window_end.isoformat(),
            "minutes": report.window_minutes,
        },
        "shard_level_metrics_enabled": list(report.shard_level_metrics),
        "shard_level_metrics_missing": list(report.missing_metrics),
        "resharded_during_window": report.resharded_during_window,
        "diagnosis": {
            "verdict": report.diagnosis.verdict.value,
            "is_problem": report.diagnosis.verdict.is_problem,
            "headline": report.diagnosis.headline,
            "evidence": list(report.diagnosis.evidence),
            "remedy": report.diagnosis.remedy,
        },
        "statistics": _statistics_to_dict(report.statistics),
        "total_throttled_records": report.total_throttled_records,
        "shards": [_shard_to_dict(shard) for shard in report.utilisation],
        "sampled_shard_id": report.sampled_shard_id,
        "sampled_partition_keys": [
            {"partition_key": item.partition_key, "count": item.count, "share": item.share}
            for item in report.samples
        ],
    }


def _statistics_to_dict(stats: SkewStatistics | None) -> Mapping[str, Any] | None:
    if stats is None:
        return None
    return {
        "shards_ranked": stats.shard_count,
        "max_to_mean_ratio": stats.max_to_mean_ratio,
        "coefficient_of_variation": stats.coefficient_of_variation,
        "hottest_share": stats.hottest_share,
        "gini": stats.gini,
        "hottest_shard_id": stats.hottest_shard_id,
        "mean_utilisation": stats.mean_utilisation,
        "max_utilisation": stats.max_utilisation,
    }


def _shard_to_dict(shard: ShardUtilisation) -> Mapping[str, Any]:
    return {
        "shard_id": shard.shard_id,
        "utilisation": shard.utilisation,
        "utilisation_by_bytes": shard.by_bytes,
        "utilisation_by_records": shard.by_records,
        "binding_dimension": shard.binding_dimension,
        "share_of_bytes": shard.share_of_bytes,
        "throttled_records": shard.throttled_records,
        "is_open": shard.is_open,
        "counted_in_statistics": shard.counted_in_statistics,
        "excluded_reason": shard.excluded_reason,
    }


def result_to_dict(result: ScanResult, disclaimer: str) -> dict[str, Any]:
    """The whole scan as plain data."""
    return {
        "scanned_at": result.started_at.astimezone(dt.UTC).isoformat(),
        "region": result.region,
        "lookback_hours": result.lookback_hours,
        "note": disclaimer,
        "counts": {verdict.value: len(result.with_verdict(verdict)) for verdict in Verdict},
        "streams": [stream_to_dict(report) for report in result.reports],
        "errors": [{"stream_name": name, "error": message} for name, message in result.errors],
    }
