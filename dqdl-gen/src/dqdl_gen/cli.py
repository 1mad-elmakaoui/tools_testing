"""The command line interface.

Collects input, calls the pure pipeline and renders the result. No profiling or rule
decisions live here.

Exit codes:
    0  the command succeeded
    1  the generated or checked DQDL is not valid
    2  a usage error (unknown flag, bad value, unreadable file)
    3  the catalogue file is missing, malformed, or missing a source or rationale
    4  `push` failed, or was run without --confirm
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from dqdl_gen import __version__, render
from dqdl_gen.catalog import Catalog, CatalogError, load_catalog
from dqdl_gen.emit import emit
from dqdl_gen.generate import generate
from dqdl_gen.models import DatasetProfile, Strictness
from dqdl_gen.profile import profile_table
from dqdl_gen.reader import ReadError, read_table
from dqdl_gen.validate import DqdlSyntaxError, ParsedRule, validate

EXIT_OK = 0
EXIT_INVALID_DQDL = 1
EXIT_BAD_CATALOG = 3
EXIT_PUSH_FAILED = 4


class OutputFormat(StrEnum):
    """How to print the result."""

    TEXT = "text"
    JSON = "json"


app = typer.Typer(
    name="dqdl-gen",
    help=(
        "Profile a CSV or Parquet file and generate a starter AWS Glue Data Quality "
        "ruleset in DQDL.\n\n"
        "Reading and generating are entirely local: no AWS calls, no credentials. Only "
        "`dqdl-gen push` talks to AWS, and only with --confirm."
    ),
    no_args_is_help=True,
    add_completion=False,
)

_OUTPUT = Annotated[OutputFormat, typer.Option("--output", "-o", help="Output format.")]
_CATALOG_FILE = Annotated[
    Path | None,
    typer.Option(
        "--catalog-file",
        help="Use a different dqdl.yaml instead of the one bundled with the tool.",
        show_default=False,
    ),
]
_STRICTNESS = Annotated[
    Strictness,
    typer.Option("--strictness", "-s", help="How much room rules leave for variation."),
]
_SAMPLE = Annotated[
    int | None,
    typer.Option(
        "--sample",
        min=1,
        help="Profile only the first N rows. Useful for very large files.",
        show_default=False,
    ),
]


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"dqdl-gen {__version__}")
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
    """Generate AWS Glue Data Quality rulesets from a data file."""


def _load(path: Path | None) -> Catalog:
    try:
        return load_catalog(path)
    except CatalogError as exc:
        typer.secho(f"Error reading the catalog: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_BAD_CATALOG) from exc


def _profile(path: Path, sample: int | None, catalog: Catalog) -> DatasetProfile:
    try:
        loaded = read_table(path, sample=sample)
    except ReadError as exc:
        raise typer.BadParameter(str(exc)) from exc
    return profile_table(
        loaded,
        max_value_set=catalog.shape.max_allowed_value_set,
        top_values_shown=catalog.shape.top_values_shown,
    )


@app.command()
def profile(
    path: Annotated[Path, typer.Argument(help="The CSV or Parquet file to profile.")],
    sample: _SAMPLE = None,
    output: _OUTPUT = OutputFormat.TEXT,
    catalog_file: _CATALOG_FILE = None,
) -> None:
    """Profile a file without generating any rules."""
    catalog = _load(catalog_file)
    dataset = _profile(path, sample, catalog)

    if output is OutputFormat.JSON:
        typer.echo(json.dumps(render.profile_to_dict(dataset), indent=2))
        return
    render.render_profile(Console(), dataset, catalog)


@app.command("generate")
def generate_rules(
    path: Annotated[Path, typer.Argument(help="The CSV or Parquet file to profile.")],
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            help="Where to write the .dqdl file. Defaults to <name>.dqdl beside the input.",
            show_default=False,
        ),
    ] = None,
    stdout: Annotated[
        bool,
        typer.Option("--stdout", help="Print the DQDL instead of writing a file."),
    ] = False,
    explain: Annotated[
        bool,
        typer.Option("--explain", help="Also print the profile table the rules came from."),
    ] = False,
    strictness: _STRICTNESS = Strictness.BALANCED,
    sample: _SAMPLE = None,
    output: _OUTPUT = OutputFormat.TEXT,
    catalog_file: _CATALOG_FILE = None,
) -> None:
    """Profile a file and generate a DQDL ruleset from it."""
    catalog = _load(catalog_file)
    dataset = _profile(path, sample, catalog)
    ruleset = generate(dataset, catalog, strictness)
    text = emit(ruleset, catalog)

    # Never write DQDL we have not checked. A ruleset that does not parse is worse than
    # no ruleset, because it fails later and further from here.
    _check(text, catalog)

    if output is OutputFormat.JSON:
        typer.echo(json.dumps(render.ruleset_to_dict(ruleset, catalog, text), indent=2))
        return

    console = Console()
    if explain:
        render.render_profile(console, dataset, catalog)

    if stdout:
        typer.echo(text, nl=False)
        return

    destination = out if out is not None else path.with_suffix(".dqdl")
    destination.write_text(text, encoding="utf-8")
    render.render_summary(console, ruleset, str(destination))


@app.command("validate")
def validate_file(
    path: Annotated[Path, typer.Argument(help="The .dqdl file to check.")],
    output: _OUTPUT = OutputFormat.TEXT,
    catalog_file: _CATALOG_FILE = None,
) -> None:
    """Check that a .dqdl file is valid.

    Covers the subset dqdl-gen generates, and checks rule type names and parameter counts
    against the catalogue, which a grammar-level parse alone would not.
    """
    catalog = _load(catalog_file)
    if not path.is_file():
        raise typer.BadParameter(f"file not found: {path}")
    text = path.read_text(encoding="utf-8")
    rules = _check(text, catalog, source=str(path))

    if output is OutputFormat.JSON:
        typer.echo(
            json.dumps(
                {
                    "valid": True,
                    "source": str(path),
                    "rule_count": len(rules),
                    "rules": [
                        {
                            "rule_type": rule.rule_type,
                            "parameters": list(rule.parameters),
                            "condition": rule.condition_text,
                            "line": rule.line,
                        }
                        for rule in rules
                    ],
                },
                indent=2,
            )
        )
        return
    console = Console()
    console.print(f"[green]{path} is valid[/green]: {len(rules)} rules")


@app.command()
def rules(
    output: _OUTPUT = OutputFormat.TEXT,
    catalog_file: _CATALOG_FILE = None,
) -> None:
    """Show the DQDL rule types this tool emits, with their sources."""
    catalog = _load(catalog_file)
    if output is OutputFormat.JSON:
        typer.echo(json.dumps(render.catalog_to_dict(catalog), indent=2))
        return
    render.render_catalog(Console(), catalog)


@app.command()
def push(
    path: Annotated[Path, typer.Argument(help="The .dqdl file to upload.")],
    name: Annotated[str, typer.Option("--name", help="Name for the Glue ruleset.")],
    confirm: Annotated[
        bool,
        typer.Option(
            "--confirm",
            help="Required. Without it nothing is sent to AWS.",
        ),
    ] = False,
    description: Annotated[str | None, typer.Option("--description", show_default=False)] = None,
    database: Annotated[
        str | None,
        typer.Option("--database", help="Glue database of the target table.", show_default=False),
    ] = None,
    table: Annotated[
        str | None,
        typer.Option("--table", help="Glue table the ruleset applies to.", show_default=False),
    ] = None,
    output: _OUTPUT = OutputFormat.TEXT,
    catalog_file: _CATALOG_FILE = None,
) -> None:
    """Create the ruleset in AWS Glue. This writes to your account.

    The only command here that touches AWS. It calls CreateDataQualityRuleset and nothing
    else: it never updates or deletes an existing ruleset.
    """
    # Imported here, not at module scope, so the rest of the tool neither needs nor loads
    # boto3.
    from dqdl_gen.push import PushError, PushRequest, create_ruleset

    catalog = _load(catalog_file)
    if not path.is_file():
        raise typer.BadParameter(f"file not found: {path}")
    text = path.read_text(encoding="utf-8")
    _check(text, catalog, source=str(path))

    if not confirm:
        typer.secho(
            f"Refusing to write to AWS without --confirm.\n"
            f"This would call glue:CreateDataQualityRuleset to create {name!r} "
            f"from {path}.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        raise typer.Exit(EXIT_PUSH_FAILED)

    try:
        request = PushRequest(
            name=name,
            ruleset=text,
            description=description,
            database_name=database,
            table_name=table,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    try:
        result = create_ruleset(request)
    except PushError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_PUSH_FAILED) from exc

    if output is OutputFormat.JSON:
        typer.echo(json.dumps({"created": result.name, "region": result.region}, indent=2))
        return
    Console().print(f"[green]Created Glue data quality ruleset[/green] {result.name}")


def _check(text: str, catalog: Catalog, source: str | None = None) -> tuple[ParsedRule, ...]:
    """Validate DQDL, turning a syntax error into a clean exit."""
    try:
        return validate(text, catalog)
    except DqdlSyntaxError as exc:
        where = f"{source}: " if source else "generated ruleset is not valid DQDL: "
        typer.secho(f"{where}{exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_INVALID_DQDL) from exc


if __name__ == "__main__":  # pragma: no cover
    app()
