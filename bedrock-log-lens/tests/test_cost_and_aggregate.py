"""Costing and grouping.

The prices are real: they come from the bundled file generated from the AWS Price List, so
these tests double as a check that the generated data is shaped the way the code expects.
"""

from __future__ import annotations

import pytest

from bedrock_log_lens.aggregate import (
    by_day,
    by_hour,
    by_identity,
    by_model,
    most_expensive,
    totals,
    window,
)
from bedrock_log_lens.catalog import Prices
from bedrock_log_lens.cost import estimate, estimate_all, unpriced_models
from bedrock_log_lens.match import InferenceScope, model_key
from tests.conftest import CLAUDE, HAIKU, NOW, OTHER_ROLE, ROLE, make_record

UNKNOWN_MODEL = "acme.unreleased-model-v1:0"


# ------------------------------------------------------------------------- costing


def test_a_known_model_is_costed_from_the_published_rates(prices: Prices) -> None:
    record = make_record(input_tokens=1_000_000, output_tokens=1_000_000)
    rates = prices.rates_for(CLAUDE, "us-east-1", InferenceScope.REGIONAL)
    assert rates is not None and rates.input is not None and rates.output is not None

    cost = estimate(record, prices)

    assert cost.is_known
    assert cost.usd == pytest.approx(rates.input + rates.output)


def test_cache_tokens_are_priced_at_their_own_rates(prices: Prices) -> None:
    """Folding cache reads into the input count would misprice caching workloads badly."""
    rates = prices.rates_for(CLAUDE, "us-east-1", InferenceScope.REGIONAL)
    assert rates is not None and rates.cache_read is not None
    assert rates.cache_read < (rates.input or 0), "a cache read should be cheaper than input"

    cached = make_record(input_tokens=0, output_tokens=0, cache_read=1_000_000)
    assert estimate(cached, prices).usd == pytest.approx(rates.cache_read)


def test_an_unknown_model_is_unpriced_not_free(prices: Prices) -> None:
    """A zero here would silently shrink every total the request belongs to."""
    cost = estimate(make_record(model_id=UNKNOWN_MODEL), prices)
    assert cost.usd is None
    assert cost.no_price_reason is not None
    assert UNKNOWN_MODEL in cost.no_price_reason


def test_a_region_with_no_prices_says_so(prices: Prices) -> None:
    cost = estimate(make_record(region="mars-north-1"), prices)
    assert cost.usd is None
    assert cost.no_price_reason is not None
    assert "mars-north-1" in cost.no_price_reason


def test_a_global_inference_profile_uses_the_global_rate(prices: Prices) -> None:
    profile = f"global.{CLAUDE}"
    assert model_key(profile).scope is InferenceScope.GLOBAL
    rates = prices.rates_for(profile, "us-east-1", InferenceScope.GLOBAL)
    assert rates is not None
    assert rates.input is not None


def test_a_regional_inference_profile_resolves_to_the_same_model(prices: Prices) -> None:
    assert model_key(f"us.{CLAUDE}").tokens == model_key(CLAUDE).tokens


def test_unpriced_models_are_listed_for_the_report(prices: Prices) -> None:
    costed = estimate_all(
        [make_record(), make_record(model_id=UNKNOWN_MODEL)],
        prices,
    )
    assert unpriced_models(costed) == (UNKNOWN_MODEL,)


# ------------------------------------------------------------------------ grouping


def test_totals_add_up(prices: Prices) -> None:
    costed = estimate_all([make_record(), make_record(), make_record()], prices)
    summary = totals(costed)

    assert summary.requests == 3
    assert summary.input_tokens == 360
    assert summary.output_tokens == 1_020
    assert summary.total_tokens == 1_380
    assert summary.cost_is_complete


def test_a_group_with_an_unpriced_request_says_its_cost_is_incomplete(prices: Prices) -> None:
    costed = estimate_all([make_record(), make_record(model_id=UNKNOWN_MODEL)], prices)
    summary = totals(costed)

    assert summary.requests == 2
    assert summary.unpriced_requests == 1
    assert summary.cost_is_complete is False


def test_grouping_by_model_and_identity(prices: Prices) -> None:
    costed = estimate_all(
        [
            make_record(model_id=CLAUDE, identity=ROLE),
            make_record(model_id=CLAUDE, identity=OTHER_ROLE),
            make_record(model_id=HAIKU, identity=ROLE),
        ],
        prices,
    )

    models = {group.key: group.requests for group in by_model(costed)}
    assert models == {CLAUDE: 2, HAIKU: 1}

    identities = {group.key: group.requests for group in by_identity(costed)}
    assert identities == {ROLE: 2, OTHER_ROLE: 1}


def test_model_groups_are_ordered_by_cost(prices: Prices) -> None:
    """The expensive model should be the first thing a reader sees."""
    costed = estimate_all(
        [
            make_record(model_id=HAIKU, input_tokens=10, output_tokens=10),
            make_record(model_id=CLAUDE, input_tokens=500_000, output_tokens=500_000),
        ],
        prices,
    )
    assert by_model(costed)[0].key == CLAUDE


def test_time_groups_are_in_chronological_order(prices: Prices) -> None:
    """Hours sorted by size would be unreadable."""
    costed = estimate_all(
        [make_record(minutes=0), make_record(minutes=90), make_record(minutes=200)],
        prices,
    )
    hours = [group.key for group in by_hour(costed)]
    assert hours == sorted(hours)
    assert len(hours) == 3
    assert len(by_day(costed)) == 1


def test_a_missing_identity_is_grouped_explicitly(prices: Prices) -> None:
    costed = estimate_all([make_record(identity="")], prices)
    assert by_identity(costed)[0].key == "<no identity recorded>"


def test_the_most_expensive_requests_come_back_in_order(prices: Prices) -> None:
    costed = estimate_all(
        [
            make_record(input_tokens=10, output_tokens=10, request_id="cheap"),
            make_record(input_tokens=900_000, output_tokens=900_000, request_id="dear"),
            make_record(input_tokens=5_000, output_tokens=5_000, request_id="middling"),
        ],
        prices,
    )
    ranked = most_expensive(costed, limit=2)

    assert [record.request_id for record, _cost in ranked] == ["dear", "middling"]


def test_the_most_expensive_list_carries_only_metadata(prices: Prices) -> None:
    """It is safe to print by construction: the record type cannot hold a prompt."""
    import dataclasses

    from bedrock_log_lens.models import content_bearing_fields

    costed = estimate_all([make_record()], prices)
    record, _cost = most_expensive(costed, limit=1)[0]
    names = [field.name for field in dataclasses.fields(record)]
    assert content_bearing_fields(names) == ()


def test_unpriced_requests_cannot_be_the_most_expensive(prices: Prices) -> None:
    """Ranking by an unknown cost would be ranking by nothing."""
    costed = estimate_all([make_record(model_id=UNKNOWN_MODEL)], prices)
    assert most_expensive(costed, limit=5) == ()


def test_the_window_reports_what_was_covered() -> None:
    records = [make_record(minutes=5), make_record(minutes=0), make_record(minutes=60)]
    first, last = window(records)
    assert first == NOW
    assert last is not None and last > first


def test_an_empty_window_is_none_not_now() -> None:
    assert window([]) == (None, None)
