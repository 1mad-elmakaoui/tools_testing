"""Regenerate docs/demo.svg from a real run of the tool.

The log records are invented here and written to a temporary directory in Bedrock's own
layout, so the image can be rebuilt by anyone with no AWS account and no log files.
Everything downstream is the real thing: the same reader, parser, costing, heuristics and
renderer the CLI runs.

The invented records carry prompt and response bodies, exactly as real ones do. That is
deliberate — the demo exercises the path that drops them, and the image is evidence that
none of it reaches the report.

    .venv/bin/python scripts/make_demo.py
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import random
import tempfile
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.text import Text

from bedrock_log_lens.analyse import analyse
from bedrock_log_lens.catalog import load_policy, load_prices
from bedrock_log_lens.read import read_directory
from bedrock_log_lens.render import render_report

TOOL_ROOT = Path(__file__).resolve().parent.parent
OUTPUT = TOOL_ROOT / "docs" / "demo.svg"
WIDTH = 104

#: A fixed clock and a fixed seed keep the SVG identical between runs, so a rebuild with no
#: code change produces no diff.
NOW = dt.datetime(2026, 9, 24, 9, 0, tzinfo=dt.UTC)
SEED = 20260924

SONNET = "anthropic.claude-sonnet-4-20250514-v1:0"
HAIKU = "anthropic.claude-3-haiku-20240307-v1:0"

SUPPORT = "arn:aws:sts::123456789012:assumed-role/SupportAssistantRole/session-3a1"
BATCH = "arn:aws:sts::123456789012:assumed-role/NightlyEnrichmentRole/run-118"
AGENT = "arn:aws:sts::123456789012:assumed-role/TriageAgentRole/agent-7f2"


def record(
    *,
    when: dt.datetime,
    request_id: str,
    model_id: str,
    identity: str,
    input_tokens: int,
    output_tokens: int,
) -> dict[str, Any]:
    """One log record in the documented shape, prompt and response included."""
    return {
        "schemaType": "ModelInvocationLog",
        "schemaVersion": "1.0",
        "timestamp": when.isoformat().replace("+00:00", "Z"),
        "accountId": "123456789012",
        "region": "us-east-1",
        "requestId": request_id,
        "operation": "Converse",
        "modelId": model_id,
        "identity": {"arn": identity},
        "input": {
            "inputContentType": "application/json",
            "inputTokenCount": input_tokens,
            "inputBodyJson": {
                "messages": [{"role": "user", "content": [{"text": "…the customer's message…"}]}]
            },
        },
        "output": {
            "outputContentType": "application/json",
            "outputTokenCount": output_tokens,
            "outputBodyJson": {
                "output": {
                    "message": {
                        "role": "assistant",
                        "content": [{"text": "…the model's reply…"}],
                    }
                },
                "amazon-bedrock-invocationMetrics": {
                    "inputTokenCount": input_tokens,
                    "outputTokenCount": output_tokens,
                    "invocationLatency": 1_450,
                    "firstByteLatency": 310,
                },
            },
        },
    }


def build_records() -> list[dict[str, Any]]:
    """An afternoon in an account: real traffic, a batch job, and an agent in a loop."""
    rng = random.Random(SEED)
    records: list[dict[str, Any]] = []

    for index in range(260):
        records.append(
            record(
                when=NOW + dt.timedelta(seconds=index * 11),
                request_id=f"chat-{index:04d}",
                model_id=SONNET,
                identity=SUPPORT,
                input_tokens=rng.randint(400, 3_000),
                output_tokens=rng.randint(80, 900),
            )
        )

    for index in range(400):
        records.append(
            record(
                when=NOW + dt.timedelta(seconds=index * 7),
                request_id=f"enrich-{index:04d}",
                model_id=HAIKU,
                identity=BATCH,
                input_tokens=rng.randint(200, 600),
                output_tokens=rng.randint(20, 120),
            )
        )

    records.append(
        record(
            when=NOW + dt.timedelta(minutes=30),
            request_id="contract-review-1",
            model_id=SONNET,
            identity=SUPPORT,
            input_tokens=180_000,
            output_tokens=12_000,
        )
    )

    # The agent re-sends the same context: the input size stops changing.
    for index in range(48):
        records.append(
            record(
                when=NOW + dt.timedelta(minutes=45, seconds=index * 1.1),
                request_id=f"triage-{index:03d}",
                model_id=SONNET,
                identity=AGENT,
                input_tokens=31_744,
                output_tokens=96,
            )
        )

    return records


def main() -> None:
    """Render the demo and write the SVG."""
    with tempfile.TemporaryDirectory() as workspace:
        directory = (
            Path(workspace)
            / "AWSLogs/123456789012/BedrockModelInvocationLogs/us-east-1/2026/09/24/09"
        )
        directory.mkdir(parents=True)
        lines = [json.dumps(item) for item in build_records()]
        # One truncated line and one record from another service, because real directories
        # have them and the report should say so.
        lines.append('{"schemaType": "ModelInvocationLog", "timestamp": "2026-09-24T09')
        lines.append(json.dumps({"schemaType": "SomethingElse", "timestamp": "2026-09-24"}))
        (directory / "invocations.json.gz").write_bytes(
            gzip.compress(("\n".join(lines) + "\n").encode("utf-8"))
        )

        policy, prices = load_policy(), load_prices()
        outcome = read_directory(Path(workspace))
        report = analyse(outcome, policy, prices, sources_read=1)

    console = Console(record=True, width=WIDTH)
    prompt = Text()
    prompt.append("$ ", style="bold green")
    prompt.append("bedrock-log-lens analyse ./bedrock-logs")
    console.print(prompt)
    render_report(console, report, policy.disclaimer)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    console.save_svg(str(OUTPUT), title="bedrock-log-lens")
    print(f"wrote {OUTPUT.relative_to(TOOL_ROOT)} ({OUTPUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
