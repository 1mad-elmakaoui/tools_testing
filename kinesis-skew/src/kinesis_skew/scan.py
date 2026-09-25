"""Tie the pieces together: collect, measure, score, diagnose.

The AWS-facing part is a thin shell around :mod:`collect`, :mod:`metrics` and
:mod:`sample`; everything that decides anything is a pure call into :mod:`stats` and
:mod:`diagnose`. A whole scan can therefore be reconstructed in a test from stubbed clients.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace

from kinesis_skew import collect, diagnose, metrics, sample, stats
from kinesis_skew.aws import AwsError, ReadOnlyClient
from kinesis_skew.catalog import Catalog, Thresholds
from kinesis_skew.models import (
    KeySample,
    ScanResult,
    Shard,
    ShardUtilisation,
    StreamReport,
)


def scan_stream(
    clients: Mapping[str, ReadOnlyClient],
    stream_name: str,
    region: str,
    catalog: Catalog,
    start: dt.datetime,
    end: dt.datetime,
    thresholds: Thresholds,
    sample_keys: bool = False,
) -> StreamReport:
    """Work out what is happening to one stream."""
    kinesis = clients["kinesis"]
    mode, open_shard_count, enabled_metrics, _created = collect.describe_stream(
        kinesis, stream_name
    )
    missing = collect.missing_shard_metrics(enabled_metrics, catalog.aws)
    shards = collect.list_shards(kinesis, stream_name, start)

    utilisation: tuple[ShardUtilisation, ...] = ()
    if not missing and shards:
        measured = metrics.fetch_traffic(
            clients["cloudwatch"],
            stream_name,
            [shard.shard_id for shard in shards],
            start,
            end,
            catalog.aws,
            catalog.limits,
        )
        # Carry each shard's open or closed state across from ListShards. CloudWatch has no
        # opinion on it, and a closed shard must not be shown as one still taking writes.
        ordered = [
            replace(measured[shard.shard_id], is_open=shard.is_open)
            for shard in shards
            if shard.shard_id in measured
        ]
        utilisation = stats.utilisation(ordered, catalog.aws, thresholds)

    statistics = stats.statistics(utilisation, thresholds) if utilisation else None
    verdict = diagnose.diagnose(
        capacity_mode=mode,
        scored=utilisation,
        stats=statistics,
        missing_metrics=missing,
        aws=catalog.aws,
        thresholds=thresholds,
    )

    samples: tuple[KeySample, ...] = ()
    sampled_shard: str | None = None
    if sample_keys and utilisation:
        shares = {item.shard_id: item.share_of_bytes for item in utilisation}
        sampled_shard = sample.hottest_sampleable_shard(
            [shard.shard_id for shard in collect.open_shards(shards)], shares
        )
        if sampled_shard is not None:
            samples = sample.sample_keys(
                kinesis, stream_name, sampled_shard, start, catalog.aws, thresholds
            )

    return StreamReport(
        stream_name=stream_name,
        region=region,
        capacity_mode=mode,
        open_shard_count=open_shard_count or sum(1 for _ in collect.open_shards(shards)),
        shard_level_metrics=enabled_metrics,
        window_start=start,
        window_end=end,
        utilisation=utilisation,
        statistics=statistics,
        diagnosis=verdict,
        total_throttled_records=sum(item.throttled_records for item in utilisation),
        resharded_during_window=_was_resharded(shards),
        samples=samples,
        sampled_shard_id=sampled_shard,
    )


def _was_resharded(shards: Sequence[Shard]) -> bool:
    """Whether the window contains a split or a merge.

    A closed shard means one happened; so does a shard with a parent, which is the other
    half of the same event seen from the child's side.
    """
    return any(not shard.is_open or shard.was_resharded for shard in shards)


def scan(
    client_factory: Callable[[str], Mapping[str, ReadOnlyClient]],
    region: str,
    stream_names: Sequence[str] | None,
    catalog: Catalog,
    now: dt.datetime,
    lookback_hours: float,
    thresholds: Thresholds,
    sample_keys: bool = False,
) -> ScanResult:
    """Scan one or every stream in a region, collecting errors rather than abandoning.

    A stream that cannot be read is reported alongside the rest. A run over an account with
    one badly configured stream is far more useful than a run that dies on it.
    """
    start = now - dt.timedelta(hours=lookback_hours)
    clients = client_factory(region)

    names = list(stream_names) if stream_names else collect.list_streams(clients["kinesis"])

    reports: list[StreamReport] = []
    errors: list[tuple[str, str]] = []
    for name in names:
        try:
            reports.append(
                scan_stream(clients, name, region, catalog, start, now, thresholds, sample_keys)
            )
        except (AwsError, OSError) as exc:
            errors.append((name, str(exc)))

    reports.sort(key=_sort_key)
    return ScanResult(
        reports=tuple(reports),
        region=region,
        lookback_hours=lookback_hours,
        started_at=now,
        errors=tuple(errors),
    )


def _sort_key(report: StreamReport) -> tuple[int, float, str]:
    """Problems first, then by how much was rejected, then by name."""
    return (
        0 if report.diagnosis.verdict.is_problem else 1,
        -report.total_throttled_records,
        report.stream_name,
    )
