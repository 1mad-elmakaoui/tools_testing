"""The command line interface.

Collects options, reads the logs, prints the report. Nothing here decides what the numbers
mean, and nothing here can print a prompt: the report it renders is built from records that
cannot hold one. Showing content is a separate command, on purpose.

Exit codes:
    0  the logs were analysed
    1  the logs were analysed and something matched an anomaly heuristic
    2  a usage error
    3  a data file is missing, malformed, or missing a source or rationale
    4  the logs could not be read at all
"""

from __future__ import annotations

import datetime as dt
import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from bedrock_log_lens import __version__, html, render
from bedrock_log_lens.analyse import analyse
from bedrock_log_lens.catalog import CatalogError, Policy, Prices, load_policy, load_prices
from bedrock_log_lens.content import WARNING, fetch
from bedrock_log_lens.models import ParseOutcome
from bedrock_log_lens.read import find_log_files, read_directory

EXIT_OK = 0
EXIT_ANOMALIES_FOUND = 1
EXIT_BAD_DATA = 3
EXIT_UNREADABLE = 4


class OutputFormat(StrEnum):
    """How to print the result."""

    TEXT = "text"
    JSON = "json"


app = typer.Typer(
    name="bedrock-log-lens",
    help=(
        "Analyse Amazon Bedrock model invocation logs: token usage, cost and anomalies.\n\n"
        "These logs contain every prompt and every response. This tool discards both when "
        "it parses a record, so no report it produces can contain them."
    ),
    no_args_is_help=True,
    add_completion=False,
)

_OUTPUT = Annotated[OutputFormat, typer.Option("--output", "-o", help="Output format.")]
_PRICES = Annotated[
    Path | None,
    typer.Option("--prices", help="Use a different prices.yaml.", show_default=False),
]
_POLICY = Annotated[
    Path | None,
    typer.Option("--policy-file", help="Use a different policy.yaml.", show_default=False),
]


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"bedrock-log-lens {__version__}")
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
    """Analyse Bedrock invocation logs without reading what was said."""


