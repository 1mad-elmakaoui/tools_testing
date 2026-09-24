"""The AWS layer: the read-only guarantee, backoff and pagination.

Nothing here builds a real client or reads credentials. The guard is tested by trying to
break it, because a guarantee nobody has attacked is only a hope.
"""

from __future__ import annotations

from typing import Any

import pytest

from sagemaker_idle_finder.aws import (
    AwsError,
    CallRecorder,
    ReadOnlyClient,
    ReadOnlyViolationError,
    RetryPolicy,
    _is_throttling,
    _method_name,
)

ALLOWED = frozenset({"ListEndpoints", "DescribeEndpoint"})


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

    Named without an Error suffix on purpose: the detection works off the class name AWS
    actually uses, so the double has to carry the same one.
    """


def _client(
    fake: FakeClient,
    retry: RetryPolicy | None = None,
    recorder: CallRecorder | None = None,
    service: str = "sagemaker",
    allowed: frozenset[str] = ALLOWED,
) -> ReadOnlyClient:
    """Wrap a fake in the real guard, so tests exercise the guard rather than bypass it."""
    return ReadOnlyClient(
        fake,  # type: ignore[arg-type]
        service,
        allowed,
        retry or RetryPolicy(max_retries=3, base_delay_seconds=0.0, sleep=lambda _: None),
        recorder,
    )


# ------------------------------------------------------------------ the read-only guard


def test_an_allowed_operation_goes_through() -> None:
    fake = FakeClient({"list_endpoints": {"Endpoints": [{"EndpointName": "a"}]}})
    assert _client(fake).call("ListEndpoints")["Endpoints"] == [{"EndpointName": "a"}]


@pytest.mark.parametrize(
    "operation", ["DeleteEndpoint", "UpdateEndpoint", "CreateEndpoint", "InvokeEndpoint"]
)
def test_a_write_operation_is_refused_before_it_reaches_the_network(operation: str) -> None:
    """The point of the wrapper: a future edit reaching for a write fails here."""
    fake = FakeClient()
    with pytest.raises(ReadOnlyViolationError, match="read-only"):
        _client(fake).call(operation)
    assert fake.calls == [], "nothing should have reached the client"


def test_the_refusal_lists_what_is_allowed() -> None:
    with pytest.raises(ReadOnlyViolationError, match="DescribeEndpoint, ListEndpoints"):
        _client(FakeClient()).call("DeleteEndpoint")


def test_calls_are_recorded_for_inspection() -> None:
    recorder = CallRecorder()
    fake = FakeClient({"list_endpoints": {"Endpoints": []}})
    _client(fake, recorder=recorder).call("ListEndpoints")
    assert recorder.operations("sagemaker") == ("ListEndpoints",)
    assert recorder.operations() == ("ListEndpoints",)


def test_a_refused_operation_is_not_recorded_as_called() -> None:
    recorder = CallRecorder()
    with pytest.raises(ReadOnlyViolationError):
        _client(FakeClient(), recorder=recorder).call("DeleteEndpoint")
    assert recorder.calls == []


# ----------------------------------------------------------------------- name mapping


@pytest.mark.parametrize(
    ("operation", "method"),
    [
        ("ListEndpoints", "list_endpoints"),
        ("DescribeEndpointConfig", "describe_endpoint_config"),
        ("GetMetricData", "get_metric_data"),
        ("DescribeScalableTargets", "describe_scalable_targets"),
    ],
)
def test_operation_names_map_to_boto3_methods(operation: str, method: str) -> None:
    assert _method_name(operation) == method


# --------------------------------------------------------------------------- throttling


def test_throttling_is_retried_then_succeeds() -> None:
    fake = FakeClient(
        {"list_endpoints": [ThrottlingException(), ThrottlingException(), {"Endpoints": []}]}
    )
    slept: list[float] = []
    retry = RetryPolicy(max_retries=3, base_delay_seconds=0.5, sleep=slept.append)
    assert _client(fake, retry).call("ListEndpoints") == {"Endpoints": []}
    assert slept == [0.5, 1.0], "backoff should double"


def test_throttling_eventually_gives_up_with_a_clear_error() -> None:
    fake = FakeClient({"list_endpoints": [ThrottlingException() for _ in range(5)]})
    retry = RetryPolicy(max_retries=2, base_delay_seconds=0.0, sleep=lambda _: None)
    with pytest.raises(AwsError, match="sagemaker:ListEndpoints failed"):
        _client(fake, retry).call("ListEndpoints")


def test_a_non_throttling_error_is_not_retried() -> None:
    fake = FakeClient({"list_endpoints": [ValueError("boom"), {"Endpoints": []}]})
    slept: list[float] = []
    retry = RetryPolicy(max_retries=3, base_delay_seconds=1.0, sleep=slept.append)
    with pytest.raises(AwsError, match="boom"):
        _client(fake, retry).call("ListEndpoints")
    assert slept == []


def test_throttling_is_recognised_from_an_error_code() -> None:
    error = Exception()
    error.response = {"Error": {"Code": "TooManyRequestsException"}}  # type: ignore[attr-defined]
    assert _is_throttling(error)


def test_throttling_is_recognised_from_a_429() -> None:
    error = Exception()
    error.response = {"ResponseMetadata": {"HTTPStatusCode": 429}}  # type: ignore[attr-defined]
    assert _is_throttling(error)


def test_an_ordinary_error_is_not_throttling() -> None:
    error = Exception()
    error.response = {"Error": {"Code": "ValidationException"}}  # type: ignore[attr-defined]
    assert not _is_throttling(error)
    assert not _is_throttling(ValueError("nope"))


def test_backoff_doubles() -> None:
    retry = RetryPolicy(max_retries=5, base_delay_seconds=0.5, sleep=lambda _: None)
    assert [retry.delay_for(n) for n in range(4)] == [0.5, 1.0, 2.0, 4.0]


# --------------------------------------------------------------------------- pagination


def test_pagination_follows_next_token() -> None:
    fake = FakeClient(
        {
            "list_endpoints": [
                {"Endpoints": [{"EndpointName": "a"}], "NextToken": "t1"},
                {"Endpoints": [{"EndpointName": "b"}], "NextToken": "t2"},
                {"Endpoints": [{"EndpointName": "c"}]},
            ]
        }
    )
    names = [item["EndpointName"] for item in _client(fake).paginate("ListEndpoints", "Endpoints")]
    assert names == ["a", "b", "c"]
    assert [kwargs.get("NextToken") for _, kwargs in fake.calls] == [None, "t1", "t2"]


def test_pagination_of_an_empty_result() -> None:
    fake = FakeClient({"list_endpoints": {"Endpoints": []}})
    assert list(_client(fake).paginate("ListEndpoints", "Endpoints")) == []


def test_pagination_tolerates_a_missing_key() -> None:
    fake = FakeClient({"list_endpoints": {}})
    assert list(_client(fake).paginate("ListEndpoints", "Endpoints")) == []


def test_pagination_is_guarded_too() -> None:
    """Pagination is hand-rolled precisely so it cannot slip past the allowlist."""
    with pytest.raises(ReadOnlyViolationError):
        list(_client(FakeClient()).paginate("DeleteEndpoint", "Endpoints"))
