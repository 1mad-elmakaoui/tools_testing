"""Turn parsed records into a finished report.

The join between the pure layers: cost everything, group it, measure the distributions,
run the heuristics. Pure itself — it takes records and returns a Report, with no file or
network in sight.
"""

from __future__ import annotations

from bedrock_log_lens import aggregate, anomaly, cost, stats
from bedrock_log_lens.catalog import Policy, Prices
from bedrock_log_lens.models import ParseOutcome, Report


def analyse(
    outcome: ParseOutcome,
    policy: Policy,
    prices: Prices,
    sources_read: int = 0,
) -> Report:
    """Build the report from everything that parsed."""
    thresholds = policy.thresholds
    records = outcome.records
    costed = cost.estimate_all(records, prices)
    first, last = aggregate.window(records)

    return Report(
        totals=aggregate.totals(costed),
        by_model=aggregate.by_model(costed),
        by_identity=aggregate.by_identity(costed),
        by_hour=aggregate.by_hour(costed),
        by_day=aggregate.by_day(costed),
        percentiles_input=stats.percentiles_by_model(records, output=False),
        percentiles_output=stats.percentiles_by_model(records, output=True),
        outliers=stats.outliers(records, thresholds),
        anomalies=anomaly.detect(costed, thresholds),
        most_expensive=aggregate.most_expensive(costed, thresholds.top_expensive_requests),
        issues=outcome.issues,
        sources_read=sources_read,
        window_start=first,
        window_end=last,
        prices_published=prices.publication_date,
        unpriced_models=cost.unpriced_models(costed),
    )
