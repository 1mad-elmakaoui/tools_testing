"""Percentiles, outliers, and the three anomaly heuristics.

The loop detector is the one worth reading. An agent stuck re-sending the same context
produces a run of calls with an identical input token count, and that is visible in the
metadata without reading a prompt — which is the point of the whole tool.
"""

from __future__ import annotations

import pytest

from bedrock_log_lens.anomaly import detect
from bedrock_log_lens.catalog import Policy, Prices, Thresholds
from bedrock_log_lens.cost import estimate_all
from bedrock_log_lens.models import AnomalyKind, InvocationRecord
from bedrock_log_lens.stats import outliers, percentile, percentiles_by_model
from tests.conftest import CLAUDE, HAIKU, OTHER_ROLE, ROLE, make_record

# ---------------------------------------------------------------------- percentiles


def test_nearest_rank_returns_a_value_that_actually_occurred() -> None:
    """There is no such thing as 4,096.5 tokens, so no interpolation."""
    sample = [10, 20, 30, 40]
    for percent in (25, 50, 75, 100):
        assert percentile(sample, percent) in sample


@pytest.mark.parametrize(
    ("percent", "expected"),
    [(50, 5), (95, 10), (99, 10), (100, 10), (10, 1)],
)
def test_known_percentiles(percent: float, expected: int) -> None:
    assert percentile(list(range(1, 11)), percent) == expected


def test_a_single_sample_is_every_percentile() -> None:
    assert percentile([7], 50) == percentile([7], 99) == 7


def test_an_empty_sample_raises_rather_than_returning_zero() -> None:
    """Zero is a plausible token count, so it must not double as "no data"."""
    with pytest.raises(ValueError, match="empty sample"):
        percentile([], 50)


@pytest.mark.parametrize("percent", [0, -1, 101])
def test_an_impossible_percentile_is_refused(percent: float) -> None:
    with pytest.raises(ValueError, match="percent"):
        percentile([1, 2, 3], percent)


def test_percentiles_are_reported_per_model() -> None:
    records = [make_record(model_id=CLAUDE, input_tokens=100) for _ in range(30)]
    records += [make_record(model_id=HAIKU, input_tokens=10) for _ in range(5)]

    summaries = {item.model_id: item for item in percentiles_by_model(records)}

    assert summaries[CLAUDE].p50 == 100
    assert summaries[CLAUDE].count == 30
    assert summaries[HAIKU].p50 == 10


def test_a_thin_sample_is_marked_as_such() -> None:
    """With five requests a p99 is the largest one wearing a statistical hat."""
    thin = percentiles_by_model([make_record(input_tokens=i) for i in range(1, 6)])[0]
    assert thin.count == 5
    assert thin.is_meaningful is False

    thick = percentiles_by_model([make_record(input_tokens=i) for i in range(1, 40)])[0]
    assert thick.is_meaningful is True


def test_records_without_token_counts_are_left_out_of_the_distribution() -> None:
    records = [make_record(input_tokens=None) for _ in range(5)]
    records += [make_record(input_tokens=50) for _ in range(3)]
    assert percentiles_by_model(records)[0].count == 3


# ------------------------------------------------------------------------ outliers


def test_a_huge_request_is_flagged_against_its_model(policy: Policy) -> None:
    records = [make_record(input_tokens=100, request_id=f"normal-{i}") for i in range(120)]
    records.append(make_record(input_tokens=500_000, request_id="whopper"))

    found = outliers(records, policy.thresholds)

    assert found
    assert found[0].record.request_id == "whopper"
    assert found[0].kind == "input"
    assert found[0].value == 500_000


def test_output_outliers_are_found_too(policy: Policy) -> None:
    records = [make_record(output_tokens=50, request_id=f"n-{i}") for i in range(120)]
    records.append(make_record(output_tokens=90_000, request_id="runaway"))

    kinds = {item.kind for item in outliers(records, policy.thresholds)}
    assert "output" in kinds


def test_a_model_with_too_few_requests_has_no_outliers(policy: Policy) -> None:
    """Otherwise the largest of three requests is an outlier against itself."""
    records = [make_record(input_tokens=tokens) for tokens in (10, 20, 900_000)]
    assert outliers(records, policy.thresholds) == ()


# ------------------------------------------------------------------------ anomalies


def burst_records(
    count: int, *, identity: str = ROLE, spacing: float = 0.5
) -> list[InvocationRecord]:
    return [
        make_record(seconds=index * spacing, identity=identity, request_id=f"b-{index}")
        for index in range(count)
    ]


