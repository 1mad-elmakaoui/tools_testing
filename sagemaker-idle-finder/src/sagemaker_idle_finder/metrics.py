"""Fetch Invocations from CloudWatch in as few calls as the quotas allow.

The batch size is computed rather than guessed. ``GetMetricData`` caps a single call at 500
query structures *and* 100,800 datapoints, and with an hourly period over a fortnight the
datapoint cap is the one that bites first: 336 datapoints per query means 300 queries fit,
not 500. Getting that wrong means either wasted round trips or a rejected request.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Sequence
from dataclasses import dataclass

from sagemaker_idle_finder.aws import ReadOnlyClient
from sagemaker_idle_finder.catalog import AwsFacts, Limits
from sagemaker_idle_finder.models import MetricWindow

#: CloudWatch query ids must start with a lowercase letter.
_QUERY_PREFIX = "m"


@dataclass(frozen=True)
class MetricTarget:
    """One variant whose invocations we want."""

    endpoint_name: str
    variant_name: str

    @property
    def key(self) -> tuple[str, str]:
        """A hashable identity for matching results back."""
        return (self.endpoint_name, self.variant_name)


def datapoints_per_query(start: dt.datetime, end: dt.datetime, period_seconds: int) -> int:
    """How many datapoints one query over this window will return."""
    if period_seconds <= 0:
        raise ValueError("period_seconds must be positive")
    span = (end - start).total_seconds()
    return max(1, math.ceil(span / period_seconds))


def batch_size(start: dt.datetime, end: dt.datetime, period_seconds: int, limits: Limits) -> int:
    """The largest number of queries that fits in one GetMetricData call.

    Bounded by whichever documented cap binds first.
    """
    per_query = datapoints_per_query(start, end, period_seconds)
    by_datapoints = limits.max_datapoints_per_call // per_query
    return max(1, min(limits.max_metric_queries_per_call, by_datapoints))


def build_queries(
    targets: Sequence[MetricTarget], aws: AwsFacts, period_seconds: int
) -> list[dict[str, object]]:
    """Build the MetricDataQuery structures for a batch of targets."""
    return [
        {
            "Id": f"{_QUERY_PREFIX}{index}",
            "MetricStat": {
                "Metric": {
                    "Namespace": aws.metric_namespace,
                    "MetricName": aws.invocations_metric,
                    "Dimensions": [
                        {"Name": "EndpointName", "Value": target.endpoint_name},
                        {"Name": "VariantName", "Value": target.variant_name},
                    ],
                },
                "Period": period_seconds,
                "Stat": "Sum",
            },
            "ReturnData": True,
        }
        for index, target in enumerate(targets)
    ]


def fetch_invocations(
    client: ReadOnlyClient,
    targets: Sequence[MetricTarget],
    start: dt.datetime,
    end: dt.datetime,
    aws: AwsFacts,
    limits: Limits,
    period_seconds: int,
) -> dict[tuple[str, str], MetricWindow]:
    """Fetch the invocation total for every target, batching within the quotas.

    A target CloudWatch has no data for gets a window whose total is ``None`` rather than
    zero: "no data" and "measured zero" are different claims, and only the caller knows
    whether the endpoint existed for the window.
    """
    windows: dict[tuple[str, str], MetricWindow] = {}
    if not targets:
        return windows

    size = batch_size(start, end, period_seconds, limits)
    for offset in range(0, len(targets), size):
        batch = targets[offset : offset + size]
        queries = build_queries(batch, aws, period_seconds)
        results = _fetch_batch(client, queries, start, end)
        for index, target in enumerate(batch):
            values = results.get(f"{_QUERY_PREFIX}{index}")
            # CloudWatch answers a metric it has never seen with a result whose Values list
            # is empty, not by omitting the result. Summing that to zero would turn "we
            # have no data" into "we measured nothing happening", which are different
            # claims and only one of them justifies calling an endpoint idle.
            has_data = bool(values)
            windows[target.key] = MetricWindow(
                start=start,
                end=end,
                total_invocations=float(sum(values)) if has_data and values else None,
                datapoints=len(values) if values else 0,
            )
    return windows


def _fetch_batch(
    client: ReadOnlyClient,
    queries: list[dict[str, object]],
    start: dt.datetime,
    end: dt.datetime,
) -> dict[str, list[float]]:
    """Run one GetMetricData call, following NextToken until the results are complete."""
    collected: dict[str, list[float]] = {}
    token: str | None = None
    while True:
        arguments: dict[str, object] = {
            "MetricDataQueries": queries,
            "StartTime": start,
            "EndTime": end,
            "ScanBy": "TimestampAscending",
        }
        if token:
            arguments["NextToken"] = token
        response = client.call("GetMetricData", **arguments)
        for result in response.get("MetricDataResults") or []:
            identifier = str(result.get("Id"))
            values = [float(value) for value in result.get("Values") or []]
            collected.setdefault(identifier, []).extend(values)
        next_token = response.get("NextToken")
        if not next_token:
            return collected
        token = str(next_token)
