"""Parsing the documented schema, and surviving everything else.

These logs are written by a service and read after the fact. Truncation, partial delivery
and schema drift are normal, so the parser's job is to be specific about what it could not
use and to never stop the run.
"""

from __future__ import annotations

import datetime as dt

import pytest

from bedrock_log_lens.models import InvocationRecord, IssueKind, ParseIssue
from bedrock_log_lens.parse import parse_record
from tests.conftest import CLAUDE, NOW, ROLE, make_raw


def parsed(**kwargs: object) -> InvocationRecord:
    result = parse_record(make_raw(**kwargs))  # type: ignore[arg-type]
    assert isinstance(result, InvocationRecord), result
    return result


def issue(**kwargs: object) -> ParseIssue:
    result = parse_record(make_raw(**kwargs))  # type: ignore[arg-type]
    assert isinstance(result, ParseIssue), result
    return result


# ------------------------------------------------------------------ the happy path


def test_a_documented_record_parses_into_metadata() -> None:
    record = parsed()
    assert record.model_id == CLAUDE
    assert record.identity_arn == ROLE
    assert record.input_tokens == 120
    assert record.output_tokens == 340
    assert record.operation == "Converse"
    assert record.region == "us-east-1"
    assert record.account_id == "123456789012"
    assert record.timestamp == NOW


def test_cache_token_counts_are_read_when_present() -> None:
    record = parsed(cache_read=4_000, cache_write=1_000)
    assert record.cache_read_tokens == 4_000
    assert record.cache_write_tokens == 1_000
    assert record.total_tokens == 120 + 340 + 4_000 + 1_000


def test_a_record_without_bodies_still_parses() -> None:
    """Bodies are absent when Text data delivery is off, which is a normal configuration."""
    record = parsed(include_bodies=False)
    assert record.input_tokens == 120
    assert record.invocation_latency_ms is None


def test_latency_is_read_when_the_body_carries_it() -> None:
    assert parsed().invocation_latency_ms == 1_450
    assert parsed().first_byte_latency_ms == 310


@pytest.mark.parametrize(
    "operation",
    ["InvokeModel", "InvokeModelWithResponseStream", "Converse", "ConverseStream"],
)
def test_every_documented_operation_is_accepted(operation: str) -> None:
    assert parsed(operation=operation).operation == operation


def test_an_unknown_operation_is_kept_rather_than_rejected() -> None:
    """AWS adds operations. A new one should appear in the report as itself."""
    assert parsed(operation="ConverseWithSomethingNew").operation == "ConverseWithSomethingNew"


# --------------------------------------------------------------------- timestamps


@pytest.mark.parametrize(
    "value",
    [
        "2026-09-24T12:00:00Z",
        "2026-09-24T12:00:00+00:00",
        "2026-09-24T12:00:00.123456Z",
    ],
)
def test_iso_timestamps_are_read(value: str) -> None:
    assert parsed(timestamp=value).timestamp.year == 2026


def test_epoch_seconds_and_milliseconds_are_both_read() -> None:
    """Re-exported copies of these logs often carry epoch times."""
    epoch = NOW.timestamp()
    assert parsed(timestamp=epoch).timestamp == NOW
    assert parsed(timestamp=epoch * 1000).timestamp == NOW


def test_a_naive_timestamp_is_treated_as_utc() -> None:
    record = parsed(timestamp="2026-09-24T12:00:00")
    assert record.timestamp.tzinfo is not None
    assert record.timestamp == NOW


def test_an_unreadable_timestamp_is_reported_not_guessed() -> None:
    assert issue(timestamp="last Tuesday").kind is IssueKind.BAD_TIMESTAMP


def test_a_missing_timestamp_is_reported() -> None:
    raw = make_raw()
    del raw["timestamp"]
    result = parse_record(raw)
    assert isinstance(result, ParseIssue)
    assert result.kind is IssueKind.MISSING_TIMESTAMP


# ------------------------------------------------------------------- what is skipped


def test_a_non_object_is_not_a_record() -> None:
    for value in ([1, 2, 3], "a string", 42, None):
        result = parse_record(value)
        assert isinstance(result, ParseIssue)
        assert result.kind is IssueKind.NOT_A_RECORD


def test_a_different_schema_type_is_skipped() -> None:
    raw = make_raw()
    raw["schemaType"] = "SomethingElse"
    result = parse_record(raw)
    assert isinstance(result, ParseIssue)
    assert result.kind is IssueKind.WRONG_SCHEMA_TYPE
    assert "SomethingElse" in result.detail


def test_a_future_major_schema_version_is_skipped_rather_than_guessed() -> None:
    """Field names could mean anything in a version this parser has not seen."""
    raw = make_raw()
    raw["schemaVersion"] = "2.0"
    result = parse_record(raw)
    assert isinstance(result, ParseIssue)
    assert result.kind is IssueKind.UNSUPPORTED_SCHEMA_VERSION


def test_a_later_minor_version_is_still_read() -> None:
    raw = make_raw()
    raw["schemaVersion"] = "1.7"
    assert isinstance(parse_record(raw), InvocationRecord)


def test_a_record_without_a_model_id_is_reported() -> None:
    assert issue(model_id="").kind is IssueKind.MISSING_MODEL_ID


def test_a_record_with_no_token_counts_is_reported_not_counted_as_zero() -> None:
    """Counting it as zero tokens would quietly shrink every total it belongs to."""
    result = issue(input_tokens=None, output_tokens=None)
    assert result.kind is IssueKind.MISSING_TOKEN_COUNTS
    assert CLAUDE in result.detail


def test_an_error_record_without_token_counts_is_kept() -> None:
    """A throttled or failed call really did happen, and costs nothing. It is not an issue."""
    record = parsed(input_tokens=None, output_tokens=None, error_code="ThrottlingException")
    assert record.error_code == "ThrottlingException"
    assert record.has_token_counts is False


def test_one_missing_count_is_enough_to_keep_the_record() -> None:
    record = parsed(output_tokens=None)
    assert record.input_tokens == 120
    assert record.output_tokens is None


def test_a_missing_identity_becomes_empty_not_a_crash() -> None:
    assert parsed(identity=None).identity_arn == ""


def test_negative_and_non_numeric_counts_are_refused() -> None:
    assert parsed(input_tokens=-5).input_tokens is None
    raw = make_raw()
    raw["input"]["inputTokenCount"] = "many"
    result = parse_record(raw)
    assert isinstance(result, InvocationRecord)
    assert result.input_tokens is None


def test_a_malformed_input_section_does_not_stop_the_record() -> None:
    """The output count survives, and one count is enough to keep the record."""
    raw = make_raw()
    raw["input"] = "not an object"
    result = parse_record(raw)
    assert isinstance(result, InvocationRecord)
    assert result.input_tokens is None
    assert result.output_tokens == 340


def test_both_sections_malformed_is_reported() -> None:
    raw = make_raw()
    raw["input"] = "not an object"
    raw["output"] = ["nor", "is", "this"]
    result = parse_record(raw)
    assert isinstance(result, ParseIssue)
    assert result.kind is IssueKind.MISSING_TOKEN_COUNTS


def test_grouping_helpers_truncate_correctly() -> None:
    record = parsed(timestamp="2026-09-24T12:34:56Z")
    assert record.hour == dt.datetime(2026, 9, 24, 12, 0, tzinfo=dt.UTC)
    assert record.day == dt.date(2026, 9, 24)
