"""The command line interface.

This layer only collects input, calls the pure engine and hands the result to a renderer.
No decision logic lives here.

Exit codes:
    0  a recommendation was produced
    1  no option satisfies the requirements (the conflicts are reported)
    2  a usage error (unknown flag, bad value)
    3  the limits file is missing, malformed, or has a value without a source
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from sagemaker_inference_picker import __version__, render
from sagemaker_inference_picker.engine import recommend as run_engine
from sagemaker_inference_picker.limits import LimitsData, LimitsError, load_limits
from sagemaker_inference_picker.models import TrafficPattern, Workload
from sagemaker_inference_picker.prompt import prompt_for_workload

EXIT_OK = 0
EXIT_NO_VIABLE_OPTION = 1
EXIT_BAD_LIMITS = 3


class OutputFormat(StrEnum):
    """How to print the result."""

    TEXT = "text"
    JSON = "json"


app = typer.Typer(
    name="sagemaker-inference-picker",
    help=(
        "Recommend a SageMaker inference option for a workload, and explain why every "
        "other option was ruled out.\n\n"
        "Read-only: this tool makes no AWS API calls and needs no credentials."
    ),
    no_args_is_help=True,
    add_completion=False,
)

_LIMITS_FILE = Annotated[
    Path | None,
    typer.Option(
        "--limits-file",
        help="Use a different limits.yaml instead of the one bundled with the tool.",
        show_default=False,
    ),
]

_OUTPUT = Annotated[
    OutputFormat,
    typer.Option("--output", "-o", help="Output format."),
]


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"sagemaker-inference-picker {__version__}")
        raise typer.Exit(EXIT_OK)


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """Recommend a SageMaker inference option for a workload."""


def _load(path: Path | None) -> LimitsData:
    try:
        return load_limits(path)
    except LimitsError as exc:
        typer.secho(f"Error reading limits: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_BAD_LIMITS) from exc


@app.command()
def recommend(
    payload_mb: Annotated[
        float | None,
        typer.Option("--payload-mb", help="Request payload size in MB.", show_default=False),
    ] = None,
    response_mb: Annotated[
        float | None,
        typer.Option("--response-mb", help="Response size in MB.", show_default=False),
    ] = None,
    processing_seconds: Annotated[
        float | None,
        typer.Option(
            "--processing-seconds",
            help="Model processing time per request, in seconds.",
            show_default=False,
        ),
    ] = None,
    traffic: Annotated[
        TrafficPattern | None,
        typer.Option("--traffic", help="Traffic pattern.", show_default=False),
    ] = None,
    latency_p99_ms: Annotated[
        float | None,
        typer.Option(
            "--latency-p99-ms",
            help="p99 latency requirement in ms. Omit if there is none.",
            show_default=False,
        ),
    ] = None,
    zero_idle_cost: Annotated[
        bool,
        typer.Option(
            "--zero-idle-cost/--no-zero-idle-cost",
            help="Must cost nothing while idle.",
        ),
    ] = False,
    immediate_response: Annotated[
        bool,
        typer.Option(
            "--immediate-response/--no-immediate-response",
            help="The caller needs the prediction back in the same request.",
        ),
    ] = False,
    needs_notification: Annotated[
        bool,
        typer.Option(
            "--needs-notification/--no-needs-notification",
            help="A completion notification is required.",
        ),
    ] = False,
    gpu: Annotated[bool, typer.Option("--gpu/--no-gpu", help="A GPU is required.")] = False,
    models: Annotated[int, typer.Option("--models", min=1, help="Number of models to host.")] = 1,
    interactive: Annotated[
        bool,
        typer.Option(
            "--interactive",
            "-i",
            help="Ask for the inputs instead of taking them from flags.",
        ),
    ] = False,
    output: _OUTPUT = OutputFormat.TEXT,
    limits_file: _LIMITS_FILE = None,
) -> None:
    """Recommend an inference option, or report why no option fits.

    Exits 1 when nothing satisfies every requirement.
    """
    limits = _load(limits_file)
    console = Console(stderr=output is OutputFormat.JSON)

    if interactive:
        workload = prompt_for_workload(console)
    else:
        workload = _workload_from_flags(
            payload_mb=payload_mb,
            response_mb=response_mb,
            processing_seconds=processing_seconds,
            traffic=traffic,
            latency_p99_ms=latency_p99_ms,
            zero_idle_cost=zero_idle_cost,
            immediate_response=immediate_response,
            needs_notification=needs_notification,
            gpu=gpu,
            models=models,
        )

    result = run_engine(workload, limits)

    if output is OutputFormat.JSON:
        typer.echo(json.dumps(render.recommendation_to_dict(result, limits), indent=2))
    else:
        render.render_recommendation(console, result, limits)

    if not result.resolved:
        raise typer.Exit(EXIT_NO_VIABLE_OPTION)


def _workload_from_flags(
    *,
    payload_mb: float | None,
    response_mb: float | None,
    processing_seconds: float | None,
    traffic: TrafficPattern | None,
    latency_p99_ms: float | None,
    zero_idle_cost: bool,
    immediate_response: bool,
    needs_notification: bool,
    gpu: bool,
    models: int,
) -> Workload:
    """Build a workload from flags, refusing to guess at anything left out."""
    if payload_mb is None or response_mb is None or processing_seconds is None or traffic is None:
        missing = [
            name
            for name, value in (
                ("--payload-mb", payload_mb),
                ("--response-mb", response_mb),
                ("--processing-seconds", processing_seconds),
                ("--traffic", traffic),
            )
            if value is None
        ]
        raise typer.BadParameter(
            f"missing required option(s): {', '.join(missing)}. "
            f"Pass them, or use --interactive to be asked."
        )

    try:
        return Workload(
            payload_mb=payload_mb,
            response_mb=response_mb,
            processing_seconds=processing_seconds,
            traffic=traffic,
            latency_p99_ms=latency_p99_ms,
            zero_idle_cost=zero_idle_cost,
            immediate_response=immediate_response,
            needs_notification=needs_notification,
            gpu_required=gpu,
            model_count=models,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command()
def limits(
    output: _OUTPUT = OutputFormat.TEXT,
    limits_file: _LIMITS_FILE = None,
) -> None:
    """Show the AWS-documented limits behind every decision, with their sources."""
    data = _load(limits_file)
    if output is OutputFormat.JSON:
        typer.echo(json.dumps(render.limits_to_dict(data), indent=2))
        return
    render_limits_console = Console()
    render.render_limits(render_limits_console, data)


if __name__ == "__main__":  # pragma: no cover
    app()
