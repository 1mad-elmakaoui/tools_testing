"""Plain data types shared by the rules, the engine and the renderers.

Everything here is a frozen dataclass or an enum with no I/O and no AWS dependency, so the
decision logic can be unit-tested without touching the network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Option(StrEnum):
    """The four SageMaker inference options this tool chooses between."""

    REAL_TIME = "real_time"
    SERVERLESS = "serverless"
    ASYNC = "async"
    BATCH_TRANSFORM = "batch_transform"


class TrafficPattern(StrEnum):
    """How requests arrive over time."""

    STEADY = "steady"
    BURSTY_IDLE = "bursty-idle"
    SCHEDULED_BATCH = "scheduled-batch"


@dataclass(frozen=True)
class Workload:
    """The workload being placed. All sizes are megabytes and all times are seconds."""

    payload_mb: float
    response_mb: float
    processing_seconds: float
    traffic: TrafficPattern = TrafficPattern.STEADY
    latency_p99_ms: float | None = None
    zero_idle_cost: bool = False
    immediate_response: bool = False
    needs_notification: bool = False
    gpu_required: bool = False
    model_count: int = 1

    def __post_init__(self) -> None:
        for name in ("payload_mb", "response_mb", "processing_seconds"):
            value = float(getattr(self, name))
            if value < 0:
                raise ValueError(f"{name} must not be negative, got {value}")
        if self.latency_p99_ms is not None and self.latency_p99_ms <= 0:
            raise ValueError(f"latency_p99_ms must be positive, got {self.latency_p99_ms}")
        if self.model_count < 1:
            raise ValueError(f"model_count must be at least 1, got {self.model_count}")

    @property
    def requires_inline_response(self) -> bool:
        """Whether the caller must receive the prediction in the same request.

        A p99 latency target implies this: an option that never answers the caller cannot
        meet a latency target at all.
        """
        return self.immediate_response or self.latency_p99_ms is not None


@dataclass(frozen=True)
class Elimination:
    """One hard constraint that ruled one option out."""

    option: Option
    constraint_id: str
    requirement: str
    message: str
    source: str
    limit: str | None = None
    actual: str | None = None


@dataclass(frozen=True)
class Preference:
    """One soft preference that pushed an option up or down the ranking."""

    option: Option
    rule_id: str
    weight: float
    message: str


@dataclass(frozen=True)
class RankedOption:
    """A surviving option with its score and the reasons behind it."""

    option: Option
    score: float
    reasons: tuple[Preference, ...] = ()


@dataclass(frozen=True)
class Advice:
    """A secondary deployment pattern worth considering alongside the recommendation."""

    advice_id: str
    title: str
    message: str
    source: str


@dataclass(frozen=True)
class Conflict:
    """A requirement that eliminated options, reported when nothing survives."""

    constraint_id: str
    requirement: str
    eliminated: tuple[Option, ...]


@dataclass(frozen=True)
class Recommendation:
    """The full result: what to use, why, and why not everything else."""

    workload: Workload
    recommended: Option | None
    ranked: tuple[RankedOption, ...] = ()
    eliminations: tuple[Elimination, ...] = ()
    advice: tuple[Advice, ...] = ()
    conflicts: tuple[Conflict, ...] = ()
    limits_last_verified: str = ""
    _by_option: dict[Option, tuple[Elimination, ...]] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        grouped: dict[Option, list[Elimination]] = {}
        for elimination in self.eliminations:
            grouped.setdefault(elimination.option, []).append(elimination)
        object.__setattr__(
            self, "_by_option", {key: tuple(value) for key, value in grouped.items()}
        )

    @property
    def resolved(self) -> bool:
        """Whether an option satisfies every hard constraint."""
        return self.recommended is not None

    def eliminations_for(self, option: Option) -> tuple[Elimination, ...]:
        """Every hard constraint that ruled `option` out, in evaluation order."""
        return self._by_option.get(option, ())

    def score_for(self, option: Option) -> float | None:
        """The soft-preference score for a surviving `option`, or None if eliminated."""
        for ranked in self.ranked:
            if ranked.option is option:
                return ranked.score
        return None
