"""The CLI and the three output formats, driven through Typer's test runner.

The scan itself is replaced with a prepared result, so nothing here builds an AWS client.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from sagemaker_idle_finder import __version__, render
from sagemaker_idle_finder.cli import EXIT_BAD_DATA, EXIT_OK, EXIT_WASTE_FOUND, app
from sagemaker_idle_finder.models import (
    CostEstimate,
    Endpoint,
    EndpointStatus,
    Finding,
    Remedy,
    ScanResult,
    Verdict,
)
from tests.conftest import NOW, make_endpoint, make_variant, make_window

EXIT_USAGE = 2
runner = CliRunner(env={"COLUMNS": "220", "TERM": "dumb", "NO_COLOR": "1"})


def _finding(
    name: str = "idle-one",
    verdict: Verdict = Verdict.IDLE,
    wasted: float | None = 186.88,
    monthly: float | None = 186.88,
    remedy: Remedy = Remedy.DELETE,
    endpoint: Endpoint | None = None,
    variant_name: str = "AllTraffic",
) -> Finding:
    return Finding(
        endpoint=endpoint or make_endpoint(name=name),
        variant=make_variant(name=variant_name),
        verdict=verdict,
        window=make_window(0.0),
        cost=CostEstimate(monthly_usd=monthly, wasted_monthly_usd=wasted, instance_hour_usd=0.128),
        remedy=remedy,
        evidence=f"{name}: nothing invoked it",
    )


def _result(*findings: Finding, errors: tuple[tuple[str, str], ...] = ()) -> ScanResult:
    return ScanResult(
        findings=findings,
        regions=("eu-west-1",),
        lookback_days=14,
        started_at=NOW,
        prices_published="2026-09-22T23:07:50Z",
        errors=errors,
    )


def _run(*args: str) -> Any:
    return runner.invoke(app, list(args))


# ------------------------------------------------------------------------------ basics


def test_version() -> None:
    result = _run("--version")
    assert result.exit_code == EXIT_OK
    assert __version__ in result.stdout


def test_help_states_it_is_read_only() -> None:
    combined = _run("--help").stdout.replace("\n", " ")
    assert "Read-only" in combined


def test_bare_invocation_lists_the_commands() -> None:
    out = _run().stdout
    assert "scan" in out
    assert "policy" in out


# ------------------------------------------------------------------------- policy


def test_policy_prints_the_iam_document() -> None:
    result = _run("policy")
    assert result.exit_code == EXIT_OK
    assert "sagemaker:ListEndpoints" in result.stdout
    assert "cloudwatch:GetMetricData" in result.stdout
    assert "2012-10-17" in result.stdout
    assert "Enforced at runtime" in result.stdout


def test_policy_json_matches_the_enforced_allowlist() -> None:
    payload = json.loads(_run("policy", "--output", "json").stdout)
    actions = set(payload["iam_policy"]["Statement"][0]["Action"])
    flattened = {
        f"{service}:{operation}"
        for service, operations in payload["allowed_operations"].items()
        for operation in operations
    }
    assert actions == flattened
    assert payload["prices_published"]


def test_policy_lists_no_write_action() -> None:
    payload = json.loads(_run("policy", "--output", "json").stdout)
    for action in payload["iam_policy"]["Statement"][0]["Action"]:
        assert any(action.split(":")[1].startswith(v) for v in ("Describe", "List", "Get"))


def test_a_broken_policy_file_exits_three(tmp_path: Path) -> None:
    target = tmp_path / "policy.yaml"
    target.write_text("schema_version: 1\n", encoding="utf-8")
    result = _run("policy", "--policy-file", str(target))
    assert result.exit_code == EXIT_BAD_DATA
    assert "Error reading data" in result.stderr


def test_a_custom_threshold_file_is_honoured(tmp_path: Path, raw_policy: dict[str, Any]) -> None:
    raw_policy["thresholds"]["underused_invocations_per_instance_hour"]["value"] = 42.0
    target = tmp_path / "policy.yaml"
    target.write_text(yaml.safe_dump(raw_policy), encoding="utf-8")
    payload = json.loads(_run("policy", "--output", "json", "--policy-file", str(target)).stdout)
    assert payload["thresholds"]["underused_invocations_per_instance_hour"] == 42.0


# --------------------------------------------------------------------------- scanning


def test_scan_reports_findings_and_exits_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wasteful finding is a non-zero exit, so a scheduled scan can gate on it."""
    import sagemaker_idle_finder.cli as cli_module

    monkeypatch.setattr(cli_module, "scan", lambda *a, **k: _result(_finding()))
    result = _run("scan", "--regions", "eu-west-1")
    assert result.exit_code == EXIT_WASTE_FOUND
    assert "idle-one" in result.stdout
    assert "idle" in result.stdout
    assert "TOTAL" in result.stdout


def test_a_clean_account_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    import sagemaker_idle_finder.cli as cli_module

    healthy = _finding("busy", Verdict.HEALTHY, wasted=0.0, remedy=Remedy.NONE)
    monkeypatch.setattr(cli_module, "scan", lambda *a, **k: _result(healthy))
    result = _run("scan", "--regions", "eu-west-1")
    assert result.exit_code == EXIT_OK
    assert "No idle or underused endpoints found" in result.stdout


