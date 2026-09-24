"""Plain data types shared by the collector, the classifier and the renderers.

Frozen dataclasses and enums with no boto3 and no I/O, so classification, costing and
recommendation can be tested directly on hand-built values.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum


class EndpointStatus(StrEnum):
    """The endpoint statuses SageMaker reports.

    Only ``IN_SERVICE`` bills normally for instances; the rest are reported but never
    counted as waste, because the figure would be wrong or meaningless.
    """

    IN_SERVICE = "InService"
    CREATING = "Creating"
    UPDATING = "Updating"
    SYSTEM_UPDATING = "SystemUpdating"
    ROLLING_BACK = "RollingBack"
    DELETING = "Deleting"
    FAILED = "Failed"
    OUT_OF_SERVICE = "OutOfService"
    UNKNOWN = "Unknown"

    @property
    def bills_for_instances(self) -> bool:
        """Whether instances behind this endpoint are charged for as usual."""
        return self in {
            EndpointStatus.IN_SERVICE,
            EndpointStatus.UPDATING,
            EndpointStatus.SYSTEM_UPDATING,
            EndpointStatus.ROLLING_BACK,
        }


class Verdict(StrEnum):
    """What the tool concluded about one variant."""

    IDLE = "idle"
    UNDERUSED = "underused"
    HEALTHY = "healthy"
    #: Serverless, or a scalable target that can reach zero: there is no idle instance cost.
    NOT_APPLICABLE = "not_applicable"
    #: Created too recently for the lookback to say anything.
    TOO_NEW = "too_new"
    #: Not in a state where instances bill normally.
    NOT_BILLING = "not_billing"

    @property
    def is_waste(self) -> bool:
        """Whether this verdict implies money is being wasted."""
        return self in {Verdict.IDLE, Verdict.UNDERUSED}


class Remedy(StrEnum):
    """The suggested fix for a finding."""

    DELETE = "delete"
    MOVE_TO_SERVERLESS = "move_to_serverless"
    MOVE_TO_ASYNC_SCALE_TO_ZERO = "move_to_async_scale_to_zero"
    REDUCE_INSTANCE_COUNT = "reduce_instance_count"
    NONE = "none"


@dataclass(frozen=True)
class ScalableTarget:
    """An Application Auto Scaling target registered against a variant or component."""

    resource_id: str
    scalable_dimension: str
    min_capacity: int
    max_capacity: int

    @property
    def can_scale_to_zero(self) -> bool:
        """Whether this target is allowed to drop to nothing, and so cost nothing idle."""
        return self.min_capacity == 0


@dataclass(frozen=True)
class Variant:
    """One production variant of an endpoint, as described by SageMaker."""

    name: str
    instance_type: str | None = None
    instance_count: int = 0
    serverless_memory_mb: int | None = None
    serverless_max_concurrency: int | None = None
    #: Names of inference components hosted on this variant, if any.
    inference_components: tuple[str, ...] = ()
    scalable_target: ScalableTarget | None = None

    def __post_init__(self) -> None:
        if self.instance_count < 0:
            raise ValueError(f"instance_count must not be negative, got {self.instance_count}")

    @property
    def is_serverless(self) -> bool:
        """Serverless variants have no instances, so no idle instance cost."""
        return self.serverless_memory_mb is not None

    @property
    def uses_inference_components(self) -> bool:
        """Whether models on this variant are deployed as inference components."""
        return bool(self.inference_components)

    @property
    def can_scale_to_zero(self) -> bool:
        """Whether autoscaling is allowed to take this variant to zero capacity."""
        return self.scalable_target is not None and self.scalable_target.can_scale_to_zero


@dataclass(frozen=True)
class Endpoint:
    """An endpoint and its variants, in one region."""

    name: str
    region: str
    status: EndpointStatus
    created_at: dt.datetime
    variants: tuple[Variant, ...] = ()
    config_name: str | None = None
    failure_reason: str | None = None


@dataclass(frozen=True)
class MetricWindow:
    """The invocation history observed for one variant."""

    start: dt.datetime
    end: dt.datetime
    #: Total invocations summed across the window. None when CloudWatch returned no data
    #: at all, which is not the same as a measured zero.
    total_invocations: float | None
    datapoints: int = 0

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError("a metric window must not end before it starts")

    @property
    def hours(self) -> float:
        """Length of the window in hours."""
        return (self.end - self.start).total_seconds() / 3600

    @property
    def observed(self) -> float:
        """Invocations, treating an empty CloudWatch response as zero.

        Safe only once the caller has established the endpoint existed for the window; that
        is what the ``too_new`` verdict is for.
        """
        return 0.0 if self.total_invocations is None else self.total_invocations


@dataclass(frozen=True)
class CostEstimate:
    """An estimated monthly cost, and how much of it is judged wasted."""

    monthly_usd: float | None
    wasted_monthly_usd: float | None
    instance_hour_usd: float | None = None
    #: What the same traffic would cost on serverless inference, when the caller supplied
    #: the two things AWS cannot be asked for: how long a request takes and how much
    #: memory the model needs.
    serverless_monthly_usd: float | None = None
    #: Set when a figure could not be established, so the renderer can say why rather
    #: than print a zero that would read as "nothing to reclaim here".
    no_estimate_reason: str | None = None

    @property
    def is_known(self) -> bool:
        """Whether a cost could be estimated at all."""
        return self.monthly_usd is not None


@dataclass(frozen=True)
class ServerlessWhatIf:
    """The caller's assumptions for costing a move to serverless inference.

    Neither figure can be read from an endpoint: ``Invocations`` says how often a model was
    called, not how long a call took, and nothing reports the memory a model would need on
    serverless. Rather than guess at them, the tool asks for them and says the answer rests
    on what was supplied.
    """

    seconds_per_invocation: float
    memory_gb: int


@dataclass(frozen=True)
class Finding:
    """The verdict for one variant, with the evidence and the suggested remedy."""

    endpoint: Endpoint
    variant: Variant
    verdict: Verdict
    window: MetricWindow | None
    cost: CostEstimate
    remedy: Remedy
    evidence: str
    #: Fraction of the requested lookback the endpoint actually existed for.
    window_coverage: float = 1.0

    @property
    def wasted_monthly_usd(self) -> float:
        """Estimated monthly waste, or zero when none was established."""
        return self.cost.wasted_monthly_usd or 0.0

    @property
    def invocations_per_instance_hour(self) -> float | None:
        """Observed traffic density, or None when it cannot be computed."""
        if self.window is None or self.variant.instance_count <= 0:
            return None
        denominator = self.window.hours * self.variant.instance_count
        if denominator <= 0:
            return None
        return self.window.observed / denominator


@dataclass(frozen=True)
class ScanResult:
    """Everything one scan found."""

    findings: tuple[Finding, ...]
    regions: tuple[str, ...]
    lookback_days: int
    started_at: dt.datetime
    prices_published: str
    #: Regions that could not be scanned, with the reason.
    errors: tuple[tuple[str, str], ...] = ()

    @property
    def total_wasted_monthly_usd(self) -> float:
        """Estimated monthly waste across every finding."""
        return sum(finding.wasted_monthly_usd for finding in self.findings)

    @property
    def total_monthly_usd(self) -> float:
        """Estimated monthly cost across every finding with a known price."""
        return sum(
            finding.cost.monthly_usd or 0.0 for finding in self.findings if finding.cost.is_known
        )

    @property
    def unquantified_waste(self) -> tuple[Finding, ...]:
        """Wasteful findings whose waste could not be put in dollars.

        Tracked so a total can say it is incomplete. A total that silently drops them reads
        as "this is all the waste there is", which is a stronger claim than the data
        supports.
        """
        return tuple(
            finding
            for finding in self.findings
            if finding.verdict.is_waste and finding.cost.wasted_monthly_usd is None
        )

    def with_verdict(self, verdict: Verdict) -> tuple[Finding, ...]:
        """Every finding carrying `verdict`."""
        return tuple(finding for finding in self.findings if finding.verdict is verdict)
