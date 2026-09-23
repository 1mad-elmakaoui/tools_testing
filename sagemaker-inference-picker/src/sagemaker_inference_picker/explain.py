"""Turn a :class:`Recommendation` into plain-language lines.

Kept separate from the Rich and JSON renderers so the wording itself can be asserted on in
tests without going near a terminal.
"""

from __future__ import annotations

from dataclasses import dataclass

from sagemaker_inference_picker.limits import LimitsData
from sagemaker_inference_picker.models import Option, Recommendation


@dataclass(frozen=True)
class Rejection:
    """One option that was ruled out, with every constraint that ruled it out."""

    option: Option
    display_name: str
    reasons: tuple[str, ...]
    sources: tuple[str, ...]


def headline(recommendation: Recommendation, limits: LimitsData) -> str:
    """The one-sentence verdict."""
    if recommendation.recommended is None:
        return "No SageMaker inference option satisfies all of these requirements."

    name = limits.limits_for(recommendation.recommended).display_name
    ranked = recommendation.ranked
    if len(ranked) == 1:
        return f"Use {name} — the only option that satisfies every hard constraint."
    if ranked[0].score == ranked[1].score:
        runner_up = limits.limits_for(ranked[1].option).display_name
        return (
            f"Use {name}, narrowly — it scores level with {runner_up} and wins the tie on "
            f"having less always-on infrastructure to run."
        )
    return f"Use {name}."


def deciding_factors(recommendation: Recommendation, limits: LimitsData) -> tuple[str, ...]:
    """Why the recommended option won, in plain language."""
    if recommendation.recommended is None or not recommendation.ranked:
        return ()

    top = recommendation.ranked[0]
    factors = [preference.message for preference in top.reasons if preference.weight > 0]
    if len(recommendation.ranked) == 1:
        factors.append("Every other option was ruled out by a hard constraint, listed below.")
    elif not factors:
        factors.append(
            "Nothing about this workload favours one surviving option over another, so the "
            f"tie-break decided it: {limits.tie_break_rationale}"
        )
    return tuple(factors)


def caveats(recommendation: Recommendation) -> tuple[str, ...]:
    """Soft preferences that counted against the recommended option."""
    if not recommendation.ranked:
        return ()
    return tuple(
        preference.message
        for preference in recommendation.ranked[0].reasons
        if preference.weight < 0
    )


def runners_up(recommendation: Recommendation, limits: LimitsData) -> tuple[str, ...]:
    """Surviving options that were not chosen.

    Without this, an option that satisfied every hard constraint but ranked lower would
    appear in neither the recommendation nor the rejections, and simply vanish.
    """
    if recommendation.recommended is None or len(recommendation.ranked) < 2:
        return ()
    winner = recommendation.ranked[0]
    return tuple(
        f"{limits.limits_for(ranked.option).display_name} also satisfies every hard "
        f"constraint, but fits this workload less well "
        f"(scored {ranked.score:g} against {winner.score:g})."
        for ranked in recommendation.ranked[1:]
    )


def rejections(recommendation: Recommendation, limits: LimitsData) -> tuple[Rejection, ...]:
    """Every eliminated option with the specific constraints that eliminated it."""
    result: list[Rejection] = []
    for option in Option:
        eliminations = recommendation.eliminations_for(option)
        if not eliminations:
            continue
        sources: list[str] = []
        for elimination in eliminations:
            if elimination.source not in sources:
                sources.append(elimination.source)
        result.append(
            Rejection(
                option=option,
                display_name=limits.limits_for(option).display_name,
                reasons=tuple(elimination.message for elimination in eliminations),
                sources=tuple(sources),
            )
        )
    return tuple(result)


def conflict_summary(recommendation: Recommendation, limits: LimitsData) -> tuple[str, ...]:
    """Name the requirements that cannot be satisfied together.

    Empty unless every option was eliminated.
    """
    if recommendation.recommended is not None or not recommendation.conflicts:
        return ()
    lines = [
        f"{_capitalise(conflict.requirement)} — rules out "
        f"{_join([limits.limits_for(option).display_name for option in conflict.eliminated])}."
        for conflict in recommendation.conflicts
    ]
    lines.append(
        "No single SageMaker option covers all of these at once. Drop or relax one of them, "
        "or split the workload so that different requirements are served by different options."
    )
    return tuple(lines)


def _join(items: list[str]) -> str:
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _capitalise(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text
