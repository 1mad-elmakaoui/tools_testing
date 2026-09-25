"""The guarded client: the allowlist, pagination and backoff.

Nothing here builds a real client. The doubles answer the way botocore does, including the
parts that are easy to get wrong: ListShards refusing a StreamName alongside its NextToken,
and throttling arriving as an exception class built at runtime.
"""

from __future__ import annotations

from typing import Any

import pytest

from kinesis_skew.aws import (
    AwsError,
    CallRecorder,
    ReadOnlyClient,
    ReadOnlyViolationError,
    RetryPolicy,
    _is_throttling,
    _method_name,
)

READS = frozenset({"ListStreams", "ListShards", "DescribeStreamSummary"})


class FakeClient:
    """Records calls and replays scripted responses or errors."""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        def method(**kwargs: Any) -> Any:
            self.calls.append((name, kwargs))
            value = self.responses.get(name, {})
            result = value.pop(0) if isinstance(value, list) else value
            if isinstance(result, Exception):
                raise result
            return result

        return method


class ThrottlingException(Exception):  # noqa: N818
    """Stands in for the class botocore builds at runtime.

    Deliberately named without an Error suffix: the detection works off the class name AWS
    actually uses, so the double has to carry the same one.
    """


def _client(
    fake: FakeClient,
    allowed: frozenset[str] = READS,
    retry: RetryPolicy | None = None,
    recorder: CallRecorder | None = None,
    service: str = "kinesis",
) -> ReadOnlyClient:
    policy = retry or RetryPolicy(max_retries=2, base_delay_seconds=0.0, sleep=lambda _: None)
    return ReadOnlyClient(fake, service, allowed, policy, recorder)  # type: ignore[arg-type]


# ------------------------------------------------------------------ the allowlist


@pytest.mark.parametrize(
    "operation",
    [
        "DeleteStream",
        "CreateStream",
        "PutRecord",
        "PutRecords",
        "MergeShards",
        "SplitShard",
        "EnableEnhancedMonitoring",
        "UpdateShardCount",
        "DecreaseStreamRetentionPeriod",
    ],
)
def test_a_write_is_refused_before_it_reaches_the_network(operation: str) -> None:
    """The point of the wrapper: a future edit reaching for a write fails here."""
    fake = FakeClient()
    with pytest.raises(ReadOnlyViolationError, match="read-only"):
        _client(fake).call(operation)
    assert fake.calls == [], "nothing should have reached the client"


def test_enabling_enhanced_monitoring_is_refused_however_helpful_it_would_be() -> None:
    """The tool tells you to run it. It must never run it itself."""
    fake = FakeClient()
    with pytest.raises(ReadOnlyViolationError):
        _client(fake).call("EnableEnhancedMonitoring", StreamName="s")
    assert fake.calls == []


def test_sampling_is_refused_unless_the_allowlist_includes_it() -> None:
    """--sample-keys builds the allowlist, so a plain run cannot reach GetRecords."""
    fake = FakeClient()
    with pytest.raises(ReadOnlyViolationError, match="GetRecords"):
        _client(fake).call("GetRecords", ShardIterator="x")
    assert fake.calls == []

    permitted = _client(fake, allowed=READS | {"GetRecords"})
    permitted.call("GetRecords", ShardIterator="x")
    assert fake.calls == [("get_records", {"ShardIterator": "x"})]


def test_the_refusal_names_what_is_allowed() -> None:
    with pytest.raises(ReadOnlyViolationError, match="DescribeStreamSummary, ListShards"):
        _client(FakeClient()).call("DeleteStream")


def test_a_refused_operation_is_not_recorded_as_called() -> None:
    recorder = CallRecorder()
    with pytest.raises(ReadOnlyViolationError):
        _client(FakeClient(), recorder=recorder).call("DeleteStream")
    assert recorder.calls == []


def test_pagination_is_guarded_too() -> None:
    """Hand-rolled precisely so no page can slip past the allowlist."""
    with pytest.raises(ReadOnlyViolationError):
        list(_client(FakeClient()).paginate("DeleteStream", "Shards"))


# ------------------------------------------------------------------- pagination


def test_every_page_is_followed() -> None:
    fake = FakeClient(
        {
            "list_streams": [
                {"StreamNames": ["a", "b"], "NextToken": "t1"},
                {"StreamNames": ["c"]},
            ]
        }
    )
    assert list(_client(fake).paginate("ListStreams", "StreamNames")) == ["a", "b", "c"]


