"""Interactive prompt mode: ask the same questions the flags ask.

Kept apart from the Typer wiring so the question set exists in exactly one place, and so it
can be driven from a scripted stdin in tests.
"""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, FloatPrompt, IntPrompt, Prompt

from sagemaker_inference_picker.models import TrafficPattern, Workload

_TRAFFIC_HELP = {
    TrafficPattern.STEADY: "steady — a fairly constant request rate",
    TrafficPattern.BURSTY_IDLE: "bursty-idle — bursts separated by idle periods",
    TrafficPattern.SCHEDULED_BATCH: "scheduled-batch — a known dataset on a schedule",
}


def prompt_for_workload(console: Console) -> Workload:
    """Ask for every input and build a :class:`Workload`.

    Invalid answers are re-asked rather than guessed at.
    """
    console.print()
    console.print(
        Panel(
            "Answer these and I will pick an inference option and show my working.\n"
            "Press Enter to accept the default shown in brackets.",
            title="sagemaker-inference-picker",
            border_style="cyan",
        )
    )

    console.print()
    payload_mb = _non_negative_float(console, "Request payload size (MB)", 1.0)
    response_mb = _non_negative_float(console, "Response size (MB)", 1.0)
    processing_seconds = _non_negative_float(console, "Processing time per request (s)", 1.0)

    console.print()
    for pattern in TrafficPattern:
        console.print(f"  [dim]{_TRAFFIC_HELP[pattern]}[/dim]")
    traffic = TrafficPattern(
        Prompt.ask(
            "Traffic pattern",
            console=console,
            choices=[pattern.value for pattern in TrafficPattern],
            default=TrafficPattern.STEADY.value,
        )
    )

    console.print()
    latency_p99_ms = _optional_positive_float(
        console, "p99 latency requirement in ms (blank for none)"
    )
    immediate_response = Confirm.ask(
        "Does the caller need an immediate response?", console=console, default=True
    )
    needs_notification = Confirm.ask(
        "Does it need a completion notification?", console=console, default=False
    )
    zero_idle_cost = Confirm.ask("Must it cost nothing when idle?", console=console, default=False)
    gpu_required = Confirm.ask("Is a GPU required?", console=console, default=False)
    model_count = _at_least_one(console, "How many models?", 1)

    return Workload(
        payload_mb=payload_mb,
        response_mb=response_mb,
        processing_seconds=processing_seconds,
        traffic=traffic,
        latency_p99_ms=latency_p99_ms,
        zero_idle_cost=zero_idle_cost,
        immediate_response=immediate_response,
        needs_notification=needs_notification,
        gpu_required=gpu_required,
        model_count=model_count,
    )


def _non_negative_float(console: Console, question: str, default: float) -> float:
    while True:
        value = FloatPrompt.ask(question, console=console, default=default)
        if value >= 0:
            return value
        console.print("[red]Please enter zero or a positive number.[/red]")


def _optional_positive_float(console: Console, question: str) -> float | None:
    while True:
        answer = Prompt.ask(question, console=console, default="").strip()
        if not answer:
            return None
        try:
            value = float(answer)
        except ValueError:
            console.print("[red]Please enter a number, or leave it blank for none.[/red]")
            continue
        if value > 0:
            return value
        console.print("[red]A latency target must be greater than zero.[/red]")


def _at_least_one(console: Console, question: str, default: int) -> int:
    while True:
        value = IntPrompt.ask(question, console=console, default=default)
        if value >= 1:
            return value
        console.print("[red]There must be at least one model.[/red]")
