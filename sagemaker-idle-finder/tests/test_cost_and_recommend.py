"""Costing and remedies: both pure, both exercised without AWS."""

from __future__ import annotations

import pytest

from sagemaker_idle_finder.catalog import Policy, Prices
from sagemaker_idle_finder.cost import (
    estimate,
    serverless_monthly_estimate,
    with_serverless_what_if,
)
from sagemaker_idle_finder.models import Remedy, ServerlessWhatIf, Verdict
from sagemaker_idle_finder.recommend import recommend
from tests.conftest import make_variant, make_window

REGION = "eu-west-1"


# --------------------------------------------------------------------------- costing


def test_an_idle_variant_wastes_its_whole_cost(policy: Policy, prices: Prices) -> None:
    variant = make_variant(instance_count=2)
    result = estimate(REGION, variant, Verdict.IDLE, make_window(0.0), prices, policy.thresholds)
    hourly = prices.instance_hour(REGION, "ml.m5.large")
    assert hourly is not None
    expected = round(hourly * 2 * policy.thresholds.hours_per_month, 2)
    assert result.monthly_usd == expected
    assert result.wasted_monthly_usd == expected


def test_a_healthy_variant_wastes_nothing(policy: Policy, prices: Prices) -> None:
    result = estimate(
        REGION, make_variant(), Verdict.HEALTHY, make_window(500_000.0), prices, policy.thresholds
    )
    assert result.monthly_usd is not None
    assert result.wasted_monthly_usd == 0.0


def test_an_underused_variant_wastes_only_the_surplus(policy: Policy, prices: Prices) -> None:
    """Four instances, traffic justifying one: three are surplus, not all four."""
    variant = make_variant(instance_count=4)
    result = estimate(
        REGION, variant, Verdict.UNDERUSED, make_window(100.0), prices, policy.thresholds
    )
    hourly = prices.instance_hour(REGION, "ml.m5.large")
    assert hourly is not None
    assert result.wasted_monthly_usd == round(hourly * 3 * policy.thresholds.hours_per_month, 2)
    assert result.monthly_usd == round(hourly * 4 * policy.thresholds.hours_per_month, 2)


def test_a_single_underused_instance_reports_no_figure_rather_than_zero(
    policy: Policy, prices: Prices
) -> None:
    """An endpoint that exists needs one instance, so there is no surplus to strip.

    Reporting 0.0 here would put a row in the table flagged as waste and priced at nothing,
    which reads as "there is nothing to reclaim". The cost is real; what cannot be
    established is how much of it a replacement would give back.
    """
    result = estimate(
        REGION,
        make_variant(instance_count=1),
        Verdict.UNDERUSED,
        make_window(100.0),
        prices,
        policy.thresholds,
    )
    assert result.wasted_monthly_usd is None
    assert result.monthly_usd is not None and result.monthly_usd > 0
    assert result.no_estimate_reason is not None
    assert "cannot run on fewer" in result.no_estimate_reason


def test_a_serverless_variant_has_no_standing_cost(policy: Policy, prices: Prices) -> None:
    result = estimate(
        REGION,
        make_variant(serverless_memory_mb=2048),
        Verdict.NOT_APPLICABLE,
        None,
        prices,
        policy.thresholds,
    )
    assert result.monthly_usd is None
    assert result.wasted_monthly_usd == 0.0
    assert result.no_estimate_reason is not None
    assert "per request" in result.no_estimate_reason


def test_an_unknown_instance_type_reports_why_rather_than_zero(
    policy: Policy, prices: Prices
) -> None:
    """A missing price must not silently understate the total."""
    variant = make_variant(instance_type="ml.imaginary.xlarge")
    result = estimate(REGION, variant, Verdict.IDLE, make_window(0.0), prices, policy.thresholds)
    assert result.monthly_usd is None
    assert result.wasted_monthly_usd is None
    assert result.no_estimate_reason is not None
    assert "ml.imaginary.xlarge" in result.no_estimate_reason


def test_an_unknown_region_reports_why(policy: Policy, prices: Prices) -> None:
    result = estimate(
        "mars-north-1", make_variant(), Verdict.IDLE, make_window(0.0), prices, policy.thresholds
    )
    assert result.monthly_usd is None
    assert result.no_estimate_reason is not None


def test_a_variant_with_no_instances_has_nothing_to_price(policy: Policy, prices: Prices) -> None:
    result = estimate(
        REGION,
        make_variant(instance_count=0),
        Verdict.NOT_APPLICABLE,
        None,
        prices,
        policy.thresholds,
    )
    assert result.monthly_usd is None
    assert result.wasted_monthly_usd == 0.0


def test_the_serverless_alternative_can_be_priced(policy: Policy, prices: Prices) -> None:
    monthly = serverless_monthly_estimate(REGION, 1000.0, 0.5, 2, prices, 336.0, policy.thresholds)
    assert monthly is not None
    assert monthly > 0


def test_the_serverless_alternative_is_none_where_unpriced(policy: Policy, prices: Prices) -> None:
    assert (
        serverless_monthly_estimate(
            "mars-north-1", 1000.0, 0.5, 2, prices, 336.0, policy.thresholds
        )
        is None
    )


# -------------------------------------------------------------------------- remedies


def test_an_idle_endpoint_is_proposed_for_deletion(policy: Policy) -> None:
    remedy, why = recommend(
        make_variant(), Verdict.IDLE, make_window(0.0), policy.thresholds, policy.aws
    )
    assert remedy is Remedy.DELETE
    assert "should exist at all" in why


def test_surplus_instances_are_proposed_for_removal(policy: Policy) -> None:
    remedy, why = recommend(
        make_variant(instance_count=6),
        Verdict.UNDERUSED,
        make_window(100.0),
        policy.thresholds,
        policy.aws,
    )
    assert remedy is Remedy.REDUCE_INSTANCE_COUNT
    assert "surplus" in why


