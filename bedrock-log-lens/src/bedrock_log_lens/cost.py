"""Estimate what an invocation cost.

Pure. Every figure is an estimate from published on-demand list prices, so a model the
price file does not cover produces ``None`` and a stated reason rather than a zero that
would quietly shrink a total.

Cache tokens are priced separately and deliberately. A cached read is roughly a tenth of a
fresh input token, and a cache write costs more than one; folding them into the input count
would misprice exactly the workloads that use caching heavily.
"""

from __future__ import annotations

from collections.abc import Iterable

from bedrock_log_lens.catalog import TOKENS_PER_PRICE_UNIT, Prices
from bedrock_log_lens.match import model_key
from bedrock_log_lens.models import Cost, InvocationRecord


def estimate(record: InvocationRecord, prices: Prices) -> Cost:
    """What one invocation cost, or why it cannot be said."""
    key = model_key(record.model_id)
    rates = prices.rates_for(record.model_id, record.region, key.scope)
    if rates is None:
        if record.region not in prices.rates:
            return Cost(
                None,
                f"the price file has no rates for {record.region or 'an unnamed region'}",
            )
        return Cost(None, f"no published price for {record.model_id} in {record.region}")

    charged: list[tuple[str, int]] = [
        ("input", record.input_tokens or 0),
        ("output", record.output_tokens or 0),
        ("cache_read", record.cache_read_tokens or 0),
        ("cache_write", record.cache_write_tokens or 0),
    ]

    total = 0.0
    missing: list[str] = []
    for kind, tokens in charged:
        if tokens <= 0:
            continue
        rate = rates.rate_for(kind)
        if rate is None:
            missing.append(kind)
            continue
        total += tokens * rate / TOKENS_PER_PRICE_UNIT

    if missing:
        # Some tokens were charged at a rate AWS does not publish for this model. Reporting
        # the partial figure would understate it without saying so.
        return Cost(
            None,
            f"{rates.service_name} has no published rate for "
            f"{', '.join(missing)} tokens in {record.region}",
        )
    return Cost(round(total, 6))


def estimate_all(
    records: Iterable[InvocationRecord], prices: Prices
) -> list[tuple[InvocationRecord, Cost]]:
    """Cost every record, keeping each one beside its own figure."""
    return [(record, estimate(record, prices)) for record in records]


def unpriced_models(costed: Iterable[tuple[InvocationRecord, Cost]]) -> tuple[str, ...]:
    """Every model ID that could not be priced, so a total can say it is a floor."""
    return tuple(sorted({record.model_id for record, cost in costed if not cost.is_known}))
