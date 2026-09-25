"""The shapes this tool reasons about.

All frozen, all plain data. Nothing here calls AWS or knows that CloudWatch exists, so the
whole diagnosis can be reconstructed in a test from numbers alone.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum


class CapacityMode(StrEnum):
    """How the stream's capacity is managed."""

    PROVISIONED = "PROVISIONED"
    ON_DEMAND = "ON_DEMAND"

    @property
    def shard_count_is_yours_to_set(self) -> bool:
        """Whether adding shards is even an option the reader has.

        On-demand streams have no shard count to set, so any advice that amounts to
        "add shards" is wrong for them however the traffic looks.
        """
        return self is CapacityMode.PROVISIONED


class Verdict(StrEnum):
    """What the numbers say is wrong, if anything.

    ``BURSTY`` is the honest answer to a case the metrics cannot resolve: throttling, with
    traffic spread evenly and every shard's busiest minute well under its limit. AWS decides
    throttling per second and CloudWatch reports per minute, so the spikes doing the damage
    are inside a datapoint. Calling that ``capacity`` would name a cause the numbers do not
    show.
    """

    SKEW = "skew"
    CAPACITY = "capacity"
    MIXED = "mixed"
    BURSTY = "bursty"
    HEALTHY = "healthy"
    NO_SHARD_METRICS = "no_shard_metrics"
    NO_TRAFFIC = "no_traffic"

    @property
    def is_problem(self) -> bool:
        """Whether this verdict describes something wrong with the stream."""
        return self in {Verdict.SKEW, Verdict.CAPACITY, Verdict.MIXED, Verdict.BURSTY}


@dataclass(frozen=True)
class Shard:
    """One shard, as ListShards describes it."""

    shard_id: str
    #: Absent while the shard is open. Present once it has been split or merged away.
    ending_sequence_number: str | None = None
    parent_shard_id: str | None = None
    adjacent_parent_shard_id: str | None = None

    @property
    def is_open(self) -> bool:
        """Whether the shard can still accept writes.

        A closed shard has an ending sequence number. It keeps its data until the retention
        period expires, and it still holds the traffic it took before it closed, which is
        why the window has to include it.
        """
        return self.ending_sequence_number is None

    @property
    def was_resharded(self) -> bool:
        """Whether this shard came from a split or a merge."""
        return self.parent_shard_id is not None or self.adjacent_parent_shard_id is not None


@dataclass(frozen=True)
class ShardTraffic:
    """What one shard carried over the window, and how close to its limit it came.

    ``peak_*`` are the busiest single minute, ``total_*`` the whole window. The peak is what
    the diagnosis uses: throttling is decided per second, and a shard pinned for two minutes
    an hour averages out to almost nothing over a day.
    """

    shard_id: str
    total_bytes: float
    total_records: float
    throttled_records: float
    peak_bytes_per_minute: float
    peak_records_per_minute: float
    #: Minutes of the window for which CloudWatch returned any data at all.
    observed_minutes: int
    window_minutes: int
    is_open: bool = True

    @property
    def has_metrics(self) -> bool:
        """Whether CloudWatch had anything to say about this shard."""
        return self.observed_minutes > 0

    @property
    def coverage(self) -> float:
        """Fraction of the window this shard reported for."""
        if self.window_minutes <= 0:
            return 0.0
        return min(1.0, self.observed_minutes / self.window_minutes)

    @property
    def was_throttled(self) -> bool:
        """Whether any write to this shard was rejected for throughput."""
        return self.throttled_records > 0


@dataclass(frozen=True)
class ShardUtilisation:
    """A shard's busiest minute expressed as a fraction of its documented limits."""

    shard_id: str
    by_bytes: float
    by_records: float
    throttled_records: float
    share_of_bytes: float
    is_open: bool
    counted_in_statistics: bool
    #: Why it was left out, when it was.
    excluded_reason: str | None = None

    @property
    def utilisation(self) -> float:
        """The binding limit: whichever of the two the shard is closer to."""
        return max(self.by_bytes, self.by_records)

    @property
    def binding_dimension(self) -> str:
        """Which documented limit this shard is closest to."""
        return "records" if self.by_records > self.by_bytes else "bytes"


@dataclass(frozen=True)
class SkewStatistics:
    """How unevenly the traffic is spread across the shards that count."""

    shard_count: int
    max_to_mean_ratio: float
    coefficient_of_variation: float
    hottest_share: float
    gini: float
    hottest_shard_id: str
    mean_utilisation: float
    max_utilisation: float


@dataclass(frozen=True)
class KeySample:
    """One partition key seen while sampling, and how often."""

    partition_key: str
    count: int
    share: float


@dataclass(frozen=True)
class Diagnosis:
    """The verdict, in plain language, with what to do about it."""

    verdict: Verdict
    headline: str
    evidence: tuple[str, ...]
    remedy: str


@dataclass(frozen=True)
class StreamReport:
    """Everything this tool worked out about one stream."""

    stream_name: str
    region: str
    capacity_mode: CapacityMode
    open_shard_count: int
    shard_level_metrics: tuple[str, ...]
    window_start: dt.datetime
    window_end: dt.datetime
    utilisation: tuple[ShardUtilisation, ...]
    statistics: SkewStatistics | None
    diagnosis: Diagnosis
    total_throttled_records: float
    resharded_during_window: bool = False
    samples: tuple[KeySample, ...] = ()
    sampled_shard_id: str | None = None

    @property
    def window_minutes(self) -> int:
        """Length of the window in whole minutes."""
        return int((self.window_end - self.window_start).total_seconds() // 60)

    @property
    def missing_metrics(self) -> tuple[str, ...]:
        """Shard-level metrics this tool needs that the stream is not emitting."""
        needed = ("IncomingBytes", "IncomingRecords", "WriteProvisionedThroughputExceeded")
        if "ALL" in self.shard_level_metrics:
            return ()
        return tuple(name for name in needed if name not in self.shard_level_metrics)


@dataclass(frozen=True)
class ScanResult:
    """Every stream looked at in one run."""

    reports: tuple[StreamReport, ...]
    region: str
    lookback_hours: float
    started_at: dt.datetime
    #: Streams that could not be read, with the reason.
    errors: tuple[tuple[str, str], ...] = ()

    @property
    def problems(self) -> tuple[StreamReport, ...]:
        """Reports describing something wrong."""
        return tuple(report for report in self.reports if report.diagnosis.verdict.is_problem)

    def with_verdict(self, verdict: Verdict) -> tuple[StreamReport, ...]:
        """Every report carrying `verdict`."""
        return tuple(report for report in self.reports if report.diagnosis.verdict is verdict)
