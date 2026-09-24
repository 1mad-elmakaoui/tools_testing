"""The command line interface.

Collects options, runs the scan and renders it. No classification or costing lives here.

Exit codes:
    0  the scan completed and found nothing wasteful
    1  the scan completed and found idle or underused endpoints
    2  a usage error
    3  a data file is missing, malformed, or missing a source or rationale
    4  the scan could not complete (credentials, permissions, every region failed)
"""

from __future__ import annotations

import datetime as dt
import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from sagemaker_idle_finder import __version__, render
from sagemaker_idle_finder.aws import (
    AwsError,
    CallRecorder,
    ReadOnlyClient,
    ReadOnlyViolationError,
    RetryPolicy,
    build_clients,
)
from sagemaker_idle_finder.catalog import CatalogError, Policy, Prices, load_policy, load_prices
from sagemaker_idle_finder.models import ServerlessWhatIf
from sagemaker_idle_finder.scan import scan

EXIT_OK = 0
EXIT_WASTE_FOUND = 1
EXIT_BAD_DATA = 3
EXIT_SCAN_FAILED = 4


class OutputFormat(StrEnum):
    """How to print the result."""

    TEXT = "text"
    JSON = "json"
    CSV = "csv"


app = typer.Typer(
    name="sagemaker-idle-finder",
    help=(
        "Find SageMaker endpoints that cost money while receiving little or no traffic.\n\n"
        "Read-only: it calls five describe-and-read APIs and nothing else. The allowlist "
        "is enforced at runtime, so it cannot create, modify or delete anything."
    ),
    no_args_is_help=True,
    add_completion=False,
)

_OUTPUT = Annotated[OutputFormat, typer.Option("--output", "-o", help="Output format.")]
_POLICY_FILE = Annotated[
    Path | None,
    typer.Option("--policy-file", help="Use a different policy.yaml.", show_default=False),
]
_PRICES_FILE = Annotated[
    Path | None,
    typer.Option("--prices", help="Use a different prices.yaml.", show_default=False),
]


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"sagemaker-idle-finder {__version__}")
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
    """Find idle SageMaker endpoints and estimate the waste."""


