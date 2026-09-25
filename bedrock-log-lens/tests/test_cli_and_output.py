"""The CLI, the three output formats, and the content command.

The privacy tests here are the ones that matter: they run the real commands over fixtures
whose prompts contain a canary, and assert it appears in none of the output.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from rich.console import Console
from typer.testing import CliRunner

from bedrock_log_lens import __version__, html, render
from bedrock_log_lens.analyse import analyse
from bedrock_log_lens.catalog import Policy, Prices
from bedrock_log_lens.cli import EXIT_ANOMALIES_FOUND, EXIT_BAD_DATA, EXIT_OK, app
from bedrock_log_lens.read import read_directory
from tests.conftest import CANARY, CLAUDE, NOW, ROLE, make_raw, write_log_dir

EXIT_USAGE = 2
runner = CliRunner(env={"COLUMNS": "200", "TERM": "dumb", "NO_COLOR": "1"})


def busy_logs(tmp_path: Path, count: int = 120) -> Path:
    """A directory with enough traffic to trip the heuristics."""
    import datetime as dt

    records = [
        make_raw(
            timestamp=NOW + dt.timedelta(seconds=index * 0.5),
            request_id=f"req-{index:04d}",
            input_tokens=8_192,
            output_tokens=200 + index,
        )
        for index in range(count)
    ]
    write_log_dir(tmp_path, records, gzipped=True)
    return tmp_path


# -------------------------------------------------------------- content never leaks


def test_the_terminal_report_never_contains_a_prompt(
    tmp_path: Path, policy: Policy, prices: Prices
) -> None:
    import io

    report = analyse(read_directory(busy_logs(tmp_path)), policy, prices, sources_read=1)
    stream = io.StringIO()
    render.render_report(Console(file=stream, width=200, no_color=True), report, policy.disclaimer)

    assert CANARY not in stream.getvalue()


def test_the_json_output_never_contains_a_prompt(
    tmp_path: Path, policy: Policy, prices: Prices
) -> None:
    report = analyse(read_directory(busy_logs(tmp_path)), policy, prices, sources_read=1)
    assert CANARY not in json.dumps(render.report_to_dict(report, policy.disclaimer))


def test_the_html_output_never_contains_a_prompt(
    tmp_path: Path, policy: Policy, prices: Prices
) -> None:
    report = analyse(read_directory(busy_logs(tmp_path)), policy, prices, sources_read=1)
    assert CANARY not in html.render_html(report, policy.disclaimer, NOW)


def test_the_cli_never_prints_a_prompt_in_any_format(tmp_path: Path) -> None:
    """End to end, through the real commands, over logs full of prompts."""
    logs = busy_logs(tmp_path)
    report_path = tmp_path / "report.html"

    text_run = runner.invoke(app, ["analyse", str(logs)])
    json_run = runner.invoke(app, ["analyse", str(logs), "--output", "json"])
    html_run = runner.invoke(app, ["analyse", str(logs), "--html", str(report_path)])

    assert CANARY not in text_run.output
    assert CANARY not in json_run.output
    assert CANARY not in html_run.output
    assert CANARY not in report_path.read_text(encoding="utf-8")


# ------------------------------------------------------------------- the analysis


def test_the_report_counts_what_it_read(tmp_path: Path) -> None:
    result = runner.invoke(app, ["analyse", str(busy_logs(tmp_path, count=30)), "--output", "json"])
    payload = json.loads(result.output)

    assert payload["totals"]["requests"] == 30
    assert payload["totals"]["input_tokens"] == 30 * 8_192
    assert payload["by_model"][0]["key"] == CLAUDE
    assert payload["by_identity"][0]["key"] == ROLE
    assert payload["privacy"].startswith("This report is built from log metadata only")


def test_a_loop_is_reported_and_sets_the_exit_code(tmp_path: Path) -> None:
    result = runner.invoke(app, ["analyse", str(busy_logs(tmp_path)), "--output", "json"])

    assert result.exit_code == EXIT_ANOMALIES_FOUND
    payload = json.loads(result.output)
    kinds = {item["kind"] for item in payload["anomalies"]}
    assert "repeated_shape" in kinds
    assert all(item["heuristic"] is True for item in payload["anomalies"])


def test_quiet_logs_exit_zero(tmp_path: Path) -> None:
    import datetime as dt

    write_log_dir(
        tmp_path,
        [
            make_raw(timestamp=NOW + dt.timedelta(minutes=index * 10), request_id=f"q-{index}")
            for index in range(5)
        ],
    )
    result = runner.invoke(app, ["analyse", str(tmp_path)])
    assert result.exit_code == EXIT_OK


def test_unusable_records_are_reported_not_hidden(tmp_path: Path) -> None:
    path = tmp_path / "mixed.json"
    good = json.dumps(make_raw(request_id="good"))
    path.write_text(f"{good}\nnot json at all\n{good}\n")

    result = runner.invoke(app, ["analyse", str(path), "--output", "json"])
    payload = json.loads(result.output)

    assert payload["totals"]["requests"] == 2
    assert payload["skipped_records"]["total"] == 1
    assert payload["skipped_records"]["by_reason"]["bad_json"] == 1


def test_an_empty_directory_is_an_error_not_an_empty_report(tmp_path: Path) -> None:
    """A zero-record report would look like a quiet account rather than a wrong path."""
    result = runner.invoke(app, ["analyse", str(tmp_path)])
    assert result.exit_code == 4
    assert "No log records" in result.output


def test_a_missing_path_is_a_usage_error(tmp_path: Path) -> None:
    result = runner.invoke(app, ["analyse", str(tmp_path / "nope")])
    assert result.exit_code == EXIT_USAGE


def test_a_local_path_and_s3_together_is_a_usage_error(tmp_path: Path) -> None:
    result = runner.invoke(app, ["analyse", str(tmp_path), "--s3", "s3://logs/prefix"])
    assert result.exit_code == EXIT_USAGE
    assert "either" in result.output


def test_thresholds_can_be_relaxed_from_the_command_line(tmp_path: Path) -> None:
    logs = busy_logs(tmp_path)
    strict = runner.invoke(app, ["analyse", str(logs), "--output", "json"])
    relaxed = runner.invoke(
        app,
        ["analyse", str(logs), "--output", "json", "--burst-count", "10000", "--top", "3"],
    )

    assert json.loads(strict.output)["anomalies"]
    assert len(json.loads(relaxed.output)["most_expensive"]) <= 3


# ------------------------------------------------------------------------ the HTML


def test_the_html_report_is_one_self_contained_file(
    tmp_path: Path, policy: Policy, prices: Prices
) -> None:
    """It gets attached to tickets and opened offline, so nothing may be fetched."""
    report = analyse(read_directory(busy_logs(tmp_path)), policy, prices, sources_read=1)
    page = html.render_html(report, policy.disclaimer, NOW)

    assert page.startswith("<!doctype html>")
    assert "<script" not in page.lower()
    assert "http://" not in page and "https://" not in page
    assert "<style>" in page


def test_the_html_escapes_identifiers_from_the_logs(
    tmp_path: Path, policy: Policy, prices: Prices
) -> None:
    """Model IDs and ARNs come from a file this tool did not write."""
    write_log_dir(
        tmp_path,
        [make_raw(identity="arn:aws:sts::1:assumed-role/<script>alert(1)</script>/s")],
    )
    report = analyse(read_directory(tmp_path), policy, prices, sources_read=1)
    page = html.render_html(report, policy.disclaimer, NOW)

    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


# --------------------------------------------------------------------- show-content


def test_show_content_refuses_without_the_flag(tmp_path: Path) -> None:
    write_log_dir(tmp_path, [make_raw(request_id="wanted")])
    result = runner.invoke(app, ["show-content", str(tmp_path), "--request-id", "wanted"])

    assert CANARY not in result.output
    assert "Refusing" in result.output


def test_show_content_prints_the_prompt_when_asked(tmp_path: Path) -> None:
    """The one path that shows content, and only for the request named."""
    write_log_dir(
        tmp_path,
        [make_raw(request_id="wanted"), make_raw(request_id="other")],
    )
    result = runner.invoke(
        app,
        ["show-content", str(tmp_path), "--request-id", "wanted", "--show-content"],
    )

    assert result.exit_code == EXIT_OK
    assert CANARY in result.output
    assert "wanted" in result.output


def test_show_content_warns_every_time(tmp_path: Path) -> None:
    write_log_dir(tmp_path, [make_raw(request_id="wanted")])
    result = runner.invoke(
        app, ["show-content", str(tmp_path), "--request-id", "wanted", "--show-content"]
    )
    # The warning is wrapped to the terminal, so compare it as words rather than as a line.
    flattened = " ".join(result.output.split())
    assert "may include personal data" in flattened
    assert "Nothing is redacted" in flattened


def test_show_content_says_when_nothing_matched(tmp_path: Path) -> None:
    write_log_dir(tmp_path, [make_raw(request_id="wanted")])
    result = runner.invoke(
        app, ["show-content", str(tmp_path), "--request-id", "absent", "--show-content"]
    )
    assert "None of those request ids were found" in " ".join(result.output.split())
    assert CANARY not in result.output


# ----------------------------------------------------------------------- the policy


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == EXIT_OK
    assert __version__ in result.output


def test_the_printed_iam_policy_is_generated_from_the_allowlist(policy: Policy) -> None:
    """The README quotes this. If it could drift from the code it would eventually lie."""
    result = runner.invoke(app, ["policy", "--output", "json"])
    payload = json.loads(result.output)

    printed = {
        action for statement in payload["iam_policy"]["Statement"] for action in statement["Action"]
    }
    expected = {
        policy.aws.iam_actions[operation] for operation in policy.aws.read_only_operations["s3"]
    }
    assert printed == expected


def test_the_policy_grants_nothing_that_writes() -> None:
    payload = json.loads(runner.invoke(app, ["policy", "--output", "json"]).output)
    for statement in payload["iam_policy"]["Statement"]:
        for action in statement["Action"]:
            assert not action.startswith(("s3:Put", "s3:Delete", "s3:Create", "s3:Replicate"))


def test_listing_is_scoped_to_the_log_prefix() -> None:
    payload = json.loads(runner.invoke(app, ["policy", "--output", "json"]).output)
    listing = payload["iam_policy"]["Statement"][0]
    assert "BedrockModelInvocationLogs" in listing["Condition"]["StringLike"]["s3:prefix"][0]


def test_a_broken_data_file_is_reported(tmp_path: Path) -> None:
    from importlib import resources

    raw = yaml.safe_load(
        resources.files("bedrock_log_lens").joinpath("data/policy.yaml").read_text(encoding="utf-8")
    )
    del raw["thresholds"]["burst_request_count"]["rationale"]
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    result = runner.invoke(app, ["policy", "--policy-file", str(path)])

    assert result.exit_code == EXIT_BAD_DATA
    assert "rationale" in result.output


@pytest.mark.parametrize("width", [80, 120, 200])
def test_the_report_fits_the_terminal(
    tmp_path: Path, policy: Policy, prices: Prices, width: int
) -> None:
    import io

    report = analyse(read_directory(busy_logs(tmp_path)), policy, prices, sources_read=1)
    stream = io.StringIO()
    render.render_report(
        Console(file=stream, width=width, no_color=True), report, policy.disclaimer
    )

    for line in stream.getvalue().splitlines():
        assert len(line) <= width, f"{len(line)} > {width}: {line!r}"
