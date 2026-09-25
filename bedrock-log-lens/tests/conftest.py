"""Shared fixtures.

The log records here follow the documented ModelInvocationLog schema, including the body
fields that hold the prompt and the response. They carry a canary string precisely so the
privacy tests can prove it never comes out the other end.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

#: A string that appears only inside prompt and response bodies in the fixtures. If it ever
#: shows up in output, content has leaked.
CANARY = "CANARY-SECRET-PATIENT-NAME-Jane-Doe-DOB-1970-01-01"

NOW = dt.datetime(2026, 9, 24, 12, 0, tzinfo=dt.UTC)
ROLE = "arn:aws:sts::123456789012:assumed-role/SupportAgentRole/session-42"
OTHER_ROLE = "arn:aws:sts::123456789012:assumed-role/BatchJobRole/nightly"
CLAUDE = "anthropic.claude-sonnet-4-20250514-v1:0"
HAIKU = "anthropic.claude-3-haiku-20240307-v1:0"


def make_raw(
    *,
    timestamp: dt.datetime | str | None = None,
    model_id: str = CLAUDE,
    identity: str | None = ROLE,
    input_tokens: int | None = 120,
    output_tokens: int | None = 340,
    cache_read: int | None = None,
    cache_write: int | None = None,
    operation: str = "Converse",
    request_id: str = "11111111-2222-3333-4444-555555555555",
    latency_ms: int | None = 1_450,
    first_byte_ms: int | None = 310,
    include_bodies: bool = True,
    error_code: str | None = None,
) -> dict[str, Any]:
    """One log record in the documented shape, prompts and all.

    `include_bodies` mirrors the Text data delivery setting: with it off, Bedrock omits the
    body fields entirely, and the tool has to cope with both.
    """
    when = timestamp if timestamp is not None else NOW
    record: dict[str, Any] = {
        "schemaType": "ModelInvocationLog",
        "schemaVersion": "1.0",
        "timestamp": when.isoformat().replace("+00:00", "Z")
        if isinstance(when, dt.datetime)
        else when,
        "accountId": "123456789012",
        "region": "us-east-1",
        "requestId": request_id,
        "operation": operation,
        "modelId": model_id,
    }
    if identity is not None:
        record["identity"] = {"arn": identity}

    input_section: dict[str, Any] = {"inputContentType": "application/json"}
    if input_tokens is not None:
        input_section["inputTokenCount"] = input_tokens
    if cache_read is not None:
        input_section["cacheReadInputTokenCount"] = cache_read
    if cache_write is not None:
        input_section["cacheWriteInputTokenCount"] = cache_write
    if include_bodies:
        input_section["inputBodyJson"] = {
            "messages": [
                {
                    "role": "user",
                    "content": [{"text": f"Summarise this record: {CANARY}"}],
                }
            ],
            "system": [{"text": f"You are a clinical assistant. Patient: {CANARY}"}],
        }
    record["input"] = input_section

    output_section: dict[str, Any] = {"outputContentType": "application/json"}
    if output_tokens is not None:
        output_section["outputTokenCount"] = output_tokens
    if include_bodies:
        body: dict[str, Any] = {
            "output": {
                "message": {
                    "role": "assistant",
                    "content": [{"text": f"The patient {CANARY} was admitted on Tuesday."}],
                }
            },
            "stopReason": "end_turn",
        }
        if latency_ms is not None or first_byte_ms is not None:
            metrics: dict[str, Any] = {
                "inputTokenCount": input_tokens,
                "outputTokenCount": output_tokens,
            }
            if latency_ms is not None:
                metrics["invocationLatency"] = latency_ms
            if first_byte_ms is not None:
                metrics["firstByteLatency"] = first_byte_ms
            body["amazon-bedrock-invocationMetrics"] = metrics
        output_section["outputBodyJson"] = body
    record["output"] = output_section

    if error_code is not None:
        record["errorCode"] = error_code
    return record


@pytest.fixture
def raw_record() -> dict[str, Any]:
    """One well-formed record carrying prompt and response content."""
    return make_raw()


def write_log_dir(root: Path, records: list[dict[str, Any]], *, gzipped: bool = False) -> Path:
    """Write records in Bedrock's S3 layout, one JSON object per line."""
    import gzip

    directory = root / "AWSLogs" / "123456789012" / "BedrockModelInvocationLogs" / "us-east-1"
    directory = directory / "2026" / "09" / "24" / "12"
    directory.mkdir(parents=True, exist_ok=True)
    payload = "\n".join(json.dumps(record) for record in records) + "\n"
    if gzipped:
        path = directory / "invocations.json.gz"
        path.write_bytes(gzip.compress(payload.encode("utf-8")))
    else:
        path = directory / "invocations.json"
        path.write_text(payload, encoding="utf-8")
    return path


def make_record(
    *,
    minutes: float = 0.0,
    seconds: float = 0.0,
    model_id: str = CLAUDE,
    identity: str = ROLE,
    input_tokens: int | None = 120,
    output_tokens: int | None = 340,
    cache_read: int | None = None,
    cache_write: int | None = None,
    region: str = "us-east-1",
    request_id: str | None = None,
) -> Any:
    """An already-parsed record, for the analysis layers that never see raw JSON."""
    from bedrock_log_lens.models import InvocationRecord

    offset = dt.timedelta(minutes=minutes, seconds=seconds)
    return InvocationRecord(
        timestamp=NOW + offset,
        request_id=request_id or f"req-{minutes}-{seconds}-{input_tokens}",
        model_id=model_id,
        operation="Converse",
        region=region,
        account_id="123456789012",
        identity_arn=identity,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
    )


@pytest.fixture(scope="session")
def policy() -> Any:
    from bedrock_log_lens.catalog import load_policy

    return load_policy()


@pytest.fixture(scope="session")
def prices() -> Any:
    from bedrock_log_lens.catalog import load_prices

    return load_prices()
