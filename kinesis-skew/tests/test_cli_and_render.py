"""The CLI and both output formats.

The scan itself is replaced with a prepared result, so nothing here builds an AWS client.
"""

from __future__ import annotations

import datetime as dt
import io
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from rich.console import Console
from typer.testing import CliRunner

from kinesis_skew import __version__, render
from kinesis_skew.catalog import Catalog
from kinesis_skew.cli import EXIT_BAD_DATA, EXIT_OK, app
from kinesis_skew.diagnose import diagnose
from kinesis_skew.models import (
    CapacityMode,
    ScanResult,
    ShardTraffic,
    StreamReport,
)
from kinesis_skew.stats import statistics, utilisation
from tests.conftest import make_traffic

EXIT_USAGE = 2
NOW = dt.datetime(2026, 9, 24, 12, 0, tzinfo=dt.UTC)
runner = CliRunner(env={"COLUMNS": "200", "TERM": "dumb", "NO_COLOR": "1"})


def _report(
    traffic: list[ShardTraffic], catalog: Catalog, *, mode: CapacityMode = CapacityMode.PROVISIONED
) -> StreamReport:
    scored = utilisation(traffic, catalog.aws, catalog.thresholds)
    stats = statistics(scored, catalog.thresholds)
    return StreamReport(
        stream_name="orders",
        region="eu-west-1",
        capacity_mode=mode,
        open_shard_count=len(traffic),
        shard_level_metrics=("ALL",),
        window_start=NOW - dt.timedelta(hours=24),
        window_end=NOW,
        utilisation=scored,
        statistics=stats,
        diagnosis=diagnose(
            capacity_mode=mode,
            scored=scored,
            stats=stats,
            missing_metrics=(),
            aws=catalog.aws,
            thresholds=catalog.thresholds,
        ),
        total_throttled_records=sum(item.throttled_records for item in scored),
    )


def _skewed(catalog: Catalog) -> StreamReport:
    traffic = [make_traffic(f"shardId-{i:012d}", peak_fraction=0.02) for i in range(1, 8)]
    traffic.append(make_traffic("shardId-000000000000", peak_fraction=0.99, throttled=41_000))
    return _report(traffic, catalog)


def _result(*reports: StreamReport) -> ScanResult:
    return ScanResult(
        reports=tuple(reports), region="eu-west-1", lookback_hours=24.0, started_at=NOW
    )


def _render(result: ScanResult, width: int = 120, show_all: bool = True) -> str:
    stream = io.StringIO()
    render.render_result(Console(file=stream, width=width, no_color=True), result, show_all)
    return stream.getvalue()


# ------------------------------------------------------------------------- the report


def test_the_report_leads_with_the_verdict_and_the_remedy(catalog: Catalog) -> None:
    output = _render(_result(_skewed(catalog)))
    assert "skew" in output
    assert "What to do" in output
    assert "will not help" in output


def test_every_shard_gets_a_bar(catalog: Catalog) -> None:
    """Skew is a shape. The bars are how it becomes obvious before any statistic is read."""
    output = _render(_result(_skewed(catalog)))
    assert output.count("█") > 0
    assert "Busiest minute" in output
    assert "99%" in output


def test_the_statistics_are_shown_under_the_bars(catalog: Catalog) -> None:
    output = _render(_result(_skewed(catalog)))
    for label in ("busiest/mean", "hottest share", "Gini", "CV"):
        assert label in output


def test_a_closed_shard_is_marked_as_closed(catalog: Catalog) -> None:
    traffic = [
        make_traffic("shardId-000000000000", peak_fraction=0.4, is_open=False),
        make_traffic("shardId-000000000001", peak_fraction=0.4),
    ]
    assert "closed" in _render(_result(_report(traffic, catalog)))


def test_unranked_shards_are_explained_rather_than_hidden(catalog: Catalog) -> None:
    traffic = [make_traffic(f"shardId-{i:012d}", peak_fraction=0.3) for i in range(3)]
    traffic.append(make_traffic("shardId-000000000009", peak_fraction=0.3, observed_minutes=60))
    output = _render(_result(_report(traffic, catalog)))
    assert "not ranked" in output
    assert "shown but not ranked" in output


def test_a_clean_region_says_so_once(catalog: Catalog) -> None:
    healthy = _report(
        [make_traffic(f"shardId-{i:012d}", peak_fraction=0.2) for i in range(3)], catalog
    )
    output = _render(_result(healthy), show_all=False)
    assert "Nothing throttled" in output
    assert "Busiest minute" not in output


@pytest.mark.parametrize("width", [70, 100, 120, 200])
def test_the_report_fits_the_terminal(catalog: Catalog, width: int) -> None:
    for line in _render(_result(_skewed(catalog)), width=width).splitlines():
        assert len(line) <= width, f"{len(line)} > {width}: {line!r}"


# -------------------------------------------------------------------------- the JSON


