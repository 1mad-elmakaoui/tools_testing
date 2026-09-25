"""Turn per-shard traffic into utilisation and skew statistics.

Pure. Every function here takes numbers and returns numbers, so the interesting cases —
one hot shard, an evenly loaded stream, a split that happened mid-window — can be written
down directly in a test without a stream to point at.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from kinesis_skew.catalog import AwsFacts, Thresholds
from kinesis_skew.models import ShardTraffic, ShardUtilisation, SkewStatistics


def utilisation(
    traffic: Sequence[ShardTraffic], aws: AwsFacts, thresholds: Thresholds
) -> tuple[ShardUtilisation, ...]:
    """Score each shard's busiest minute against the documented per-shard write limits.

    The busiest minute rather than the average, because throttling is a per-second event: a
    shard pinned for two minutes an hour averages out to nearly nothing over a day, and
    averaging is how a hot shard hides.

    A shard that existed for less than `minimum_window_coverage` of the window is scored and
    displayed but kept out of the statistics. Otherwise a split halfway through the window
    would make both halves look quiet next to shards that were open throughout, which reads
    as skew that is not there.
    """
    with_metrics = [shard for shard in traffic if shard.has_metrics]
    total_bytes = sum(shard.total_bytes for shard in with_metrics)

    scored: list[ShardUtilisation] = []
    for shard in traffic:
        reason = _exclusion_reason(shard, thresholds)
        scored.append(
            ShardUtilisation(
                shard_id=shard.shard_id,
                by_bytes=shard.peak_bytes_per_minute / aws.shard_write_bytes_per_minute,
                by_records=shard.peak_records_per_minute / aws.shard_write_records_per_minute,
                throttled_records=shard.throttled_records,
                share_of_bytes=(shard.total_bytes / total_bytes) if total_bytes > 0 else 0.0,
                is_open=shard.is_open,
                counted_in_statistics=reason is None,
                excluded_reason=reason,
            )
        )
    return tuple(scored)


def _exclusion_reason(shard: ShardTraffic, thresholds: Thresholds) -> str | None:
    if not shard.has_metrics:
        return "CloudWatch returned no data for this shard"
    if shard.coverage < thresholds.minimum_window_coverage:
        return (
            f"open for only {shard.coverage:.0%} of the window, too little to rank against "
            f"shards that were open throughout"
        )
    return None


def statistics(scored: Sequence[ShardUtilisation], thresholds: Thresholds) -> SkewStatistics | None:
    """Summarise how unevenly traffic is spread, or None when the question does not apply.

    None means there are too few comparable shards to say anything. A one-shard stream
    cannot be skewed: every key lands on the same shard whatever its cardinality, so the
    ratios would all come out at 1.0 and read as perfectly even rather than as not a
    question worth asking.
    """
    counted = [shard for shard in scored if shard.counted_in_statistics]
    if len(counted) < thresholds.minimum_shards_for_skew_statistics:
        return None

    shares = [shard.share_of_bytes for shard in counted]
    utilisations = [shard.utilisation for shard in counted]
    total_share = sum(shares)
    hottest = max(counted, key=lambda shard: shard.share_of_bytes)

    return SkewStatistics(
        shard_count=len(counted),
        max_to_mean_ratio=_max_to_mean(shares),
        coefficient_of_variation=_coefficient_of_variation(shares),
        hottest_share=(hottest.share_of_bytes / total_share) if total_share > 0 else 0.0,
        gini=gini(shares),
        hottest_shard_id=hottest.shard_id,
        mean_utilisation=sum(utilisations) / len(utilisations),
        max_utilisation=max(utilisations),
    )


def _max_to_mean(values: Sequence[float]) -> float:
    """How many times the average the busiest shard carries.

    Perfectly even traffic gives 1.0. So does no traffic at all, which is why the caller
    checks for an idle stream before reading anything into this.
    """
    mean = sum(values) / len(values) if values else 0.0
    if mean <= 0:
        return 1.0
    return max(values) / mean


def _coefficient_of_variation(values: Sequence[float]) -> float:
    """Standard deviation over the mean, so the spread is comparable between streams."""
    if not values:
        return 0.0
    mean = sum(values) / len(values)
    if mean <= 0:
        return 0.0
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return math.sqrt(variance) / mean


def gini(values: Sequence[float]) -> float:
    """The Gini coefficient of `values`, from 0 (perfectly even) to 1 (all on one).

    Hash-distributed keys over many shards land around 0.1 to 0.2 in practice; a single hot
    key pushes it towards 1. Unlike the max-to-mean ratio it uses every shard, so it
    notices a handful of warm shards rather than only the hottest one.

    Negative values are not meaningful here and the caller never produces them, but they
    would silently break the formula, so they are rejected rather than tolerated.
    """
    if not values:
        return 0.0
    if any(value < 0 for value in values):
        raise ValueError("gini is not defined for negative values")
    total = sum(values)
    if total <= 0:
        return 0.0
    ordered = sorted(values)
    count = len(ordered)
    weighted = sum((index + 1) * value for index, value in enumerate(ordered))
    coefficient = (2 * weighted) / (count * total) - (count + 1) / count
    # Perfectly even values land a hair below zero in floating point, and "Gini -0.00" reads
    # as though the measure had gone wrong. It cannot be negative for non-negative inputs.
    return max(0.0, coefficient)
