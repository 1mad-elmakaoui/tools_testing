"""Proof that prompts and responses never come out.

This is the tool's central promise, so these tests are written to fail loudly rather than
to pass quietly. Every fixture record contains a canary string in its prompt and its
response; anything that reaches a user must not contain it.

The structural test matters most. It asserts the record type has no field that could hold
content at all, so the guarantee survives a future edit by someone who has not read this
file.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest

from bedrock_log_lens.models import CONTENT_FIELDS, InvocationRecord, content_bearing_fields
from bedrock_log_lens.parse import parse_record, redacted
from tests.conftest import CANARY, make_raw


def test_the_record_type_has_nowhere_to_put_content() -> None:
    """The guarantee is structural: the type cannot hold a prompt, so it cannot leak one.

    If this fails, someone has added a field that could carry content. That may be
    deliberate, but it must be argued for, not slipped in.
    """
    names = [field.name for field in dataclasses.fields(InvocationRecord)]
    assert content_bearing_fields(names) == ()


def test_parsing_keeps_no_reference_to_the_body() -> None:
    raw = make_raw()
    parsed = parse_record(raw)
    assert isinstance(parsed, InvocationRecord)

    rendered = json.dumps(dataclasses.asdict(parsed), default=str)
    assert CANARY not in rendered
    assert CANARY not in repr(parsed)
    assert CANARY not in str(parsed)


def test_the_canary_really_is_in_the_input(raw_record: dict[str, Any]) -> None:
    """Guard against the other failure mode: a test that passes because nothing was there.

    If the fixture stopped carrying content, every test in this file would pass while
    proving nothing.
    """
    serialised = json.dumps(raw_record)
    assert serialised.count(CANARY) >= 3
    assert CANARY in json.dumps(raw_record["input"]["inputBodyJson"])
    assert CANARY in json.dumps(raw_record["output"]["outputBodyJson"])


def test_metrics_are_read_without_carrying_the_response_out() -> None:
    """Latency comes from inside the output body; nothing else from it may follow."""
    parsed = parse_record(make_raw(latency_ms=2_500, first_byte_ms=180))
    assert isinstance(parsed, InvocationRecord)
    assert parsed.invocation_latency_ms == 2_500
    assert parsed.first_byte_latency_ms == 180
    assert CANARY not in repr(parsed)


def test_redaction_replaces_content_but_keeps_the_shape() -> None:
    raw = make_raw()
    safe = redacted(raw)

    assert CANARY not in json.dumps(safe)
    assert safe["input"]["inputBodyJson"] == "<redacted>"
    assert safe["output"]["outputBodyJson"] == "<redacted>"
    # Everything that is not content survives, so the record is still diagnosable.
    assert safe["modelId"] == raw["modelId"]
    assert safe["input"]["inputTokenCount"] == raw["input"]["inputTokenCount"]


@pytest.mark.parametrize("field_name", CONTENT_FIELDS)
def test_the_content_field_list_matches_the_schema(field_name: str) -> None:
    """The two fields named as content are the two the schema actually uses for it."""
    raw = make_raw()
    assert field_name in json.dumps(raw)
    assert field_name in {"inputBodyJson", "outputBodyJson"}