def test_json_output_is_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    import sagemaker_idle_finder.cli as cli_module

    monkeypatch.setattr(cli_module, "scan", lambda *a, **k: _result(_finding()))
    result = _run("scan", "--regions", "eu-west-1", "--output", "json")
    payload = json.loads(result.stdout)
    assert payload["regions"] == ["eu-west-1"]
    assert payload["lookback_days"] == 14
    assert payload["estimates"]["total_estimated_wasted_monthly_usd"] == 186.88
    assert payload["estimates"]["prices_published"]
    assert "estimates" in payload
    assert payload["findings"][0]["verdict"] == "idle"
    assert payload["findings"][0]["evidence"]


def test_csv_output_has_a_header_and_a_row(monkeypatch: pytest.MonkeyPatch) -> None:
    import sagemaker_idle_finder.cli as cli_module

    monkeypatch.setattr(cli_module, "scan", lambda *a, **k: _result(_finding()))
    result = _run("scan", "--regions", "eu-west-1", "--output", "csv")
    rows = list(csv.DictReader(io.StringIO(result.stdout)))
    assert len(rows) == 1
    assert rows[0]["endpoint"] == "idle-one"
    assert rows[0]["verdict"] == "idle"
    assert rows[0]["estimated_wasted_monthly_usd"] == "186.88"


def test_explain_prints_the_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    import sagemaker_idle_finder.cli as cli_module

    monkeypatch.setattr(cli_module, "scan", lambda *a, **k: _result(_finding()))
    result = _run("scan", "--regions", "eu-west-1", "--explain")
    assert "nothing invoked it" in result.stdout


def test_all_shows_healthy_variants_too(monkeypatch: pytest.MonkeyPatch) -> None:
    import sagemaker_idle_finder.cli as cli_module

    healthy = _finding("busy", Verdict.HEALTHY, wasted=0.0, remedy=Remedy.NONE)
    monkeypatch.setattr(cli_module, "scan", lambda *a, **k: _result(healthy))
    assert "busy" not in _run("scan", "--regions", "eu-west-1").stdout
    assert "busy" in _run("scan", "--regions", "eu-west-1", "--all").stdout


def test_a_failed_region_is_surfaced(monkeypatch: pytest.MonkeyPatch) -> None:
    import sagemaker_idle_finder.cli as cli_module

    monkeypatch.setattr(
        cli_module, "scan", lambda *a, **k: _result(_finding(), errors=(("us-east-1", "denied"),))
    )
    result = _run("scan", "--regions", "eu-west-1,us-east-1")
    assert "us-east-1 could not be scanned" in result.stdout


def test_regions_all_expands_to_every_priced_region(monkeypatch: pytest.MonkeyPatch) -> None:
    import sagemaker_idle_finder.cli as cli_module

    seen: list[list[str]] = []

    def fake_scan(factory: Any, regions: Any, *args: Any, **kwargs: Any) -> ScanResult:
        seen.append(list(regions))
        return _result()

    monkeypatch.setattr(cli_module, "scan", fake_scan)
    _run("scan", "--regions", "all")
    assert len(seen[0]) > 10


def test_an_invalid_lookback_is_a_usage_error() -> None:
    assert _run("scan", "--regions", "eu-west-1", "--lookback-days", "0").exit_code == EXIT_USAGE


# ---------------------------------------------------------------------------- render


def test_money_columns_are_labelled_as_estimates() -> None:
    from rich.console import Console

    console = Console(file=io.StringIO(), width=220, no_color=True)
    render.render_result(console, _result(_finding()), show_all=False)
    output = console.file.getvalue()  # type: ignore[attr-defined]
    assert "est. $/month" in output
    assert "est. wasted" in output
    assert "an estimate from on-demand list prices" in output


def test_an_unpriced_finding_says_so_rather_than_counting_as_zero() -> None:
    """A total that quietly omits an unpriced finding overstates how complete it is."""
    from rich.console import Console

    unpriced = _finding(monthly=None, wasted=None)
    result = _result(unpriced)
    assert len(result.unquantified_waste) == 1

    console = Console(file=io.StringIO(), width=220, no_color=True)
    render.render_result(console, result, show_all=True)
    output = console.file.getvalue()  # type: ignore[attr-defined]
    assert "have no dollar figure" in output
    assert "floor rather than the whole picture" in output
    assert "idle-one/AllTraffic" in output

    payload = render.result_to_dict(result)
    assert payload["estimates"]["findings_without_a_waste_estimate"] == 1
    assert payload["estimates"]["totals_are_complete"] is False


def test_serverless_shows_memory_rather_than_an_instance_count() -> None:
    serverless = Finding(
        endpoint=make_endpoint(),
        variant=make_variant(serverless_memory_mb=2048),
        verdict=Verdict.NOT_APPLICABLE,
        window=None,
        cost=CostEstimate(None, 0.0),
        remedy=Remedy.NONE,
        evidence="serverless",
    )
    from rich.console import Console

    console = Console(file=io.StringIO(), width=220, no_color=True)
    render.render_result(console, _result(serverless), show_all=True)
    assert "serverless 2048 MB" in console.file.getvalue()  # type: ignore[attr-defined]


