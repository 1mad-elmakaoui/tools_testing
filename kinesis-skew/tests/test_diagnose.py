"""The verdict, case by case.

The tool exists to tell two things apart that look identical from stream-level metrics, so
the pair that matters most is `test_a_hot_shard_...` and `test_an_evenly_loaded_...`: same
throttling, opposite answers, opposite advice.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from kinesis_skew.catalog import AwsFacts, Thresholds
from kinesis_skew.diagnose import diagnose
from kinesis_skew.models import CapacityMode, Diagnosis, ShardTraffic, Verdict
from kinesis_skew.stats import statistics, utilisation
from tests.conftest import WINDOW_MINUTES, make_traffic


def judge(
    traffic: Sequence[ShardTraffic],
    aws: AwsFacts,
    thresholds: Thresholds,
    *,
    mode: CapacityMode = CapacityMode.PROVISIONED,
    missing: Sequence[str] = (),
) -> Diagnosis:
    """Run the real pipeline: score, summarise, decide."""
    scored = utilisation(traffic, aws, thresholds)
    return diagnose(
        capacity_mode=mode,
        scored=scored,
        stats=statistics(scored, thresholds),
        missing_metrics=missing,
        aws=aws,
        thresholds=thresholds,
    )


def _evenly_loaded(count: int, peak: float, throttled: float = 0.0) -> list[ShardTraffic]:
    return [
        make_traffic(f"shard-{i:03d}", peak_fraction=peak, throttled=throttled / count)
        for i in range(count)
    ]


# ------------------------------------------------------------------- the central pair


def test_a_hot_shard_with_headroom_elsewhere_is_skew(aws: AwsFacts, thresholds: Thresholds) -> None:
    traffic = [make_traffic(f"cold-{i}", peak_fraction=0.02) for i in range(7)]
    traffic.append(make_traffic("hot", peak_fraction=0.99, throttled=41_000))

    result = judge(traffic, aws, thresholds)

    assert result.verdict is Verdict.SKEW
    assert "will not help" in result.remedy
    assert "partition key with more distinct values" in result.remedy
    assert any("hot" in line for line in result.evidence)


def test_an_evenly_loaded_stream_at_its_limit_is_capacity(
    aws: AwsFacts, thresholds: Thresholds
) -> None:
    """Identical throttling to the case above, opposite cause and opposite advice."""
    result = judge(_evenly_loaded(8, peak=0.95, throttled=41_000), aws, thresholds)

    assert result.verdict is Verdict.CAPACITY
    assert "add shards" in result.remedy.lower()
    assert "will not help" not in result.remedy


# ------------------------------------------------------------------- the other verdicts


def test_no_throttling_is_healthy(aws: AwsFacts, thresholds: Thresholds) -> None:
    result = judge(_evenly_loaded(4, peak=0.3), aws, thresholds)
    assert result.verdict is Verdict.HEALTHY
    assert result.verdict.is_problem is False


def test_uneven_but_unthrottled_traffic_is_healthy_and_says_so(
    aws: AwsFacts, thresholds: Thresholds
) -> None:
    """Skew only matters once it costs something. Until then it is worth watching."""
    traffic = [make_traffic(f"cold-{i}", peak_fraction=0.01) for i in range(7)]
    traffic.append(make_traffic("warm", peak_fraction=0.4))

    result = judge(traffic, aws, thresholds)

    assert result.verdict is Verdict.HEALTHY
    assert any("watching" in line for line in result.evidence)


def test_a_hot_shard_on_a_busy_stream_is_mixed(aws: AwsFacts, thresholds: Thresholds) -> None:
    """One key is hot and the rest of the stream is nearly full: fixing either alone fails."""
    traffic = [make_traffic(f"busy-{i}", peak_fraction=0.72) for i in range(6)]
    traffic.append(make_traffic("hot", peak_fraction=1.0, throttled=9_000, total_bytes=9e11))

    result = judge(traffic, aws, thresholds)

    assert result.verdict is Verdict.MIXED
    assert "add shards" in result.remedy
    assert "distinct values" in result.remedy


def test_throttling_with_low_even_utilisation_is_reported_as_bursts(
    aws: AwsFacts, thresholds: Thresholds
) -> None:
    """The metrics are per minute and AWS throttles per second, so this is what is left.

    Calling it capacity would name a cause these numbers do not show.
    """
    result = judge(_evenly_loaded(6, peak=0.2, throttled=1_200), aws, thresholds)

    assert result.verdict is Verdict.BURSTY
    assert "per second" in " ".join(result.evidence)
    assert "PutRecords" in result.remedy


def test_missing_shard_metrics_stop_the_analysis_and_say_how_to_fix_it(
    aws: AwsFacts, thresholds: Thresholds
) -> None:
    result = judge(
        _evenly_loaded(4, peak=0.5),
        aws,
        thresholds,
        missing=["IncomingBytes", "WriteProvisionedThroughputExceeded"],
    )

    assert result.verdict is Verdict.NO_SHARD_METRICS
    assert "aws kinesis enable-enhanced-monitoring" in result.remedy
    # Spelled as the API's enum, which is what the CLI accepts.
    assert "--shard-level-metrics IncomingBytes WriteProvisionedThroughputExceeded" in result.remedy
    assert "will not run that for you" in result.remedy


def test_an_idle_stream_is_not_called_healthy_or_skewed(
    aws: AwsFacts, thresholds: Thresholds
) -> None:
    traffic = [make_traffic(f"s{i}", observed_minutes=0) for i in range(3)]
    result = judge(traffic, aws, thresholds)
    assert result.verdict is Verdict.NO_TRAFFIC
    assert result.verdict.is_problem is False


def test_a_throttled_single_shard_stream_is_capacity_not_skew(
    aws: AwsFacts, thresholds: Thresholds
) -> None:
    """With one shard every key lands on it, so no key could spread the load."""
    result = judge([make_traffic("only", peak_fraction=1.0, throttled=5_000)], aws, thresholds)

    assert result.verdict is Verdict.CAPACITY
    assert "no key that would spread" in result.remedy


# --------------------------------------------------------------------------- resharding


def test_a_split_mid_window_does_not_look_like_skew(aws: AwsFacts, thresholds: Thresholds) -> None:
    """The closed parent and its two young children each hold part of the window's traffic.

    Ranked naively, the parent looks like a hot shard next to two quiet ones. It is not: it
    is the same traffic, before and after the split.
    """
    parent = make_traffic(
        "parent",
        peak_fraction=0.5,
        observed_minutes=WINDOW_MINUTES // 4,
        is_open=False,
        throttled=0.0,
    )
    children = [
        make_traffic(f"child-{i}", peak_fraction=0.25, observed_minutes=WINDOW_MINUTES // 4)
        for i in range(2)
    ]
    steady = [make_traffic(f"steady-{i}", peak_fraction=0.3) for i in range(3)]

    scored = utilisation([parent, *children, *steady], aws, thresholds)
    stats = statistics(scored, thresholds)

    assert stats is not None
    # Only the three shards open across the whole window are ranked.
    assert stats.shard_count == 3
    assert stats.max_to_mean_ratio == pytest.approx(1.0)
    excluded = {shard.shard_id for shard in scored if not shard.counted_in_statistics}
    assert excluded == {"parent", "child-0", "child-1"}


# ------------------------------------------------------------------------- on-demand


def test_an_on_demand_stream_is_never_told_to_add_shards(
    aws: AwsFacts, thresholds: Thresholds
) -> None:
    """There is no shard count to set, so that advice would be unfollowable."""
    result = judge(
        _evenly_loaded(8, peak=0.95, throttled=41_000),
        aws,
        thresholds,
        mode=CapacityMode.ON_DEMAND,
    )

    assert result.verdict is Verdict.CAPACITY
    assert "add shards" not in result.remedy.lower()
    assert "no shard count to change" in result.remedy
    assert "15 minutes" in result.remedy


def test_skew_on_an_on_demand_stream_still_blames_the_key(
    aws: AwsFacts, thresholds: Thresholds
) -> None:
    """Auto-scaling cannot save a stream from one hot key: it still hashes to one shard."""
    traffic = [make_traffic(f"cold-{i}", peak_fraction=0.02) for i in range(7)]
    traffic.append(make_traffic("hot", peak_fraction=0.99, throttled=41_000))

    result = judge(traffic, aws, thresholds, mode=CapacityMode.ON_DEMAND)

    assert result.verdict is Verdict.SKEW
    assert "scales itself" in result.remedy
    assert "distinct values" in result.remedy


@pytest.mark.parametrize("mode", list(CapacityMode))
def test_every_verdict_carries_a_remedy_and_evidence(
    aws: AwsFacts, thresholds: Thresholds, mode: CapacityMode
) -> None:
    """A verdict with nothing to do about it would be a worse answer than none."""
    cases = [
        _evenly_loaded(4, peak=0.3),
        _evenly_loaded(8, peak=0.95, throttled=41_000),
        _evenly_loaded(6, peak=0.2, throttled=1_200),
        [make_traffic("only", peak_fraction=1.0, throttled=5_000)],
        [make_traffic(f"s{i}", observed_minutes=0) for i in range(3)],
    ]
    for traffic in cases:
        result = judge(traffic, aws, thresholds, mode=mode)
        assert result.headline
        assert result.remedy
        assert result.evidence


def test_the_missing_metrics_remedy_claims_only_what_was_checked(
    aws: AwsFacts, thresholds: Thresholds
) -> None:
    """The tool never reads stream-level metrics, so it must not report what they show."""
    result = judge(_evenly_loaded(4, peak=0.5), aws, thresholds, missing=["IncomingBytes"])
    joined = " ".join(result.evidence).lower()
    assert "throttling happened" not in joined
    assert "stream-level metrics show" not in joined
