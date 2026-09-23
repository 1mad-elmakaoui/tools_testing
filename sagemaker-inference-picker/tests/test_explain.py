"""The plain-language layer: what the user actually reads."""

from __future__ import annotations

from sagemaker_inference_picker import explain
from sagemaker_inference_picker.engine import recommend
from sagemaker_inference_picker.limits import LimitsData
from sagemaker_inference_picker.models import Option, TrafficPattern, Workload


def _workload(**overrides: object) -> Workload:
    base: dict[str, object] = {
        "payload_mb": 1.0,
        "response_mb": 1.0,
        "processing_seconds": 1.0,
        "traffic": TrafficPattern.STEADY,
    }
    base.update(overrides)
    return Workload(**base)  # type: ignore[arg-type]


def test_headline_names_the_winner(limits: LimitsData) -> None:
    result = recommend(_workload(immediate_response=True), limits)
    assert explain.headline(result, limits) == "Use Real-time endpoint."


def test_headline_flags_a_sole_survivor(limits: LimitsData) -> None:
    result = recommend(
        _workload(zero_idle_cost=True, immediate_response=True, traffic=TrafficPattern.BURSTY_IDLE),
        limits,
    )
    headline = explain.headline(result, limits)
    assert "Serverless inference" in headline
    assert "only option" in headline


def test_headline_flags_a_tie(limits: LimitsData) -> None:
    result = recommend(_workload(zero_idle_cost=True), limits)
    assert result.recommended is Option.BATCH_TRANSFORM
    assert result.ranked[0].score == result.ranked[1].score
    headline = explain.headline(result, limits)
    assert "narrowly" in headline
    assert "Asynchronous inference" in headline


def test_headline_when_nothing_fits(limits: LimitsData) -> None:
    result = recommend(_workload(immediate_response=True, needs_notification=True), limits)
    assert explain.headline(result, limits) == (
        "No SageMaker inference option satisfies all of these requirements."
    )


def test_deciding_factors_are_the_winners_positive_reasons(limits: LimitsData) -> None:
    result = recommend(_workload(immediate_response=True), limits)
    factors = explain.deciding_factors(result, limits)
    assert any("steady" in factor.lower() for factor in factors)


def test_deciding_factors_fall_back_to_the_tie_break_rationale(limits: LimitsData) -> None:
    """A scheduled-batch workload whose caller still waits leaves the two online options.

    Neither gets a preference (the batch preferences went to the options that were then
    eliminated for not answering the caller), so the tie-break has to decide.
    """
    result = recommend(
        _workload(traffic=TrafficPattern.SCHEDULED_BATCH, immediate_response=True), limits
    )
    assert result.recommended is Option.SERVERLESS
    assert [ranked.score for ranked in result.ranked] == [0.0, 0.0]
    factors = explain.deciding_factors(result, limits)
    assert factors
    assert any("tie-break" in factor for factor in factors)


def test_there_are_no_deciding_factors_without_a_recommendation(limits: LimitsData) -> None:
    result = recommend(_workload(immediate_response=True, needs_notification=True), limits)
    assert explain.deciding_factors(result, limits) == ()


def test_caveats_surface_the_cold_start_warning(limits: LimitsData) -> None:
    result = recommend(
        _workload(
            traffic=TrafficPattern.BURSTY_IDLE,
            zero_idle_cost=True,
            immediate_response=True,
            latency_p99_ms=200,
        ),
        limits,
    )
    assert result.recommended is Option.SERVERLESS
    caveats = explain.caveats(result)
    assert any("cold start" in caveat for caveat in caveats)


def test_there_are_no_caveats_when_nothing_counts_against_the_winner(
    limits: LimitsData,
) -> None:
    result = recommend(_workload(immediate_response=True), limits)
    assert explain.caveats(result) == ()


def test_rejections_list_every_eliminated_option_with_sources(limits: LimitsData) -> None:
    result = recommend(
        _workload(payload_mb=800.0, processing_seconds=1800.0, needs_notification=True),
        limits,
    )
    rejections = explain.rejections(result, limits)
    rejected = {rejection.option for rejection in rejections}
    assert rejected == {Option.REAL_TIME, Option.SERVERLESS, Option.BATCH_TRANSFORM}
    for rejection in rejections:
        assert rejection.reasons
        assert rejection.display_name
        assert all(source.startswith("https://") for source in rejection.sources)
        assert len(set(rejection.sources)) == len(rejection.sources)


def test_rejections_are_empty_when_nothing_is_eliminated(limits: LimitsData) -> None:
    result = recommend(_workload(payload_mb=0.1, processing_seconds=0.1), limits)
    assert explain.rejections(result, limits) == ()


def test_conflict_summary_names_each_requirement_and_what_it_ruled_out(
    limits: LimitsData,
) -> None:
    result = recommend(
        _workload(
            traffic=TrafficPattern.BURSTY_IDLE,
            gpu_required=True,
            zero_idle_cost=True,
            immediate_response=True,
        ),
        limits,
    )
    lines = explain.conflict_summary(result, limits)
    joined = " ".join(lines)
    assert "A GPU is required" in joined
    assert "Must cost nothing when idle" in joined
    assert "The caller needs an immediate response" in joined
    assert "Asynchronous inference and Batch transform" in joined
    assert "Drop or relax one of them" in lines[-1]


def test_conflict_summary_is_empty_when_something_fits(limits: LimitsData) -> None:
    result = recommend(_workload(immediate_response=True), limits)
    assert explain.conflict_summary(result, limits) == ()


def test_runners_up_name_the_options_that_fit_but_lost(limits: LimitsData) -> None:
    result = recommend(
        _workload(
            payload_mb=12.0,
            response_mb=3.0,
            processing_seconds=240.0,
            traffic=TrafficPattern.BURSTY_IDLE,
            zero_idle_cost=True,
            needs_notification=True,
            gpu_required=True,
        ),
        limits,
    )
    assert result.recommended is Option.ASYNC
    lines = explain.runners_up(result, limits)
    assert len(lines) == 1
    assert "Batch transform" in lines[0]
    assert "satisfies every hard constraint" in lines[0]


def test_a_sole_survivor_has_no_runners_up(limits: LimitsData) -> None:
    result = recommend(
        _workload(
            traffic=TrafficPattern.BURSTY_IDLE,
            zero_idle_cost=True,
            immediate_response=True,
        ),
        limits,
    )
    assert len(result.ranked) == 1
    assert explain.runners_up(result, limits) == ()


def test_there_are_no_runners_up_without_a_recommendation(limits: LimitsData) -> None:
    result = recommend(_workload(immediate_response=True, needs_notification=True), limits)
    assert explain.runners_up(result, limits) == ()