def test_a_burst_of_calls_from_one_identity_is_flagged(policy: Policy, prices: Prices) -> None:
    costed = estimate_all(burst_records(100), prices)
    found = [item for item in detect(costed, policy.thresholds) if item.kind is AnomalyKind.BURST]

    assert found
    assert found[0].identity_arn == ROLE
    assert found[0].request_count >= policy.thresholds.burst_request_count
    assert found[0].cost_usd > 0
    assert found[0].calls_per_minute > 0


def test_a_quiet_identity_is_not_flagged(policy: Policy, prices: Prices) -> None:
    costed = estimate_all(
        [make_record(minutes=index * 10, request_id=f"q-{index}") for index in range(20)],
        prices,
    )
    assert detect(costed, policy.thresholds) == ()


def test_one_burst_produces_one_finding_not_one_per_call(policy: Policy, prices: Prices) -> None:
    """A finding per call inside a burst would bury the report in its own output."""
    costed = estimate_all(burst_records(200), prices)
    bursts = [item for item in detect(costed, policy.thresholds) if item.kind is AnomalyKind.BURST]
    assert len(bursts) <= 3


def test_a_loop_is_caught_by_its_identical_request_shape(policy: Policy, prices: Prices) -> None:
    """An agent re-sending the same context sends the same input size, over and over."""
    looping = [
        make_record(seconds=index * 2, input_tokens=8_192, request_id=f"loop-{index}")
        for index in range(40)
    ]
    costed = estimate_all(looping, prices)

    found = [
        item
        for item in detect(costed, policy.thresholds)
        if item.kind is AnomalyKind.REPEATED_SHAPE
    ]

    assert found
    assert found[0].model_id == CLAUDE
    assert "8,192 tokens" in found[0].detail
    assert "loop" in found[0].detail


def test_a_healthy_conversation_is_not_a_loop(policy: Policy, prices: Prices) -> None:
    """A real conversation grows, so its input size changes on every turn."""
    growing = [
        make_record(seconds=index * 2, input_tokens=1_000 + index * 250, request_id=f"g-{index}")
        for index in range(40)
    ]
    costed = estimate_all(growing, prices)

    found = [
        item
        for item in detect(costed, policy.thresholds)
        if item.kind is AnomalyKind.REPEATED_SHAPE
    ]
    assert found == []


def test_a_rate_jump_is_measured_against_the_identity_own_baseline(
    policy: Policy, prices: Prices
) -> None:
    """A steady high-volume caller should not be flagged simply for being busy."""
    quiet = [
        make_record(minutes=index, seconds=0, input_tokens=100, request_id=f"quiet-{index}")
        for index in range(12)
    ]
    spike = [
        make_record(minutes=20, seconds=index * 0.5, input_tokens=100, request_id=f"spike-{index}")
        for index in range(40)
    ]
    costed = estimate_all(quiet + spike, prices)

    found = [
        item for item in detect(costed, policy.thresholds) if item.kind is AnomalyKind.RATE_JUMP
    ]

    assert found
    assert found[0].request_count == 40
    assert "jump" in found[0].detail


def test_no_baseline_means_no_rate_jump_finding(policy: Policy, prices: Prices) -> None:
    """A median needs something to be the middle of."""
    costed = estimate_all(burst_records(80), prices)
    found = [
        item for item in detect(costed, policy.thresholds) if item.kind is AnomalyKind.RATE_JUMP
    ]
    assert found == []


def test_identities_are_judged_separately(policy: Policy, prices: Prices) -> None:
    """Two busy callers are two findings, not one merged one."""
    costed = estimate_all(
        burst_records(90, identity=ROLE) + burst_records(90, identity=OTHER_ROLE),
        prices,
    )
    identities = {item.identity_arn for item in detect(costed, policy.thresholds)}
    assert identities == {ROLE, OTHER_ROLE}


def test_thresholds_can_be_loosened(policy: Policy, prices: Prices) -> None:
    """A batch job and an interactive assistant look nothing alike."""
    costed = estimate_all(burst_records(70), prices)
    assert detect(costed, policy.thresholds)

    relaxed: Thresholds = policy.thresholds.overridden(burst_count=1_000)
    bursts = [item for item in detect(costed, relaxed) if item.kind is AnomalyKind.BURST]
    assert bursts == []
