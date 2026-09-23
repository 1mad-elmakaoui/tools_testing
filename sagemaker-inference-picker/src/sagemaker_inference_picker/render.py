"""Rendering: the same recommendation as a Rich report or as a JSON document.

Both renderers read from the same :class:`Recommendation`, so ``--output json`` can never
drift from what the terminal shows.
"""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from sagemaker_inference_picker import explain, units
from sagemaker_inference_picker.limits import LimitsData, OptionLimits, Sourced
from sagemaker_inference_picker.models import Elimination, Option, Recommendation, Workload

_RECOMMENDED_STYLE = "bold green"
_REJECTED_STYLE = "red"
_CONFLICT_STYLE = "bold red"


# --------------------------------------------------------------------------------------
# JSON
# --------------------------------------------------------------------------------------


def workload_to_dict(workload: Workload) -> dict[str, Any]:
    """Serialise the inputs, so a JSON result records what it was asked."""
    return {
        "payload_mb": workload.payload_mb,
        "response_mb": workload.response_mb,
        "processing_seconds": workload.processing_seconds,
        "traffic": workload.traffic.value,
        "latency_p99_ms": workload.latency_p99_ms,
        "zero_idle_cost": workload.zero_idle_cost,
        "immediate_response": workload.immediate_response,
        "needs_notification": workload.needs_notification,
        "gpu_required": workload.gpu_required,
        "model_count": workload.model_count,
    }


def recommendation_to_dict(recommendation: Recommendation, limits: LimitsData) -> dict[str, Any]:
    """Serialise a recommendation, including every rejection and its source."""
    return {
        "recommended": (recommendation.recommended.value if recommendation.recommended else None),
        "headline": explain.headline(recommendation, limits),
        "resolved": recommendation.resolved,
        "workload": workload_to_dict(recommendation.workload),
        "limits": {
            "last_verified": limits.last_verified,
            "origin": limits.origin,
            "disclaimer": limits.disclaimer,
        },
        "deciding_factors": list(explain.deciding_factors(recommendation, limits)),
        "caveats": list(explain.caveats(recommendation)),
        "runners_up": list(explain.runners_up(recommendation, limits)),
        "ranked": [
            {
                "option": ranked.option.value,
                "display_name": limits.limits_for(ranked.option).display_name,
                "score": ranked.score,
                "reasons": [
                    {
                        "rule_id": reason.rule_id,
                        "weight": reason.weight,
                        "message": reason.message,
                    }
                    for reason in ranked.reasons
                ],
            }
            for ranked in recommendation.ranked
        ],
        "rejected": [
            {
                "option": elimination.option.value,
                "display_name": limits.limits_for(elimination.option).display_name,
                "reasons": [
                    {
                        "constraint_id": item.constraint_id,
                        "requirement": item.requirement,
                        "message": item.message,
                        "limit": item.limit,
                        "actual": item.actual,
                        "source": item.source,
                    }
                    for item in recommendation.eliminations_for(elimination.option)
                ],
            }
            for elimination in _first_elimination_per_option(recommendation)
        ],
        "advice": [
            {
                "id": item.advice_id,
                "title": item.title,
                "message": item.message,
                "source": item.source,
            }
            for item in recommendation.advice
        ],
        "conflicts": [
            {
                "constraint_id": conflict.constraint_id,
                "requirement": conflict.requirement,
                "eliminated": [option.value for option in conflict.eliminated],
            }
            for conflict in recommendation.conflicts
        ],
        "conflict_summary": list(explain.conflict_summary(recommendation, limits)),
    }


def limits_to_dict(limits: LimitsData) -> dict[str, Any]:
    """Serialise the limits data itself, sources and all."""
    return {
        "schema_version": limits.schema_version,
        "last_verified": limits.last_verified,
        "verification_note": limits.verification_note,
        "disclaimer": limits.disclaimer,
        "origin": limits.origin,
        "options": {
            option.value: {
                "display_name": entry.display_name,
                "summary": entry.summary,
                "limits": {
                    "max_request_payload_mb": _sourced(entry.max_request_payload_mb),
                    "max_response_payload_mb": _sourced(entry.max_response_payload_mb),
                    "max_processing_seconds": _sourced(entry.max_processing_seconds),
                },
                "capabilities": {
                    "gpu_supported": _sourced(entry.gpu_supported),
                    "scales_to_zero": _sourced(entry.scales_to_zero),
                    "returns_inline_response": _sourced(entry.returns_inline_response),
                    "native_completion_notification": _sourced(
                        entry.native_completion_notification
                    ),
                    "supports_multi_model_endpoint": _sourced(entry.supports_multi_model_endpoint),
                },
                "facts": {name: _sourced(fact) for name, fact in entry.facts.items()},
            }
            for option, entry in limits.options.items()
        },
        "patterns": {
            key: {
                "display_name": pattern.display_name,
                "applies_to": pattern.applies_to.value,
                "note": pattern.note,
                "source": pattern.source,
            }
            for key, pattern in limits.patterns.items()
        },
        "heuristics": {
            name: {"value": item.value, "rationale": item.rationale}
            for name, item in {**limits.heuristics, **limits.weights}.items()
        },
        "tie_break_order": [option.value for option in limits.tie_break_order],
    }


def _sourced(entry: Sourced[Any]) -> dict[str, Any]:
    return {"value": entry.value, "source": entry.source, "note": entry.note}


def _first_elimination_per_option(recommendation: Recommendation) -> list[Elimination]:
    seen: set[Option] = set()
    ordered: list[Elimination] = []
    for option in Option:
        eliminations = recommendation.eliminations_for(option)
        if eliminations and option not in seen:
            seen.add(option)
            ordered.append(eliminations[0])
    return ordered


