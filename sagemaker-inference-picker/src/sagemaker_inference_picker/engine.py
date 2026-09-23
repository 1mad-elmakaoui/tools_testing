"""Turn a workload plus a set of documented limits into a recommendation.

Pure: given the same :class:`Workload` and :class:`LimitsData` this always produces the same
:class:`Recommendation`, and it touches neither the filesystem nor AWS.
"""

from __future__ import annotations

from sagemaker_inference_picker.limits import LimitsData
from sagemaker_inference_picker.models import (
    Elimination,
    Option,
    Preference,
    RankedOption,
    Recommendation,
    Workload,
)
from sagemaker_inference_picker.rules import (
    build_conflicts,
    evaluate_advice,
    evaluate_hard_constraints,
    evaluate_preferences,
)

_SCORE_PRECISION = 6


def recommend(workload: Workload, limits: LimitsData) -> Recommendation:
    """Recommend an inference option for `workload`, or report why none fits.

    Hard constraints eliminate first; soft preferences only rank what survives. When every
    option is eliminated, ``recommended`` is ``None`` and ``conflicts`` names the
    requirements that cannot hold at the same time.
    """
    eliminations: tuple[Elimination, ...] = tuple(
        elimination
        for option in Option
        for elimination in evaluate_hard_constraints(workload, limits.limits_for(option))
    )
    eliminated = {elimination.option for elimination in eliminations}
    survivors = [option for option in Option if option not in eliminated]

    preferences = evaluate_preferences(workload, limits)
    ranked = _rank(survivors, preferences, limits.tie_break_order)
    winner = ranked[0].option if ranked else None

    return Recommendation(
        workload=workload,
        recommended=winner,
        ranked=ranked,
        eliminations=eliminations,
        advice=evaluate_advice(workload, limits, winner, eliminations),
        conflicts=() if winner is not None else build_conflicts(eliminations),
        limits_last_verified=limits.last_verified,
    )


def _rank(
    survivors: list[Option],
    preferences: tuple[Preference, ...],
    tie_break_order: tuple[Option, ...],
) -> tuple[RankedOption, ...]:
    """Score survivors and order them, breaking ties with the documented order."""
    reasons: dict[Option, list[Preference]] = {option: [] for option in survivors}
    for preference in preferences:
        bucket = reasons.get(preference.option)
        if bucket is not None:
            bucket.append(preference)

    tie_break = {option: index for index, option in enumerate(tie_break_order)}
    ranked = [
        RankedOption(
            option=option,
            score=round(sum(item.weight for item in reasons[option]), _SCORE_PRECISION),
            reasons=tuple(reasons[option]),
        )
        for option in survivors
    ]
    ranked.sort(key=lambda entry: (-entry.score, tie_break[entry.option]))
    return tuple(ranked)