def test_a_small_thinly_used_instance_is_proposed_for_serverless(policy: Policy) -> None:
    variant = make_variant(instance_type="ml.m5.large", instance_count=1)
    remedy, why = recommend(
        variant, Verdict.UNDERUSED, make_window(100.0), policy.thresholds, policy.aws
    )
    assert remedy is Remedy.MOVE_TO_SERVERLESS
    assert "bills per request" in why


@pytest.mark.parametrize("instance_type", ["ml.g5.xlarge", "ml.p4d.24xlarge", "ml.inf2.xlarge"])
def test_an_accelerated_instance_is_proposed_for_async(policy: Policy, instance_type: str) -> None:
    """Serverless supports no GPU, so the remedy has to be the other one."""
    variant = make_variant(instance_type=instance_type, instance_count=1)
    remedy, why = recommend(
        variant, Verdict.UNDERUSED, make_window(100.0), policy.thresholds, policy.aws
    )
    assert remedy is Remedy.MOVE_TO_ASYNC_SCALE_TO_ZERO
    assert "scale-to-zero" in why


def test_a_large_instance_is_proposed_for_async(policy: Policy) -> None:
    """Serverless tops out at 6 GB, so a big box cannot simply move there."""
    variant = make_variant(instance_type="ml.m5.12xlarge", instance_count=1)
    remedy, _ = recommend(
        variant, Verdict.UNDERUSED, make_window(100.0), policy.thresholds, policy.aws
    )
    assert remedy is Remedy.MOVE_TO_ASYNC_SCALE_TO_ZERO


def test_an_unrecognisable_instance_size_falls_back_to_async(policy: Policy) -> None:
    variant = make_variant(instance_type="ml.weird.gigantic", instance_count=1)
    remedy, _ = recommend(
        variant, Verdict.UNDERUSED, make_window(100.0), policy.thresholds, policy.aws
    )
    assert remedy is Remedy.MOVE_TO_ASYNC_SCALE_TO_ZERO


def test_a_healthy_variant_gets_no_remedy(policy: Policy) -> None:
    remedy, why = recommend(
        make_variant(), Verdict.HEALTHY, make_window(500_000.0), policy.thresholds, policy.aws
    )
    assert remedy is Remedy.NONE
    assert why == ""


def test_a_too_new_variant_gets_no_remedy(policy: Policy) -> None:
    remedy, _ = recommend(
        make_variant(), Verdict.TOO_NEW, make_window(0.0), policy.thresholds, policy.aws
    )
    assert remedy is Remedy.NONE


# ---------------------------------------------------- the serverless what-if

WHAT_IF = ServerlessWhatIf(seconds_per_invocation=0.5, memory_gb=2)


def test_the_what_if_quantifies_waste_that_instance_counting_could_not(
    policy: Policy, prices: Prices
) -> None:
    """The single-instance case gets a figure once the caller supplies the assumptions."""
    variant = make_variant(instance_count=1)
    window = make_window(100.0)
    base = estimate(REGION, variant, Verdict.UNDERUSED, window, prices, policy.thresholds)
    assert base.wasted_monthly_usd is None

    costed = with_serverless_what_if(base, REGION, window, WHAT_IF, prices, policy.thresholds)
    assert costed.serverless_monthly_usd is not None
    assert costed.wasted_monthly_usd is not None
    assert costed.no_estimate_reason is None
    # The waste is what the instance costs, less what the same traffic would cost per request.
    assert base.monthly_usd is not None
    assert costed.wasted_monthly_usd == round(base.monthly_usd - costed.serverless_monthly_usd, 2)


def test_the_what_if_never_reduces_waste_below_zero(policy: Policy, prices: Prices) -> None:
    """Heavy assumed traffic can cost more on serverless; that is not negative waste."""
    variant = make_variant(instance_count=1)
    window = make_window(50_000_000.0)
    base = estimate(REGION, variant, Verdict.UNDERUSED, window, prices, policy.thresholds)
    costed = with_serverless_what_if(
        base, REGION, window, ServerlessWhatIf(10.0, 6), prices, policy.thresholds
    )
    assert costed.wasted_monthly_usd == 0.0


def test_the_what_if_leaves_an_established_surplus_alone(policy: Policy, prices: Prices) -> None:
    """Where instances can be removed, that remains the waste; serverless is extra context."""
    variant = make_variant(instance_count=4)
    window = make_window(100.0)
    base = estimate(REGION, variant, Verdict.UNDERUSED, window, prices, policy.thresholds)
    costed = with_serverless_what_if(base, REGION, window, WHAT_IF, prices, policy.thresholds)
    assert costed.wasted_monthly_usd == base.wasted_monthly_usd
    assert costed.serverless_monthly_usd is not None


def test_a_region_without_serverless_prices_adds_nothing(policy: Policy, prices: Prices) -> None:
    variant = make_variant(instance_count=1)
    window = make_window(100.0)
    base = estimate(REGION, variant, Verdict.UNDERUSED, window, prices, policy.thresholds)
    costed = with_serverless_what_if(
        base, REGION, window, ServerlessWhatIf(0.5, 999), prices, policy.thresholds
    )
    assert costed.serverless_monthly_usd is None
    assert costed.wasted_monthly_usd is None


def test_the_what_if_needs_a_window(policy: Policy, prices: Prices) -> None:
    base = estimate(
        REGION, make_variant(instance_count=1), Verdict.UNDERUSED, None, prices, policy.thresholds
    )
    assert with_serverless_what_if(base, REGION, None, WHAT_IF, prices, policy.thresholds) is base
