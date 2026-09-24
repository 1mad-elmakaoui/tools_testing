"""Metric batching: the arithmetic that keeps requests inside the documented quotas."""

from __future__ import annotations

import datetime as dt

import pytest

from sagemaker_idle_finder.catalog import AwsFacts, Limits, Policy
from sagemaker_idle_finder.metrics import (
    MetricTarget,
    batch_size,
    build_queries,
    datapoints_per_query,
    fetch_invocations,
)
from tests.conftest import NOW, WINDOW_START
from tests.test_aws import FakeClient, _client

HOUR = 3600


def _limits() -> Limits:
    return Limits(max_metric_queries_per_call=500, max_datapoints_per_call=100_800)


def test_datapoints_per_query_over_a_fortnight() -> None:
    assert datapoints_per_query(WINDOW_START, NOW, HOUR) == 336


def test_a_zero_period_is_rejected() -> None:
    with pytest.raises(ValueError, match="period_seconds must be positive"):
        datapoints_per_query(WINDOW_START, NOW, 0)


def test_a_short_window_still_has_a_datapoint() -> None:
    assert datapoints_per_query(NOW, NOW, HOUR) == 1


def test_the_datapoint_cap_binds_before_the_query_cap() -> None:
    """336 datapoints per query means 300 fit, not the 500 the other cap would allow."""
    assert batch_size(WINDOW_START, NOW, HOUR, _limits()) == 300


def test_a_short_lookback_is_bound_by_the_query_cap() -> None:
    one_day = NOW - dt.timedelta(days=1)
    assert batch_size(one_day, NOW, HOUR, _limits()) == 500


def test_a_long_lookback_shrinks_the_batch() -> None:
    ninety = NOW - dt.timedelta(days=90)
    assert batch_size(ninety, NOW, HOUR, _limits()) == 46


def test_the_batch_never_drops_below_one() -> None:
    tiny = Limits(max_metric_queries_per_call=500, max_datapoints_per_call=1)
    assert batch_size(WINDOW_START, NOW, HOUR, tiny) == 1


def test_queries_carry_the_documented_namespace_and_dimensions(policy: Policy) -> None:
    targets = [MetricTarget("orders", "AllTraffic")]
    queries = build_queries(targets, policy.aws, HOUR)
    assert len(queries) == 1
    stat = queries[0]["MetricStat"]
    assert isinstance(stat, dict)
    assert stat["Metric"]["Namespace"] == "AWS/SageMaker"
    assert stat["Metric"]["MetricName"] == "Invocations"
    assert stat["Metric"]["Dimensions"] == [
        {"Name": "EndpointName", "Value": "orders"},
        {"Name": "VariantName", "Value": "AllTraffic"},
    ]
    assert stat["Stat"] == "Sum"


def test_query_ids_are_unique_and_start_with_a_letter(policy: Policy) -> None:
    """CloudWatch rejects an id that does not start with a lowercase letter."""
    targets = [MetricTarget(f"e{index}", "AllTraffic") for index in range(5)]
    ids = [str(query["Id"]) for query in build_queries(targets, policy.aws, HOUR)]
    assert len(set(ids)) == len(ids)
    assert all(identifier[0].islower() for identifier in ids)


def _facts(policy: Policy) -> AwsFacts:
    return policy.aws


def test_fetching_nothing_calls_nothing(policy: Policy) -> None:
    fake = FakeClient()
    client = _client(fake)
    assert fetch_invocations(client, [], WINDOW_START, NOW, _facts(policy), _limits(), HOUR) == {}
    assert fake.calls == []


def test_values_are_summed_across_the_window(policy: Policy) -> None:
    fake = FakeClient(
        {"get_metric_data": {"MetricDataResults": [{"Id": "m0", "Values": [1.0, 2.0, 3.0]}]}}
    )
    client = _client(fake, service="cloudwatch", allowed=frozenset({"GetMetricData"}))
    windows = fetch_invocations(
        client, [MetricTarget("e", "v")], WINDOW_START, NOW, _facts(policy), _limits(), HOUR
    )
    assert windows[("e", "v")].total_invocations == 6.0
    assert windows[("e", "v")].datapoints == 3


def test_an_empty_values_list_means_no_data_not_zero(policy: Policy) -> None:
    """The distinction the whole 'too new' guard depends on."""
    fake = FakeClient({"get_metric_data": {"MetricDataResults": [{"Id": "m0", "Values": []}]}})
    client = _client(fake, service="cloudwatch", allowed=frozenset({"GetMetricData"}))
    windows = fetch_invocations(
        client, [MetricTarget("e", "v")], WINDOW_START, NOW, _facts(policy), _limits(), HOUR
    )
    assert windows[("e", "v")].total_invocations is None


def test_a_measured_zero_is_kept_as_zero(policy: Policy) -> None:
    fake = FakeClient(
        {"get_metric_data": {"MetricDataResults": [{"Id": "m0", "Values": [0.0, 0.0]}]}}
    )
    client = _client(fake, service="cloudwatch", allowed=frozenset({"GetMetricData"}))
    windows = fetch_invocations(
        client, [MetricTarget("e", "v")], WINDOW_START, NOW, _facts(policy), _limits(), HOUR
    )
    assert windows[("e", "v")].total_invocations == 0.0


def test_results_are_paged_with_next_token(policy: Policy) -> None:
    fake = FakeClient(
        {
            "get_metric_data": [
                {"MetricDataResults": [{"Id": "m0", "Values": [1.0]}], "NextToken": "t"},
                {"MetricDataResults": [{"Id": "m0", "Values": [2.0]}]},
            ]
        }
    )
    client = _client(fake, service="cloudwatch", allowed=frozenset({"GetMetricData"}))
    windows = fetch_invocations(
        client, [MetricTarget("e", "v")], WINDOW_START, NOW, _facts(policy), _limits(), HOUR
    )
    assert windows[("e", "v")].total_invocations == 3.0


def test_targets_are_split_across_calls_when_they_exceed_the_batch(policy: Policy) -> None:
    tiny = Limits(max_metric_queries_per_call=2, max_datapoints_per_call=100_800)
    fake = FakeClient(
        {
            "get_metric_data": [
                {
                    "MetricDataResults": [
                        {"Id": "m0", "Values": [1.0]},
                        {"Id": "m1", "Values": [2.0]},
                    ]
                },
                {"MetricDataResults": [{"Id": "m0", "Values": [3.0]}]},
            ]
        }
    )
    client = _client(fake, service="cloudwatch", allowed=frozenset({"GetMetricData"}))
    targets = [MetricTarget("a", "v"), MetricTarget("b", "v"), MetricTarget("c", "v")]
    windows = fetch_invocations(client, targets, WINDOW_START, NOW, _facts(policy), tiny, HOUR)
    assert windows[("a", "v")].total_invocations == 1.0
    assert windows[("b", "v")].total_invocations == 2.0
    assert windows[("c", "v")].total_invocations == 3.0
    assert len([call for call in fake.calls if call[0] == "get_metric_data"]) == 2
