"""Decide what the numbers mean, in words someone can act on.

Pure, and deliberately the only place a verdict is chosen. It takes per-shard numbers and
returns a verdict, the evidence behind it and what to do — so every case the tool can
reach is a few lines in a test rather than an AWS account in a particular state.

The distinction the whole tool exists for: throttling with traffic piled onto one shard is
a partition key problem, and adding shards will not fix it, because the same key still
hashes to one shard. Throttling with traffic spread evenly is a size problem, and adding
shards is exactly the fix. They are indistinguishable in the stream-level metrics and
obvious in the shard-level ones.
"""

from __future__ import annotations

from collections.abc import Sequence

from kinesis_skew.catalog import AwsFacts, Thresholds
from kinesis_skew.models import (
    CapacityMode,
    Diagnosis,
    ShardUtilisation,
    SkewStatistics,
    Verdict,
)

_HIGHER_CARDINALITY = (
    "use a partition key with more distinct values, or append a suffix to spread one busy "
    "key across shards and reassemble downstream"
)


def diagnose(
    *,
    capacity_mode: CapacityMode,
    scored: Sequence[ShardUtilisation],
    stats: SkewStatistics | None,
    missing_metrics: Sequence[str],
    aws: AwsFacts,
    thresholds: Thresholds,
) -> Diagnosis:
    """Work out what is wrong with one stream, if anything."""
    if missing_metrics:
        return _no_shard_metrics(missing_metrics)

    throttled = sum(shard.throttled_records for shard in scored)
    carried_traffic = any(shard.utilisation > 0 for shard in scored)

    # Nothing written and nothing rejected. A stream that is rejecting every write reports
    # almost no incoming bytes too, so the throttle count has to be part of this test.
    if not carried_traffic and not throttled:
        return _no_traffic()
    if not throttled:
        return _healthy(stats, thresholds)

    if stats is None:
        return _single_shard_capacity(capacity_mode, throttled)

    concentrated = (
        stats.max_to_mean_ratio >= thresholds.skew_max_to_mean_ratio
        and stats.gini >= thresholds.skew_gini
    )
    has_headroom = stats.mean_utilisation <= thresholds.skew_headroom_utilisation
    near_capacity = stats.mean_utilisation >= thresholds.capacity_utilisation

    if concentrated and has_headroom:
        return _skew(capacity_mode, stats, throttled, thresholds)
    if concentrated:
        return _mixed(capacity_mode, stats, throttled)
    if near_capacity:
        return _capacity(capacity_mode, stats, throttled, aws)
    return _bursty(capacity_mode, stats, throttled, aws)


# --------------------------------------------------------------------------- the verdicts


def _no_shard_metrics(missing: Sequence[str]) -> Diagnosis:
    names = " ".join(missing)
    return Diagnosis(
        verdict=Verdict.NO_SHARD_METRICS,
        headline="Shard-level metrics are not enabled, so skew cannot be measured",
        evidence=(
            f"the stream is not emitting {', '.join(missing)} per shard",
            "without them there is no per-shard traffic to compare, and whether one shard "
            "is carrying the stream is exactly the question this tool answers",
        ),
        remedy=(
            "enable enhanced monitoring, wait for a representative window, then run this "
            "again:\n"
            f"    aws kinesis enable-enhanced-monitoring --stream-name <name> "
            f"--shard-level-metrics {names}\n"
            "  This tool will not run that for you: it only reads. Shard-level metrics are "
            "billed as CloudWatch custom metrics, per shard per metric."
        ),
    )


def _no_traffic() -> Diagnosis:
    return Diagnosis(
        verdict=Verdict.NO_TRAFFIC,
        headline="No writes recorded in the window",
        evidence=("shard-level metrics are enabled but reported no incoming data",),
        remedy=(
            "nothing to judge. If you expected traffic, check the window and that producers "
            "are writing to this stream"
        ),
    )


def _healthy(stats: SkewStatistics | None, thresholds: Thresholds) -> Diagnosis:
    evidence = ["no write throttling in the window"]
    if stats is not None:
        evidence.append(
            f"busiest shard at {stats.max_utilisation:.0%} of its write limit in its busiest "
            f"minute, average {stats.mean_utilisation:.0%}"
        )
        if stats.gini >= thresholds.skew_gini:
            evidence.append(
                f"traffic is uneven (Gini {stats.gini:.2f}) but nothing is being rejected, "
                f"so it is worth watching rather than fixing"
            )
    return Diagnosis(
        verdict=Verdict.HEALTHY,
        headline="No throttling, and no shard close enough to its limit to worry about",
        evidence=tuple(evidence),
        remedy="nothing to do",
    )


