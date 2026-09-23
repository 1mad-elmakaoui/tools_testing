"""The static page must reach the same decisions as the CLI.

`web/engine.mjs` is a second implementation of the rules, written in JavaScript so the
GitHub Pages page can run without a server. Two implementations can drift, so this feeds
both the same workloads and fails the build if any decision differs.

Parity is defined over the *decision*: which option is recommended, the ranking and scores,
which constraint eliminated which option, which advice fires, and the conflict report.
Prose wording is not compared, since the page is free to phrase things for a web context.

Skipped when Node is not installed, so a contributor without it can still run the suite.
The CI runner always has Node, so the check is never silently skipped there.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from sagemaker_inference_picker.engine import recommend
from sagemaker_inference_picker.limits import LimitsData
from sagemaker_inference_picker.models import Workload
from sagemaker_inference_picker.render import limits_to_dict
from tests.test_engine_scenarios import SCENARIOS

TOOL_ROOT = Path(__file__).resolve().parent.parent
RUNNER = TOOL_ROOT / "web" / "parity_runner.mjs"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _workload_for_js(workload: Workload) -> dict[str, Any]:
    """Translate a Workload into the shape engine.mjs expects."""
    return {
        "payloadMb": workload.payload_mb,
        "responseMb": workload.response_mb,
        "processingSeconds": workload.processing_seconds,
        "traffic": workload.traffic.value,
        "latencyP99Ms": workload.latency_p99_ms,
        "zeroIdleCost": workload.zero_idle_cost,
        "immediateResponse": workload.immediate_response,
        "needsNotification": workload.needs_notification,
        "gpuRequired": workload.gpu_required,
        "modelCount": workload.model_count,
    }


def _python_decision(workload: Workload, limits: LimitsData) -> dict[str, Any]:
    result = recommend(workload, limits)
    return {
        "recommended": result.recommended.value if result.recommended else None,
        "resolved": result.resolved,
        "ranked": [[item.option.value, item.score] for item in result.ranked],
        "eliminations": [[item.option.value, item.constraint_id] for item in result.eliminations],
        "advice": [item.advice_id for item in result.advice],
        "conflicts": [
            [item.constraint_id, [option.value for option in item.eliminated]]
            for item in result.conflicts
        ],
    }


@pytest.fixture(scope="session")
def javascript_decisions(limits: LimitsData, tmp_path_factory: pytest.TempPathFactory) -> Any:
    """Every scenario's decision, as computed by the JavaScript engine under Node."""
    assert NODE is not None
    workdir = tmp_path_factory.mktemp("parity")
    limits_path = workdir / "limits.json"
    workloads_path = workdir / "workloads.json"
    limits_path.write_text(json.dumps(limits_to_dict(limits)), encoding="utf-8")
    workloads_path.write_text(
        json.dumps([_workload_for_js(scenario.workload) for scenario in SCENARIOS]),
        encoding="utf-8",
    )
    # Fixed argv, no shell, and every path is one this fixture just created.
    completed = subprocess.run(
        [NODE, str(RUNNER), str(limits_path), str(workloads_path)],
        capture_output=True,
        text=True,
        check=False,
        cwd=TOOL_ROOT,
    )
    assert completed.returncode == 0, f"node failed:\n{completed.stderr}"
    return json.loads(completed.stdout)


def test_the_runner_produced_one_decision_per_scenario(javascript_decisions: Any) -> None:
    assert len(javascript_decisions) == len(SCENARIOS)


@pytest.mark.parametrize("index", range(len(SCENARIOS)), ids=[s.name for s in SCENARIOS])
def test_javascript_matches_python(
    index: int, javascript_decisions: Any, limits: LimitsData
) -> None:
    scenario = SCENARIOS[index]
    expected = _python_decision(scenario.workload, limits)
    actual = javascript_decisions[index]
    assert actual == expected, (
        f"the page and the CLI disagree on {scenario.name!r}\n"
        f"  python: {json.dumps(expected, indent=2)}\n"
        f"  js:     {json.dumps(actual, indent=2)}"
    )
