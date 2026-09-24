"""Regenerate docs/demo.svg from a real run of the tool.

The scan is driven by in-memory AWS clients holding an invented account, so the image can
be rebuilt by anyone without credentials. Everything downstream of those responses is the
real thing: the same collect, classify, cost, recommend and render code the CLI runs.

    .venv/bin/python scripts/make_demo.py
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.text import Text

from sagemaker_idle_finder import render
from sagemaker_idle_finder.aws import ReadOnlyClient, RetryPolicy
from sagemaker_idle_finder.catalog import load_policy, load_prices
from sagemaker_idle_finder.scan import scan

TOOL_ROOT = Path(__file__).resolve().parent.parent
OUTPUT = TOOL_ROOT / "docs" / "demo.svg"
WIDTH = 130
REGION = "eu-west-1"

#: A fixed "now" keeps the SVG identical between runs, so a rebuild with no code change
#: produces no diff.
NOW = dt.datetime(2026, 9, 22, 9, 0, tzinfo=dt.UTC)
LOOKBACK_DAYS = 14

#: name -> (instance type, count, invocations over the window, age in days)
ACCOUNT: dict[str, tuple[str | None, int, float | None, int]] = {
    "fraud-scoring-prod": ("ml.m5.xlarge", 3, 4_812_006.0, 420),
    "recsys-candidate-gen": ("ml.g5.xlarge", 2, 0.0, 190),
    "churn-model-v3": ("ml.m5.large", 2, 0.0, 260),
    "pricing-batch-scorer": ("ml.c5.2xlarge", 4, 611.0, 300),
    "nlp-sandbox-dev": ("ml.m5.large", 1, 38.0, 95),
    "summariser-serverless": (None, 1, 1240.0, 150),
    "vision-rollout-canary": ("ml.m5.large", 1, 12.0, 4),
}


class _Fake:
    """Answers the five read-only calls the scan makes, from ACCOUNT."""

    def __init__(self, service: str) -> None:
        self.service = service

    def list_endpoints(self, **_: Any) -> dict[str, Any]:
        return {"Endpoints": [{"EndpointName": name} for name in ACCOUNT]}

    def describe_endpoint(self, **kwargs: Any) -> dict[str, Any]:
        name = kwargs["EndpointName"]
        instance_type, count, _, age = ACCOUNT[name]
        variant: dict[str, Any] = {"VariantName": "AllTraffic"}
        if instance_type is None:
            variant["CurrentServerlessConfig"] = {"MemorySizeInMB": 3072, "MaxConcurrency": 10}
        else:
            variant["CurrentInstanceCount"] = count
        return {
            "EndpointName": name,
            "EndpointStatus": "InService",
            "EndpointConfigName": f"{name}-config",
            "CreationTime": NOW - dt.timedelta(days=age),
            "ProductionVariants": [variant],
        }

    def describe_endpoint_config(self, **kwargs: Any) -> dict[str, Any]:
        name = kwargs["EndpointConfigName"].removesuffix("-config")
        instance_type, count, _, _ = ACCOUNT[name]
        variant: dict[str, Any] = {"VariantName": "AllTraffic", "ModelName": f"{name}-model"}
        if instance_type is None:
            variant["ServerlessConfig"] = {"MemorySizeInMB": 3072, "MaxConcurrency": 10}
        else:
            variant["InstanceType"] = instance_type
            variant["InitialInstanceCount"] = count
        return {"EndpointConfigName": kwargs["EndpointConfigName"], "ProductionVariants": [variant]}

    def get_metric_data(self, **kwargs: Any) -> dict[str, Any]:
        results = []
        for query in kwargs["MetricDataQueries"]:
            dimensions = query["MetricStat"]["Metric"]["Dimensions"]
            name = next(d["Value"] for d in dimensions if d["Name"] == "EndpointName")
            total = ACCOUNT[name][2]
            results.append(
                {"Id": query["Id"], "Values": [] if total is None else [total], "Timestamps": []}
            )
        return {"MetricDataResults": results}

    def describe_scalable_targets(self, **_: Any) -> dict[str, Any]:
        return {"ScalableTargets": []}


def _clients(_region: str) -> dict[str, ReadOnlyClient]:
    policy = load_policy()
    retry = RetryPolicy(max_retries=1, base_delay_seconds=0.0, sleep=lambda _: None)
    return {
        service: ReadOnlyClient(
            _Fake(service),  # type: ignore[arg-type]
            service,
            operations,
            retry,
            None,
        )
        for service, operations in policy.aws.allowed_operations.items()
    }


def main() -> None:
    """Render the demo and write the SVG."""
    policy, prices = load_policy(), load_prices()
    result = scan(_clients, [REGION], policy, prices, NOW, LOOKBACK_DAYS, None)

    console = Console(record=True, width=WIDTH)
    prompt = Text()
    prompt.append("$ ", style="bold green")
    prompt.append("sagemaker-idle-finder scan --regions eu-west-1 --explain")
    console.print(prompt)
    render.render_result(console, result, show_all=False)
    render.render_detail(console, result)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    console.save_svg(str(OUTPUT), title="sagemaker-idle-finder")
    print(f"wrote {OUTPUT.relative_to(TOOL_ROOT)} ({OUTPUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
