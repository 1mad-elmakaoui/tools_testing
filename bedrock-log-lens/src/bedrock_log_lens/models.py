"""The shapes this tool reasons about.

Read :class:`InvocationRecord` before anything else. It is the type every report is built
from, and it has no field capable of holding a prompt or a response. That is the privacy
guarantee, and it is structural rather than procedural: there is no rule here for a future
edit to forget, because the type simply has nowhere to put the content. A test asserts the
field list stays that way.

Everything is frozen and made of plain data, so the whole analysis can be reconstructed in
a test from a handful of records.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

#: The two fields in a Bedrock log record that hold what was actually said. They are
#: dropped in the parser and never reach anything downstream. Named here so the parser, the
#: tests and the documentation all point at the same list.
CONTENT_FIELDS = ("inputBodyJson", "outputBodyJson")


class Operation(StrEnum):
    """The Bedrock operation that produced a record.

    Unrecognised values are kept verbatim rather than rejected: AWS adds operations, and a
    new one should show up in the report as itself, not as a parse failure.
    """

    INVOKE_MODEL = "InvokeModel"
    INVOKE_MODEL_STREAM = "InvokeModelWithResponseStream"
    CONVERSE = "Converse"
    CONVERSE_STREAM = "ConverseStream"


class IssueKind(StrEnum):
    """Why a record could not be used.

    Counted and reported rather than raised. A run over a million log files should not die
    on one truncated line, and it should not quietly under-report either.
    """

    UNREADABLE_FILE = "unreadable_file"
    BAD_JSON = "bad_json"
    NOT_A_RECORD = "not_a_record"
    WRONG_SCHEMA_TYPE = "wrong_schema_type"
    UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"
    MISSING_TIMESTAMP = "missing_timestamp"
    BAD_TIMESTAMP = "bad_timestamp"
    MISSING_MODEL_ID = "missing_model_id"
    MISSING_TOKEN_COUNTS = "missing_token_counts"


@dataclass(frozen=True)
class InvocationRecord:
    """One model invocation, as metadata only.

    Every field here is a count, an identifier or a timestamp. None of them can hold a
    prompt, a response, or any fragment of one.
    """

    timestamp: dt.datetime
    request_id: str
    model_id: str
    operation: str
    region: str
    account_id: str
    #: The caller's ARN. An identifier, not content — and the thing the report groups by.
    identity_arn: str
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    #: From amazon-bedrock-invocationMetrics, when the record carries it.
    invocation_latency_ms: int | None = None
    first_byte_latency_ms: int | None = None
    error_code: str | None = None
    #: Where this record came from, so an outlier can be looked up without a full re-scan.
    source: str = ""

    @property
    def billable_input_tokens(self) -> int:
        """Input tokens excluding cache reads, which are priced separately."""
        return self.input_tokens or 0

    @property
    def total_tokens(self) -> int:
        """Every token this call is accountable for."""
        return (
            (self.input_tokens or 0)
            + (self.output_tokens or 0)
            + (self.cache_read_tokens or 0)
            + (self.cache_write_tokens or 0)
        )

    @property
    def has_token_counts(self) -> bool:
        """Whether this record can contribute to a token or cost total."""
        return self.input_tokens is not None or self.output_tokens is not None

    @property
    def hour(self) -> dt.datetime:
        """The record's timestamp truncated to the hour, for grouping."""
        return self.timestamp.replace(minute=0, second=0, microsecond=0)

    @property
    def day(self) -> dt.date:
        """The record's date, for grouping."""
        return self.timestamp.date()


@dataclass(frozen=True)
class ParseIssue:
    """One record that could not be used, and why."""

    kind: IssueKind
    source: str
    detail: str = ""


@dataclass(frozen=True)
class ParseOutcome:
    """Everything one parse produced: the records, and what was skipped."""

    records: tuple[InvocationRecord, ...] = ()
    issues: tuple[ParseIssue, ...] = ()

    @property
    def total_seen(self) -> int:
        """Records attempted, whether or not they were usable."""
        return len(self.records) + len(self.issues)

    @property
    def issue_counts(self) -> dict[IssueKind, int]:
        """How many records each kind of problem accounted for."""
        counts: dict[IssueKind, int] = {}
        for issue in self.issues:
            counts[issue.kind] = counts.get(issue.kind, 0) + 1
        return counts

    @property
    def usable_fraction(self) -> float:
        """Share of records that could be used. A low number should be obvious in a report."""
        if not self.total_seen:
            return 0.0
        return len(self.records) / self.total_seen

    def merged_with(self, other: ParseOutcome) -> ParseOutcome:
        """Combine two outcomes, as reading many files produces many of them."""
        return ParseOutcome(
            records=self.records + other.records,
            issues=self.issues + other.issues,
        )


