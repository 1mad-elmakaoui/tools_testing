"""Batching against the documented caps, and the bounded sampler.

Both are promises the README makes: that a scan stays inside CloudWatch's limits, and that
sampling cannot run away with a shard's read throughput.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from kinesis_skew.aws import CallRecorder, ReadOnlyClient, RetryPolicy
from kinesis_skew.catalog import Catalog
from kinesis_skew.metrics import build_queries, datapoints_per_query, shards_per_call
from kinesis_skew.sample import hottest_sampleable_shard, sample_keys
from tests.test_aws import FakeClient

NOW = dt.datetime(2026, 9, 24, 12, 0, tzinfo=dt.UTC)


def window(hours: float) -> tuple[dt.datetime, dt.datetime]:
    return NOW - dt.timedelta(hours=hours), NOW


# ------------------------------------------------------------------------- batching


@pytest.mark.parametrize(
    ("hours", "expected"),
    [(1, 60), (24, 1440), (168, 10080)],
)
def test_datapoints_follow_the_window_at_one_minute(hours: float, expected: int) -> None:
    start, end = window(hours)
    assert datapoints_per_query(start, end, 60) == expected


def test_a_zero_period_is_rejected() -> None:
    start, end = window(1)
    with pytest.raises(ValueError, match="positive"):
        datapoints_per_query(start, end, 0)


def test_batches_stay_inside_the_datapoint_cap(catalog: Catalog) -> None:
    """100,800 datapoints per call is the cap that binds, not the 500 queries."""
    start, end = window(24)
    per_shard = 3
    shards = shards_per_call(start, end, catalog.aws, catalog.limits)
    used = shards * per_shard * datapoints_per_query(start, end, 60)

    assert shards == 23
    assert used <= catalog.limits.max_datapoints_per_call
    assert shards * per_shard <= catalog.limits.max_metric_queries_per_call


def test_a_short_window_is_limited_by_the_query_count_instead(catalog: Catalog) -> None:
    start, end = window(1)
    shards = shards_per_call(start, end, catalog.aws, catalog.limits)
    assert shards * 3 <= catalog.limits.max_metric_queries_per_call


def test_at_least_one_shard_fits_however_long_the_window(catalog: Catalog) -> None:
    start, end = window(24 * 365)
    assert shards_per_call(start, end, catalog.aws, catalog.limits) >= 1


def test_queries_ask_for_sums_not_maximums(catalog: Catalog) -> None:
    """CloudWatch's Maximum on these metrics is the largest single put, not the busiest
    minute. Asking for it and calling the answer a peak understates a hot shard badly."""
    queries, index = build_queries("orders", ["shardId-000000000000"], catalog.aws)

    assert {query["MetricStat"]["Stat"] for query in queries} == {"Sum"}
    assert {entry.metric_name for entry in index.values()} == {
        "IncomingBytes",
        "IncomingRecords",
        "WriteProvisionedThroughputExceeded",
    }


def test_queries_carry_both_dimensions(catalog: Catalog) -> None:
    """Shard-level metrics need StreamName and ShardId; one alone gives the stream total."""
    queries, _ = build_queries("orders", ["shardId-000000000000"], catalog.aws)
    dimensions = queries[0]["MetricStat"]["Metric"]["Dimensions"]
    assert {d["Name"] for d in dimensions} == {"StreamName", "ShardId"}
    assert queries[0]["MetricStat"]["Period"] == catalog.aws.shard_metric_period_seconds
    assert queries[0]["MetricStat"]["Metric"]["Namespace"] == "AWS/Kinesis"


def test_query_ids_are_unique_across_shards(catalog: Catalog) -> None:
    """Duplicate ids would make CloudWatch reject the call, or silently merge results."""
    queries, index = build_queries("orders", [f"shardId-{i:012d}" for i in range(20)], catalog.aws)
    identifiers = [query["Id"] for query in queries]
    assert len(identifiers) == len(set(identifiers)) == len(index)
    assert all(str(identifier)[0].isalpha() for identifier in identifiers)


# ------------------------------------------------------------------------- sampling


class RecordingKinesis(FakeClient):
    """Returns an endless supply of records, and counts how often it was asked."""

    def __init__(self, per_call: int = 100) -> None:
        super().__init__()
        self.per_call = per_call

    def get_shard_iterator(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_shard_iterator", kwargs))
        return {"ShardIterator": "iter-0"}

    def get_records(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_records", kwargs))
        return {
            "Records": [{"PartitionKey": f"key-{i % 3}"} for i in range(self.per_call)],
            "NextShardIterator": "iter-next",
            "MillisBehindLatest": 5_000,
        }


def _sampler(fake: FakeClient) -> ReadOnlyClient:
    retry = RetryPolicy(max_retries=1, base_delay_seconds=0.0, sleep=lambda _: None)
    return ReadOnlyClient(
        fake,  # type: ignore[arg-type]
        "kinesis",
        frozenset({"GetShardIterator", "GetRecords"}),
        retry,
        CallRecorder(),
    )


def test_sampling_stops_at_the_call_budget(catalog: Catalog) -> None:
    """Sampling borrows throughput from real consumers, so it has to be bounded."""
    fake = RecordingKinesis(per_call=10)
    waits: list[float] = []

    sample_keys(
        _sampler(fake),
        "orders",
        "shardId-000000000000",
        NOW,
        catalog.aws,
        catalog.thresholds,
        sleep=waits.append,
    )

    calls = [name for name, _ in fake.calls if name == "get_records"]
    assert len(calls) == catalog.thresholds.sample_max_get_records_calls


def test_sampling_stops_at_the_record_budget(catalog: Catalog) -> None:
    fake = RecordingKinesis(per_call=catalog.thresholds.sample_max_records)
    sample_keys(
        _sampler(fake),
        "orders",
        "shardId-000000000000",
        NOW,
        catalog.aws,
        catalog.thresholds,
        sleep=lambda _: None,
    )
    assert len([name for name, _ in fake.calls if name == "get_records"]) == 1


def test_sampling_paces_itself_under_the_documented_rate(catalog: Catalog) -> None:
    """Five GetRecords per second per shard. Sampling must not be why something throttles."""
    fake = RecordingKinesis(per_call=10)
    waits: list[float] = []

    sample_keys(
        _sampler(fake),
        "orders",
        "shardId-000000000000",
        NOW,
        catalog.aws,
        catalog.thresholds,
        sleep=waits.append,
    )

    expected = 1.0 / catalog.aws.get_records_calls_per_second_per_shard
    assert waits and all(pause == pytest.approx(expected) for pause in waits)


def test_sampling_ranks_keys_by_share(catalog: Catalog) -> None:
    fake = RecordingKinesis(per_call=9)
    samples = sample_keys(
        _sampler(fake),
        "orders",
        "shardId-000000000000",
        NOW,
        catalog.aws,
        catalog.thresholds,
        sleep=lambda _: None,
    )
    assert [entry.partition_key for entry in samples] == ["key-0", "key-1", "key-2"]
    assert sum(entry.share for entry in samples) == pytest.approx(1.0)


def test_a_shard_that_cannot_be_read_yields_no_sample(catalog: Catalog) -> None:
    """A failed sample must not cost the diagnosis that earned it."""
    fake = FakeClient({"get_shard_iterator": RuntimeError("AccessDeniedException")})
    assert (
        sample_keys(
            _sampler(fake),
            "orders",
            "shardId-000000000000",
            NOW,
            catalog.aws,
            catalog.thresholds,
            sleep=lambda _: None,
        )
        == ()
    )


def test_an_exhausted_shard_stops_early(catalog: Catalog) -> None:
    fake = FakeClient(
        {
            "get_shard_iterator": {"ShardIterator": "iter-0"},
            "get_records": {"Records": [], "NextShardIterator": "n", "MillisBehindLatest": 0},
        }
    )
    assert (
        sample_keys(
            _sampler(fake),
            "orders",
            "shardId-000000000000",
            NOW,
            catalog.aws,
            catalog.thresholds,
            sleep=lambda _: None,
        )
        == ()
    )
    assert len([name for name, _ in fake.calls if name == "get_records"]) == 1


def test_the_busiest_open_shard_is_the_one_sampled() -> None:
    shares = {"a": 0.1, "b": 0.7, "c": 0.2}
    assert hottest_sampleable_shard(["a", "b", "c"], shares) == "b"
    # A closed shard is not a candidate however much it carried.
    assert hottest_sampleable_shard(["a", "c"], shares) == "c"
    assert hottest_sampleable_shard([], shares) is None


def test_the_long_tail_of_keys_is_summarised_rather_than_listed(catalog: Catalog) -> None:
    """One key at 88% next to nine at 0.1% is a finding buried in its own evidence."""

    class OneHotKey(FakeClient):
        def get_shard_iterator(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(("get_shard_iterator", kwargs))
            return {"ShardIterator": "iter-0"}

        def get_records(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(("get_records", kwargs))
            records = [{"PartitionKey": "tenant-acme"} for _ in range(880)]
            records += [{"PartitionKey": f"t-{i}"} for i in range(120)]
            return {"Records": records, "NextShardIterator": None, "MillisBehindLatest": 0}

    samples = sample_keys(
        _sampler(OneHotKey()),
        "orders",
        "shardId-000000000000",
        NOW,
        catalog.aws,
        catalog.thresholds,
        sleep=lambda _: None,
    )

    assert [entry.partition_key for entry in samples] == ["tenant-acme"]
    assert samples[0].share == pytest.approx(0.88)
    # The rest are counted, not named: the shares deliberately do not add to one.
    assert sum(entry.share for entry in samples) < 1.0