def test_csv_columns_are_stable() -> None:
    text = render.result_to_csv(_result(_finding()))
    assert text.splitlines()[0] == ",".join(render.CSV_COLUMNS)


def test_an_empty_scan_renders_without_error() -> None:
    from rich.console import Console

    console = Console(file=io.StringIO(), width=220, no_color=True)
    render.render_result(console, _result(), show_all=False)
    render.render_detail(console, _result())
    assert "No idle or underused" in console.file.getvalue()  # type: ignore[attr-defined]


def test_no_data_renders_distinctly_from_zero() -> None:
    no_data = Finding(
        endpoint=make_endpoint(),
        variant=make_variant(),
        verdict=Verdict.IDLE,
        window=make_window(None),
        cost=CostEstimate(10.0, 10.0),
        remedy=Remedy.DELETE,
        evidence="no data",
    )
    from rich.console import Console

    console = Console(file=io.StringIO(), width=220, no_color=True)
    render.render_result(console, _result(no_data), show_all=True)
    assert "no data" in console.file.getvalue()  # type: ignore[attr-defined]


def test_the_result_dict_counts_every_verdict() -> None:
    payload = render.result_to_dict(_result(_finding()))
    assert set(payload["counts"]) == {verdict.value for verdict in Verdict}
    assert payload["counts"]["idle"] == 1


def test_timestamps_are_iso_formatted() -> None:
    payload = render.result_to_dict(_result(_finding()))
    assert dt.datetime.fromisoformat(payload["scanned_at"]) == NOW
    assert dt.datetime.fromisoformat(payload["findings"][0]["created_at"])


def test_status_reaches_the_output() -> None:
    failed = _finding(endpoint=make_endpoint(status=EndpointStatus.FAILED))
    payload = render.result_to_dict(_result(failed))
    assert payload["findings"][0]["status"] == "Failed"


def test_the_serverless_assumptions_must_be_given_together() -> None:
    """Half the assumptions would mean the tool filling in the other half with a guess."""
    for args in (
        ["scan", "--regions", "eu-west-1", "--serverless-seconds", "0.5"],
        ["scan", "--regions", "eu-west-1", "--serverless-memory-gb", "2"],
    ):
        result = runner.invoke(app, args)
        assert result.exit_code == EXIT_USAGE
        assert "go together" in result.output


# ------------------------------------------------------------------- table layout

NARROW = 70
WIDE = 132


def _render_at(width: int, result: ScanResult) -> str:
    from rich.console import Console

    stream = io.StringIO()
    render.render_result(Console(file=stream, width=width, no_color=True), result, show_all=True)
    return stream.getvalue()


@pytest.mark.parametrize("width", [60, 70, 80, 100, 118, 132, 200])
def test_the_table_fits_the_terminal_at_any_width(width: int) -> None:
    """A clipped table loses whichever column falls off the right, silently."""
    output = _render_at(width, _result(_finding(name="a-rather-long-endpoint-name")))
    for line in output.splitlines():
        assert len(line) <= width, f"{len(line)} > {width}: {line!r}"


@pytest.mark.parametrize("width", [60, 70, 80, 100, 118, 132])
def test_the_waste_and_the_remedy_survive_every_width(width: int) -> None:
    """These two are the reason to run the command, so they are never the ones dropped."""
    output = _render_at(width, _result(_finding()))
    assert "est. wasted" in output
    assert "Remedy" in output
    assert "$186.88" in output


def test_a_narrow_terminal_says_which_columns_it_dropped() -> None:
    output = _render_at(NARROW, _result(_finding()))
    assert "Invocations" not in output
    assert "too narrow" in output
    assert "invocations" in output


def test_a_wide_terminal_drops_nothing() -> None:
    output = _render_at(WIDE, _result(_finding()))
    assert "Invocations" in output
    assert "est. $/month" in output
    assert "too narrow" not in output


def test_the_region_column_appears_only_when_several_were_scanned() -> None:
    """Repeating one region on every row is width spent saying nothing."""
    one = _result(_finding())
    assert "Region" not in _render_at(WIDE, one)

    several = ScanResult(
        findings=one.findings,
        regions=("eu-west-1", "us-east-1"),
        lookback_days=14,
        started_at=NOW,
        prices_published="2026-09-22T23:07:50Z",
    )
    assert "Region" in _render_at(WIDE, several)


def test_the_variant_column_appears_only_when_an_endpoint_has_several() -> None:
    single = _result(_finding())
    assert "Variant" not in _render_at(WIDE, single)

    pair = ScanResult(
        findings=(
            _finding(name="shared"),
            _finding(name="shared", variant_name="Challenger"),
        ),
        regions=("eu-west-1",),
        lookback_days=14,
        started_at=NOW,
        prices_published="2026-09-22T23:07:50Z",
    )
    rendered = _render_at(WIDE, pair)
    assert "Variant" in rendered
    assert "Challenger" in rendered
