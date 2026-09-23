"""Table-driven scenarios covering the whole decision engine.

Includes values sitting exactly on each documented limit, values one step past it, and
deliberately contradictory requirements that no option can satisfy.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import pytest

from sagemaker_inference_picker.engine import recommend
from sagemaker_inference_picker.limits import LimitsData
from sagemaker_inference_picker.models import Option, TrafficPattern, Workload

BURSTY = TrafficPattern.BURSTY_IDLE
STEADY = TrafficPattern.STEADY
SCHEDULED = TrafficPattern.SCHEDULED_BATCH


@dataclass(frozen=True)
class Scenario:
    """One expected outcome, asserted end to end through :func:`recommend`."""

    name: str
    workload: Workload
    expected: Option | None
    #: option -> the constraint id that must be the reason it was eliminated
    must_eliminate: Mapping[Option, str] = field(default_factory=dict)
    #: options that must NOT be eliminated
    must_survive: tuple[Option, ...] = ()
    #: advice ids that must appear
    must_advise: tuple[str, ...] = ()

    def __str__(self) -> str:
        return self.name


SCENARIOS: tuple[Scenario, ...] = (
    # ---------------------------------------------------------------- clean winners
    Scenario(
        name="steady-low-latency-api-picks-real-time",
        workload=Workload(
            payload_mb=0.05,
            response_mb=0.01,
            processing_seconds=0.2,
            traffic=STEADY,
            latency_p99_ms=100,
            immediate_response=True,
        ),
        expected=Option.REAL_TIME,
        must_eliminate={
            Option.ASYNC: "no_inline_response",
            Option.BATCH_TRANSFORM: "no_inline_response",
        },
        must_survive=(Option.SERVERLESS,),
    ),
    Scenario(
        name="bursty-zero-idle-cost-picks-serverless",
        workload=Workload(
            payload_mb=1.0,
            response_mb=0.5,
            processing_seconds=5.0,
            traffic=BURSTY,
            zero_idle_cost=True,
            immediate_response=True,
        ),
        expected=Option.SERVERLESS,
        must_eliminate={Option.REAL_TIME: "cannot_scale_to_zero"},
    ),
    Scenario(
        name="scheduled-dataset-picks-batch-transform",
        workload=Workload(
            payload_mb=10.0,
            response_mb=2.0,
            processing_seconds=300.0,
            traffic=SCHEDULED,
        ),
        expected=Option.BATCH_TRANSFORM,
        must_eliminate={
            Option.REAL_TIME: "processing_time_over_limit",
            Option.SERVERLESS: "request_payload_over_limit",
        },
        must_survive=(Option.ASYNC,),
    ),
    Scenario(
        name="large-payload-long-job-with-notification-picks-async",
        workload=Workload(
            payload_mb=800.0,
            response_mb=50.0,
            processing_seconds=1800.0,
            traffic=BURSTY,
            needs_notification=True,
        ),
        expected=Option.ASYNC,
        must_eliminate={
            Option.REAL_TIME: "request_payload_over_limit",
            Option.SERVERLESS: "request_payload_over_limit",
            Option.BATCH_TRANSFORM: "request_payload_over_limit",
        },
    ),
    Scenario(
        name="gpu-long-offline-inference-picks-async",
        workload=Workload(
            payload_mb=500.0,
            response_mb=120.0,
            processing_seconds=1800.0,
            traffic=BURSTY,
            gpu_required=True,
            needs_notification=True,
        ),
        expected=Option.ASYNC,
        must_eliminate={Option.SERVERLESS: "request_payload_over_limit"},
    ),
    # ------------------------------------------------- serverless, exactly at limits
    Scenario(
        name="serverless-request-payload-exactly-at-4mb-survives",
        workload=Workload(
            payload_mb=4.0,
            response_mb=0.5,
            processing_seconds=5.0,
            traffic=BURSTY,
            zero_idle_cost=True,
            immediate_response=True,
        ),
        expected=Option.SERVERLESS,
        must_survive=(Option.SERVERLESS,),
    ),
    Scenario(
        name="serverless-request-payload-just-over-4mb-is-eliminated",
        workload=Workload(
            payload_mb=4.01,
            response_mb=0.5,
            processing_seconds=5.0,
            traffic=BURSTY,
            zero_idle_cost=True,
            immediate_response=True,
        ),
        expected=None,
        must_eliminate={
            Option.SERVERLESS: "request_payload_over_limit",
            Option.REAL_TIME: "cannot_scale_to_zero",
        },
    ),
    Scenario(
        name="serverless-response-payload-exactly-at-4mb-survives",
        workload=Workload(
            payload_mb=0.5,
            response_mb=4.0,
            processing_seconds=5.0,
            traffic=BURSTY,
            zero_idle_cost=True,
            immediate_response=True,
        ),
        expected=Option.SERVERLESS,
        must_survive=(Option.SERVERLESS,),
    ),
    Scenario(
        name="serverless-response-payload-just-over-4mb-is-eliminated",
        workload=Workload(
            payload_mb=0.5,
            response_mb=4.5,
            processing_seconds=5.0,
            traffic=BURSTY,
            immediate_response=True,
        ),
        expected=Option.REAL_TIME,
        must_eliminate={Option.SERVERLESS: "response_payload_over_limit"},
    ),
    Scenario(
        name="serverless-processing-exactly-at-60s-survives",
        workload=Workload(
            payload_mb=1.0,
            response_mb=1.0,
            processing_seconds=60.0,
            traffic=BURSTY,
            zero_idle_cost=True,
            immediate_response=True,
        ),
        expected=Option.SERVERLESS,
        must_survive=(Option.SERVERLESS,),
    ),
    Scenario(
        name="processing-just-over-60s-eliminates-both-online-options",
        workload=Workload(
            payload_mb=1.0,
            response_mb=1.0,
            processing_seconds=60.1,
            traffic=BURSTY,
        ),
        expected=Option.ASYNC,
        must_eliminate={
            Option.SERVERLESS: "processing_time_over_limit",
            Option.REAL_TIME: "processing_time_over_limit",
        },
    ),
    # ------------------------------------------------- real-time, exactly at limits
    Scenario(
        name="real-time-payload-exactly-at-25mb-survives",
        workload=Workload(
            payload_mb=25.0,
            response_mb=1.0,
            processing_seconds=10.0,
            traffic=STEADY,
            immediate_response=True,
        ),
        expected=Option.REAL_TIME,
        must_eliminate={Option.SERVERLESS: "request_payload_over_limit"},
    ),
    Scenario(
        name="real-time-payload-just-over-25mb-is-eliminated",
        workload=Workload(
            payload_mb=25.5,
            response_mb=1.0,
            processing_seconds=10.0,
            traffic=STEADY,
            immediate_response=True,
        ),
        expected=None,
        must_eliminate={
            Option.REAL_TIME: "request_payload_over_limit",
            Option.SERVERLESS: "request_payload_over_limit",
        },
    ),
    Scenario(
        name="real-time-processing-exactly-at-60s-survives",
        workload=Workload(
            payload_mb=10.0,
            response_mb=1.0,
            processing_seconds=60.0,
            traffic=STEADY,
            immediate_response=True,
        ),
        expected=Option.REAL_TIME,
        must_survive=(Option.REAL_TIME,),
    ),
    # ----------------------------------------------------- async, exactly at limits
    Scenario(
        name="async-payload-exactly-at-1gb-survives",
        workload=Workload(
            payload_mb=1024.0,
            response_mb=10.0,
            processing_seconds=600.0,
            traffic=BURSTY,
        ),
        expected=Option.ASYNC,
        must_survive=(Option.ASYNC,),
        must_eliminate={Option.BATCH_TRANSFORM: "request_payload_over_limit"},
    ),
    Scenario(
        name="payload-just-over-1gb-fits-nowhere",
        workload=Workload(
            payload_mb=1025.0,
            response_mb=10.0,
            processing_seconds=600.0,
            traffic=BURSTY,
        ),
        expected=None,
        must_eliminate={
            Option.REAL_TIME: "request_payload_over_limit",
            Option.SERVERLESS: "request_payload_over_limit",
            Option.ASYNC: "request_payload_over_limit",
            Option.BATCH_TRANSFORM: "request_payload_over_limit",
        },
    ),
    Scenario(
        name="processing-exactly-at-3600s-survives-offline",
        workload=Workload(
            payload_mb=50.0,
            response_mb=10.0,
            processing_seconds=3600.0,
            traffic=SCHEDULED,
        ),
        expected=Option.BATCH_TRANSFORM,
        must_survive=(Option.ASYNC, Option.BATCH_TRANSFORM),
    ),
    Scenario(
        name="processing-just-over-3600s-fits-nowhere",
        workload=Workload(
            payload_mb=50.0,
            response_mb=10.0,
            processing_seconds=3601.0,
            traffic=SCHEDULED,
        ),
        expected=None,
        must_eliminate={
            Option.REAL_TIME: "processing_time_over_limit",
            Option.SERVERLESS: "processing_time_over_limit",
            Option.ASYNC: "processing_time_over_limit",
            Option.BATCH_TRANSFORM: "processing_time_over_limit",
        },
    ),
    # --------------------------------------------- batch transform, exactly at limit
    Scenario(
        name="batch-record-exactly-at-100mb-survives",
        workload=Workload(
            payload_mb=100.0,
            response_mb=20.0,
            processing_seconds=400.0,
            traffic=SCHEDULED,
        ),
        expected=Option.BATCH_TRANSFORM,
        must_survive=(Option.BATCH_TRANSFORM,),
    ),
    Scenario(
        name="batch-record-just-over-100mb-falls-back-to-async",
        workload=Workload(
            payload_mb=101.0,
            response_mb=20.0,
            processing_seconds=400.0,
            traffic=SCHEDULED,
        ),
        expected=Option.ASYNC,
        must_eliminate={Option.BATCH_TRANSFORM: "request_payload_over_limit"},
    ),
    # ---------------------------------------------------------- deliberate conflicts
    Scenario(
        name="conflict-gpu-plus-zero-idle-cost-plus-immediate-response",
        workload=Workload(
            payload_mb=1.0,
            response_mb=1.0,
            processing_seconds=10.0,
            traffic=BURSTY,
            gpu_required=True,
            zero_idle_cost=True,
            immediate_response=True,
        ),
        expected=None,
        must_eliminate={
            Option.REAL_TIME: "cannot_scale_to_zero",
            Option.SERVERLESS: "gpu_not_supported",
            Option.ASYNC: "no_inline_response",
            Option.BATCH_TRANSFORM: "no_inline_response",
        },
        must_advise=("inference_components_scale_to_zero",),
    ),
    Scenario(
        name="conflict-immediate-response-plus-completion-notification",
        workload=Workload(
            payload_mb=1.0,
            response_mb=1.0,
            processing_seconds=10.0,
            traffic=STEADY,
            immediate_response=True,
            needs_notification=True,
        ),
        expected=None,
        must_eliminate={
            Option.REAL_TIME: "no_native_completion_notification",
            Option.SERVERLESS: "no_native_completion_notification",
            Option.ASYNC: "no_inline_response",
            Option.BATCH_TRANSFORM: "no_inline_response",
        },
    ),
    Scenario(
        name="conflict-tight-latency-target-with-two-minute-inference",
        workload=Workload(
            payload_mb=2.0,
            response_mb=1.0,
            processing_seconds=120.0,
            traffic=STEADY,
            latency_p99_ms=200,
        ),
        expected=None,
        must_eliminate={
            Option.REAL_TIME: "processing_time_over_limit",
            Option.SERVERLESS: "processing_time_over_limit",
            Option.ASYNC: "no_inline_response",
            Option.BATCH_TRANSFORM: "no_inline_response",
        },
    ),
    Scenario(
        name="conflict-gpu-plus-zero-idle-with-tight-latency",
        workload=Workload(
            payload_mb=2.0,
            response_mb=1.0,
            processing_seconds=3.0,
            traffic=BURSTY,
            latency_p99_ms=250,
            gpu_required=True,
            zero_idle_cost=True,
        ),
        expected=None,
        must_eliminate={
            Option.REAL_TIME: "cannot_scale_to_zero",
            Option.SERVERLESS: "gpu_not_supported",
        },
        must_advise=("inference_components_scale_to_zero",),
    ),
    # -------------------------------------------------------------- secondary advice
    Scenario(
        name="many-idle-models-on-gpu-advises-mme-and-inference-components",
        workload=Workload(
            payload_mb=1.0,
            response_mb=1.0,
            processing_seconds=2.0,
            traffic=BURSTY,
            gpu_required=True,
            immediate_response=True,
            model_count=12,
        ),
        expected=Option.REAL_TIME,
        must_advise=("multi_model_endpoint", "inference_components"),
    ),
    Scenario(
        name="serverless-with-a-latency-target-advises-provisioned-concurrency",
        workload=Workload(
            payload_mb=1.0,
            response_mb=0.5,
            processing_seconds=5.0,
            traffic=BURSTY,
            latency_p99_ms=800,
            zero_idle_cost=True,
            immediate_response=True,
        ),
        expected=Option.SERVERLESS,
        must_advise=("provisioned_concurrency",),
    ),
    Scenario(
        name="two-models-on-real-time-advises-inference-components-only",
        workload=Workload(
            payload_mb=1.0,
            response_mb=1.0,
            processing_seconds=2.0,
            traffic=STEADY,
            immediate_response=True,
            model_count=2,
        ),
        expected=Option.REAL_TIME,
        must_advise=("inference_components",),
    ),
    Scenario(
        name="gpu-zero-idle-offline-advises-the-inference-component-escape-hatch",
        workload=Workload(
            payload_mb=1.0,
            response_mb=1.0,
            processing_seconds=10.0,
            traffic=BURSTY,
            gpu_required=True,
            zero_idle_cost=True,
        ),
        expected=Option.ASYNC,
        must_advise=("inference_components_scale_to_zero",),
    ),
    # --------------------------------------------------------------------- tie-break
    Scenario(
        name="tie-between-offline-options-prefers-the-least-infrastructure",
        workload=Workload(
            payload_mb=1.0,
            response_mb=1.0,
            processing_seconds=10.0,
            traffic=STEADY,
            zero_idle_cost=True,
        ),
        expected=Option.BATCH_TRANSFORM,
        must_eliminate={Option.REAL_TIME: "cannot_scale_to_zero"},
        must_survive=(Option.SERVERLESS, Option.ASYNC, Option.BATCH_TRANSFORM),
    ),
    Scenario(
        name="trivial-workload-with-no-constraints-picks-real-time",
        workload=Workload(
            payload_mb=0.01,
            response_mb=0.01,
            processing_seconds=0.05,
            traffic=STEADY,
            immediate_response=True,
        ),
        expected=Option.REAL_TIME,
        must_survive=(Option.REAL_TIME, Option.SERVERLESS),
    ),
)


def test_the_table_is_substantial() -> None:
    assert len(SCENARIOS) >= 20
    assert len({scenario.name for scenario in SCENARIOS}) == len(SCENARIOS)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=str)
def test_scenario(scenario: Scenario, limits: LimitsData) -> None:
    result = recommend(scenario.workload, limits)

    assert result.recommended == scenario.expected, (
        f"expected {scenario.expected} but got {result.recommended}; "
        f"ranked={[(r.option.value, r.score) for r in result.ranked]}"
    )

    for option, constraint_id in scenario.must_eliminate.items():
        eliminations = result.eliminations_for(option)
        assert eliminations, f"{option.value} should have been eliminated but survived"
        assert constraint_id in {item.constraint_id for item in eliminations}, (
            f"{option.value} was eliminated, but not by {constraint_id}: "
            f"{[item.constraint_id for item in eliminations]}"
        )

    for option in scenario.must_survive:
        assert not result.eliminations_for(option), (
            f"{option.value} should have survived but was eliminated by "
            f"{[item.constraint_id for item in result.eliminations_for(option)]}"
        )

    advice_ids = {item.advice_id for item in result.advice}
    for advice_id in scenario.must_advise:
        assert advice_id in advice_ids, f"expected advice {advice_id}, got {sorted(advice_ids)}"


@pytest.mark.parametrize("scenario", SCENARIOS, ids=str)
def test_every_elimination_carries_a_source_and_a_reason(
    scenario: Scenario, limits: LimitsData
) -> None:
    result = recommend(scenario.workload, limits)
    for elimination in result.eliminations:
        assert elimination.source.startswith("https://")
        assert elimination.message.strip()
        assert elimination.requirement.strip()


@pytest.mark.parametrize("scenario", SCENARIOS, ids=str)
def test_unresolved_scenarios_name_their_conflicts(scenario: Scenario, limits: LimitsData) -> None:
    result = recommend(scenario.workload, limits)
    if result.resolved:
        assert result.conflicts == ()
        return
    assert result.conflicts, "an unresolved recommendation must name the conflicts"
    covered = {option for conflict in result.conflicts for option in conflict.eliminated}
    assert covered == set(Option), "every option must appear in the conflict report"


@pytest.mark.parametrize("scenario", SCENARIOS, ids=str)
def test_recommendation_is_deterministic(scenario: Scenario, limits: LimitsData) -> None:
    first = recommend(scenario.workload, limits)
    second = recommend(scenario.workload, limits)
    assert first.recommended == second.recommended
    assert [(r.option, r.score) for r in first.ranked] == [
        (r.option, r.score) for r in second.ranked
    ]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=str)
def test_a_recommended_option_is_never_eliminated(scenario: Scenario, limits: LimitsData) -> None:
    result = recommend(scenario.workload, limits)
    if result.recommended is not None:
        assert result.eliminations_for(result.recommended) == ()
        assert result.score_for(result.recommended) is not None
