"""A whole scan, driven by in-memory clients.

Covers what the AWS layer is easy to get wrong: enhanced monitoring being off, a split
mid-window, a shard CloudWatch has nothing to say about, and sampling that only happens
when it was asked for.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from kinesis_skew.aws import CallRecorder, ReadOnlyClient, RetryPolicy
from kinesis_skew.catalog import Catalog
from kinesis_skew.models import CapacityMode, Verdict
from kinesis_skew.scan import scan, scan_stream
from tests.test_aws import FakeClient

REGION = "eu-west-1"
NOW = dt.datetime(2026, 9, 24, 12, 0, tzinfo=dt.UTC)
START = NOW - dt.timedelta(hours=24)
WINDOW_MINUTES = 24 * 60
BYTES_LIMIT_PER_MINUTE = 1048576 * 60


class KinesisFake(FakeClient):
    """Answers the three read calls from a described stream."""

    def __init__(
        self,
        shards: list[dict[str, Any]],
        *,
        metrics: list[str] | None = None,
        mode: str = "PROVISIONED",
        records: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__()
        self._shards = shards
        self._metrics = metrics if metrics is not None else ["ALL"]
        self._mode = mode
        self._records = records or []

    def list_streams(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("list_streams", kwargs))
        return {"StreamNames": ["orders"]}

    def describe_stream_summary(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("describe_stream_summary", kwargs))
        return {
            "StreamDescriptionSummary": {
                "StreamName": kwargs["StreamName"],
                "StreamModeDetails": {"StreamMode": self._mode},
                "OpenShardCount": sum(
                    1
                    for s in self._shards
                    if not s.get("SequenceNumberRange", {}).get("EndingSequenceNumber")
                ),
                "EnhancedMonitoring": [{"ShardLevelMetrics": self._metrics}],
            }
        }

    def list_shards(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("list_shards", kwargs))
        return {"Shards": self._shards}

    def get_shard_iterator(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_shard_iterator", kwargs))
        return {"ShardIterator": "iter-1"}

    def get_records(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_records", kwargs))
        return {"Records": self._records, "NextShardIterator": None, "MillisBehindLatest": 0}


class CloudWatchFake(FakeClient):
    """Answers GetMetricData from a per-shard script of per-minute values."""

    def __init__(self, series: dict[str, dict[str, list[float]]]) -> None:
        super().__init__()
        self._series = series

    def get_metric_data(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_metric_data", kwargs))
        results = []
        for query in kwargs["MetricDataQueries"]:
            stat = query["MetricStat"]
            name = stat["Metric"]["MetricName"]
            shard = next(d["Value"] for d in stat["Metric"]["Dimensions"] if d["Name"] == "ShardId")
            values = self._series.get(shard, {}).get(name, [])
            results.append({"Id": query["Id"], "Values": list(values), "Timestamps": []})
        return {"MetricDataResults": results}


def shard(shard_id: str, *, closed: bool = False, parent: str | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "ShardId": shard_id,
        "SequenceNumberRange": {"StartingSequenceNumber": "1"},
    }
    if closed:
        entry["SequenceNumberRange"]["EndingSequenceNumber"] = "9"
    if parent:
        entry["ParentShardId"] = parent
    return entry


def series(
    fraction: float, minutes: int = WINDOW_MINUTES, throttled: float = 0.0
) -> dict[str, list[float]]:
    """A shard writing `fraction` of its byte limit every minute for `minutes`."""
    return {
        "IncomingBytes": [BYTES_LIMIT_PER_MINUTE * fraction] * minutes,
        "IncomingRecords": [1000 * 60 * fraction] * minutes,
        "WriteProvisionedThroughputExceeded": ([throttled / minutes] * minutes) if minutes else [],
    }


def clients(
    kinesis: KinesisFake,
    cloudwatch: CloudWatchFake,
    *,
    sampling: bool = False,
    recorder: CallRecorder | None = None,
) -> dict[str, ReadOnlyClient]:
    retry = RetryPolicy(max_retries=1, base_delay_seconds=0.0, sleep=lambda _: None)
    kinesis_ops = {"ListStreams", "DescribeStreamSummary", "ListShards"}
    if sampling:
        kinesis_ops |= {"GetShardIterator", "GetRecords"}
    return {
        "kinesis": ReadOnlyClient(
            kinesis,  # type: ignore[arg-type]
            "kinesis",
            frozenset(kinesis_ops),
            retry,
            recorder,
        ),
        "cloudwatch": ReadOnlyClient(
            cloudwatch,  # type: ignore[arg-type]
            "cloudwatch",
            frozenset({"GetMetricData"}),
            retry,
            recorder,
        ),
    }


def run(
    kinesis: KinesisFake,
    cloudwatch: CloudWatchFake,
    catalog: Catalog,
    *,
    sampling: bool = False,
    recorder: CallRecorder | None = None,
) -> Any:
    return scan_stream(
        clients(kinesis, cloudwatch, sampling=sampling, recorder=recorder),
        "orders",
        REGION,
        catalog,
        START,
        NOW,
        catalog.thresholds,
        sampling,
    )


# ------------------------------------------------------------------- the main paths


def test_a_hot_shard_is_diagnosed_as_skew(catalog: Catalog) -> None:
    shards = [shard(f"shardId-{i:012d}") for i in range(8)]
    metrics = {f"shardId-{i:012d}": series(0.02) for i in range(1, 8)}
    metrics["shardId-000000000000"] = series(0.99, throttled=41_000)

    report = run(KinesisFake(shards), CloudWatchFake(metrics), catalog)

    assert report.diagnosis.verdict is Verdict.SKEW
    assert report.statistics is not None
    assert report.statistics.hottest_shard_id == "shardId-000000000000"
    assert report.total_throttled_records == pytest.approx(41_000)


def test_an_evenly_loaded_stream_is_diagnosed_as_capacity(catalog: Catalog) -> None:
    shards = [shard(f"shardId-{i:012d}") for i in range(6)]
    metrics = {f"shardId-{i:012d}": series(0.95, throttled=6_000) for i in range(6)}

    report = run(KinesisFake(shards), CloudWatchFake(metrics), catalog)

    assert report.diagnosis.verdict is Verdict.CAPACITY
    assert "add shards" in report.diagnosis.remedy.lower()


def test_enhanced_monitoring_off_stops_before_asking_cloudwatch(catalog: Catalog) -> None:
    """Asking for metrics that cannot exist would return nothing and look like an idle stream."""
    kinesis = KinesisFake([shard("shardId-000000000000")], metrics=[])
    cloudwatch = CloudWatchFake({})

    report = run(kinesis, cloudwatch, catalog)

    assert report.diagnosis.verdict is Verdict.NO_SHARD_METRICS
    assert cloudwatch.calls == [], "no metric should be requested when none can exist"
    assert "enable-enhanced-monitoring" in report.diagnosis.remedy
    assert set(report.missing_metrics) == {
        "IncomingBytes",
        "IncomingRecords",
        "WriteProvisionedThroughputExceeded",
    }


def test_partial_enhanced_monitoring_is_still_not_enough(catalog: Catalog) -> None:
    kinesis = KinesisFake([shard("shardId-000000000000")], metrics=["IncomingBytes"])
    report = run(kinesis, CloudWatchFake({}), catalog)
    assert report.diagnosis.verdict is Verdict.NO_SHARD_METRICS
    assert report.missing_metrics == ("IncomingRecords", "WriteProvisionedThroughputExceeded")


def test_all_satisfies_every_metric(catalog: Catalog) -> None:
    kinesis = KinesisFake([shard("shardId-000000000000")], metrics=["ALL"])
    metrics = {"shardId-000000000000": series(0.2)}
    report = run(kinesis, CloudWatchFake(metrics), catalog)
    assert report.missing_metrics == ()
    assert report.diagnosis.verdict is not Verdict.NO_SHARD_METRICS


# --------------------------------------------------------------------- resharding


def test_a_split_mid_window_is_flagged_and_does_not_read_as_skew(catalog: Catalog) -> None:
    """The closed parent and its young children are shown but kept out of the ranking."""
    shards = [
        shard("shardId-000000000000", closed=True),
        shard("shardId-000000000001", parent="shardId-000000000000"),
        shard("shardId-000000000002", parent="shardId-000000000000"),
        shard("shardId-000000000003"),
        shard("shardId-000000000004"),
    ]
    quarter = WINDOW_MINUTES // 4
    metrics = {
        "shardId-000000000000": series(0.6, minutes=quarter),
        "shardId-000000000001": series(0.3, minutes=quarter),
        "shardId-000000000002": series(0.3, minutes=quarter),
        "shardId-000000000003": series(0.3),
        "shardId-000000000004": series(0.3),
    }

    report = run(KinesisFake(shards), CloudWatchFake(metrics), catalog)

    assert report.resharded_during_window is True
    assert report.statistics is not None
    assert report.statistics.shard_count == 2
    closed = [item for item in report.utilisation if not item.is_open]
    assert [item.shard_id for item in closed] == ["shardId-000000000000"]
    not_ranked = {item.shard_id for item in report.utilisation if not item.counted_in_statistics}
    assert not_ranked == {
        "shardId-000000000000",
        "shardId-000000000001",
        "shardId-000000000002",
    }


def test_a_shard_with_no_metrics_is_distinguished_from_a_quiet_one(catalog: Catalog) -> None:
    shards = [shard("shardId-000000000000"), shard("shardId-000000000001")]
    metrics = {"shardId-000000000000": series(0.3)}  # nothing at all for the second

    report = run(KinesisFake(shards), CloudWatchFake(metrics), catalog)

    silent = next(item for item in report.utilisation if item.shard_id.endswith("1"))
    assert silent.counted_in_statistics is False
    assert silent.excluded_reason is not None
    assert "no data" in silent.excluded_reason


# ---------------------------------------------------------------------- sampling


def test_sampling_does_not_happen_unless_asked(catalog: Catalog) -> None:
    kinesis = KinesisFake([shard("shardId-000000000000")])
    report = run(kinesis, CloudWatchFake({"shardId-000000000000": series(0.4)}), catalog)

    assert report.samples == ()
    assert report.sampled_shard_id is None
    called = {name for name, _ in kinesis.calls}
    assert "get_records" not in called
    assert "get_shard_iterator" not in called


def test_sampling_ranks_the_keys_on_the_busiest_shard(catalog: Catalog) -> None:
    records = [{"PartitionKey": "tenant-42"} for _ in range(90)]
    records += [{"PartitionKey": f"tenant-{i}"} for i in range(10)]
    kinesis = KinesisFake(
        [shard("shardId-000000000000"), shard("shardId-000000000001")], records=records
    )
    metrics = {
        "shardId-000000000000": series(0.9),
        "shardId-000000000001": series(0.05),
    }

    report = run(kinesis, CloudWatchFake(metrics), catalog, sampling=True)

    assert report.sampled_shard_id == "shardId-000000000000"
    assert report.samples[0].partition_key == "tenant-42"
    assert report.samples[0].count == 90
    assert report.samples[0].share == pytest.approx(0.9)
    iterator_call = next(kwargs for name, kwargs in kinesis.calls if name == "get_shard_iterator")
    assert iterator_call["ShardIteratorType"] == "AT_TIMESTAMP"
    assert iterator_call["ShardId"] == "shardId-000000000000"


# -------------------------------------------------------------- the whole region


def test_scan_reports_a_stream_that_cannot_be_read(catalog: Catalog) -> None:
    class Broken(KinesisFake):
        def describe_stream_summary(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("AccessDeniedException")

    result = scan(
        lambda _region: clients(Broken([]), CloudWatchFake({})),
        REGION,
        None,
        catalog,
        NOW,
        24.0,
        catalog.thresholds,
    )

    assert result.reports == ()
    assert len(result.errors) == 1
    assert result.errors[0][0] == "orders"


def test_scan_puts_problems_first(catalog: Catalog) -> None:
    class TwoStreams(KinesisFake):
        def list_streams(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(("list_streams", kwargs))
            return {"StreamNames": ["quiet", "throttled"]}

    shards = [shard(f"shardId-{i:012d}") for i in range(4)]

    def factory(_region: str) -> Any:
        kinesis = TwoStreams(shards)
        cloudwatch = CloudWatchFake(
            {f"shardId-{i:012d}": series(0.95, throttled=1_000) for i in range(4)}
        )
        return clients(kinesis, cloudwatch)

    result = scan(factory, REGION, None, catalog, NOW, 24.0, catalog.thresholds)

    assert len(result.reports) == 2
    assert result.reports[0].diagnosis.verdict.is_problem
    assert len(result.problems) == 2


def test_on_demand_streams_are_read_as_such(catalog: Catalog) -> None:
    kinesis = KinesisFake([shard(f"shardId-{i:012d}") for i in range(4)], mode="ON_DEMAND")
    metrics = {f"shardId-{i:012d}": series(0.95, throttled=1_000) for i in range(4)}

    report = run(kinesis, CloudWatchFake(metrics), catalog)

    assert report.capacity_mode is CapacityMode.ON_DEMAND
    assert "add shards" not in report.diagnosis.remedy.lower()