def _skew(
    capacity_mode: CapacityMode,
    stats: SkewStatistics,
    throttled: float,
    thresholds: Thresholds,
) -> Diagnosis:
    add_shards = (
        "adding shards will not help: the same partition key still hashes to one shard"
        if capacity_mode.shard_count_is_yours_to_set
        else "the stream scales itself, but that cannot help here: the same partition key "
        "still hashes to one shard however many there are"
    )
    return Diagnosis(
        verdict=Verdict.SKEW,
        headline="Partition key skew: one shard is hot while the stream has room to spare",
        evidence=(
            f"{throttled:,.0f} records rejected for throughput",
            f"{stats.hottest_shard_id} carries {stats.hottest_share:.0%} of the bytes across "
            f"{stats.shard_count} shards, {stats.max_to_mean_ratio:.1f}x the average",
            f"Gini {stats.gini:.2f}, coefficient of variation {stats.coefficient_of_variation:.2f}",
            f"the average shard peaks at {stats.mean_utilisation:.0%} of its write limit, "
            f"below the {thresholds.skew_headroom_utilisation:.0%} that would mean the "
            f"stream is simply full",
        ),
        remedy=f"{add_shards}. Instead, {_HIGHER_CARDINALITY}",
    )


def _mixed(capacity_mode: CapacityMode, stats: SkewStatistics, throttled: float) -> Diagnosis:
    scale = (
        "add shards to raise the ceiling"
        if capacity_mode.shard_count_is_yours_to_set
        else "the stream will scale itself as the new peak settles"
    )
    return Diagnosis(
        verdict=Verdict.MIXED,
        headline="Both: traffic is concentrated, and the rest of the stream is busy too",
        evidence=(
            f"{throttled:,.0f} records rejected for throughput",
            f"{stats.hottest_shard_id} carries {stats.hottest_share:.0%} of the bytes, "
            f"{stats.max_to_mean_ratio:.1f}x the average (Gini {stats.gini:.2f})",
            f"but the average shard also peaks at {stats.mean_utilisation:.0%} of its write "
            f"limit, so the shards that are not hot are not idle either",
        ),
        remedy=(
            f"{scale}, and {_HIGHER_CARDINALITY}. Fixing only the key leaves a stream that "
            f"is close to full; fixing only the size leaves the hot shard hot"
        ),
    )


def _capacity(
    capacity_mode: CapacityMode, stats: SkewStatistics, throttled: float, aws: AwsFacts
) -> Diagnosis:
    if capacity_mode.shard_count_is_yours_to_set:
        remedy = (
            "add shards. The traffic is spread evenly, so more shards means proportionally "
            "more capacity"
        )
        headline = "Under-provisioned: evenly loaded, and out of room"
    else:
        remedy = (
            f"there is no shard count to change. An on-demand stream carries up to "
            f"{aws.on_demand_peak_multiple:g}x its peak write throughput of the last 30 days "
            f"and throttles if traffic more than doubles inside "
            f"{aws.on_demand_scaling_window_minutes} minutes, so ramp up more gradually, "
            f"retry throttled writes with backoff, or ask AWS to raise the stream's limits"
        )
        headline = "Beyond what the stream has scaled to, with traffic spread evenly"
    return Diagnosis(
        verdict=Verdict.CAPACITY,
        headline=headline,
        evidence=(
            f"{throttled:,.0f} records rejected for throughput",
            f"traffic is spread evenly across {stats.shard_count} shards "
            f"(Gini {stats.gini:.2f}, busiest shard {stats.max_to_mean_ratio:.1f}x the "
            f"average)",
            f"the average shard peaks at {stats.mean_utilisation:.0%} of its write limit",
        ),
        remedy=remedy,
    )


def _bursty(
    capacity_mode: CapacityMode, stats: SkewStatistics, throttled: float, aws: AwsFacts
) -> Diagnosis:
    scale = (
        "adding shards raises the ceiling the spikes hit"
        if capacity_mode.shard_count_is_yours_to_set
        else "the stream scales on sustained throughput, not on spikes inside a minute"
    )
    return Diagnosis(
        verdict=Verdict.BURSTY,
        headline="Throttled in bursts too short for these metrics to show",
        evidence=(
            f"{throttled:,.0f} records rejected for throughput",
            f"traffic is spread evenly (Gini {stats.gini:.2f}) and the busiest shard only "
            f"reaches {stats.max_utilisation:.0%} of its write limit in its busiest minute",
            f"AWS decides throttling per second while these metrics are per "
            f"{aws.shard_metric_period_seconds} seconds, so the spikes causing it are inside "
            f"a single datapoint",
        ),
        remedy=(
            f"this is not skew and not a stream that is simply full. Even out the producer: "
            f"batch with PutRecords, spread retries with backoff, or stagger whatever fires "
            f"them all at once. Failing that, {scale}"
        ),
    )


def _single_shard_capacity(capacity_mode: CapacityMode, throttled: float) -> Diagnosis:
    """One shard, and it is being throttled.

    Skew is not a possible answer here: with one shard every key lands on it whatever its
    cardinality, so the only way out is more capacity.
    """
    remedy = (
        "add shards. With one shard there is no key that would spread the load"
        if capacity_mode.shard_count_is_yours_to_set
        else "the stream will add shards itself as the new peak settles; retry throttled "
        "writes with backoff until it does"
    )
    return Diagnosis(
        verdict=Verdict.CAPACITY,
        headline="Out of room on a stream too small to be skewed",
        evidence=(
            f"{throttled:,.0f} records rejected for throughput",
            "there are not enough comparable shards to talk about skew: every key lands on "
            "the same shard",
        ),
        remedy=remedy,
    )
