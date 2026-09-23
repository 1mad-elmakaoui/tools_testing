"""The plain data types: validation and derived properties."""

from __future__ import annotations

import pytest

from sagemaker_inference_picker.models import (
    Elimination,
    Option,
    RankedOption,
    Recommendation,
    TrafficPattern,
    Workload,
)


def _workload(**overrides: object) -> Workload:
    base: dict[str, object] = {
        "payload_mb": 1.0,
        "response_mb": 1.0,
        "processing_seconds": 1.0,
        "traffic": TrafficPattern.STEADY,
    }
    base.update(overrides)
    return Workload(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["payload_mb", "response_mb", "processing_seconds"])
def test_negative_sizes_and_times_are_rejected(field: str) -> None:
    with pytest.raises(ValueError, match=f"{field} must not be negative"):
        _workload(**{field: -1.0})


def test_zero_is_allowed_for_sizes_and_times() -> None:
    workload = _workload(payload_mb=0.0, response_mb=0.0, processing_seconds=0.0)
    assert workload.payload_mb == 0.0


@pytest.mark.parametrize("latency", [0, -5])
def test_a_non_positive_latency_target_is_rejected(latency: float) -> None:
    with pytest.raises(ValueError, match="latency_p99_ms must be positive"):
        _workload(latency_p99_ms=latency)


@pytest.mark.parametrize("count", [0, -3])
def test_model_count_must_be_at_least_one(count: int) -> None:
    with pytest.raises(ValueError, match="model_count must be at least 1"):
        _workload(model_count=count)


def test_a_latency_target_implies_the_caller_waits() -> None:
    assert _workload(latency_p99_ms=250).requires_inline_response is True
    assert _workload(immediate_response=True).requires_inline_response is True
    assert _workload().requires_inline_response is False


def test_workloads_are_frozen() -> None:
    workload = _workload()
    with pytest.raises(AttributeError):
        workload.payload_mb = 5.0  # type: ignore[misc]


def test_eliminations_are_grouped_by_option() -> None:
    first = Elimination(
        option=Option.SERVERLESS,
        constraint_id="gpu_not_supported",
        requirement="a GPU is required",
        message="no GPUs",
        source="https://example.invalid",
    )
    second = Elimination(
        option=Option.SERVERLESS,
        constraint_id="request_payload_over_limit",
        requirement="payload",
        message="too big",
        source="https://example.invalid",
    )
    recommendation = Recommendation(
        workload=_workload(),
        recommended=Option.REAL_TIME,
        ranked=(RankedOption(option=Option.REAL_TIME, score=3.0),),
        eliminations=(first, second),
    )
    assert recommendation.eliminations_for(Option.SERVERLESS) == (first, second)
    assert recommendation.eliminations_for(Option.ASYNC) == ()
    assert recommendation.resolved is True
    assert recommendation.score_for(Option.REAL_TIME) == 3.0
    assert recommendation.score_for(Option.SERVERLESS) is None


def test_an_unresolved_recommendation_reports_itself_as_such() -> None:
    recommendation = Recommendation(workload=_workload(), recommended=None)
    assert recommendation.resolved is False
