"""Turn Kinesis responses into the shapes the pure layer reasons about.

Thin on purpose: it reads, it converts, it makes no judgements. Everything that decides
anything lives in :mod:`stats` and :mod:`diagnose`.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from kinesis_skew.aws import ReadOnlyClient
from kinesis_skew.catalog import AwsFacts
from kinesis_skew.models import CapacityMode, Shard

#: ListShards filter that returns the shards open at a moment, plus those closed since.
#: It is how a window that contains a split gets the closed parent as well as its children.
_FROM_TIMESTAMP = "FROM_TIMESTAMP"


def list_streams(client: ReadOnlyClient) -> list[str]:
    """Every stream name in the region, paginated."""
    return [str(name) for name in client.paginate("ListStreams", "StreamNames")]


def describe_stream(
    client: ReadOnlyClient, stream_name: str
) -> tuple[CapacityMode, int, tuple[str, ...], dt.datetime | None]:
    """The stream's capacity mode, open shard count, shard-level metrics and creation time.

    The shard-level metrics are what says whether enhanced monitoring is on. Without them
    CloudWatch has nothing per shard to return, and the tool's whole question is
    unanswerable — so this is read before any metric is requested rather than after an
    empty result has been mistaken for an idle stream.
    """
    response = client.call("DescribeStreamSummary", StreamName=stream_name)
    summary = _mapping(response.get("StreamDescriptionSummary"))

    mode_details = _mapping(summary.get("StreamModeDetails"))
    raw_mode = str(mode_details.get("StreamMode") or CapacityMode.PROVISIONED.value)
    mode = (
        CapacityMode(raw_mode)
        if raw_mode in {item.value for item in CapacityMode}
        else CapacityMode.PROVISIONED
    )

    metrics: list[str] = []
    for entry in summary.get("EnhancedMonitoring") or []:
        metrics.extend(str(name) for name in _mapping(entry).get("ShardLevelMetrics") or [])

    created = summary.get("StreamCreationTimestamp")
    return (
        mode,
        int(summary.get("OpenShardCount") or 0),
        tuple(metrics),
        created if isinstance(created, dt.datetime) else None,
    )


def list_shards(client: ReadOnlyClient, stream_name: str, window_start: dt.datetime) -> list[Shard]:
    """Every shard that could have taken writes during the window.

    Filtered from the start of the window rather than to the open shards, because a shard
    closed by a split or merge partway through still holds the traffic it took before it
    closed. Leaving it out would lose that traffic and make the window look quieter than it
    was.
    """
    raw = client.paginate(
        "ListShards",
        "Shards",
        # ListShards refuses these alongside the NextToken it issued for them.
        first_page_only=("StreamName", "ShardFilter"),
        StreamName=stream_name,
        ShardFilter={"Type": _FROM_TIMESTAMP, "Timestamp": window_start},
    )
    return [_shard(item) for item in raw]


def _shard(raw: Mapping[str, Any]) -> Shard:
    sequence = _mapping(raw.get("SequenceNumberRange"))
    ending = sequence.get("EndingSequenceNumber")
    return Shard(
        shard_id=str(raw.get("ShardId") or ""),
        # Present only once the shard has been closed by a split or a merge.
        ending_sequence_number=str(ending) if ending else None,
        parent_shard_id=_optional_str(raw.get("ParentShardId")),
        adjacent_parent_shard_id=_optional_str(raw.get("AdjacentParentShardId")),
    )


def missing_shard_metrics(enabled: Sequence[str], aws: AwsFacts) -> tuple[str, ...]:
    """Which of the metrics this tool needs the stream is not emitting per shard.

    ``ALL`` is a valid value meaning every shard-level metric, so it satisfies all of them.
    """
    if "ALL" in enabled:
        return ()
    return tuple(name for name in aws.shard_level_metric_names if name not in enabled)


def open_shards(shards: Sequence[Shard]) -> Iterator[Shard]:
    """The shards still accepting writes."""
    return (shard for shard in shards if shard.is_open)


def _mapping(raw: object) -> Mapping[str, Any]:
    return raw if isinstance(raw, dict) else {}


def _optional_str(raw: object) -> str | None:
    return str(raw) if raw else None
