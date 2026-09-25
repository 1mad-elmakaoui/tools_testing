"""The data file, and the discipline it is supposed to enforce.

These tests exist because a number without a source is how a tool starts being confidently
wrong. Breaking the file on purpose is the only way to know the loader would notice.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from kinesis_skew.catalog import Catalog, CatalogError, load_catalog


@pytest.fixture
def raw() -> dict[str, Any]:
    from importlib import resources

    text = resources.files("kinesis_skew").joinpath("data/limits.yaml").read_text(encoding="utf-8")
    loaded: dict[str, Any] = yaml.safe_load(text)
    return loaded


def _write(tmp_path: Path, document: dict[str, Any]) -> Path:
    path = tmp_path / "limits.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


# --------------------------------------------------------------- the file as shipped


def test_every_aws_fact_cites_a_source(raw: dict[str, Any]) -> None:
    for key, entry in raw["aws"].items():
        assert isinstance(entry.get("source"), str), f"aws.{key} has no source"
        assert entry["source"].startswith("http"), f"aws.{key} source is not a URL"
    for key, entry in raw["limits"].items():
        assert entry.get("source", "").startswith("http"), f"limits.{key} has no source"


def test_every_threshold_carries_a_rationale(raw: dict[str, Any]) -> None:
    for key, entry in raw["thresholds"].items():
        rationale = entry.get("rationale")
        assert isinstance(rationale, str) and rationale.strip(), f"thresholds.{key}"


def test_no_threshold_pretends_to_be_an_aws_fact(raw: dict[str, Any]) -> None:
    """A source URL on a judgement call would dress up an opinion as documentation."""
    for key, entry in raw["thresholds"].items():
        assert "source" not in entry, f"thresholds.{key} cites a source; it is our choice"


def test_the_documented_shard_limits_are_the_published_ones(catalog: Catalog) -> None:
    """1 MB/s and 1,000 records/s per shard. If these are wrong, everything else is."""
    assert catalog.aws.shard_write_bytes_per_second == 1024 * 1024
    assert catalog.aws.shard_write_records_per_second == 1000
    assert catalog.aws.shard_metric_period_seconds == 60


def test_the_allowlist_holds_only_reads(catalog: Catalog) -> None:
    forbidden = ("Create", "Delete", "Update", "Put", "Merge", "Split", "Enable", "Disable")
    for service, operations in catalog.aws.operations_for(sampling=True).items():
        for operation in operations:
            assert not operation.startswith(forbidden), f"{service}:{operation} is not a read"


def test_sampling_operations_are_absent_unless_asked_for(catalog: Catalog) -> None:
    """The flag builds the allowlist, so a plain run cannot reach GetRecords at all."""
    plain = catalog.aws.operations_for(sampling=False)
    assert "GetRecords" not in plain["kinesis"]
    assert "GetShardIterator" not in plain["kinesis"]

    sampling = catalog.aws.operations_for(sampling=True)
    assert "GetRecords" in sampling["kinesis"]
    assert "GetShardIterator" in sampling["kinesis"]
    # Asking for sampling must not quietly drop anything else.
    assert plain["kinesis"] <= sampling["kinesis"]
    assert plain["cloudwatch"] == sampling["cloudwatch"]


def test_last_verified_is_an_iso_date(catalog: Catalog) -> None:
    import datetime as dt

    dt.date.fromisoformat(catalog.last_verified)


# ------------------------------------------------------------------ breaking it on purpose


def test_an_aws_fact_without_a_source_is_rejected(tmp_path: Path, raw: dict[str, Any]) -> None:
    del raw["aws"]["shard_write_bytes_per_second"]["source"]
    with pytest.raises(CatalogError, match="source"):
        load_catalog(_write(tmp_path, raw))


def test_a_threshold_without_a_rationale_is_rejected(tmp_path: Path, raw: dict[str, Any]) -> None:
    del raw["thresholds"]["skew_gini"]["rationale"]
    with pytest.raises(CatalogError, match="rationale"):
        load_catalog(_write(tmp_path, raw))


def test_the_sampling_allowlist_needs_a_source_too(tmp_path: Path, raw: dict[str, Any]) -> None:
    del raw["aws"]["sampling_operations"]["source"]
    with pytest.raises(CatalogError, match="source"):
        load_catalog(_write(tmp_path, raw))


def test_an_empty_allowlist_is_rejected(tmp_path: Path, raw: dict[str, Any]) -> None:
    raw["aws"]["allowed_operations"]["value"] = {}
    with pytest.raises(CatalogError, match="must not be empty"):
        load_catalog(_write(tmp_path, raw))


def test_a_non_numeric_limit_is_rejected(tmp_path: Path, raw: dict[str, Any]) -> None:
    raw["aws"]["shard_write_records_per_second"]["value"] = "one thousand"
    with pytest.raises(CatalogError, match="must be a number"):
        load_catalog(_write(tmp_path, raw))


def test_an_unsupported_schema_version_is_rejected(tmp_path: Path, raw: dict[str, Any]) -> None:
    raw["schema_version"] = 99
    with pytest.raises(CatalogError, match="schema_version"):
        load_catalog(_write(tmp_path, raw))


def test_a_missing_section_is_reported(tmp_path: Path, raw: dict[str, Any]) -> None:
    del raw["thresholds"]
    with pytest.raises(CatalogError, match="thresholds is missing"):
        load_catalog(_write(tmp_path, raw))


def test_a_missing_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(CatalogError, match="file not found"):
        load_catalog(tmp_path / "nope.yaml")


def test_invalid_yaml_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "limits.yaml"
    path.write_text("aws: [unclosed\n", encoding="utf-8")
    with pytest.raises(CatalogError, match="not valid YAML"):
        load_catalog(path)


def test_errors_name_the_file(tmp_path: Path, raw: dict[str, Any]) -> None:
    del raw["aws"]["metric_namespace"]["source"]
    path = _write(tmp_path, raw)
    with pytest.raises(CatalogError, match=str(path.name)):
        load_catalog(path)


# ------------------------------------------------------------------------- overrides


def test_thresholds_can_be_overridden_one_at_a_time(catalog: Catalog) -> None:
    changed = catalog.thresholds.overridden(gini=0.9)
    assert changed.skew_gini == 0.9
    assert changed.skew_max_to_mean_ratio == catalog.thresholds.skew_max_to_mean_ratio
    assert catalog.thresholds.skew_gini != 0.9, "the original must not be mutated"


def test_no_overrides_returns_the_same_thresholds(catalog: Catalog) -> None:
    assert catalog.thresholds.overridden() is catalog.thresholds