def _load(policy_file: Path | None, prices_file: Path | None) -> tuple[Policy, Prices]:
    try:
        return load_policy(policy_file), load_prices(prices_file)
    except CatalogError as exc:
        typer.secho(f"Error reading data: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_BAD_DATA) from exc


def _resolve_regions(requested: str | None, prices: Prices, profile: str | None) -> list[str]:
    """Work out which regions to scan.

    The default is the caller's configured region alone: an account-wide sweep across every
    region is slow and is not what someone expects from a command they just typed.
    """
    if requested and requested.strip().lower() == "all":
        return list(prices.regions)
    if requested:
        return [item.strip() for item in requested.split(",") if item.strip()]

    import boto3

    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    region = session.region_name
    if not region:
        raise typer.BadParameter(
            "no region configured. Pass --regions, or set AWS_REGION, or configure a "
            "profile with a region."
        )
    return [region]


@app.command("scan")
def scan_endpoints(
    regions: Annotated[
        str | None,
        typer.Option(
            "--regions",
            help="Comma-separated regions, or 'all'. Default: your configured region.",
            show_default=False,
        ),
    ] = None,
    lookback_days: Annotated[
        int | None,
        typer.Option("--lookback-days", min=1, help="How far back to look.", show_default=False),
    ] = None,
    threshold: Annotated[
        float | None,
        typer.Option(
            "--threshold",
            min=0.0,
            help="Invocations per instance-hour below which a variant is underused.",
            show_default=False,
        ),
    ] = None,
    show_all: Annotated[
        bool,
        typer.Option("--all", help="Show healthy and not-applicable variants too."),
    ] = False,
    explain: Annotated[
        bool, typer.Option("--explain", help="Print the evidence behind each finding.")
    ] = False,
    serverless_seconds: Annotated[
        float | None,
        typer.Option(
            "--serverless-seconds",
            min=0.0,
            help=(
                "Assumed seconds per request, to cost a move to serverless. "
                "Requires --serverless-memory-gb."
            ),
            show_default=False,
        ),
    ] = None,
    serverless_memory_gb: Annotated[
        int | None,
        typer.Option(
            "--serverless-memory-gb",
            min=1,
            help=("Assumed serverless memory size in GB. Requires --serverless-seconds."),
            show_default=False,
        ),
    ] = None,
    profile: Annotated[
        str | None, typer.Option("--profile", help="AWS profile to use.", show_default=False)
    ] = None,
    output: _OUTPUT = OutputFormat.TEXT,
    policy_file: _POLICY_FILE = None,
    prices_file: _PRICES_FILE = None,
) -> None:
    """Scan for endpoints that cost money without earning it."""
    policy, prices = _load(policy_file, prices_file)
    chosen = _resolve_regions(regions, prices, profile)
    days = lookback_days or policy.thresholds.default_lookback_days
    what_if = _serverless_what_if(serverless_seconds, serverless_memory_gb)

    retry = RetryPolicy(
        max_retries=policy.thresholds.max_retries,
        base_delay_seconds=policy.thresholds.retry_base_delay_seconds,
    )
    recorder = CallRecorder()

    def factory(region: str) -> dict[str, ReadOnlyClient]:
        return build_clients(region, policy.aws.allowed_operations, retry, profile, recorder)

    try:
        result = scan(
            factory,
            chosen,
            policy,
            prices,
            dt.datetime.now(tz=dt.UTC),
            days,
            threshold,
            what_if,
        )
    except ReadOnlyViolationError as exc:  # pragma: no cover - a bug, not a condition
        typer.secho(f"Refused: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_SCAN_FAILED) from exc
    except AwsError as exc:
        typer.secho(f"Scan failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_SCAN_FAILED) from exc

    if result.errors and not result.findings:
        for region, message in result.errors:
            typer.secho(f"{region}: {message}", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_SCAN_FAILED)

    if output is OutputFormat.JSON:
        typer.echo(json.dumps(render.result_to_dict(result), indent=2))
    elif output is OutputFormat.CSV:
        typer.echo(render.result_to_csv(result), nl=False)
    else:
        console = Console()
        render.render_result(console, result, show_all)
        if explain:
            render.render_detail(console, result)

    if any(finding.verdict.is_waste for finding in result.findings):
        raise typer.Exit(EXIT_WASTE_FOUND)


def _serverless_what_if(seconds: float | None, memory_gb: int | None) -> ServerlessWhatIf | None:
    """Build the serverless assumptions, insisting on both halves or neither.

    Costing serverless needs a request duration and a memory size, and neither can be read
    from an endpoint. Accepting one without the other would mean filling in the missing one
    with a guess, which is the thing this tool refuses to do.
    """
    if seconds is None and memory_gb is None:
        return None
    if seconds is None or memory_gb is None:
        raise typer.BadParameter(
            "--serverless-seconds and --serverless-memory-gb go together: costing "
            "serverless needs both, and neither can be read from AWS"
        )
    return ServerlessWhatIf(seconds_per_invocation=seconds, memory_gb=memory_gb)


@app.command("policy")
def policy(
    output: _OUTPUT = OutputFormat.TEXT,
    policy_file: _POLICY_FILE = None,
    prices_file: _PRICES_FILE = None,
) -> None:
    """Show the IAM policy this tool needs, and the thresholds it uses."""
    loaded, prices = _load(policy_file, prices_file)
    document = _iam_policy(loaded)

    if output is OutputFormat.JSON:
        typer.echo(
            json.dumps(
                {
                    "iam_policy": document,
                    "allowed_operations": {
                        service: sorted(operations)
                        for service, operations in loaded.aws.allowed_operations.items()
                    },
                    "thresholds": vars(loaded.thresholds),
                    "last_verified": loaded.last_verified,
                    "prices_published": prices.publication_date,
                    "price_regions": len(prices.regions),
                },
                indent=2,
            )
        )
        return

    console = Console()
    console.print()
    console.print("[bold]The complete set of AWS calls this tool can make[/bold]")
    for service, operations in sorted(loaded.aws.allowed_operations.items()):
        for operation in sorted(operations):
            console.print(f"  {service}:{operation}")
    console.print(
        "\n[dim]Enforced at runtime, not just documented: anything else raises before it "
        "reaches the network.[/dim]"
    )
    console.print("\n[bold]Minimal IAM policy[/bold]")
    console.print(json.dumps(document, indent=2))
    console.print("\n[bold]Thresholds[/bold]")
    for name, value in vars(loaded.thresholds).items():
        console.print(f"  {name}: {value}")
    console.print(
        f"\n[dim]Policy verified {loaded.last_verified}. Prices published "
        f"{prices.publication_date}, covering {len(prices.regions)} regions.[/dim]"
    )


def _iam_policy(loaded: Policy) -> dict[str, object]:
    """Build the least-privilege policy from the same allowlist the code enforces."""
    actions = sorted(
        f"{service}:{operation}"
        for service, operations in loaded.aws.allowed_operations.items()
        for operation in operations
    )
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "SageMakerIdleFinderReadOnly",
                "Effect": "Allow",
                "Action": actions,
                "Resource": "*",
            }
        ],
    }


if __name__ == "__main__":  # pragma: no cover
    app()
