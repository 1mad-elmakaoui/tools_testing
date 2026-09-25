"""The command line interface.

Collects options, runs the scan and prints it. Nothing here decides what the numbers mean.

Exit codes:
    0  the scan completed and found nothing throttled
    1  the scan completed and found a stream with a problem
    2  a usage error
    3  the data file is missing, malformed, or missing a source or rationale
    4  the scan could not complete (credentials, permissions, no streams readable)
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from kinesis_skew import __version__, render
from kinesis_skew.aws import (
    AwsError,
    CallRecorder,
    ReadOnlyClient,
    ReadOnlyViolationError,
    RetryPolicy,
    build_clients,
)
from kinesis_skew.catalog import Catalog, CatalogError, load_catalog
from kinesis_skew.scan import scan

EXIT_OK = 0
EXIT_PROBLEM_FOUND = 1
EXIT_BAD_DATA = 3
EXIT_SCAN_FAILED = 4

_HOURS_PER_DAY = 24

#: GetRecords and GetShardIterator can be scoped to stream ARNs, unlike the base
#: operations, so the optional statement is granted narrowly.
_STREAM_ARN_PATTERN = "arn:aws:kinesis:*:*:stream/*"


class OutputFormat(StrEnum):
    """How to print the result."""

    TEXT = "text"
    JSON = "json"


app = typer.Typer(
    name="kinesis-skew",
    help=(
        "Tell partition key skew apart from genuine under-provisioning in Kinesis Data "
        "Streams.\n\n"
        "Read-only. The operations it may call are checked at runtime, and the two sampling "
        "operations are only on that list when --sample-keys is passed."
    ),
    no_args_is_help=True,
    add_completion=False,
)

_OUTPUT = Annotated[OutputFormat, typer.Option("--output", "-o", help="Output format.")]
_LIMITS_FILE = Annotated[
    Path | None,
    typer.Option("--limits-file", help="Use a different limits.yaml.", show_default=False),
]


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"kinesis-skew {__version__}")
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
    """Find partition key skew in Kinesis Data Streams."""


def _load(limits_file: Path | None) -> Catalog:
    try:
        return load_catalog(limits_file)
    except CatalogError as exc:
        typer.secho(f"Error reading data: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_BAD_DATA) from exc


def _resolve_region(requested: str | None, profile: str | None) -> str:
    if requested:
        return requested

    import boto3

    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    region = session.region_name
    if not region:
        raise typer.BadParameter(
            "no region configured. Pass --region, or set AWS_REGION, or configure a "
            "profile with a region."
        )
    return str(region)


def _check_lookback(hours: float, catalog: Catalog) -> None:
    """Refuse a window CloudWatch cannot answer at one-minute resolution.

    Past the retention period for one-minute data, CloudWatch returns nothing rather than
    an error, and a stream with no datapoints would be reported as idle. Refusing is the
    only honest answer.
    """
    retention_hours = catalog.aws.cloudwatch_one_minute_retention_days * _HOURS_PER_DAY
    if hours > retention_hours:
        raise typer.BadParameter(
            f"--lookback-hours {hours:g} is beyond the {retention_hours:g} hours CloudWatch "
            f"keeps one-minute data for. Shard metrics are emitted per minute, so a longer "
            f"window would come back empty and read as an idle stream."
        )


@app.command("scan")
def scan_streams(
    stream: Annotated[
        list[str] | None,
        typer.Option(
            "--stream",
            help="Stream to examine. Repeatable. Default: every stream in the region.",
            show_default=False,
        ),
    ] = None,
    region: Annotated[
        str | None,
        typer.Option("--region", help="Region to scan.", show_default=False),
    ] = None,
    lookback_hours: Annotated[
        float | None,
        typer.Option("--lookback-hours", min=0.1, help="How far back to look.", show_default=False),
    ] = None,
    sample_keys: Annotated[
        bool,
        typer.Option(
            "--sample-keys",
            help=(
                "Read a bounded sample of records from the busiest shard to show its most "
                "frequent partition keys. Consumes read throughput shared with your "
                "consumers."
            ),
        ),
    ] = False,
    show_all: Annotated[
        bool, typer.Option("--all", help="Show healthy streams too, not only problems.")
    ] = False,
    hot: Annotated[
        float | None,
        typer.Option("--hot", min=0.0, max=1.0, help="Hot shard threshold.", show_default=False),
    ] = None,
    skew_ratio: Annotated[
        float | None,
        typer.Option(
            "--skew-ratio", min=1.0, help="Busiest-to-mean ratio for skew.", show_default=False
        ),
    ] = None,
    gini: Annotated[
        float | None,
        typer.Option("--gini", min=0.0, max=1.0, help="Gini threshold.", show_default=False),
    ] = None,
    profile: Annotated[
        str | None, typer.Option("--profile", help="AWS profile to use.", show_default=False)
    ] = None,
    output: _OUTPUT = OutputFormat.TEXT,
    limits_file: _LIMITS_FILE = None,
) -> None:
    """Look for partition key skew, and say whether it is the reason for any throttling."""
    catalog = _load(limits_file)
    chosen_region = _resolve_region(region, profile)
    hours = lookback_hours or catalog.thresholds.default_lookback_hours
    _check_lookback(hours, catalog)
    thresholds = catalog.thresholds.overridden(hot=hot, skew_ratio=skew_ratio, gini=gini)

    retry = RetryPolicy(
        max_retries=thresholds.max_retries,
        base_delay_seconds=thresholds.retry_base_delay_seconds,
    )
    recorder = CallRecorder()
    # The flag builds the allowlist. Without --sample-keys the two sampling operations are
    # not on it, so this process cannot call GetRecords even if the code tried to.
    allowed = catalog.aws.operations_for(sampling=sample_keys)

    if sample_keys and output is OutputFormat.TEXT:
        typer.secho(
            "--sample-keys will read records from the busiest shard, using read throughput "
            "shared with this stream's consumers.",
            fg=typer.colors.YELLOW,
            err=True,
        )

    def factory(scan_region: str) -> dict[str, ReadOnlyClient]:
        return build_clients(scan_region, allowed, retry, profile, recorder)

    try:
        result = scan(
            factory,
            chosen_region,
            stream or None,
            catalog,
            dt.datetime.now(tz=dt.UTC),
            hours,
            thresholds,
            sample_keys,
        )
    except ReadOnlyViolationError as exc:  # pragma: no cover - a bug, not a condition
        typer.secho(f"Refused: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_SCAN_FAILED) from exc
    except AwsError as exc:
        typer.secho(f"Scan failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_SCAN_FAILED) from exc

    if result.errors and not result.reports:
        for name, message in result.errors:
            typer.secho(f"{name}: {message}", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_SCAN_FAILED)

    if output is OutputFormat.JSON:
        typer.echo(json.dumps(render.result_to_dict(result, catalog.disclaimer), indent=2))
    else:
        render.render_result(Console(), result, show_all)

    if result.problems:
        raise typer.Exit(EXIT_PROBLEM_FOUND)


@app.command("policy")
def policy(output: _OUTPUT = OutputFormat.TEXT, limits_file: _LIMITS_FILE = None) -> None:
    """Show the IAM policy this tool needs, and the thresholds it uses."""
    catalog = _load(limits_file)
    base = _iam_statement(catalog.aws.operations_for(sampling=False), "KinesisSkewReadOnly")
    sampling = _iam_statement(
        catalog.aws.sampling_operations,
        "KinesisSkewOptionalSampling",
        resource=_STREAM_ARN_PATTERN,
    )

    if output is OutputFormat.JSON:
        typer.echo(
            json.dumps(
                {
                    "iam_policy": {"Version": "2012-10-17", "Statement": [base]},
                    "iam_policy_with_sampling": {
                        "Version": "2012-10-17",
                        "Statement": [base, sampling],
                    },
                    "thresholds": _thresholds_dict(catalog),
                    "last_verified": catalog.last_verified,
                },
                indent=2,
            )
        )
        return

    console = Console()
    console.print()
    console.print("[bold]The complete set of AWS calls this tool can make[/bold]")
    for service, operations in sorted(catalog.aws.operations_for(sampling=False).items()):
        for operation in sorted(operations):
            console.print(f"  {service}:{operation}")
    console.print()
    console.print("[bold]Only with --sample-keys[/bold]")
    for service, operations in sorted(catalog.aws.sampling_operations.items()):
        for operation in sorted(operations):
            console.print(f"  {service}:{operation}")
    console.print()
    console.print(
        "Enforced at runtime, not just documented: anything else raises before it reaches "
        "the network, and the sampling operations are absent from the allowlist entirely "
        "unless you pass --sample-keys."
    )
    console.print()
    console.print("[bold]Minimal IAM policy[/bold]")
    console.print(json.dumps({"Version": "2012-10-17", "Statement": [base]}, indent=2))
    console.print()
    console.print("[bold]Thresholds[/bold]")
    for name, value in _thresholds_dict(catalog).items():
        console.print(f"  {name}: {value}")
    console.print()
    console.print(f"[dim]Verified {catalog.last_verified}. {catalog.disclaimer}[/dim]")


def _iam_statement(
    operations: Mapping[str, frozenset[str]], sid: str, resource: str = "*"
) -> dict[str, object]:
    """Build an IAM statement from the allowlist the code enforces.

    Generated rather than written out, so the policy in the README cannot drift away from
    what the tool actually calls.

    The default resource is "*" because ListStreams and GetMetricData are not
    resource-scoped, so a narrower grant on the base statement would simply not work. The
    sampling operations are scopable, and are given a narrower one.
    """
    actions = sorted(
        f"{service}:{operation}" for service, names in operations.items() for operation in names
    )
    return {
        "Sid": sid,
        "Effect": "Allow",
        "Action": actions,
        "Resource": resource,
    }


def _thresholds_dict(catalog: Catalog) -> dict[str, float | int]:
    thresholds = catalog.thresholds
    return {
        "default_lookback_hours": thresholds.default_lookback_hours,
        "hot_shard_utilisation": thresholds.hot_shard_utilisation,
        "skew_max_to_mean_ratio": thresholds.skew_max_to_mean_ratio,
        "skew_gini": thresholds.skew_gini,
        "skew_headroom_utilisation": thresholds.skew_headroom_utilisation,
        "capacity_utilisation": thresholds.capacity_utilisation,
        "minimum_window_coverage": thresholds.minimum_window_coverage,
        "sample_max_get_records_calls": thresholds.sample_max_get_records_calls,
        "sample_max_records": thresholds.sample_max_records,
    }
