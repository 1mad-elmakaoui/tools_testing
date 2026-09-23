"""Regenerate docs/demo.svg from a real run of the tool.

The demo in the README is exported from actual output rather than mocked up, so it cannot
drift away from what the tool prints. Run it after any change to the renderers:

    .venv/bin/python scripts/make_demo.py
"""

from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.text import Text

from sagemaker_inference_picker.engine import recommend
from sagemaker_inference_picker.limits import load_limits
from sagemaker_inference_picker.models import TrafficPattern, Workload
from sagemaker_inference_picker.render import render_recommendation

OUTPUT = Path(__file__).resolve().parent.parent / "docs" / "demo.svg"
WIDTH = 104

DEMO_COMMAND = (
    "sagemaker-inference-picker recommend \\\n"
    "    --payload-mb 12 --response-mb 3 --processing-seconds 240 \\\n"
    "    --traffic bursty-idle --zero-idle-cost --needs-notification --gpu"
)

DEMO_WORKLOAD = Workload(
    payload_mb=12,
    response_mb=3,
    processing_seconds=240,
    traffic=TrafficPattern.BURSTY_IDLE,
    zero_idle_cost=True,
    needs_notification=True,
    gpu_required=True,
)


def main() -> None:
    """Render the demo workload and write the SVG."""
    console = Console(record=True, width=WIDTH)
    prompt = Text()
    prompt.append("$ ", style="bold green")
    prompt.append(DEMO_COMMAND)
    console.print(prompt)

    limits = load_limits()
    render_recommendation(console, recommend(DEMO_WORKLOAD, limits), limits)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    console.save_svg(str(OUTPUT), title="sagemaker-inference-picker")
    print(f"wrote {OUTPUT} ({OUTPUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
