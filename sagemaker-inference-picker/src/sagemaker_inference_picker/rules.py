"""The decision rules: hard constraints that eliminate, soft preferences that rank.

Every number these rules compare against comes from :mod:`limits`, which reads
``limits.yaml``. Nothing here performs I/O, so all of it is unit-testable directly.

A hard constraint is a documented limit or a missing capability: it removes an option from
consideration entirely. A soft preference only moves an option up or down the ranking, and
carries a weight that is itself declared in ``limits.yaml`` under the same name as the rule.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from sagemaker_inference_picker import units
from sagemaker_inference_picker.limits import LimitsData, OptionLimits
from sagemaker_inference_picker.models import (
    Advice,
    Conflict,
    Elimination,
    Option,
    Preference,
    TrafficPattern,
    Workload,
)

HardConstraint = Callable[[Workload, OptionLimits], Elimination | None]


# --------------------------------------------------------------------------------------
# Hard constraints
# --------------------------------------------------------------------------------------


def _request_payload_over_limit(workload: Workload, option: OptionLimits) -> Elimination | None:
    cap = option.max_request_payload_mb
    if cap.value is None or workload.payload_mb <= cap.value:
        return None
    return Elimination(
        option=option.option,
        constraint_id="request_payload_over_limit",
        requirement=f"request payload of {units.megabytes(workload.payload_mb)}",
        message=_with_note(
            f"{option.display_name} accepts at most {units.megabytes(cap.value)} per request.",
            cap.note,
        ),
        source=cap.source,
        limit=units.megabytes(cap.value),
        actual=units.megabytes(workload.payload_mb),
    )


def _response_payload_over_limit(workload: Workload, option: OptionLimits) -> Elimination | None:
    cap = option.max_response_payload_mb
    if cap.value is None or workload.response_mb <= cap.value:
        return None
    return Elimination(
        option=option.option,
        constraint_id="response_payload_over_limit",
        requirement=f"response payload of {units.megabytes(workload.response_mb)}",
        message=_with_note(
            f"{option.display_name} returns at most {units.megabytes(cap.value)} per response.",
            cap.note,
        ),
        source=cap.source,
        limit=units.megabytes(cap.value),
        actual=units.megabytes(workload.response_mb),
    )


def _processing_time_over_limit(workload: Workload, option: OptionLimits) -> Elimination | None:
    cap = option.max_processing_seconds
    if cap.value is None or workload.processing_seconds <= cap.value:
        return None
    return Elimination(
        option=option.option,
        constraint_id="processing_time_over_limit",
        requirement=f"processing time of {units.seconds(workload.processing_seconds)} per request",
        message=_with_note(
            f"{option.display_name} allows at most {units.seconds(cap.value)} per invocation.",
            cap.note,
        ),
        source=cap.source,
        limit=units.seconds(cap.value),
        actual=units.seconds(workload.processing_seconds),
    )


def _gpu_not_supported(workload: Workload, option: OptionLimits) -> Elimination | None:
    capability = option.gpu_supported
    if not workload.gpu_required or capability.value:
        return None
    return Elimination(
        option=option.option,
        constraint_id="gpu_not_supported",
        requirement="a GPU is required",
        message=_with_note(
            f"{option.display_name} does not support GPU instances.", capability.note
        ),
        source=capability.source,
    )


def _cannot_scale_to_zero(workload: Workload, option: OptionLimits) -> Elimination | None:
    capability = option.scales_to_zero
    if not workload.zero_idle_cost or capability.value:
        return None
    return Elimination(
        option=option.option,
        constraint_id="cannot_scale_to_zero",
        requirement="must cost nothing when idle",
        message=_with_note(
            f"{option.display_name} keeps at least one instance running between requests, "
            f"so it bills while idle.",
            capability.note,
        ),
        source=capability.source,
    )


def _no_inline_response(workload: Workload, option: OptionLimits) -> Elimination | None:
    capability = option.returns_inline_response
    if not workload.requires_inline_response or capability.value:
        return None
    if workload.immediate_response:
        requirement = "the caller needs an immediate response"
    else:
        requirement = (
            f"a p99 latency target of {units.milliseconds(workload.latency_p99_ms or 0)} "
            f"implies the caller waits for the result"
        )
    return Elimination(
        option=option.option,
        constraint_id="no_inline_response",
        requirement=requirement,
        message=_with_note(
            f"{option.display_name} never returns the prediction to the caller.",
            capability.note,
        ),
        source=capability.source,
    )


def _no_native_completion_notification(
    workload: Workload, option: OptionLimits
) -> Elimination | None:
    capability = option.native_completion_notification
    if not workload.needs_notification or capability.value:
        return None
    return Elimination(
        option=option.option,
        constraint_id="no_native_completion_notification",
        requirement="a completion notification is required",
        message=_with_note(
            f"{option.display_name} has no native completion notification.", capability.note
        ),
        source=capability.source,
    )


#: Evaluated in this order, so rejections read from the concrete to the architectural.
HARD_CONSTRAINTS: tuple[HardConstraint, ...] = (
    _request_payload_over_limit,
    _response_payload_over_limit,
    _processing_time_over_limit,
    _gpu_not_supported,
    _cannot_scale_to_zero,
    _no_inline_response,
    _no_native_completion_notification,
)


def evaluate_hard_constraints(workload: Workload, option: OptionLimits) -> tuple[Elimination, ...]:
    """Return every hard constraint that rules `option` out for `workload`."""
    found = (constraint(workload, option) for constraint in HARD_CONSTRAINTS)
    return tuple(elimination for elimination in found if elimination is not None)


# --------------------------------------------------------------------------------------
# Soft preferences
# --------------------------------------------------------------------------------------


def evaluate_preferences(workload: Workload, limits: LimitsData) -> tuple[Preference, ...]:
    """Score the options against the workload's soft preferences.

    Each rule id doubles as the key of its weight in ``limits.yaml``, so no weight is
    hard-coded here.
    """
    preferences: list[Preference] = []

    def add(option: Option, rule_id: str, message: str) -> None:
        preferences.append(
            Preference(
                option=option,
                rule_id=rule_id,
                weight=limits.weight(rule_id),
                message=message,
            )
        )

    if workload.traffic is TrafficPattern.STEADY:
        add(
            Option.REAL_TIME,
            "steady_traffic_favours_real_time",
            "Traffic is steady, which keeps provisioned instances busy — the one case where "
            "paying for always-on capacity is efficient.",
        )
    elif workload.traffic is TrafficPattern.BURSTY_IDLE:
        add(
            Option.SERVERLESS,
            "bursty_traffic_favours_serverless",
            "Traffic is bursty with idle periods, so paying per request beats paying for "
            "instances that sit idle between bursts.",
        )
        add(
            Option.ASYNC,
            "bursty_traffic_favours_async",
            "Traffic is bursty with idle periods, and an asynchronous endpoint scales to "
            "zero between bursts too, adding queueing and an Amazon S3 round trip.",
        )
    elif workload.traffic is TrafficPattern.SCHEDULED_BATCH:
        add(
            Option.BATCH_TRANSFORM,
            "scheduled_batch_favours_batch_transform",
            "The work is a known dataset on a schedule, which is what a transform job is: "
            "instances exist only while the job runs.",
        )
        add(
            Option.ASYNC,
            "scheduled_batch_favours_async",
            "An asynchronous endpoint can drain a scheduled backlog, but leaves an endpoint "
            "to operate that a transform job does not.",
        )

    if workload.latency_p99_ms is not None:
        target = units.milliseconds(workload.latency_p99_ms)
        if workload.latency_p99_ms < limits.heuristic("tight_latency_p99_ms"):
            add(
                Option.REAL_TIME,
                "tight_latency_favours_real_time",
                f"A p99 target of {target} needs warm instances, which a real-time endpoint "
                f"keeps warm by construction.",
            )
            cold_start = limits.limits_for(Option.SERVERLESS).facts["worst_case_cold_start_seconds"]
            add(
                Option.SERVERLESS,
                "tight_latency_penalises_serverless",
                f"A serverless cold start can add up to {units.seconds(cold_start.value)}, "
                f"which would miss a p99 target of {target} by a wide margin unless you add "
                f"provisioned concurrency.",
            )

    if not workload.requires_inline_response:
        add(
            Option.ASYNC,
            "offline_result_favours_async",
            "Nobody is waiting on the response, so queueing the request costs nothing in "
            "user-visible latency.",
        )
        add(
            Option.BATCH_TRANSFORM,
            "offline_result_favours_batch_transform",
            "Nobody is waiting on the response, so a job with no endpoint to operate is the "
            "cheapest thing to run.",
        )

    if workload.model_count >= limits.heuristic("multi_model_endpoint_min_models"):
        add(
            Option.REAL_TIME,
            "many_models_favour_real_time",
            f"With {workload.model_count} models, it matters that real-time is the only one "
            f"of the four options supporting multi-model endpoints and inference components.",
        )

    return tuple(preferences)


# --------------------------------------------------------------------------------------
# Secondary advice
# --------------------------------------------------------------------------------------


def evaluate_advice(
    workload: Workload,
    limits: LimitsData,
    winner: Option | None,
    eliminations: Iterable[Elimination],
) -> tuple[Advice, ...]:
    """Suggest deployment patterns that modify the recommended option.

    These are additions to a recommendation, not options in their own right.
    """
    advice: list[Advice] = []
    eliminations = tuple(eliminations)

    if winner is Option.SERVERLESS and workload.latency_p99_ms is not None:
        pattern = limits.pattern("provisioned_concurrency")
        cold_start = limits.limits_for(Option.SERVERLESS).facts["worst_case_cold_start_seconds"]
        advice.append(
            Advice(
                advice_id=pattern.key,
                title=pattern.display_name,
                message=(
                    f"You set a p99 target of {units.milliseconds(workload.latency_p99_ms)}, "
                    f"but a serverless cold start can reach {units.seconds(cold_start.value)}. "
                    f"{pattern.note}"
                ),
                source=pattern.source,
            )
        )

    if winner is Option.REAL_TIME:
        mme_minimum = limits.heuristic("multi_model_endpoint_min_models")
        real_time = limits.limits_for(Option.REAL_TIME)
        if (
            workload.model_count >= mme_minimum
            and workload.traffic is TrafficPattern.BURSTY_IDLE
            and real_time.supports_multi_model_endpoint.value
        ):
            pattern = limits.pattern("multi_model_endpoint")
            advice.append(
                Advice(
                    advice_id=pattern.key,
                    title=pattern.display_name,
                    message=(
                        f"{workload.model_count} models that are mostly idle is the case a "
                        f"multi-model endpoint is built for: one set of instances, each model "
                        f"loaded on demand. {pattern.note}"
                    ),
                    source=pattern.source,
                )
            )
        if workload.model_count >= limits.heuristic("inference_components_min_models"):
            pattern = limits.pattern("inference_components")
            advice.append(
                Advice(
                    advice_id=pattern.key,
                    title=pattern.display_name,
                    message=(
                        f"With {workload.model_count} models on one endpoint, inference "
                        f"components let each get its own resources and scale separately. "
                        f"{pattern.note}"
                    ),
                    source=pattern.source,
                )
            )

    pushed_offline = (
        winner is not None and not limits.limits_for(winner).returns_inline_response.value
    )
    if workload.zero_idle_cost and (winner is None or pushed_offline):
        blockers = {
            elimination.constraint_id
            for elimination in eliminations
            if elimination.option is Option.REAL_TIME
        }
        if blockers == {"cannot_scale_to_zero"}:
            pattern = limits.pattern("inference_components")
            advice.append(
                Advice(
                    advice_id=f"{pattern.key}_scale_to_zero",
                    title=f"{pattern.display_name} (scale to zero)",
                    message=(
                        "A real-time endpoint was ruled out only because it bills while idle. "
                        "That is the one constraint inference components can lift: an "
                        "inference-component endpoint can scale to zero instances, so it is "
                        "worth evaluating if the rest of real-time suits you. "
                        f"{pattern.note}"
                    ),
                    source=pattern.source,
                )
            )

    return tuple(advice)


# --------------------------------------------------------------------------------------
# Conflict reporting
# --------------------------------------------------------------------------------------


def build_conflicts(eliminations: Iterable[Elimination]) -> tuple[Conflict, ...]:
    """Group eliminations by the requirement that caused them.

    Used only when nothing survives, to name the requirements that cannot hold together.
    """
    order: list[str] = []
    requirements: dict[str, str] = {}
    eliminated: dict[str, list[Option]] = {}
    for elimination in eliminations:
        constraint_id = elimination.constraint_id
        if constraint_id not in requirements:
            order.append(constraint_id)
            requirements[constraint_id] = elimination.requirement
            eliminated[constraint_id] = []
        if elimination.option not in eliminated[constraint_id]:
            eliminated[constraint_id].append(elimination.option)
    conflicts = [
        Conflict(
            constraint_id=constraint_id,
            requirement=requirements[constraint_id],
            eliminated=tuple(eliminated[constraint_id]),
        )
        for constraint_id in order
    ]
    conflicts.sort(key=lambda conflict: (-len(conflict.eliminated), conflict.constraint_id))
    return tuple(conflicts)


def _with_note(message: str, note: str | None) -> str:
    """Append the documentation note to a rejection message when there is one."""
    if not note:
        return message
    return f"{message} {note}"
