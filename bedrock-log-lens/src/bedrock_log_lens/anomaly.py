"""Heuristics for calls that look like something has gone wrong.

Pure, and heuristics throughout. Each detector describes a shape in a call pattern: a lot
of calls in a short window, a sudden jump against an identity's own baseline, or the same
request shape repeating. None of them proves a fault. A nightly batch job is
indistinguishable from a runaway loop by call rate alone, which is why every finding names
the identity, the window and the cost, and leaves the judgement to a person.

The third detector is the one written for agents. An agent stuck in a loop re-sends nearly
the same context over and over, so the same identity hits the same model with an identical
input token count, many times, quickly. That is visible in the metadata, without reading a
single prompt — which is the whole point of this tool.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections.abc import Sequence

from bedrock_log_lens.catalog import Thresholds
from bedrock_log_lens.models import Anomaly, AnomalyKind, Cost, InvocationRecord

Costed = tuple[InvocationRecord, Cost]


def detect(costed: Sequence[Costed], thresholds: Thresholds) -> tuple[Anomaly, ...]:
    """Every heuristic, over every identity."""
    by_identity: dict[str, list[Costed]] = {}
    for record, cost in costed:
        by_identity.setdefault(record.identity_arn or "<no identity recorded>", []).append(
            (record, cost)
        )

    found: list[Anomaly] = []
    for identity, items in by_identity.items():
        ordered = sorted(items, key=lambda item: item[0].timestamp)
        found.extend(_bursts(identity, ordered, thresholds))
        found.extend(_rate_jumps(identity, ordered, thresholds))
        found.extend(_repeated_shapes(identity, ordered, thresholds))

    found.sort(key=lambda item: (-item.request_count, item.window_start, item.identity_arn))
    return tuple(found)


def _bursts(identity: str, ordered: Sequence[Costed], thresholds: Thresholds) -> list[Anomaly]:
    """Windows where one identity made a lot of calls in a short time.

    A sliding window over the calls themselves rather than over fixed clock intervals, so a
    burst straddling a minute boundary is still one burst.
    """
    window = dt.timedelta(seconds=thresholds.burst_window_seconds)
    found: list[Anomaly] = []
    start = 0
    reported_until: dt.datetime | None = None

    for end in range(len(ordered)):
        while ordered[end][0].timestamp - ordered[start][0].timestamp > window:
            start += 1
        count = end - start + 1
        if count < thresholds.burst_request_count:
            continue
        first = ordered[start][0].timestamp
        if reported_until is not None and first <= reported_until:
            # Still inside a burst already reported; one finding per burst, not per call.
            continue
        span = ordered[start : end + 1]
        found.append(
            Anomaly(
                kind=AnomalyKind.BURST,
                identity_arn=identity,
                window_start=first,
                window_end=ordered[end][0].timestamp,
                request_count=count,
                cost_usd=_cost_of(span),
                detail=(
                    f"{count} calls within {thresholds.burst_window_seconds}s, against a "
                    f"threshold of {thresholds.burst_request_count}"
                ),
            )
        )
        reported_until = ordered[end][0].timestamp
    return found


def _rate_jumps(identity: str, ordered: Sequence[Costed], thresholds: Thresholds) -> list[Anomaly]:
    """Windows carrying far more calls than this identity's own usual rate.

    Measured against the identity's own median rather than a global one, so a steady
    high-volume caller is not flagged every window for being busy.
    """
    window = dt.timedelta(seconds=thresholds.burst_window_seconds)
    buckets: dict[dt.datetime, list[Costed]] = {}
    for record, cost in ordered:
        slot = _floor(record.timestamp, window)
        buckets.setdefault(slot, []).append((record, cost))

    if len(buckets) < thresholds.baseline_minimum_windows:
        return []

    counts = [len(items) for items in buckets.values()]
    baseline = statistics.median(counts)
    if baseline <= 0:
        return []

    found: list[Anomaly] = []
    for slot, items in sorted(buckets.items()):
        count = len(items)
        if count < thresholds.rate_jump_minimum_requests:
            continue
        multiple = count / baseline
        if multiple < thresholds.rate_jump_multiple:
            continue
        found.append(
            Anomaly(
                kind=AnomalyKind.RATE_JUMP,
                identity_arn=identity,
                window_start=slot,
                window_end=slot + window,
                request_count=count,
                cost_usd=_cost_of(items),
                detail=(
                    f"{count} calls in this window against a median of {baseline:g}, "
                    f"a {multiple:.0f}x jump"
                ),
            )
        )
    return found


def _repeated_shapes(
    identity: str, ordered: Sequence[Costed], thresholds: Thresholds
) -> list[Anomaly]:
    """The same identity, model and input token count, repeating quickly.

    What an agent re-sending the same context looks like from the outside. Identical input
    token counts are a strong signal because a growing conversation changes size on every
    turn: a run of identical sizes means the input is not progressing.
    """
    window = dt.timedelta(seconds=thresholds.burst_window_seconds)
    groups: dict[tuple[str, int], list[Costed]] = {}
    for record, cost in ordered:
        if record.input_tokens is None:
            continue
        groups.setdefault((record.model_id, record.input_tokens), []).append((record, cost))

    found: list[Anomaly] = []
    for (model_id, tokens), items in groups.items():
        if len(items) < thresholds.repeated_shape_count:
            continue
        start = 0
        for end in range(len(items)):
            while items[end][0].timestamp - items[start][0].timestamp > window:
                start += 1
            count = end - start + 1
            if count < thresholds.repeated_shape_count:
                continue
            span = items[start : end + 1]
            found.append(
                Anomaly(
                    kind=AnomalyKind.REPEATED_SHAPE,
                    identity_arn=identity,
                    window_start=items[start][0].timestamp,
                    window_end=items[end][0].timestamp,
                    request_count=count,
                    cost_usd=_cost_of(span),
                    model_id=model_id,
                    detail=(
                        f"{count} calls to {model_id} with an identical input size of "
                        f"{tokens:,} tokens within "
                        f"{thresholds.burst_window_seconds}s, which is what a loop looks "
                        f"like from the metadata"
                    ),
                )
            )
            break  # One finding per repeated shape is enough to point at it.
    return found


def _cost_of(items: Sequence[Costed]) -> float:
    """Total known cost of a span. Unpriced calls contribute nothing and are not invented."""
    return round(sum(cost.usd or 0.0 for _record, cost in items), 6)


def _floor(moment: dt.datetime, window: dt.timedelta) -> dt.datetime:
    """Round a timestamp down to the start of its window."""
    seconds = int(window.total_seconds())
    if seconds <= 0:
        return moment
    epoch = int(moment.timestamp())
    return dt.datetime.fromtimestamp(epoch - epoch % seconds, tz=dt.UTC)
