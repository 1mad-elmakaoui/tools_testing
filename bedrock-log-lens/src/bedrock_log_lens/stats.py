"""Token distributions per model, and the requests that sit outside them.

Pure. Percentiles use the nearest-rank method: the p-th percentile is the value at
position ``ceil(p/100 * n)`` in the sorted sample. It returns a number that actually
occurred rather than an interpolation between two that did, which is the right answer for
token counts — there is no such thing as 4,096.5 tokens.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from bedrock_log_lens.catalog import Thresholds
from bedrock_log_lens.models import InvocationRecord, Outlier, TokenPercentiles


def percentile(values: Sequence[int], percent: float) -> int:
    """The nearest-rank percentile of `values`.

    Raises on an empty sample rather than returning zero: zero is a plausible token count,
    so it would be indistinguishable from a real answer.
    """
    if not values:
        raise ValueError("percentile of an empty sample is undefined")
    if not 0 < percent <= 100:
        raise ValueError("percent must be above 0 and at most 100")
    ordered = sorted(values)
    rank = math.ceil(percent / 100 * len(ordered))
    return ordered[min(rank, len(ordered)) - 1]


def percentiles_by_model(
    records: Sequence[InvocationRecord], *, output: bool = False
) -> tuple[TokenPercentiles, ...]:
    """Input or output token percentiles for each model, busiest model first."""
    samples: dict[str, list[int]] = {}
    for record in records:
        tokens = record.output_tokens if output else record.input_tokens
        if tokens is not None:
            samples.setdefault(record.model_id, []).append(tokens)

    summaries = [
        TokenPercentiles(
            model_id=model_id,
            count=len(values),
            p50=percentile(values, 50),
            p95=percentile(values, 95),
            p99=percentile(values, 99),
            maximum=max(values),
        )
        for model_id, values in samples.items()
        if values
    ]
    summaries.sort(key=lambda item: (-item.count, item.model_id))
    return tuple(summaries)


def outliers(records: Sequence[InvocationRecord], thresholds: Thresholds) -> tuple[Outlier, ...]:
    """Requests above their model's outlier percentile, for input or output tokens.

    A model needs enough requests for the percentile to mean something; below that the
    largest request would be flagged as an outlier against itself.
    """
    found: list[Outlier] = []
    for output in (False, True):
        kind = "output" if output else "input"
        for summary in percentiles_by_model(records, output=output):
            if summary.count < thresholds.minimum_requests_for_percentiles:
                continue
            cutoff = percentile(
                [
                    tokens
                    for record in records
                    if record.model_id == summary.model_id
                    and (tokens := (record.output_tokens if output else record.input_tokens))
                    is not None
                ],
                thresholds.outlier_percentile,
            )
            for record in records:
                if record.model_id != summary.model_id:
                    continue
                tokens = record.output_tokens if output else record.input_tokens
                if tokens is not None and tokens > cutoff:
                    found.append(Outlier(record=record, kind=kind, value=tokens, threshold=cutoff))

    found.sort(key=lambda item: (-item.value, item.record.request_id))
    return tuple(found)
