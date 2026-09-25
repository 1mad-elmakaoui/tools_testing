"""Group costed records into the totals a report is made of.

Pure. Takes records and their costs, returns totals — by model, by caller, by hour and by
day. A group containing a request that could not be priced says so, so a cost total is
never quietly a floor.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable, Sequence

from bedrock_log_lens.models import Cost, GroupTotals, InvocationRecord

Costed = tuple[InvocationRecord, Cost]


def totals(costed: Sequence[Costed], key: str = "all") -> GroupTotals:
    """One set of totals over every record given."""
    requests = 0
    input_tokens = 0
    output_tokens = 0
    cache_read = 0
    cache_write = 0
    cost = 0.0
    unpriced = 0

    for record, estimate in costed:
        requests += 1
        input_tokens += record.input_tokens or 0
        output_tokens += record.output_tokens or 0
        cache_read += record.cache_read_tokens or 0
        cache_write += record.cache_write_tokens or 0
        if estimate.usd is None:
            unpriced += 1
        else:
            cost += estimate.usd

    return GroupTotals(
        key=key,
        requests=requests,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        cost_usd=round(cost, 6),
        unpriced_requests=unpriced,
    )


def group_by(
    costed: Sequence[Costed],
    key_of: Callable[[InvocationRecord], str],
    *,
    sort_by_cost: bool = True,
) -> tuple[GroupTotals, ...]:
    """Totals per group, biggest first by default.

    Time groups want chronological order instead, which is what `sort_by_cost=False` is
    for: a report of calls per hour that jumps around by size is unreadable.
    """
    buckets: dict[str, list[Costed]] = {}
    for record, estimate in costed:
        buckets.setdefault(key_of(record), []).append((record, estimate))

    groups = [totals(items, key=key) for key, items in buckets.items()]
    if sort_by_cost:
        groups.sort(key=lambda group: (-group.cost_usd, -group.requests, group.key))
    else:
        groups.sort(key=lambda group: group.key)
    return tuple(groups)


def by_model(costed: Sequence[Costed]) -> tuple[GroupTotals, ...]:
    """Totals per model ID."""
    return group_by(costed, lambda record: record.model_id)


def by_identity(costed: Sequence[Costed]) -> tuple[GroupTotals, ...]:
    """Totals per caller ARN.

    An identity is an ARN, not a person. It is the thing a report groups by and the thing an
    anomaly names, and it is metadata rather than content.
    """
    return group_by(costed, lambda record: record.identity_arn or "<no identity recorded>")


def by_hour(costed: Sequence[Costed]) -> tuple[GroupTotals, ...]:
    """Totals per hour, in order."""
    return group_by(costed, lambda record: record.hour.isoformat(), sort_by_cost=False)


def by_day(costed: Sequence[Costed]) -> tuple[GroupTotals, ...]:
    """Totals per day, in order."""
    return group_by(costed, lambda record: record.day.isoformat(), sort_by_cost=False)


def most_expensive(costed: Sequence[Costed], limit: int) -> tuple[Costed, ...]:
    """The costliest requests, metadata only.

    What comes back is the record and its cost. The record cannot carry a prompt, so this
    list is safe to print by construction rather than by remembering to strip it.
    """
    priced = [(record, cost) for record, cost in costed if cost.usd is not None]
    priced.sort(key=lambda item: (-(item[1].usd or 0.0), item[0].request_id))
    return tuple(priced[: max(0, limit)])


def window(records: Iterable[InvocationRecord]) -> tuple[dt.datetime | None, dt.datetime | None]:
    """The first and last timestamps seen, so a report can say what it covers."""
    stamps = sorted(record.timestamp for record in records)
    if not stamps:
        return (None, None)
    return (stamps[0], stamps[-1])
