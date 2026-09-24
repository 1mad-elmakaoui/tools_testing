"""Suggest what to do about a finding.

Pure. The remedy follows from what the traffic looks like and what the variant is, not
from a general rule about endpoints:

* nothing at all, for long enough to be sure — delete it;
* a trickle, and small enough to fit serverless — move it, since serverless costs nothing
  between requests;
* a trickle, but too large or too GPU-bound for serverless — asynchronous inference with a
  scale-to-zero policy gets the same property while keeping the instance type;
* real but thin traffic on several instances — keep the endpoint, run fewer of them.
"""

from __future__ import annotations

from sagemaker_idle_finder.catalog import AwsFacts, Thresholds
from sagemaker_idle_finder.classify import justified_instances
from sagemaker_idle_finder.models import MetricWindow, Remedy, Variant, Verdict

#: Instance families that carry an accelerator. A GPU workload cannot move to serverless.
_ACCELERATED_FAMILIES = ("p2", "p3", "p4", "p5", "g4", "g5", "g6", "inf1", "inf2", "trn1")


def recommend(
    variant: Variant,
    verdict: Verdict,
    window: MetricWindow | None,
    thresholds: Thresholds,
    aws: AwsFacts,
) -> tuple[Remedy, str]:
    """Return a remedy for `variant` and a sentence saying why."""
    if verdict is Verdict.IDLE:
        return (
            Remedy.DELETE,
            "nothing invoked it across the whole lookback, so the first question is "
            "whether it should exist at all; if it must stay, an asynchronous endpoint "
            "with a scale-to-zero policy costs nothing idle",
        )

    if verdict is not Verdict.UNDERUSED or window is None:
        return Remedy.NONE, ""

    justified = justified_instances(window, variant.instance_count, thresholds)
    surplus = variant.instance_count - justified
    if surplus > 0:
        return (
            Remedy.REDUCE_INSTANCE_COUNT,
            f"the observed traffic justifies about {justified} instance"
            f"{'' if justified == 1 else 's'} at the threshold, so {surplus} of the "
            f"{variant.instance_count} look surplus",
        )

    if _serverless_candidate(variant, thresholds):
        return (
            Remedy.MOVE_TO_SERVERLESS,
            "traffic is thin and the instance is small enough that the model may fit "
            "serverless inference, which bills per request rather than per idle hour; "
            "check the model against the "
            f"{aws.serverless_max_memory_gb} GB memory ceiling before moving",
        )

    return (
        Remedy.MOVE_TO_ASYNC_SCALE_TO_ZERO,
        "traffic is thin but the instance is too large or too accelerated for serverless, "
        "so an asynchronous endpoint with a scale-to-zero policy keeps the instance type "
        "while removing the idle cost",
    )


def _serverless_candidate(variant: Variant, thresholds: Thresholds) -> bool:
    """Whether the variant is worth checking against serverless.

    Deliberately a candidacy test, not a fit test. Serverless has a hard memory ceiling, but
    the instance's RAM says nothing about the model's footprint, so the threshold is set
    generously and the wording tells the reader to check.
    """
    instance_type = variant.instance_type or ""
    if _family(instance_type) in _ACCELERATED_FAMILIES:
        return False
    memory_gb = _approximate_memory_gb(instance_type)
    if memory_gb is None:
        return False
    return memory_gb <= thresholds.serverless_candidate_max_instance_memory_gb


def _family(instance_type: str) -> str:
    parts = instance_type.split(".")
    return parts[1] if len(parts) >= 3 else ""


def _approximate_memory_gb(instance_type: str) -> int | None:
    """A rough memory figure from the instance size, for the serverless fit check only.

    Deliberately coarse: it decides between two suggestions and never feeds a cost, so a
    lookup table of every instance type's memory would be precision nobody reads.
    """
    parts = instance_type.split(".")
    if len(parts) < 3:
        return None
    sizes = {
        "medium": 4,
        "large": 8,
        "xlarge": 16,
        "2xlarge": 32,
        "4xlarge": 64,
        "8xlarge": 128,
        "12xlarge": 192,
        "16xlarge": 256,
        "24xlarge": 384,
    }
    return sizes.get(parts[2])