@dataclass(frozen=True)
class Cost:
    """An estimated cost, or an honest absence of one."""

    usd: float | None
    #: Set when no price could be found, so a report can say why rather than print zero.
    no_price_reason: str | None = None

    @property
    def is_known(self) -> bool:
        """Whether a figure could be established at all."""
        return self.usd is not None


@dataclass(frozen=True)
class GroupTotals:
    """Totals for one group: a model, an identity, an hour or a day."""

    key: str
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    #: Requests whose model had no published price, so the cost above is a floor.
    unpriced_requests: int = 0

    @property
    def total_tokens(self) -> int:
        """Every token in this group."""
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )

    @property
    def cost_is_complete(self) -> bool:
        """Whether every request in this group could be priced."""
        return self.unpriced_requests == 0


@dataclass(frozen=True)
class TokenPercentiles:
    """Distribution of a token count for one model."""

    model_id: str
    count: int
    p50: int
    p95: int
    p99: int
    maximum: int

    @property
    def is_meaningful(self) -> bool:
        """Whether there are enough samples for the high percentiles to mean anything.

        With five requests, p99 is just the largest one wearing a statistical hat.
        """
        return self.count >= 20


@dataclass(frozen=True)
class Outlier:
    """A request whose token count sits far outside its model's distribution."""

    record: InvocationRecord
    kind: str
    value: int
    threshold: int


class AnomalyKind(StrEnum):
    """Which heuristic flagged a window.

    All of these are heuristics, and the report says so wherever they appear. They describe
    a shape in the call pattern, not a proven fault.
    """

    BURST = "burst"
    RATE_JUMP = "rate_jump"
    REPEATED_SHAPE = "repeated_shape"


@dataclass(frozen=True)
class Anomaly:
    """A window of calls from one identity that looks wrong."""

    kind: AnomalyKind
    identity_arn: str
    window_start: dt.datetime
    window_end: dt.datetime
    request_count: int
    cost_usd: float
    detail: str
    model_id: str | None = None

    @property
    def duration_seconds(self) -> float:
        """Length of the flagged window."""
        return (self.window_end - self.window_start).total_seconds()

    @property
    def calls_per_minute(self) -> float:
        """Rate across the window, which is what makes a burst a burst."""
        minutes = self.duration_seconds / 60
        if minutes <= 0:
            return float(self.request_count)
        return self.request_count / minutes


@dataclass(frozen=True)
class Report:
    """Everything one run worked out."""

    totals: GroupTotals
    by_model: tuple[GroupTotals, ...]
    by_identity: tuple[GroupTotals, ...]
    by_hour: tuple[GroupTotals, ...]
    by_day: tuple[GroupTotals, ...]
    percentiles_input: tuple[TokenPercentiles, ...]
    percentiles_output: tuple[TokenPercentiles, ...]
    outliers: tuple[Outlier, ...]
    anomalies: tuple[Anomaly, ...]
    most_expensive: tuple[tuple[InvocationRecord, Cost], ...]
    issues: tuple[ParseIssue, ...]
    sources_read: int
    window_start: dt.datetime | None
    window_end: dt.datetime | None
    prices_published: str
    unpriced_models: tuple[str, ...] = field(default_factory=tuple)

    @property
    def issue_counts(self) -> dict[IssueKind, int]:
        """How many records each kind of problem accounted for."""
        counts: dict[IssueKind, int] = {}
        for issue in self.issues:
            counts[issue.kind] = counts.get(issue.kind, 0) + 1
        return counts

    @property
    def cost_is_complete(self) -> bool:
        """Whether every request could be priced."""
        return not self.unpriced_models


def content_bearing_fields(record_fields: Sequence[str]) -> tuple[str, ...]:
    """Any field name that looks like it could hold a prompt or a response.

    Used by the privacy test rather than by the program. The check is deliberately crude and
    deliberately noisy: a field called ``prompt_preview`` should fail the build and make
    somebody justify it, which is the whole point.
    """
    suspicious = ("body", "prompt", "content", "text", "message", "completion", "response")
    return tuple(name for name in record_fields if any(word in name.lower() for word in suspicious))