def test_first_page_arguments_are_dropped_once_a_token_exists() -> None:
    """ListShards refuses a request carrying both its NextToken and the StreamName.

    Getting this wrong fails on the second page, which a small test stream never reaches.
    """
    fake = FakeClient(
        {
            "list_shards": [
                {"Shards": [{"ShardId": "s-1"}], "NextToken": "t1"},
                {"Shards": [{"ShardId": "s-2"}]},
            ]
        }
    )
    shards = list(
        _client(fake).paginate(
            "ListShards",
            "Shards",
            first_page_only=("StreamName", "ShardFilter"),
            StreamName="orders",
            ShardFilter={"Type": "FROM_TIMESTAMP"},
        )
    )

    assert [shard["ShardId"] for shard in shards] == ["s-1", "s-2"]
    first, second = fake.calls
    assert first[1]["StreamName"] == "orders"
    assert "NextToken" not in first[1]
    assert second[1] == {"NextToken": "t1"}


def test_an_empty_page_is_not_an_error() -> None:
    fake = FakeClient({"list_streams": {"StreamNames": []}})
    assert not list(_client(fake).paginate("ListStreams", "StreamNames"))


# --------------------------------------------------------------------- backoff


def test_throttling_is_retried_then_succeeds() -> None:
    waits: list[float] = []
    fake = FakeClient({"list_streams": [ThrottlingException(), {"StreamNames": ["a"]}]})
    retry = RetryPolicy(max_retries=3, base_delay_seconds=0.5, sleep=waits.append)

    assert _client(fake, retry=retry).call("ListStreams") == {"StreamNames": ["a"]}
    assert waits == [0.5]


def test_backoff_doubles() -> None:
    retry = RetryPolicy(max_retries=4, base_delay_seconds=0.5, sleep=lambda _: None)
    assert [retry.delay_for(attempt) for attempt in range(4)] == [0.5, 1.0, 2.0, 4.0]


def test_throttling_that_never_clears_is_reported() -> None:
    fake = FakeClient({"list_streams": ThrottlingException()})
    retry = RetryPolicy(max_retries=2, base_delay_seconds=0.0, sleep=lambda _: None)
    with pytest.raises(AwsError, match="ListStreams"):
        _client(fake, retry=retry).call("ListStreams")


def test_a_non_throttling_error_is_not_retried() -> None:
    waits: list[float] = []
    fake = FakeClient({"list_streams": ValueError("nope")})
    retry = RetryPolicy(max_retries=3, base_delay_seconds=0.5, sleep=waits.append)
    with pytest.raises(AwsError):
        _client(fake, retry=retry).call("ListStreams")
    assert waits == []


def test_throttling_is_recognised_by_code_and_by_status() -> None:
    by_name = ThrottlingException()
    by_code = Exception()
    by_code.response = {"Error": {"Code": "ProvisionedThroughputExceededException"}}  # type: ignore[attr-defined]
    by_status = Exception()
    by_status.response = {"ResponseMetadata": {"HTTPStatusCode": 429}}  # type: ignore[attr-defined]
    other = Exception()
    other.response = {"Error": {"Code": "AccessDeniedException"}}  # type: ignore[attr-defined]

    assert _is_throttling(by_name)
    assert _is_throttling(by_code)
    assert _is_throttling(by_status)
    assert not _is_throttling(other)
    assert not _is_throttling(ValueError("x"))


# --------------------------------------------------------------------- plumbing


@pytest.mark.parametrize(
    ("operation", "method"),
    [
        ("ListShards", "list_shards"),
        ("DescribeStreamSummary", "describe_stream_summary"),
        ("GetMetricData", "get_metric_data"),
        ("GetShardIterator", "get_shard_iterator"),
    ],
)
def test_operation_names_map_to_boto3_methods(operation: str, method: str) -> None:
    assert _method_name(operation) == method


def test_the_recorder_notes_what_was_called() -> None:
    recorder = CallRecorder()
    fake = FakeClient({"list_streams": {"StreamNames": []}})
    client = _client(fake, recorder=recorder)
    client.call("ListStreams")
    client.call("ListShards")
    assert recorder.operations("kinesis") == ("ListStreams", "ListShards")
