"""Integrity of limits.yaml and of the loader that validates it.

The point of these tests is that a number can never enter the tool without a source, and a
heuristic can never enter it without a rationale.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from sagemaker_inference_picker.limits import (
    LimitsData,
    LimitsError,
    load_limits,
    parse_limits,
)
from sagemaker_inference_picker.models import Option

_SOURCED_SECTIONS = ("limits", "capabilities", "facts")


def test_every_option_is_defined(limits: LimitsData) -> None:
    assert set(limits.options) == set(Option)


def test_every_documented_value_cites_an_aws_source(raw_limits: dict[str, Any]) -> None:
    for option_key, option in raw_limits["options"].items():
        for section in _SOURCED_SECTIONS:
            for name, entry in (option.get(section) or {}).items():
                where = f"options.{option_key}.{section}.{name}"
                assert "source" in entry, f"{where} has no source"
                assert entry["source"].startswith("https://"), f"{where} source is not a URL"
                assert "value" in entry, f"{where} has no value"


def test_every_pattern_cites_a_source(raw_limits: dict[str, Any]) -> None:
    for key, pattern in raw_limits["patterns"].items():
        assert pattern["source"].startswith("https://"), f"patterns.{key} source is not a URL"


def test_every_heuristic_carries_a_rationale(limits: LimitsData) -> None:
    for name, heuristic in {**limits.heuristics, **limits.weights}.items():
        assert heuristic.rationale.strip(), f"heuristic {name} has an empty rationale"


def test_last_verified_is_an_iso_date(limits: LimitsData) -> None:
    parsed = dt.date.fromisoformat(limits.last_verified)
    assert parsed.year >= 2024


def test_tie_break_order_covers_every_option(limits: LimitsData) -> None:
    assert set(limits.tie_break_order) == set(Option)
    assert len(limits.tie_break_order) == len(Option)


def test_notes_are_whitespace_normalised(limits: LimitsData) -> None:
    note = limits.limits_for(Option.SERVERLESS).gpu_supported.note
    assert note is not None
    assert "\n" not in note


def test_unlimited_values_parse_as_none(limits: LimitsData) -> None:
    assert limits.limits_for(Option.ASYNC).max_response_payload_mb.value is None
    assert limits.limits_for(Option.BATCH_TRANSFORM).max_response_payload_mb.value is None


def test_accessors_reject_unknown_names(limits: LimitsData) -> None:
    with pytest.raises(LimitsError, match="unknown heuristic"):
        limits.heuristic("does_not_exist")
    with pytest.raises(LimitsError, match="unknown weight"):
        limits.weight("does_not_exist")
    with pytest.raises(LimitsError, match="unknown pattern"):
        limits.pattern("does_not_exist")


def test_load_from_an_explicit_path(tmp_path: Path, raw_limits: dict[str, Any]) -> None:
    import yaml

    target = tmp_path / "limits.yaml"
    target.write_text(yaml.safe_dump(raw_limits), encoding="utf-8")
    loaded = load_limits(target)
    assert loaded.origin == str(target)
    assert set(loaded.options) == set(Option)


def test_missing_file_is_reported_clearly(tmp_path: Path) -> None:
    with pytest.raises(LimitsError, match="limits file not found"):
        load_limits(tmp_path / "nope.yaml")


def test_invalid_yaml_is_reported_clearly(tmp_path: Path) -> None:
    target = tmp_path / "limits.yaml"
    target.write_text("key: [unclosed\n", encoding="utf-8")
    with pytest.raises(LimitsError, match="not valid YAML"):
        load_limits(target)


def test_unsupported_schema_version_is_rejected(raw_limits: dict[str, Any]) -> None:
    raw_limits["schema_version"] = 99
    with pytest.raises(LimitsError, match="unsupported schema_version"):
        parse_limits(raw_limits)


def test_a_value_without_a_source_is_rejected(raw_limits: dict[str, Any]) -> None:
    del raw_limits["options"]["serverless"]["limits"]["max_request_payload_mb"]["source"]
    with pytest.raises(LimitsError, match="must carry a 'source' URL"):
        parse_limits(raw_limits)


def test_a_heuristic_without_a_rationale_is_rejected(raw_limits: dict[str, Any]) -> None:
    del raw_limits["heuristics"]["weights"]["steady_traffic_favours_real_time"]["rationale"]
    with pytest.raises(LimitsError, match="must carry a non-empty 'rationale'"):
        parse_limits(raw_limits)


def test_a_missing_option_is_rejected(raw_limits: dict[str, Any]) -> None:
    del raw_limits["options"]["async"]
    with pytest.raises(LimitsError, match=r"options\.async is missing"):
        parse_limits(raw_limits)


def test_an_unknown_option_is_rejected(raw_limits: dict[str, Any]) -> None:
    raw_limits["options"]["quantum_inference"] = {}
    with pytest.raises(LimitsError, match="unknown keys"):
        parse_limits(raw_limits)


def test_a_non_boolean_capability_is_rejected(raw_limits: dict[str, Any]) -> None:
    raw_limits["options"]["serverless"]["capabilities"]["gpu_supported"]["value"] = "no"
    with pytest.raises(LimitsError, match="must be true or false"):
        parse_limits(raw_limits)


def test_a_negative_limit_is_rejected(raw_limits: dict[str, Any]) -> None:
    raw_limits["options"]["serverless"]["limits"]["max_request_payload_mb"]["value"] = -1
    with pytest.raises(LimitsError, match="must be positive"):
        parse_limits(raw_limits)


def test_an_incomplete_tie_break_order_is_rejected(raw_limits: dict[str, Any]) -> None:
    raw_limits["tie_break_order"]["value"] = ["serverless", "real_time"]
    with pytest.raises(LimitsError, match="must list every option"):
        parse_limits(raw_limits)


def test_an_unknown_option_in_tie_break_order_is_rejected(raw_limits: dict[str, Any]) -> None:
    raw_limits["tie_break_order"]["value"] = ["serverless", "nonsense", "async", "real_time"]
    with pytest.raises(LimitsError, match="not a known option"):
        parse_limits(raw_limits)


def test_a_non_mapping_document_is_rejected() -> None:
    with pytest.raises(LimitsError, match="must be a mapping"):
        parse_limits(["not", "a", "mapping"])


def test_an_unknown_applies_to_is_rejected(raw_limits: dict[str, Any]) -> None:
    raw_limits["patterns"]["multi_model_endpoint"]["applies_to"] = "telepathy"
    with pytest.raises(LimitsError, match="not a known option"):
        parse_limits(raw_limits)


def test_errors_name_the_file_they_came_from(raw_limits: dict[str, Any]) -> None:
    raw_limits["schema_version"] = 42
    with pytest.raises(LimitsError, match=r"^my-limits\.yaml: "):
        parse_limits(raw_limits, origin="my-limits.yaml")
