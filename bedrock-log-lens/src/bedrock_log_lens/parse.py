"""Turn a raw log record into metadata, and throw the rest away.

This is the only module that ever holds a prompt or a response, and it holds one for the
length of a function call. :func:`parse_record` takes the decoded JSON of one record and
returns an :class:`InvocationRecord` built field by field from the parts that are counts
and identifiers. The body fields are never copied out, so the object it returns cannot
carry them, and nothing downstream is handed the original dictionary.

Pure: it takes a dictionary and returns a record or an issue. No file handles, no network,
no clock.

Tolerance is a requirement, not a nicety. These files are written by a service, read after
the fact, and often truncated or partially delivered. A record this module cannot use is
counted and described; it never raises, because one bad line in a million must not end a
run, and must not silently shrink the totals either.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Any

from bedrock_log_lens.models import (
    CONTENT_FIELDS,
    InvocationRecord,
    IssueKind,
    ParseIssue,
)

#: The schema this parser was written against. A record announcing a different major
#: version is counted rather than guessed at: the field names could mean anything.
SUPPORTED_SCHEMA_TYPE = "ModelInvocationLog"
SUPPORTED_SCHEMA_MAJOR = "1"

#: Where Bedrock puts per-call metrics inside the output body. Reading these four integers
#: is the only time this tool looks inside a content field, and nothing else from that
#: object is copied out.
_METRICS_KEY = "amazon-bedrock-invocationMetrics"
_LATENCY_KEY = "invocationLatency"
_FIRST_BYTE_KEY = "firstByteLatency"


def parse_record(raw: object, source: str = "") -> InvocationRecord | ParseIssue:
    """Parse one decoded log record into metadata, or say why it cannot be used."""
    if not isinstance(raw, dict):
        return ParseIssue(
            IssueKind.NOT_A_RECORD, source, f"expected an object, got {type(raw).__name__}"
        )

    schema_type = raw.get("schemaType")
    if schema_type != SUPPORTED_SCHEMA_TYPE:
        return ParseIssue(
            IssueKind.WRONG_SCHEMA_TYPE,
            source,
            f"schemaType was {schema_type!r}, expected {SUPPORTED_SCHEMA_TYPE!r}",
        )

    version = str(raw.get("schemaVersion") or "")
    if version and version.split(".")[0] != SUPPORTED_SCHEMA_MAJOR:
        return ParseIssue(
            IssueKind.UNSUPPORTED_SCHEMA_VERSION,
            source,
            f"schemaVersion {version} is not a 1.x record",
        )

    raw_timestamp = raw.get("timestamp")
    if raw_timestamp is None:
        return ParseIssue(IssueKind.MISSING_TIMESTAMP, source)
    timestamp = _timestamp(raw_timestamp)
    if timestamp is None:
        return ParseIssue(IssueKind.BAD_TIMESTAMP, source, f"could not read {raw_timestamp!r}")

    model_id = _text(raw.get("modelId"))
    if not model_id:
        return ParseIssue(IssueKind.MISSING_MODEL_ID, source)

    # Each of these reads a scalar out of the record. The body fields are not among them,
    # are not copied, and do not appear in the object this function returns.
    input_section = _mapping(raw.get("input"))
    output_section = _mapping(raw.get("output"))

    input_tokens = _count(input_section.get("inputTokenCount"))
    output_tokens = _count(output_section.get("outputTokenCount"))
    latency, first_byte = _invocation_metrics(output_section)

    record = InvocationRecord(
        timestamp=timestamp,
        request_id=_text(raw.get("requestId")),
        model_id=model_id,
        operation=_text(raw.get("operation")),
        region=_text(raw.get("region")),
        account_id=_text(raw.get("accountId")),
        identity_arn=_text(_mapping(raw.get("identity")).get("arn")),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=_count(input_section.get("cacheReadInputTokenCount")),
        cache_write_tokens=_count(input_section.get("cacheWriteInputTokenCount")),
        invocation_latency_ms=latency,
        first_byte_latency_ms=first_byte,
        error_code=_text(raw.get("errorCode")) or None,
        source=source,
    )

    if input_tokens is None and output_tokens is None and record.error_code is None:
        # Not an error record, and no counts: it cannot contribute to a total, so it is
        # reported rather than quietly counted as a zero-token request.
        return ParseIssue(
            IssueKind.MISSING_TOKEN_COUNTS,
            source,
            f"{model_id} record carries neither an input nor an output token count",
        )
    return record


def _invocation_metrics(output_section: Mapping[str, Any]) -> tuple[int | None, int | None]:
    """Read the two latency figures Bedrock embeds in the output body.

    This is the one place the parser reaches into a field that also holds a response. It
    takes two integers from a known sub-object and copies nothing else; the body itself is
    not retained, returned, or logged.
    """
    body = output_section.get("outputBodyJson")
    if not isinstance(body, dict):
        return (None, None)
    metrics = body.get(_METRICS_KEY)
    if not isinstance(metrics, dict):
        return (None, None)
    return (_count(metrics.get(_LATENCY_KEY)), _count(metrics.get(_FIRST_BYTE_KEY)))


def redacted(raw: Mapping[str, Any]) -> dict[str, Any]:
    """A copy of `raw` with the content fields replaced by a marker.

    For diagnostics and for --show-content's refusal path: somewhere there has to be a way
    to show the shape of a record without showing what it said.
    """
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if key in CONTENT_FIELDS:
            out[key] = "<redacted>"
        elif isinstance(value, dict):
            out[key] = redacted(value)
        else:
            out[key] = value
    return out


def _timestamp(raw: object) -> dt.datetime | None:
    """Read a timestamp, accepting what the logs actually contain.

    Bedrock writes ISO 8601, commonly with a trailing Z that Python did not accept before
    3.11. Epoch seconds and milliseconds turn up in re-exported copies, so both are read
    rather than rejected.
    """
    if isinstance(raw, dt.datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=dt.UTC)
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        seconds = float(raw)
        # Anything this large is milliseconds: 10^11 seconds is the year 5138.
        if seconds > 1e11:
            seconds /= 1000
        try:
            return dt.datetime.fromtimestamp(seconds, tz=dt.UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def _count(raw: object) -> int | None:
    """Read a token or latency count, rejecting anything that is not a plain number."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = int(raw)
    return value if value >= 0 else None


def _text(raw: object) -> str:
    """Read a string field, tolerating its absence."""
    return raw.strip() if isinstance(raw, str) else ""


def _mapping(raw: object) -> Mapping[str, Any]:
    return raw if isinstance(raw, dict) else {}
