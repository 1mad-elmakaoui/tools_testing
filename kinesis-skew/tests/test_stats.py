"""The skew statistics, as arithmetic.

Every case is written as numbers rather than as a stream, because that is the point of
keeping this layer pure.
"""

from __future__ import annotations

import pytest

from kinesis_skew.catalog import AwsFacts, Thresholds
from kinesis_skew.stats import gini, statistics, utilisation
from tests.conftest import WINDOW_MINUTES, make_traffic

# ------------------------------------------------------------------------------ gini


def test_perfectly_even_traffic_has_a_gini_of_zero() -> None:
    assert gini([25.0, 25.0, 25.0, 25.0]) == 0.0


def test_everything_on_one_shard_approaches_one() -> None:
    """The maximum for n shards is (n-1)/n, reached when one shard has all of it."""
    assert gini([0.0, 0.0, 0.0, 100.0]) == pytest.approx(0.75)
    assert gini([0.0] * 99 + [100.0]) == pytest.approx(0.99)


def test_gini_rises_as_traffic_concentrates() -> None:
    even = gini([10.0, 10.0, 10.0, 10.0])
    lumpy = gini([5.0, 8.0, 12.0, 15.0])
    hot = gini([2.0, 2.0, 2.0, 34.0])
    assert even < lumpy < hot


def test_gini_ignores_scale() -> None:
    """Doubling every shard's traffic changes nothing about how evenly it is spread."""
    assert gini([1.0, 2.0, 7.0]) == pytest.approx(gini([10.0, 20.0, 70.0]))


def test_an_idle_stream_is_not_reported_as_skewed() -> None:
    assert gini([0.0, 0.0, 0.0]) == 0.0
    assert gini([]) == 0.0


def test_negative_values_are_rejected_rather_than_silently_wrong() -> None:
    with pytest.raises(ValueError, match="negative"):
        gini([1.0, -1.0])


# ----------------------------------------------------------------------- utilisation


def test_utilisation_scores_the_busiest_minute_against_both_limits(
    aws: AwsFacts, thresholds: Thresholds
) -> None:
    scored = utilisation([make_traffic("a", peak_fraction=0.5)], aws, thresholds)
    assert scored[0].by_bytes == pytest.approx(0.5)
    assert scored[0].utilisation == pytest.approx(0.5)


def test_the_binding_limit_is_whichever_is_closer(aws: AwsFacts, thresholds: Thresholds) -> None:
    """Small records exhaust the per-second record limit long before the byte limit."""
    records_bound = make_traffic("a", peak_fraction=0.05, records_peak_fraction=0.9)
    scored = utilisation([records_bound], aws, thresholds)
    assert scored[0].binding_dimension == "records"
    assert scored[0].utilisation == pytest.approx(0.9)


def test_shares_are_reported_across_every_shard_with_data(
    aws: AwsFacts, thresholds: Thresholds
) -> None:
    scored = utilisation(
        [make_traffic("hot", peak_fraction=0.9), make_traffic("cold", peak_fraction=0.1)],
        aws,
        thresholds,
    )
    assert scored[0].share_of_bytes == pytest.approx(0.9)
    assert sum(shard.share_of_bytes for shard in scored) == pytest.approx(1.0)


def test_a_shard_with_no_metrics_is_kept_out_of_the_statistics(
    aws: AwsFacts, thresholds: Thresholds
) -> None:
    scored = utilisation([make_traffic("a", observed_minutes=0)], aws, thresholds)
    assert scored[0].counted_in_statistics is False
    assert scored[0].excluded_reason is not None
    assert "no data" in scored[0].excluded_reason


def test_a_shard_that_existed_briefly_is_shown_but_not_ranked(
    aws: AwsFacts, thresholds: Thresholds
) -> None:
    """A split mid-window would otherwise make both halves look quiet, and fake skew."""
    newborn = make_traffic("child", observed_minutes=WINDOW_MINUTES // 10)
    scored = utilisation([newborn], aws, thresholds)
    assert scored[0].counted_in_statistics is False
    assert scored[0].excluded_reason is not None
    assert "10%" in scored[0].excluded_reason


# ------------------------------------------------------------------------ statistics


def test_statistics_describe_an_even_stream(aws: AwsFacts, thresholds: Thresholds) -> None:
    scored = utilisation(
        [make_traffic(f"s{i}", peak_fraction=0.4) for i in range(6)], aws, thresholds
    )
    stats = statistics(scored, thresholds)
    assert stats is not None
    assert stats.shard_count == 6
    assert stats.max_to_mean_ratio == pytest.approx(1.0)
    assert stats.gini == pytest.approx(0.0)
    assert stats.coefficient_of_variation == pytest.approx(0.0)
    assert stats.mean_utilisation == pytest.approx(0.4)


def test_statistics_find_the_hottest_shard(aws: AwsFacts, thresholds: Thresholds) -> None:
    shards = [make_traffic(f"cold{i}", peak_fraction=0.02) for i in range(5)]
    shards.append(make_traffic("hot", peak_fraction=0.95))
    stats = statistics(utilisation(shards, aws, thresholds), thresholds)
    assert stats is not None
    assert stats.hottest_shard_id == "hot"
    assert stats.max_to_mean_ratio > 3.0
    assert stats.gini > 0.4
    assert stats.max_utilisation == pytest.approx(0.95)


def test_a_single_shard_stream_has_no_skew_statistics(
    aws: AwsFacts, thresholds: Thresholds
) -> None:
    """Not zero skew: the question does not apply, and 1.0 ratios would read as even."""
    scored = utilisation([make_traffic("only", peak_fraction=0.9)], aws, thresholds)
    assert statistics(scored, thresholds) is None


def test_statistics_cover_only_the_comparable_shards(aws: AwsFacts, thresholds: Thresholds) -> None:
    shards = [
        make_traffic("old-a", peak_fraction=0.3),
        make_traffic("old-b", peak_fraction=0.3),
        make_traffic("child", peak_fraction=0.3, observed_minutes=60),
    ]
    stats = statistics(utilisation(shards, aws, thresholds), thresholds)
    assert stats is not None
    assert stats.shard_count == 2


def test_gini_never_reports_a_negative_zero() -> None:
    """Perfectly even values land a hair below zero in floating point.

    "Gini -0.00" in a report reads as though the measure had gone wrong, and the
    coefficient cannot be negative for non-negative inputs.
    """
    for count in range(2, 12):
        value = gini([1 / count] * count)
        assert value >= 0.0
        # What actually reaches the reader is the formatted figure.
        assert f"{value:.2f}" == "0.00"
