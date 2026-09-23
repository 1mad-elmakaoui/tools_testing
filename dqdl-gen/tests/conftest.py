"""Shared fixtures. Nothing here reaches AWS: the core tool makes no AWS calls at all."""

from __future__ import annotations

import copy
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
import yaml

from dqdl_gen.catalog import Catalog, load_catalog

FIXTURES = Path(__file__).resolve().parent / "fixtures"
GOLDEN = Path(__file__).resolve().parent / "golden"


@pytest.fixture(scope="session")
def catalog() -> Catalog:
    """The catalogue bundled with the package."""
    return load_catalog()


@pytest.fixture
def raw_catalog() -> dict[str, Any]:
    """A mutable copy of the raw YAML document, for testing validation failures."""
    text = resources.files("dqdl_gen").joinpath("data/dqdl.yaml").read_text(encoding="utf-8")
    loaded = yaml.safe_load(text)
    assert isinstance(loaded, dict)
    return copy.deepcopy(loaded)


@pytest.fixture(scope="session")
def orders_csv() -> Path:
    """A 500-row CSV with a key, two categorical columns, a measure with nulls, free text."""
    return FIXTURES / "orders.csv"


@pytest.fixture(scope="session")
def tiny_csv() -> Path:
    """A 12-row CSV, below the row threshold for allowed-value rules."""
    return FIXTURES / "tiny.csv"


@pytest.fixture(scope="session")
def sensors_parquet() -> Path:
    """A 300-row Parquet file with numeric, boolean and constant columns."""
    return FIXTURES / "sensors.parquet"


@pytest.fixture(scope="session")
def orders_profile(orders_csv: Path, catalog: Catalog) -> Any:
    """The orders fixture, profiled once for the whole session."""
    from dqdl_gen.profile import profile_table
    from dqdl_gen.reader import read_table

    return profile_table(
        read_table(orders_csv),
        max_value_set=catalog.shape.max_allowed_value_set,
        top_values_shown=catalog.shape.top_values_shown,
    )
