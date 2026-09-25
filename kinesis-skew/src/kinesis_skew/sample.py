"""Read a bounded sample of records from one shard to name the keys crowding it.

Kept apart from everything else on purpose. These are the only two operations this tool can
make that cost the account anything beyond a metric read: they consume the shard's read
throughput, which is shared with the stream's real consumers. So they are opt-in, bounded,
and absent from the allowlist entirely unless the caller asked for them.

What this answers is narrower than it looks. It reads records that are in the shard now,
which are not the records that were rejected earlier in the window. It is evidence about
which keys dominate, not proof of which key caused a particular throttle.
"""

from __future__ import annotations

import datetime as dt
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from kinesis_skew.aws import AwsError, ReadOnlyClient
from kinesis_skew.catalog import AwsFacts, Thresholds
from kinesis_skew.models import KeySample

#: Iterator type used to start at the beginning of the measured window. AWS documents that
#: a timestamp older than the trim horizon yields the oldest untrimmed record instead, so
#: this degrades rather than failing on a window longer than the retention period.
_AT_TIMESTAMP = "AT_TIMESTAMP"


def sample_keys(
    client: ReadOnlyClient,
    stream_name: str,
    shard_id: str,
    window_start: dt.datetime,
    aws: AwsFacts,
    thresholds: Thresholds,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[KeySample, ...]:
    """Read up to the configured budget of records and rank their partition keys.

    Paced to stay inside the documented five GetRecords calls per second per shard, so that
    sampling cannot itself become the reason a consumer is throttled.
    """
    iterator = _first_iterator(client, stream_name, shard_id, window_start)
    if iterator is None:
        return ()

    pace = 1.0 / max(1, aws.get_records_calls_per_second_per_shard)
    limit = min(thresholds.sample_max_records, aws.get_records_max_records_per_call)

    counts: Counter[str] = Counter()
    total = 0
    for call_number in range(thresholds.sample_max_get_records_calls):
        if call_number:
            sleep(pace)
        try:
            response = client.call("GetRecords", ShardIterator=iterator, Limit=limit)
        except AwsError:
            # A sample is a nicety. Losing it must not cost the diagnosis that earned it.
            break

        records = response.get("Records") or []
        for record in records:
            key = _mapping(record).get("PartitionKey")
            if isinstance(key, str):
                counts[key] += 1
                total += 1

        iterator = _optional_str(response.get("NextShardIterator"))
        if iterator is None or total >= thresholds.sample_max_records:
            break
        if not records and not response.get("MillisBehindLatest"):
            # Caught up with the end of the shard; more calls would return nothing.
            break

    return _rank(
        counts, total, thresholds.sample_top_keys_shown, thresholds.sample_minimum_share_shown
    )


def _first_iterator(
    client: ReadOnlyClient, stream_name: str, shard_id: str, window_start: dt.datetime
) -> str | None:
    try:
        response = client.call(
            "GetShardIterator",
            StreamName=stream_name,
            ShardId=shard_id,
            ShardIteratorType=_AT_TIMESTAMP,
            Timestamp=window_start,
        )
    except AwsError:
        return None
    return _optional_str(response.get("ShardIterator"))


def _rank(
    counts: Mapping[str, int], total: int, shown: int, minimum_share: float
) -> tuple[KeySample, ...]:
    """The keys worth naming, busiest first.

    The long tail is dropped from the list rather than printed: a hot key at 88% next to
    nine keys at 0.1% each is a finding buried in its own evidence. The caller can still
    see what was left out, because the shares no longer add to one.
    """
    if total <= 0:
        return ()
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return tuple(
        KeySample(partition_key=key, count=count, share=count / total)
        for key, count in ordered[:shown]
        if count / total >= minimum_share
    )


def hottest_sampleable_shard(shard_ids: Sequence[str], shares: Mapping[str, float]) -> str | None:
    """The open shard carrying the most traffic, which is the one worth sampling."""
    candidates = [shard_id for shard_id in shard_ids if shard_id in shares]
    if not candidates:
        return None
    return max(candidates, key=lambda shard_id: shares[shard_id])


def _mapping(raw: object) -> Mapping[str, Any]:
    return raw if isinstance(raw, dict) else {}


def _optional_str(raw: object) -> str | None:
    return str(raw) if raw else None