def _load(policy_file: Path | None, prices_file: Path | None) -> tuple[Policy, Prices]:
    try:
        return load_policy(policy_file), load_prices(prices_file)
    except CatalogError as exc:
        typer.secho(f"Error reading data: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_BAD_DATA) from exc


@app.command("analyse")
def analyse_logs(
    path: Annotated[
        Path | None,
        typer.Argument(
            help="Directory of log files, or a single file.",
            show_default=False,
            exists=False,
        ),
    ] = None,
    s3_uri: Annotated[
        str | None,
        typer.Option(
            "--s3",
            help="Read from s3://bucket/prefix instead of a local path. Read-only.",
            show_default=False,
        ),
    ] = None,
    region: Annotated[
        str | None, typer.Option("--region", help="AWS region for S3.", show_default=False)
    ] = None,
    profile: Annotated[
        str | None, typer.Option("--profile", help="AWS profile for S3.", show_default=False)
    ] = None,
    html_out: Annotated[
        Path | None,
        typer.Option(
            "--html",
            help="Also write a single self-contained HTML report to this path.",
            show_default=False,
        ),
    ] = None,
    top: Annotated[
        int | None,
        typer.Option(
            "--top", min=1, help="How many expensive requests to list.", show_default=False
        ),
    ] = None,
    burst_window: Annotated[
        int | None,
        typer.Option(
            "--burst-window", min=1, help="Anomaly window in seconds.", show_default=False
        ),
    ] = None,
    burst_count: Annotated[
        int | None,
        typer.Option(
            "--burst-count",
            min=1,
            help="Calls per window that count as a burst.",
            show_default=False,
        ),
    ] = None,
    output: _OUTPUT = OutputFormat.TEXT,
    policy_file: _POLICY = None,
    prices_file: _PRICES = None,
) -> None:
    """Report token usage, cost and anomalies from a directory of logs or an S3 prefix."""
    policy, prices = _load(policy_file, prices_file)
    policy = _with_thresholds(policy, top, burst_window, burst_count)

    if (path is None) == (s3_uri is None):
        raise typer.BadParameter("give either a local path or --s3, and not both")

    outcome, sources = _read(path, s3_uri, region, profile)

    if not outcome.total_seen:
        typer.secho(
            "No log records were found. Bedrock writes them under "
            f"{policy.aws.s3_log_prefix_template}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(EXIT_UNREADABLE)

    report = analyse(outcome, policy, prices, sources_read=sources)

    if output is OutputFormat.JSON:
        typer.echo(json.dumps(render.report_to_dict(report, policy.disclaimer), indent=2))
    else:
        render.render_report(Console(), report, policy.disclaimer)

    if html_out is not None:
        html_out.write_text(
            html.render_html(report, policy.disclaimer, dt.datetime.now(tz=dt.UTC)),
            encoding="utf-8",
        )
        if output is OutputFormat.TEXT:
            typer.echo(f"\nWrote {html_out}")

    if report.anomalies:
        raise typer.Exit(EXIT_ANOMALIES_FOUND)


def _with_thresholds(
    policy: Policy, top: int | None, burst_window: int | None, burst_count: int | None
) -> Policy:
    from dataclasses import replace

    return replace(
        policy,
        thresholds=policy.thresholds.overridden(
            top=top, burst_window=burst_window, burst_count=burst_count
        ),
    )


def _read(
    path: Path | None, s3_uri: str | None, region: str | None, profile: str | None
) -> tuple[ParseOutcome, int]:
    if path is not None:
        if not path.exists():
            raise typer.BadParameter(f"{path} does not exist")
        return read_directory(path), len(find_log_files(path))

    from bedrock_log_lens.s3 import S3Error, S3Location, build_client, read_s3

    try:
        location = S3Location.parse(s3_uri or "")
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    try:
        return read_s3(build_client(region=region, profile=profile), location)
    except S3Error as exc:
        typer.secho(f"Could not read {location}: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_UNREADABLE) from exc


@app.command("show-content")
def show_content(
    path: Annotated[Path, typer.Argument(help="Directory of log files, or a single file.")],
    request_id: Annotated[
        list[str],
        typer.Option("--request-id", help="Request to show. Repeatable.", show_default=False),
    ],
    confirm: Annotated[
        bool,
        typer.Option(
            "--show-content",
            help="Required. Confirms you intend to print prompts and responses.",
        ),
    ] = False,
) -> None:
    """Print the prompt and response for named requests. Requires --show-content.

    Nothing else in this tool can do this. The analysis pipeline discards content when it
    parses a record, so this command goes back to the files and reads them again.
    """
    console = Console(stderr=True)
    if not confirm:
        console.print(f"[yellow]{WARNING}[/yellow]")
        console.print(
            "[red]Refusing: pass --show-content as well to confirm you intend this.[/red]"
        )
        raise typer.Exit(EXIT_OK)

    if not path.exists():
        raise typer.BadParameter(f"{path} does not exist")

    console.print(f"[yellow]{WARNING}[/yellow]")
    found = 0
    for item in fetch(path, request_id):
        found += 1
        typer.echo(item.as_text())
        typer.echo("")

    if not found:
        console.print("[red]None of those request ids were found in these logs.[/red]")


@app.command("policy")
def policy_command(
    output: _OUTPUT = OutputFormat.TEXT,
    policy_file: _POLICY = None,
    prices_file: _PRICES = None,
) -> None:
    """Show the IAM policy S3 reading needs, and the thresholds this tool uses."""
    loaded, prices = _load(policy_file, prices_file)
    document = _iam_policy(loaded)

    if output is OutputFormat.JSON:
        typer.echo(
            json.dumps(
                {
                    "iam_policy": document,
                    "read_only_operations": {
                        service: sorted(operations)
                        for service, operations in loaded.aws.read_only_operations.items()
                    },
                    "thresholds": _thresholds_dict(loaded),
                    "last_verified": loaded.last_verified,
                    "prices_published": prices.publication_date,
                },
                indent=2,
            )
        )
        return

    console = Console()
    console.print()
    console.print("[bold]AWS calls this tool can make[/bold]")
    for service, operations in sorted(loaded.aws.read_only_operations.items()):
        for operation in sorted(operations):
            console.print(f"  {service}:{operation}")
    console.print()
    console.print()
    console.print("[bold]The IAM action each one needs[/bold]")
    for operation, action in sorted(loaded.aws.iam_actions.items()):
        console.print(f"  {operation} is authorised by {action}")
    console.print()
    console.print(
        "Both are reads, and only used with --s3. Analysing a local directory makes no AWS "
        "calls at all and needs no credentials."
    )
    console.print()
    console.print("[bold]Minimal IAM policy[/bold]")
    console.print(json.dumps(document, indent=2))
    console.print()
    console.print("[bold]Thresholds[/bold]")
    for name, value in _thresholds_dict(loaded).items():
        console.print(f"  {name}: {value}")
    console.print()
    console.print(
        f"[dim]Policy verified {loaded.last_verified}. Prices published "
        f"{prices.publication_date}, covering {len(prices.regions)} regions.[/dim]"
    )


def _iam_policy(policy: Policy) -> dict[str, object]:
    """Build the IAM document from the allowlist the code enforces.

    Generated rather than written out, so the policy in the README cannot drift away from
    what the tool actually calls. The two statements differ because the permissions do:
    listing is authorised on the bucket and scoped to the log prefix, reading is authorised
    on the objects.
    """
    actions = policy.aws.iam_actions
    operations = sorted(policy.aws.read_only_operations.get("s3", ()))
    unmapped = [operation for operation in operations if operation not in actions]
    if unmapped:  # pragma: no cover - a data file error, caught by a test
        raise CatalogError(
            f"no IAM action is recorded for s3:{', s3:'.join(unmapped)}; "
            f"add it to aws.iam_actions so the printed policy stays truthful"
        )

    prefix = policy.aws.s3_log_prefix_template.replace("{account_id}", "*")
    prefix = prefix.replace("{region}", "*").split("{")[0] + "*"

    listing = sorted(
        actions[operation] for operation in operations if actions[operation].endswith("ListBucket")
    )
    reading = sorted(
        actions[operation]
        for operation in operations
        if not actions[operation].endswith("ListBucket")
    )

    statements: list[dict[str, object]] = []
    if listing:
        statements.append(
            {
                "Sid": "BedrockLogLensListPrefix",
                "Effect": "Allow",
                "Action": listing,
                "Resource": "arn:aws:s3:::YOUR-LOG-BUCKET",
                "Condition": {"StringLike": {"s3:prefix": [prefix]}},
            }
        )
    if reading:
        statements.append(
            {
                "Sid": "BedrockLogLensReadObjects",
                "Effect": "Allow",
                "Action": reading,
                "Resource": f"arn:aws:s3:::YOUR-LOG-BUCKET/{prefix}",
            }
        )
    return {"Version": "2012-10-17", "Statement": statements}


def _thresholds_dict(policy: Policy) -> dict[str, float | int]:
    thresholds = policy.thresholds
    return {
        "outlier_percentile": thresholds.outlier_percentile,
        "minimum_requests_for_percentiles": thresholds.minimum_requests_for_percentiles,
        "burst_window_seconds": thresholds.burst_window_seconds,
        "burst_request_count": thresholds.burst_request_count,
        "rate_jump_multiple": thresholds.rate_jump_multiple,
        "repeated_shape_count": thresholds.repeated_shape_count,
        "top_expensive_requests": thresholds.top_expensive_requests,
    }
