"""The command line interface, driven end to end through Typer's test runner.

No AWS is involved at any point: the tool makes no AWS API calls.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from sagemaker_inference_picker import __version__
from sagemaker_inference_picker.cli import (
    EXIT_BAD_LIMITS,
    EXIT_NO_VIABLE_OPTION,
    EXIT_OK,
    app,
)

EXIT_USAGE = 2

#: A wide terminal so Rich does not wrap the phrases the assertions look for.
runner = CliRunner(env={"COLUMNS": "200", "TERM": "dumb", "NO_COLOR": "1"})

#: A workload that every option could serve, for tests that vary one thing at a time.
BASELINE = [
    "recommend",
    "--payload-mb",
    "1",
    "--response-mb",
    "1",
    "--processing-seconds",
    "5",
    "--traffic",
    "steady",
]


def _run(*args: str, stdin: str | None = None) -> Any:
    return runner.invoke(app, list(args), input=stdin)


# ------------------------------------------------------------------------------ basics


def test_version() -> None:
    result = _run("--version")
    assert result.exit_code == EXIT_OK
    assert __version__ in result.stdout


def test_bare_invocation_shows_help() -> None:
    result = _run()
    assert "recommend" in result.stdout
    assert "limits" in result.stdout


def test_help_states_that_the_tool_is_read_only() -> None:
    result = _run("--help")
    assert "no AWS API calls" in result.stdout.replace("\n", " ")


# --------------------------------------------------------------------------- recommend


def test_recommend_from_flags() -> None:
    result = _run(*BASELINE, "--immediate-response")
    assert result.exit_code == EXIT_OK
    assert "Real-time endpoint" in result.stdout
    assert "Ruled out" in result.stdout


def test_recommend_reports_a_conflict_and_exits_one() -> None:
    result = _run(
        "recommend",
        "--payload-mb",
        "1",
        "--response-mb",
        "1",
        "--processing-seconds",
        "10",
        "--traffic",
        "bursty-idle",
        "--gpu",
        "--zero-idle-cost",
        "--immediate-response",
    )
    assert result.exit_code == EXIT_NO_VIABLE_OPTION
    assert "No SageMaker inference option satisfies" in result.stdout
    assert "Conflicting requirements" in result.stdout


def test_a_latency_target_alone_rules_out_the_offline_options() -> None:
    result = _run(*BASELINE, "--latency-p99-ms", "250")
    assert result.exit_code == EXIT_OK
    assert "Real-time endpoint" in result.stdout


@pytest.mark.parametrize(
    "missing",
    [
        ["--payload-mb", "1"],
        ["--payload-mb", "1", "--response-mb", "1"],
        ["--payload-mb", "1", "--response-mb", "1", "--processing-seconds", "5"],
    ],
)
def test_incomplete_flags_are_a_usage_error_naming_what_is_missing(
    missing: list[str],
) -> None:
    result = _run("recommend", *missing)
    assert result.exit_code == EXIT_USAGE
    combined = (result.stdout + result.stderr).replace("\n", " ")
    assert "missing required option" in combined
    assert "--interactive" in combined


def test_a_negative_payload_is_a_usage_error() -> None:
    result = _run(
        "recommend",
        "--payload-mb",
        "-1",
        "--response-mb",
        "1",
        "--processing-seconds",
        "5",
        "--traffic",
        "steady",
    )
    assert result.exit_code == EXIT_USAGE
    assert "must not be negative" in (result.stdout + result.stderr).replace("\n", " ")


def test_zero_models_is_a_usage_error() -> None:
    result = _run(*BASELINE, "--models", "0")
    assert result.exit_code == EXIT_USAGE


def test_an_unknown_traffic_pattern_is_a_usage_error() -> None:
    result = _run(
        "recommend",
        "--payload-mb",
        "1",
        "--response-mb",
        "1",
        "--processing-seconds",
        "5",
        "--traffic",
        "whenever",
    )
    assert result.exit_code == EXIT_USAGE


# -------------------------------------------------------------------------- JSON output


def test_recommend_json_is_valid_and_complete() -> None:
    result = _run(*BASELINE, "--immediate-response", "--output", "json")
    assert result.exit_code == EXIT_OK
    payload = json.loads(result.stdout)
    assert payload["recommended"] == "real_time"
    assert payload["resolved"] is True
    assert payload["headline"]
    assert payload["workload"]["payload_mb"] == 1.0
    assert payload["workload"]["traffic"] == "steady"
    assert payload["limits"]["last_verified"]
    assert payload["deciding_factors"]
    assert payload["conflicts"] == []
    rejected = {entry["option"] for entry in payload["rejected"]}
    assert rejected == {"async", "batch_transform"}
    for entry in payload["rejected"]:
        for reason in entry["reasons"]:
            assert reason["source"].startswith("https://")
            assert reason["constraint_id"]


def test_recommend_json_records_the_numeric_limit_that_was_breached() -> None:
    result = _run(
        "recommend",
        "--payload-mb",
        "800",
        "--response-mb",
        "1",
        "--processing-seconds",
        "5",
        "--traffic",
        "bursty-idle",
        "--output",
        "json",
    )
    payload = json.loads(result.stdout)
    real_time = next(e for e in payload["rejected"] if e["option"] == "real_time")
    reason = next(
        r for r in real_time["reasons"] if r["constraint_id"] == "request_payload_over_limit"
    )
    assert reason["limit"] == "25 MB"
    assert reason["actual"] == "800 MB"


def test_conflict_json_still_exits_one_and_names_the_conflicts() -> None:
    result = _run(
        "recommend",
        "--payload-mb",
        "1",
        "--response-mb",
        "1",
        "--processing-seconds",
        "10",
        "--traffic",
        "steady",
        "--immediate-response",
        "--needs-notification",
        "--output",
        "json",
    )
    assert result.exit_code == EXIT_NO_VIABLE_OPTION
    payload = json.loads(result.stdout)
    assert payload["recommended"] is None
    assert payload["resolved"] is False
    assert payload["conflicts"]
    assert payload["conflict_summary"]
    constraint_ids = {conflict["constraint_id"] for conflict in payload["conflicts"]}
    assert constraint_ids == {"no_inline_response", "no_native_completion_notification"}


def test_json_output_keeps_stdout_free_of_rich_formatting() -> None:
    result = _run(*BASELINE, "--immediate-response", "--output", "json")
    assert result.stdout.lstrip().startswith("{")
    json.loads(result.stdout)


def test_advice_appears_in_json() -> None:
    result = _run(
        "recommend",
        "--payload-mb",
        "1",
        "--response-mb",
        "1",
        "--processing-seconds",
        "2",
        "--traffic",
        "bursty-idle",
        "--immediate-response",
        "--gpu",
        "--models",
        "12",
        "--output",
        "json",
    )
    payload = json.loads(result.stdout)
    advice_ids = {entry["id"] for entry in payload["advice"]}
    assert {"multi_model_endpoint", "inference_components"} <= advice_ids
    for entry in payload["advice"]:
        assert entry["source"].startswith("https://")


# ------------------------------------------------------------------------------ limits


def test_limits_table_lists_every_option_and_its_sources() -> None:
    result = _run("limits")
    assert result.exit_code == EXIT_OK
    for name in (
        "Real-time endpoint",
        "Serverless inference",
        "Asynchronous inference",
        "Batch transform",
    ):
        assert name in result.stdout
    assert "Sources" in result.stdout
    assert "https://docs.aws.amazon.com" in result.stdout
    assert "last verified" in result.stdout


def test_limits_json_carries_every_value_with_its_source() -> None:
    result = _run("limits", "--output", "json")
    assert result.exit_code == EXIT_OK
    payload = json.loads(result.stdout)
    assert set(payload["options"]) == {
        "real_time",
        "serverless",
        "async",
        "batch_transform",
    }
    assert payload["options"]["serverless"]["limits"]["max_request_payload_mb"]["value"] == 4
    assert payload["options"]["serverless"]["capabilities"]["gpu_supported"]["value"] is False
    for option in payload["options"].values():
        for section in ("limits", "capabilities", "facts"):
            for entry in option[section].values():
                assert entry["source"].startswith("https://")
    for heuristic in payload["heuristics"].values():
        assert heuristic["rationale"]
    assert payload["tie_break_order"]


# ------------------------------------------------------------------------- limits file


def _write_limits(path: Path, raw: dict[str, Any]) -> Path:
    target = path / "limits.yaml"
    target.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return target


def test_a_custom_limits_file_is_honoured(tmp_path: Path, raw_limits: dict[str, Any]) -> None:
    raw_limits["options"]["serverless"]["limits"]["max_request_payload_mb"]["value"] = 512
    target = _write_limits(tmp_path, raw_limits)
    result = _run(
        "recommend",
        "--payload-mb",
        "100",
        "--response-mb",
        "1",
        "--processing-seconds",
        "5",
        "--traffic",
        "bursty-idle",
        "--zero-idle-cost",
        "--immediate-response",
        "--limits-file",
        str(target),
        "--output",
        "json",
    )
    assert result.exit_code == EXIT_OK
    payload = json.loads(result.stdout)
    assert payload["recommended"] == "serverless"
    assert payload["limits"]["origin"] == str(target)


def test_a_missing_limits_file_exits_three(tmp_path: Path) -> None:
    result = _run(*BASELINE, "--limits-file", str(tmp_path / "absent.yaml"))
    assert result.exit_code == EXIT_BAD_LIMITS
    assert "Error reading limits" in result.stderr


def test_a_limits_file_missing_a_source_exits_three(
    tmp_path: Path, raw_limits: dict[str, Any]
) -> None:
    del raw_limits["options"]["serverless"]["limits"]["max_request_payload_mb"]["source"]
    target = _write_limits(tmp_path, raw_limits)
    result = _run(*BASELINE, "--limits-file", str(target))
    assert result.exit_code == EXIT_BAD_LIMITS
    assert "source" in result.stderr


def test_limits_command_also_validates_the_file(tmp_path: Path) -> None:
    target = tmp_path / "limits.yaml"
    target.write_text("schema_version: 1\n", encoding="utf-8")
    result = _run("limits", "--limits-file", str(target))
    assert result.exit_code == EXIT_BAD_LIMITS


# ------------------------------------------------------------------- interactive mode


def _answers(
    payload: str = "1",
    response: str = "1",
    processing: str = "5",
    traffic: str = "steady",
    latency: str = "",
    immediate: str = "y",
    notification: str = "n",
    zero_idle: str = "n",
    gpu: str = "n",
    models: str = "1",
) -> str:
    return (
        "\n".join(
            [
                payload,
                response,
                processing,
                traffic,
                latency,
                immediate,
                notification,
                zero_idle,
                gpu,
                models,
            ]
        )
        + "\n"
    )


def test_interactive_mode_asks_for_everything_and_recommends() -> None:
    result = _run("recommend", "--interactive", stdin=_answers())
    assert result.exit_code == EXIT_OK
    assert "Request payload size" in result.stdout
    assert "Traffic pattern" in result.stdout
    assert "How many models?" in result.stdout
    assert "Real-time endpoint" in result.stdout


def test_interactive_mode_accepts_defaults_on_empty_input() -> None:
    result = _run("recommend", "--interactive", stdin="\n" * 10)
    assert result.exit_code == EXIT_OK
    assert "Recommendation" in result.stdout


def test_interactive_mode_feeds_the_engine_the_given_answers() -> None:
    result = _run(
        "recommend",
        "--interactive",
        "--output",
        "json",
        stdin=_answers(payload="800", processing="1800", traffic="bursty-idle", immediate="n"),
    )
    assert result.exit_code == EXIT_OK
    payload = json.loads(result.stdout)
    assert payload["workload"]["payload_mb"] == 800.0
    assert payload["workload"]["processing_seconds"] == 1800.0
    assert payload["workload"]["traffic"] == "bursty-idle"
    assert payload["recommended"] == "async"


def test_interactive_mode_reasks_after_a_bad_number() -> None:
    result = _run("recommend", "--interactive", stdin=_answers(payload="not-a-number\n1"))
    assert result.exit_code == EXIT_OK
    assert "Recommendation" in result.stdout


def test_interactive_mode_rejects_a_negative_size_and_reasks() -> None:
    result = _run("recommend", "--interactive", stdin=_answers(payload="-5\n2"))
    assert result.exit_code == EXIT_OK
    assert "zero or a positive number" in result.stdout


def test_interactive_mode_rejects_a_non_positive_latency_and_reasks() -> None:
    result = _run("recommend", "--interactive", stdin=_answers(latency="0\n150"))
    assert result.exit_code == EXIT_OK
    assert "greater than zero" in result.stdout


def test_interactive_mode_rejects_a_non_numeric_latency_and_reasks() -> None:
    result = _run("recommend", "--interactive", stdin=_answers(latency="soon\n"))
    assert result.exit_code == EXIT_OK
    assert "leave it blank for none" in result.stdout


def test_interactive_mode_rejects_zero_models_and_reasks() -> None:
    result = _run("recommend", "--interactive", stdin=_answers(models="0\n3"))
    assert result.exit_code == EXIT_OK
    assert "at least one model" in result.stdout


def test_a_surviving_but_unchosen_option_is_still_reported() -> None:
    """An option that fits but ranks lower must not silently vanish from the output."""
    args = (
        "recommend",
        "--payload-mb",
        "12",
        "--response-mb",
        "3",
        "--processing-seconds",
        "240",
        "--traffic",
        "bursty-idle",
        "--zero-idle-cost",
        "--needs-notification",
        "--gpu",
    )
    text = _run(*args)
    assert text.exit_code == EXIT_OK
    assert "Also viable" in text.stdout
    assert "Batch transform" in text.stdout

    payload = json.loads(_run(*args, "--output", "json").stdout)
    assert payload["recommended"] == "async"
    assert any("Batch transform" in line for line in payload["runners_up"])
    assert [entry["option"] for entry in payload["ranked"]] == ["async", "batch_transform"]