# --------------------------------------------------------------------------------------
# Rich
# --------------------------------------------------------------------------------------


def render_recommendation(
    console: Console, recommendation: Recommendation, limits: LimitsData
) -> None:
    """Print the full report: verdict, reasoning, rejections, advice."""
    headline = explain.headline(recommendation, limits)
    resolved = recommendation.resolved
    console.print()
    console.print(
        Panel(
            Text(headline, style=_RECOMMENDED_STYLE if resolved else _CONFLICT_STYLE),
            title="Recommendation" if resolved else "No viable option",
            border_style="green" if resolved else "red",
        )
    )

    _bullets(console, "Why", explain.deciding_factors(recommendation, limits))
    _bullets(console, "Watch out", explain.caveats(recommendation), style="yellow")
    _bullets(console, "Also viable", explain.runners_up(recommendation, limits), style="dim")

    if not resolved:
        _bullets(
            console,
            "Conflicting requirements",
            explain.conflict_summary(recommendation, limits),
            style="red",
        )

    _render_rejections(console, recommendation, limits)
    _render_advice(console, recommendation)

    console.print()
    console.print(
        Text(
            f"Limits last verified {limits.last_verified} · {limits.origin}",
            style="dim",
        )
    )


def _render_rejections(
    console: Console, recommendation: Recommendation, limits: LimitsData
) -> None:
    rejections = explain.rejections(recommendation, limits)
    if not rejections:
        return
    table = Table(
        title="Ruled out",
        title_justify="left",
        title_style="bold",
        show_lines=True,
        expand=True,
        header_style="bold",
    )
    table.add_column("Option", style=_REJECTED_STYLE, no_wrap=True)
    table.add_column("Why it was eliminated", overflow="fold")
    for rejection in rejections:
        reasons = Text()
        for index, reason in enumerate(rejection.reasons):
            if index:
                reasons.append("\n")
            reasons.append(f"• {reason}")
        for source in rejection.sources:
            reasons.append(f"\n{source}", style="dim")
        table.add_row(rejection.display_name, reasons)
    console.print()
    console.print(table)


def _render_advice(console: Console, recommendation: Recommendation) -> None:
    if not recommendation.advice:
        return
    console.print()
    console.print(Text("Also consider", style="bold"))
    for item in recommendation.advice:
        console.print(Padding(Text(item.title, style="bold cyan"), (0, 0, 0, 2)))
        console.print(Padding(Text(item.message), (0, 0, 0, 4)))
        console.print(Padding(Text(item.source, style="dim"), (0, 0, 0, 4)))


def _bullets(console: Console, title: str, lines: tuple[str, ...], style: str = "") -> None:
    """Print a bulleted list whose wrapped lines hang under the bullet."""
    if not lines:
        return
    console.print()
    console.print(Text(title, style="bold"))
    grid = Table.grid(padding=(0, 1))
    grid.add_column(width=3, no_wrap=True, justify="right")
    grid.add_column(overflow="fold")
    for line in lines:
        grid.add_row("•", Text(line, style=style) if style else Text(line))
    console.print(grid)


def render_limits(console: Console, limits: LimitsData) -> None:
    """Print the documented limits behind every decision, with their sources."""
    table = Table(
        title=f"AWS-documented limits · last verified {limits.last_verified}",
        title_justify="left",
        title_style="bold",
        header_style="bold",
        show_lines=True,
    )
    table.add_column("Option", no_wrap=True)
    table.add_column("Max payload", justify="right")
    table.add_column("Max response", justify="right")
    table.add_column("Max time", justify="right")
    table.add_column("GPU", justify="center")
    table.add_column("To zero", justify="center")
    table.add_column("Inline", justify="center")
    table.add_column("Notify", justify="center")

    for option in Option:
        entry: OptionLimits = limits.limits_for(option)
        table.add_row(
            entry.display_name,
            _cell(units.megabytes(entry.max_request_payload_mb.value)),
            _cell(units.megabytes(entry.max_response_payload_mb.value)),
            _cell(units.seconds(entry.max_processing_seconds.value)),
            _tick(entry.gpu_supported.value),
            _tick(entry.scales_to_zero.value),
            _tick(entry.returns_inline_response.value),
            _tick(entry.native_completion_notification.value),
        )
    console.print()
    console.print(table)

    console.print(
        Text(
            '"none" means AWS documents no limit for that dimension. "To zero" means the '
            "option can scale to zero instances and stop billing while idle.",
            style="dim",
        )
    )

    console.print()
    console.print(Text("Sources", style="bold"))
    for option in Option:
        entry = limits.limits_for(option)
        console.print(Padding(Text(entry.display_name, style="bold"), (0, 0, 0, 2)))
        for source in _sources_for(entry):
            console.print(Padding(Text(source, style="dim"), (0, 0, 0, 4)))

    console.print()
    console.print(Text(limits.disclaimer, style="yellow"))
    console.print(Text(f"Loaded from {limits.origin}", style="dim"))


def _sources_for(entry: OptionLimits) -> list[str]:
    sources: list[str] = []
    for sourced in (
        entry.max_request_payload_mb,
        entry.max_response_payload_mb,
        entry.max_processing_seconds,
        entry.gpu_supported,
        entry.scales_to_zero,
        entry.returns_inline_response,
        entry.native_completion_notification,
        entry.supports_multi_model_endpoint,
    ):
        if sourced.source not in sources:
            sources.append(sourced.source)
    return sources


def _cell(rendered: str) -> str:
    """Shorten the unlimited marker so the reference table stays narrow."""
    return "none" if rendered == "no documented limit" else rendered


def _tick(value: bool) -> Text:
    return Text("yes", style="green") if value else Text("no", style="red")
