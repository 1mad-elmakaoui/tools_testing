"""Regenerate docs/demo.svg from a real run of the tool.

The scan is driven by in-memory AWS responses describing an invented stream, so the image
can be rebuilt by anyone without credentials. Everything downstream of those responses is
the real thing: the same collect, metrics, stats, diagnose and render code the CLI runs.

    .venv/bin/python scripts/make_demo.py
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.text import Text

from kinesis_skew import render
from kinesis_skew.aws import ReadOnlyClient, RetryPolicy
from kinesis_skew.catalog import load_catalog
from kinesis_skew.scan import scan_stream

TOOL_ROOT = Path(__file__).resolve().parent.parent
OUTPUT = TOOL_ROOT / "docs" / "demo.svg"
WIDTH = 112
STREAM = "orders-ingest"
REGION = "eu-west-1"

#: A fixed "now" keeps the SVG identical between runs, so a rebuild with no code change
#: produces no diff.
NOW = dt.datetime(2026, 9, 24, 9, 0, tzinfo=dt.UTC)
WINDOW_MINUTES = 24 * 60
BYTES_PER_MINUTE = 1048576 * 60
RECORDS_PER_MINUTE = 1000 * 60

#: shard suffix -> (fraction of the write limit in its busiest minute, records rejected)
SHARDS: dict[int, tuple[float, float]] = {
    0: (0.98, 412_000.0),
    1: (0.04, 0.0),
    2: (0.05, 0.0),
    3: (0.06, 0.0),
    4: (0.07, 0.0),
    5: (0.08, 0.0),
    6: (0.09, 0.0),
    7: (0.10, 0.0),
}

#: What the sampler finds on the hot shard: one tenant, and a long tail.
SAMPLE = [{"PartitionKey": "tenant-acme"} for _ in range(880)] + [
    {"PartitionKey": f"tenant-{index}"} for index in range(120)
]


def _shard_id(suffix: int) -> str:
    return f"shardId-{suffix:012d}"


class _Kinesis:
    """Answers the read calls the scan makes, plus the two sampling ones."""

    def describe_stream_summary(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "StreamDescriptionSummary": {
                "StreamName": kwargs["StreamName"],
                "StreamModeDetails": {"StreamMode": "PROVISIONED"},
                "OpenShardCount": len(SHARDS),
                "EnhancedMonitoring": [{"ShardLevelMetrics": ["ALL"]}],
            }
        }

    def list_shards(self, **_: Any) -> dict[str, Any]:
        return {
            "Shards": [
                {
                    "ShardId": _shard_id(suffix),
                    "SequenceNumberRange": {"StartingSequenceNumber": "1"},
                }
                for suffix in SHARDS
            ]
        }

    def get_shard_iterator(self, **_: Any) -> dict[str, Any]:
        return {"ShardIterator": "iterator-1"}

    def get_records(self, **_: Any) -> dict[str, Any]:
        return {"Records": SAMPLE, "NextShardIterator": None, "MillisBehindLatest": 0}


class _CloudWatch:
    """Answers GetMetricData from the per-shard fractions above."""

    def get_metric_data(self, **kwargs: Any) -> dict[str, Any]:
        results = []
        for query in kwargs["MetricDataQueries"]:
            stat = query["MetricStat"]
            name = stat["Metric"]["MetricName"]
            shard = next(
                item["Value"] for item in stat["Metric"]["Dimensions"] if item["Name"] == "ShardId"
            )
            fraction, throttled = SHARDS[int(shard.removeprefix("shardId-"))]
            if name == "IncomingBytes":
                values = [BYTES_PER_MINUTE * fraction] * WINDOW_MINUTES
            elif name == "IncomingRecords":
                values = [RECORDS_PER_MINUTE * fraction] * WINDOW_MINUTES
            else:
                values = [throttled / WINDOW_MINUTES] * WINDOW_MINUTES
            results.append({"Id": query["Id"], "Values": values, "Timestamps": []})
        return {"MetricDataResults": results}


def main() -> None:
    """Render the demo and write the SVG."""
    catalog = load_catalog()
    retry = RetryPolicy(max_retries=1, base_delay_seconds=0.0, sleep=lambda _: None)
    operations = catalog.aws.operations_for(sampling=True)
    clients = {
        "kinesis": ReadOnlyClient(
            _Kinesis(),  # type: ignore[arg-type]
            "kinesis",
            operations["kinesis"],
            retry,
        ),
        "cloudwatch": ReadOnlyClient(
            _CloudWatch(),  # type: ignore[arg-type]
            "cloudwatch",
            operations["cloudwatch"],
            retry,
        ),
    }

    report = scan_stream(
        clients,
        STREAM,
        REGION,
        catalog,
        NOW - dt.timedelta(hours=24),
        NOW,
        catalog.thresholds,
        sample_keys=True,
    )

    console = Console(record=True, width=WIDTH)
    prompt = Text()
    prompt.append("$ ", style="bold green")
    prompt.append(f"kinesis-skew scan --stream {STREAM} --sample-keys")
    console.print(prompt)
    render.render_stream(console, report)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    console.save_svg(str(OUTPUT), title="kinesis-skew")
    print(f"wrote {OUTPUT.relative_to(TOOL_ROOT)} ({OUTPUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