def test_json_carries_the_verdict_evidence_and_every_shard(catalog: Catalog) -> None:
    payload = render.result_to_dict(_result(_skewed(catalog)), catalog.disclaimer)
    stream = payload["streams"][0]

    assert stream["diagnosis"]["verdict"] == "skew"
    assert stream["diagnosis"]["is_problem"] is True
    assert stream["diagnosis"]["evidence"]
    assert stream["diagnosis"]["remedy"]
    assert len(stream["shards"]) == 8
    assert stream["statistics"]["hottest_shard_id"] == "shardId-000000000000"
    assert payload["counts"]["skew"] == 1
    assert payload["note"] == catalog.disclaimer


def test_json_says_which_metrics_are_missing(catalog: Catalog) -> None:
    report = _report([make_traffic("shardId-000000000000")], catalog)
    blind = StreamReport(**{**report.__dict__, "shard_level_metrics": ("IncomingBytes",)})
    payload = render.stream_to_dict(blind)
    assert payload["shard_level_metrics_missing"] == [
        "IncomingRecords",
        "WriteProvisionedThroughputExceeded",
    ]


def test_json_is_serialisable(catalog: Catalog) -> None:
    """A dict that json.dumps refuses is not machine-readable output."""
    json.dumps(render.result_to_dict(_result(_skewed(catalog)), catalog.disclaimer))


def test_a_stream_with_no_statistics_reports_null_rather_than_zeros(catalog: Catalog) -> None:
    single = _report([make_traffic("shardId-000000000000", peak_fraction=0.9)], catalog)
    payload = render.stream_to_dict(single)
    assert payload["statistics"] is None


# --------------------------------------------------------------------------- the CLI


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == EXIT_OK
    assert __version__ in result.output


def test_policy_prints_the_generated_iam_document() -> None:
    result = runner.invoke(app, ["policy"])
    assert result.exit_code == EXIT_OK
    assert "kinesis:ListShards" in result.output
    assert "KinesisSkewReadOnly" in result.output


def test_the_printed_policy_matches_the_enforced_allowlist(catalog: Catalog) -> None:
    """The README quotes this. If it could drift from the code it would eventually be wrong."""
    result = runner.invoke(app, ["policy", "--output", "json"])
    payload = json.loads(result.output)

    printed = set(payload["iam_policy"]["Statement"][0]["Action"])
    enforced = {
        f"{service}:{operation}"
        for service, operations in catalog.aws.operations_for(sampling=False).items()
        for operation in operations
    }
    assert printed == enforced

    with_sampling = set(payload["iam_policy_with_sampling"]["Statement"][1]["Action"])
    assert with_sampling == {"kinesis:GetShardIterator", "kinesis:GetRecords"}


def test_the_sampling_permissions_are_a_separate_statement() -> None:
    """Nobody should have to grant GetRecords to run the ordinary scan."""
    payload = json.loads(runner.invoke(app, ["policy", "--output", "json"]).output)
    base = payload["iam_policy"]["Statement"][0]["Action"]
    assert not any("GetRecords" in action for action in base)
    assert len(payload["iam_policy_with_sampling"]["Statement"]) == 2


def test_a_lookback_cloudwatch_cannot_answer_is_refused() -> None:
    """Past 15 days CloudWatch returns nothing, which would read as an idle stream."""
    result = runner.invoke(app, ["scan", "--region", "eu-west-1", "--lookback-hours", "999"])
    assert result.exit_code == EXIT_USAGE
    assert "one-minute data" in result.output


def test_a_broken_data_file_is_reported_not_ignored(tmp_path: Path) -> None:
    from importlib import resources

    raw: dict[str, Any] = yaml.safe_load(
        resources.files("kinesis_skew").joinpath("data/limits.yaml").read_text(encoding="utf-8")
    )
    del raw["thresholds"]["skew_gini"]["rationale"]
    path = tmp_path / "limits.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    result = runner.invoke(app, ["policy", "--limits-file", str(path)])

    assert result.exit_code == EXIT_BAD_DATA
    assert "rationale" in result.output


def test_thresholds_are_shown_so_a_verdict_can_be_argued_with() -> None:
    result = runner.invoke(app, ["policy"])
    assert "skew_max_to_mean_ratio" in result.output
    assert "capacity_utilisation" in result.output


def test_the_sampling_statement_is_scoped_to_streams() -> None:
    """GetRecords can be scoped to stream ARNs, unlike ListStreams and GetMetricData.

    The README quotes this statement verbatim, so a change here has to be deliberate.
    """
    payload = json.loads(runner.invoke(app, ["policy", "--output", "json"]).output)
    base, sampling = payload["iam_policy_with_sampling"]["Statement"]

    assert base["Resource"] == "*"
    assert sampling["Resource"] == "arn:aws:kinesis:*:*:stream/*"
    assert sampling["Action"] == ["kinesis:GetRecords", "kinesis:GetShardIterator"]
