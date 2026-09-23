"""Shared fixtures. Nothing here touches AWS: the tool makes no AWS API calls at all."""

from __future__ import annotations

import copy
from importlib import resources
from typing import Any

import pytest
import yaml

from sagemaker_inference_picker.limits import LimitsData, load_limits


@pytest.fixture(scope="session")
def limits() -> LimitsData:
    """The limits bundled with the package."""
    return load_limits()


@pytest.fixture
def raw_limits() -> dict[str, Any]:
    """A mutable copy of the raw YAML document, for testing validation failures."""
    text = (
        resources.files("sagemaker_inference_picker")
        .joinpath("data/limits.yaml")
        .read_text(encoding="utf-8")
    )
    loaded = yaml.safe_load(text)
    assert isinstance(loaded, dict)
    return copy.deepcopy(loaded)
