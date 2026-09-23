"""The rules in isolation: each constraint at its boundary, and the scoring machinery."""

from __future__ import annotations

import pytest

from sagemaker_inference_picker.limits import LimitsData
from sagemaker_inference_picker.models import Elimination, Option, TrafficPattern, Workload
from sagemaker_inference_picker.rules import (
    HARD_CONSTRAINTS,
    build_conflicts,
    evaluate_advice,
    evaluate_hard_constraints,
    evaluate_preferences,
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


def _constraint_ids(workload: Workload, limits: LimitsData, option: Option) -> set[str]:
    eliminations = evaluate_hard_constraints(workload, limits.limits_for(option))
    return {elimination.constraint_id for elimination in eliminations}


# ------------------------------------------------------------------ hard constraints


@pytest.mark.parametrize(
    ("option", "at_limit", "over_limit"),
    [
        (Option.SERVERLESS, 4.0, 4.000001),
        (Option.REAL_TIME, 25.0, 25.000001),
        (Option.ASYNC, 1024.0, 1024.000001),
        (Option.BATCH_TRANSFORM, 100.0, 100.000001),
    ],
)
def test_request_payload_limit_is_inclusive(
    limits: LimitsData, option: Option, at_limit: float, over_limit: float
) -> None:
    assert "request_payload_over_limit" not in _constraint_ids(
        _workload(payload_mb=at_limit), limits, option
    )
    assert "request_payload_over_limit" in _constraint_ids(
        _workload(payload_mb=over_limit), limits, option
    )


@pytest.mark.parametrize(
    ("option", "at_limit", "over_limit"),
    [
        (Option.SERVERLESS, 60.0, 60.000001),
        (Option.REAL_TIME, 60.0, 60.000001),
        (Option.ASYNC, 3600.0, 3600.000001),
        (Option.BATCH_TRANSFORM, 3600.0, 3600.000001),
    ],
)
def test_processing_time_limit_is_inclusive(
    limits: LimitsData, option: Option, at_limit: float, over_limit: float
) -> None:
    assert "processing_time_over_limit" not in _constraint_ids(
        _workload(processing_seconds=at_limit), limits, option
    )
    assert "processing_time_over_limit" in _constraint_ids(
        _workload(processing_seconds=over_limit), limits, option
    )


def test_an_undocumented_response_limit_never_eliminates(limits: LimitsData) -> None:
    huge = _workload(response_mb=100_000.0)
    assert "response_payload_over_limit" not in _constraint_ids(huge, limits, Option.ASYNC)
    assert "response_payload_over_limit" not in _constraint_ids(
        huge, limits, Option.BATCH_TRANSFORM
    )


def test_gpu_eliminates_serverless_only(limits: LimitsData) -> None:
    workload = _workload(gpu_required=True)
    assert "gpu_not_supported" in _constraint_ids(workload, limits, Option.SERVERLESS)
    for option in (Option.REAL_TIME, Option.ASYNC, Option.BATCH_TRANSFORM):
        assert "gpu_not_supported" not in _constraint_ids(workload, limits, option)


def test_zero_idle_cost_eliminates_real_time_only(limits: LimitsData) -> None:
    workload = _workload(zero_idle_cost=True)
    assert "cannot_scale_to_zero" in _constraint_ids(workload, limits, Option.REAL_TIME)
    for option in (Option.SERVERLESS, Option.ASYNC, Option.BATCH_TRANSFORM):
        assert "cannot_scale_to_zero" not in _constraint_ids(workload, limits, option)


def test_an_immediate_response_eliminates_the_offline_options(limits: LimitsData) -> None:
    workload = _workload(immediate_response=True)
    for option in (Option.ASYNC, Option.BATCH_TRANSFORM):
        assert "no_inline_response" in _constraint_ids(workload, limits, option)
    for option in (Option.REAL_TIME, Option.SERVERLESS):
        assert "no_inline_response" not in _constraint_ids(workload, limits, option)


def test_a_latency_target_alone_eliminates_the_offline_options(limits: LimitsData) -> None:
    eliminations = evaluate_hard_constraints(
        _workload(latency_p99_ms=300), limits.limits_for(Option.ASYNC)
    )
    reasons = [item for item in eliminations if item.constraint_id == "no_inline_response"]
    assert reasons
    assert "p99 latency target of 300 ms" in reasons[0].requirement


def test_an_explicit_immediate_response_is_named_as_such(limits: LimitsData) -> None:
    eliminations = evaluate_hard_constraints(
        _workload(immediate_response=True, latency_p99_ms=300),
        limits.limits_for(Option.ASYNC),
    )
    reasons = [item for item in eliminations if item.constraint_id == "no_inline_response"]
    assert reasons[0].requirement == "the caller needs an immediate response"


def test_a_notification_requirement_eliminates_the_online_options(limits: LimitsData) -> None:
    workload = _workload(needs_notification=True)
    for option in (Option.REAL_TIME, Option.SERVERLESS):
        assert "no_native_completion_notification" in _constraint_ids(workload, limits, option)
    for option in (Option.ASYNC, Option.BATCH_TRANSFORM):
        assert "no_native_completion_notification" not in _constraint_ids(workload, limits, option)


def test_an_unconstrained_workload_eliminates_nothing(limits: LimitsData) -> None:
    workload = _workload(payload_mb=0.1, response_mb=0.1, processing_seconds=0.1)
    for option in Option:
        assert evaluate_hard_constraints(workload, limits.limits_for(option)) == ()


def test_rejection_messages_quote_the_documented_limit(limits: LimitsData) -> None:
    eliminations = evaluate_hard_constraints(
        _workload(payload_mb=10.0), limits.limits_for(Option.SERVERLESS)
    )
    payload = next(
        item for item in eliminations if item.constraint_id == "request_payload_over_limit"
    )
    assert payload.limit == "4 MB"
    assert payload.actual == "10 MB"
    assert "4 MB" in payload.message


def test_every_hard_constraint_is_registered() -> None:
    assert len(HARD_CONSTRAINTS) == 7
    assert len(set(HARD_CONSTRAINTS)) == len(HARD_CONSTRAINTS)


# ---------------------------------------------------------------------- preferences


def test_preference_rule_ids_match_the_declared_weights(limits: LimitsData) -> None:
    """Every weight in limits.yaml is reachable, and every rule has a declared weight."""
    workloads = [
        _workload(traffic=TrafficPattern.STEADY),
        _workload(traffic=TrafficPattern.BURSTY_IDLE),
        _workload(traffic=TrafficPattern.SCHEDULED_BATCH),
        _workload(latency_p99_ms=100),
        _workload(immediate_response=True),
        _workload(model_count=25),
    ]
    emitted = {
        preference.rule_id
        for workload in workloads
        for preference in evaluate_preferences(workload, limits)
    }
    assert emitted == set(limits.weights)


def test_a_loose_latency_target_does_not_penalise_serverless(limits: LimitsData) -> None:
    threshold = limits.heuristic("tight_latency_p99_ms")
    loose = evaluate_preferences(_workload(latency_p99_ms=threshold), limits)
    assert not [p for p in loose if p.rule_id.startswith("tight_latency")]

    tight = evaluate_preferences(_workload(latency_p99_ms=threshold - 1), limits)
    assert {p.rule_id for p in tight} >= {
        "tight_latency_favours_real_time",
        "tight_latency_penalises_serverless",
    }


def test_the_serverless_latency_penalty_is_negative(limits: LimitsData) -> None:
    preferences = evaluate_preferences(_workload(latency_p99_ms=100), limits)
    penalty = next(p for p in preferences if p.rule_id == "tight_latency_penalises_serverless")
    assert penalty.weight < 0
    assert penalty.option is Option.SERVERLESS


def test_many_models_only_counts_above_the_documented_threshold(limits: LimitsData) -> None:
    threshold = int(limits.heuristic("multi_model_endpoint_min_models"))
    below = evaluate_preferences(_workload(model_count=threshold - 1), limits)
    assert "many_models_favour_real_time" not in {p.rule_id for p in below}
    at = evaluate_preferences(_workload(model_count=threshold), limits)
    assert "many_models_favour_real_time" in {p.rule_id for p in at}


# -------------------------------------------------------------------------- advice


def test_no_advice_when_nothing_applies(limits: LimitsData) -> None:
    assert evaluate_advice(_workload(), limits, Option.BATCH_TRANSFORM, ()) == ()


def test_provisioned_concurrency_needs_both_serverless_and_a_target(
    limits: LimitsData,
) -> None:
    assert evaluate_advice(_workload(), limits, Option.SERVERLESS, ()) == ()
    advice = evaluate_advice(_workload(latency_p99_ms=500), limits, Option.SERVERLESS, ())
    assert [item.advice_id for item in advice] == ["provisioned_concurrency"]
    assert advice[0].source.startswith("https://")


def test_mme_advice_requires_idle_traffic(limits: LimitsData) -> None:
    steady = evaluate_advice(
        _workload(model_count=10, traffic=TrafficPattern.STEADY), limits, Option.REAL_TIME, ()
    )
    assert "multi_model_endpoint" not in {item.advice_id for item in steady}

    idle = evaluate_advice(
        _workload(model_count=10, traffic=TrafficPattern.BURSTY_IDLE),
        limits,
        Option.REAL_TIME,
        (),
    )
    assert "multi_model_endpoint" in {item.advice_id for item in idle}


def test_the_scale_to_zero_escape_hatch_needs_a_sole_blocker(limits: LimitsData) -> None:
    workload = _workload(zero_idle_cost=True, gpu_required=True)
    sole_blocker = (
        Elimination(
            option=Option.REAL_TIME,
            constraint_id="cannot_scale_to_zero",
            requirement="must cost nothing when idle",
            message="bills while idle",
            source="https://example.invalid",
        ),
    )
    advice = evaluate_advice(workload, limits, None, sole_blocker)
    assert "inference_components_scale_to_zero" in {item.advice_id for item in advice}

    also_blocked = (
        *sole_blocker,
        Elimination(
            option=Option.REAL_TIME,
            constraint_id="processing_time_over_limit",
            requirement="processing time",
            message="too slow",
            source="https://example.invalid",
        ),
    )
    advice = evaluate_advice(workload, limits, None, also_blocked)
    assert "inference_components_scale_to_zero" not in {item.advice_id for item in advice}


def test_the_escape_hatch_stays_quiet_when_an_online_option_already_won(
    limits: LimitsData,
) -> None:
    blocker = (
        Elimination(
            option=Option.REAL_TIME,
            constraint_id="cannot_scale_to_zero",
            requirement="must cost nothing when idle",
            message="bills while idle",
            source="https://example.invalid",
        ),
    )
    advice = evaluate_advice(_workload(zero_idle_cost=True), limits, Option.SERVERLESS, blocker)
    assert advice == ()


# ------------------------------------------------------------------------ conflicts


def test_conflicts_group_by_constraint_and_lead_with_the_broadest() -> None:
    eliminations = (
        Elimination(
            option=Option.REAL_TIME,
            constraint_id="cannot_scale_to_zero",
            requirement="must cost nothing when idle",
            message="",
            source="https://example.invalid",
        ),
        Elimination(
            option=Option.ASYNC,
            constraint_id="no_inline_response",
            requirement="the caller needs an immediate response",
            message="",
            source="https://example.invalid",
        ),
        Elimination(
            option=Option.BATCH_TRANSFORM,
            constraint_id="no_inline_response",
            requirement="the caller needs an immediate response",
            message="",
            source="https://example.invalid",
        ),
    )
    conflicts = build_conflicts(eliminations)
    assert [conflict.constraint_id for conflict in conflicts] == [
        "no_inline_response",
        "cannot_scale_to_zero",
    ]
    assert conflicts[0].eliminated == (Option.ASYNC, Option.BATCH_TRANSFORM)
    assert conflicts[0].requirement == "the caller needs an immediate response"


def test_conflicts_from_nothing_are_empty() -> None:
    assert build_conflicts(()) == ()
