"""The published page must stay in step with limits.yaml.

`docs/index.html` is generated and committed, so GitHub Pages can serve it with no build
step. A committed generated file can go stale, which would quietly leave the website
advising against numbers the CLI no longer uses. This fails the build if that happens.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from sagemaker_inference_picker.limits import LimitsData
from sagemaker_inference_picker.models import Option
from scripts.build_page import OUTPUT, build

#: The monorepo root: this tool lives one level below it.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture(scope="session")
def page() -> str:
    """The committed page."""
    if not OUTPUT.is_file():
        pytest.fail(f"{OUTPUT} is missing; run python scripts/build_page.py")
    return OUTPUT.read_text(encoding="utf-8")


def test_the_committed_page_is_up_to_date(page: str) -> None:
    assert page == build(), (
        "docs/index.html is out of date with limits.yaml, web/page.html or web/engine.mjs. "
        "Run: python scripts/build_page.py"
    )


def test_no_placeholder_survives_the_build(page: str) -> None:
    assert "@@" not in page


def test_the_page_makes_no_network_requests(page: str) -> None:
    """A self-contained page keeps the 'nothing leaves this page' promise true."""
    for pattern in (r"<script[^>]+\bsrc=", r"<link[^>]+\bhref=", r"@import\b", r"\bfetch\("):
        assert not re.search(pattern, page), f"page references something external: {pattern}"


def test_the_page_carries_the_real_limits(page: str, limits: LimitsData) -> None:
    match = re.search(r'<script type="application/json" id="limitsData">(.*?)</script>', page, re.S)
    assert match, "the limits data block is missing"
    data = json.loads(match.group(1).encode().decode("unicode_escape"))
    assert data["last_verified"] == limits.last_verified
    assert set(data["options"]) == {option.value for option in Option}
    serverless = data["options"]["serverless"]["limits"]["max_request_payload_mb"]
    assert serverless["value"] == limits.limits_for(Option.SERVERLESS).max_request_payload_mb.value
    assert serverless["source"].startswith("https://")


def test_the_engine_is_inlined(page: str) -> None:
    assert "export function recommend(workload, limits)" in page


def test_jekyll_is_disabled_for_the_pages_site() -> None:
    """Without this, GitHub Pages runs the page through Jekyll before serving it."""
    assert (REPO_ROOT / "docs" / ".nojekyll").is_file()
