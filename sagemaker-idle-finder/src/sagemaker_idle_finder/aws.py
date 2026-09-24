"""The only module that talks to AWS, and the only one that imports boto3.

Two things matter here beyond making the calls.

**Read-only is enforced, not reviewed.** Every client is wrapped so that an operation
outside the allowlist in ``policy.yaml`` raises before it reaches the network. A future
edit that reaches for ``DeleteEndpoint`` fails immediately rather than deleting something,
and a test asserts that a full scan issues only allowed operations.

**Pagination is done by hand rather than with botocore's paginators**, so that every single
request passes through the same guard and the same backoff. A paginator would call the
service directly and slip past both.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:  # pragma: no cover - imported only for type checking
    from botocore.client import BaseClient

#: Error codes AWS uses for throttling across the three services this tool calls.
THROTTLING_CODES = frozenset(
    {
        "ThrottlingException",
        "Throttling",
        "ThrottledException",
        "TooManyRequestsException",
        "RequestLimitExceeded",
        "RequestThrottled",
        "RequestThrottledException",
        "ProvisionedThroughputExceededException",
        "SlowDown",
    }
)


#: AWS sometimes signals throttling with a bare status code rather than an error code.
_HTTP_TOO_MANY_REQUESTS = 429


class ReadOnlyViolationError(RuntimeError):
    """Raised when something tries to call an operation outside the allowlist.

    This is a programming error, not a runtime condition: it means the tool tried to do
    something it promises never to do.
    """


class AwsError(RuntimeError):
    """Raised when a call fails in a way the caller should hear about."""


@dataclass
class CallRecorder:
    """Every operation attempted, in order. Used by tests to prove what was called."""

    calls: list[tuple[str, str]] = field(default_factory=list)

    def record(self, service: str, operation: str) -> None:
        """Note one attempted operation."""
        self.calls.append((service, operation))

    def operations(self, service: str | None = None) -> tuple[str, ...]:
        """Operation names attempted, optionally for one service."""
        return tuple(
            operation
            for called_service, operation in self.calls
            if service is None or called_service == service
        )


@dataclass
class RetryPolicy:
    """Bounded exponential backoff for throttling."""

    max_retries: int
    base_delay_seconds: float
    sleep: Callable[[float], None] = time.sleep

    def delay_for(self, attempt: int) -> float:
        """Seconds to wait before retry `attempt`, counting from zero."""
        return float(self.base_delay_seconds * (2**attempt))


class ReadOnlyClient:
    """A botocore client that refuses to do anything but read.

    Operations are named as they appear in the API (``ListEndpoints``), not as boto3
    methods, because the allowlist and the IAM policy are written that way and the two
    should be comparable by eye.
    """

    def __init__(
        self,
        client: BaseClient,
        service: str,
        allowed: frozenset[str],
        retry: RetryPolicy,
        recorder: CallRecorder | None = None,
    ) -> None:
        self._client = client
        self._service = service
        self._allowed = allowed
        self._retry = retry
        self._recorder = recorder

    @property
    def service(self) -> str:
        """The service this client speaks to."""
        return self._service

    def call(
        self,
        operation: str,
        **kwargs: Any,  # noqa: ANN401 - the service's own request parameters
    ) -> Mapping[str, Any]:
        """Invoke `operation`, refusing anything not on the allowlist.

        `kwargs` are the service's own request parameters, so they are genuinely arbitrary
        and cannot be typed more tightly here.
        """
        if operation not in self._allowed:
            allowed = ", ".join(sorted(self._allowed))
            raise ReadOnlyViolationError(
                f"{self._service}:{operation} is not allowed. This tool is read-only and "
                f"may only call: {allowed}"
            )
        if self._recorder is not None:
            self._recorder.record(self._service, operation)
        return self._invoke(operation, kwargs)

    def paginate(
        self,
        operation: str,
        result_key: str,
        **kwargs: Any,  # noqa: ANN401 - the service's own request parameters
    ) -> Iterator[Mapping[str, Any]]:
        """Yield every item of a paginated operation, following NextToken by hand."""
        token: str | None = None
        while True:
            arguments = dict(kwargs)
            if token:
                arguments["NextToken"] = token
            response = self.call(operation, **arguments)
            items = response.get(result_key) or []
            yield from items
            token = response.get("NextToken")
            if not token:
                return

    def _invoke(self, operation: str, arguments: dict[str, Any]) -> Mapping[str, Any]:
        method = getattr(self._client, _method_name(operation))
        last_error: Exception | None = None
        for attempt in range(self._retry.max_retries + 1):
            try:
                result: Mapping[str, Any] = method(**arguments)
            except Exception as exc:  # botocore builds its error classes at runtime
                if not _is_throttling(exc) or attempt == self._retry.max_retries:
                    raise AwsError(f"{self._service}:{operation} failed: {exc}") from exc
                last_error = exc
                self._retry.sleep(self._retry.delay_for(attempt))
            else:
                return result
        # Unreachable: the loop either returns or raises.
        raise AwsError(f"{self._service}:{operation} failed: {last_error}")


def _method_name(operation: str) -> str:
    """Turn an API operation name into the boto3 method name."""
    out: list[str] = []
    for index, character in enumerate(operation):
        if character.isupper() and index:
            out.append("_")
        out.append(character.lower())
    return "".join(out)


def _is_throttling(exc: Exception) -> bool:
    """Whether an exception is AWS telling us to slow down."""
    if type(exc).__name__ in THROTTLING_CODES:
        return True
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code")
        if code in THROTTLING_CODES:
            return True
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status == _HTTP_TOO_MANY_REQUESTS:
            return True
    return False


def build_clients(
    region: str,
    allowed: Mapping[str, frozenset[str]],
    retry: RetryPolicy,
    profile: str | None = None,
    recorder: CallRecorder | None = None,
) -> dict[str, ReadOnlyClient]:
    """Build one guarded client per service for `region`.

    The only place a real AWS client is constructed.
    """
    import boto3

    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    # boto3-stubs types Session.client with a Literal overload per service, so a service
    # name chosen at runtime has to be cast rather than narrowed.
    make = cast("Callable[..., BaseClient]", session.client)
    return {
        service: ReadOnlyClient(
            make(service, region_name=region),
            service,
            operations,
            retry,
            recorder,
        )
        for service, operations in allowed.items()
    }
