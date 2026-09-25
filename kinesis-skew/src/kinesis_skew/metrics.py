"""Fetch shard-level write metrics from CloudWatch.

Three metrics per shard, at the 60-second period Kinesis emits them on. Asking for a
coarser period would be cheaper and would also destroy the signal: a shard pinned for two
minutes an hour disappears into an hourly average, and that shard is the entire point.

Two documented caps shape the batching, and the second is the one that binds. A call may
carry 500 queries, but only 100,800 datapoints. At 60 seconds a 24-hour window is 1,440
datapoints per query, which allows 70 queries per call, or 23 shards once each shard needs
three of them.

Everything is asked for as ``Sum``, including the busiest minute. CloudWatch's ``Maximum``
statistic on these metrics is not the busiest period: for IncomingBytes and IncomingRecords
it is the size of the largest single put operation in the period. Asking for it and calling
the answer a peak would understate a hot shard by orders of magnitude. The busiest minute is
the largest ``Sum`` datapoint once the period is already one minute.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from kinesis_skew.aws import ReadOnlyClient
from kinesis_skew.catalog import AwsFacts, Limits
from kinesis_skew.models import ShardTraffic

#: CloudWatch requires query ids to start with a lowercase letter.
_QUERY_PREFIX = "q"


@dataclass(frozen=True)
class _Query:
    """One metric for one shard, and where its answer belongs."""

    identifier: str
    shard_id: str
    metric_name: str
    statistic: str


def datapoints_per_query(start: dt.datetime, end: dt.datetime, period_seconds: int) -> int:
    """How many datapoints one query over this window will return."""
    if period_seconds <= 0:
        raise ValueError("period_seconds must be positive")
    span = (end - start).total_seconds()
    return max(1, math.ceil(span / period_seconds))


def shards_per_call(
    start: dt.datetime, end: dt.datetime, aws: AwsFacts, limits: Limits, metrics_per_shard: int = 3
) -> int:
    """How many shards fit in one GetMetricData call, under whichever cap binds first."""
    per_query = datapoints_per_query(start, end, aws.shard_metric_period_seconds)
    by_datapoints = limits.max_datapoints_per_call // max(1, per_query)
    by_queries = limits.max_metric_queries_per_call
    return max(1, min(by_queries, by_datapoints) // metrics_per_shard)


def build_queries(
    stream_name: str, shard_ids: Sequence[str], aws: AwsFacts
) -> tuple[list[dict[str, Any]], dict[str, _Query]]:
    """Build the MetricDataQuery structures for a batch of shards.

    All three are summed over a one-minute period, which gives the window total when added
    up and the busiest minute when the largest is taken. CloudWatch's Maximum statistic
    would answer a different question entirely: the largest single put, not the busiest
    minute.
    """
    wanted = (
        (aws.incoming_bytes_metric, "Sum"),
        (aws.incoming_records_metric, "Sum"),
        (aws.write_throttle_metric, "Sum"),
    )

    queries: list[dict[str, Any]] = []
    index: dict[str, _Query] = {}
    for shard_number, shard_id in enumerate(shard_ids):
        for metric_number, (metric_name, statistic) in enumerate(wanted):
            identifier = f"{_QUERY_PREFIX}{shard_number}_{metric_number}"
            index[identifier] = _Query(identifier, shard_id, metric_name, statistic)
            queries.append(
                {
                    "Id": identifier,
                    "MetricStat": {
                        "Metric": {
                            "Namespace": aws.metric_namespace,
                            "MetricName": metric_name,
                            "Dimensions": [
                                {"Name": "StreamName", "Value": stream_name},
                                {"Name": "ShardId", "Value": shard_id},
                            ],
                        },
                        "Period": aws.shard_metric_period_seconds,
                        "Stat": statistic,
                    },
                    "ReturnData": True,
                }
            )
    return queries, index


def fetch_traffic(
    client: ReadOnlyClient,
    stream_name: str,
    shard_ids: Sequence[str],
    start: dt.datetime,
    end: dt.datetime,
    aws: AwsFacts,
    limits: Limits,
) -> dict[str, ShardTraffic]:
    """Read every shard's write metrics over the window."""
    window_minutes = datapoints_per_query(start, end, aws.shard_metric_period_seconds)
    batch = shards_per_call(start, end, aws, limits)

    traffic: dict[str, ShardTraffic] = {}
    for offset in range(0, len(shard_ids), batch):
        chunk = list(shard_ids[offset : offset + batch])
        traffic.update(_fetch_batch(client, stream_name, chunk, start, end, aws, window_minutes))
    return traffic


def _fetch_batch(
    client: ReadOnlyClient,
    stream_name: str,
    shard_ids: Sequence[str],
    start: dt.datetime,
    end: dt.datetime,
    aws: AwsFacts,
    window_minutes: int,
) -> dict[str, ShardTraffic]:
    queries, index = build_queries(stream_name, shard_ids, aws)

    values: dict[str, list[float]] = {}
    token: str | None = None
    while True:
        arguments: dict[str, Any] = {
            "MetricDataQueries": queries,
            "StartTime": start,
            "EndTime": end,
            "ScanBy": "TimestampAscending",
        }
        if token:
            arguments["NextToken"] = token
        response = client.call("GetMetricData", **arguments)
        for result in response.get("MetricDataResults") or []:
            entry = _mapping(result)
            identifier = str(entry.get("Id"))
            values.setdefault(identifier, []).extend(
                float(value) for value in entry.get("Values") or []
            )
        token = response.get("NextToken")
        if not token:
            break

    return _assemble(shard_ids, index, values, window_minutes, aws)


def _assemble(
    shard_ids: Sequence[str],
    index: Mapping[str, _Query],
    values: Mapping[str, list[float]],
    window_minutes: int,
    aws: AwsFacts,
) -> dict[str, ShardTraffic]:
    """Fold the flat query results back into one record per shard."""
    collected: dict[str, dict[tuple[str, str], list[float]]] = {
        shard_id: {} for shard_id in shard_ids
    }
    for identifier, query in index.items():
        collected[query.shard_id][query.metric_name, query.statistic] = list(
            values.get(identifier, [])
        )

    traffic: dict[str, ShardTraffic] = {}
    for shard_id, by_metric in collected.items():
        incoming_bytes = by_metric.get((aws.incoming_bytes_metric, "Sum"), [])
        incoming_records = by_metric.get((aws.incoming_records_metric, "Sum"), [])
        throttled = by_metric.get((aws.write_throttle_metric, "Sum"), [])

        # A shard CloudWatch said nothing about is not a shard that carried nothing: it is
        # one we have no information on, and the caller has to be able to tell them apart.
        observed = max(len(incoming_bytes), len(incoming_records), len(throttled))

        traffic[shard_id] = ShardTraffic(
            shard_id=shard_id,
            total_bytes=sum(incoming_bytes),
            total_records=sum(incoming_records),
            throttled_records=sum(throttled),
            # The period is already one minute, so the largest datapoint is the busiest one.
            peak_bytes_per_minute=max(incoming_bytes, default=0.0),
            peak_records_per_minute=max(incoming_records, default=0.0),
            observed_minutes=observed,
            window_minutes=window_minutes,
        )
    return traffic


def _mapping(raw: object) -> Mapping[str, Any]:
    return raw if isinstance(raw, dict) else {}
