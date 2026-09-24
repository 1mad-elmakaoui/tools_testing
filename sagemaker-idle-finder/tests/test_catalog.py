"""Integrity of policy.yaml and prices.yaml, and the loader that enforces it."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
import yaml

from sagemaker_idle_finder.catalog import (
    CatalogError,
    Policy,
    Prices,
    load_policy,
    load_prices,
)


def test_every_aws_fact_cites_a_source(raw_policy: dict[str, Any]) -> None:
    for key, entry in raw_policy["aws"].items():
        assert entry["source"].startswith("https://"), f"aws.{key} has no source URL"
    for key, entry in raw_policy["limits"].items():
        assert entry["source"].startswith("https://"), f"limits.{key} has no source URL"


def test_every_threshold_carries_a_rationale(raw_policy: dict[str, Any]) -> None:
    for key, entry in raw_policy["thresholds"].items():
        assert entry.get("rationale", "").strip(), f"thresholds.{key} has no rationale"


def test_no_threshold_pretends_to_be_an_aws_fact(raw_policy: dict[str, Any]) -> None:
    """A rationale and a source mean different things; a threshold must not claim both."""
    for key, entry in raw_policy["thresholds"].items():
        assert "source" not in entry, f"thresholds.{key} is a judgement, not a documented fact"


def test_the_allowlist_contains_only_reads(policy: Policy) -> None:
    """Every operation must be a describe, list or get."""
    for service, operations in policy.aws.allowed_operations.items():
        for operation in operations:
            assert operation.startswith(("Describe", "List", "Get")), (
                f"{service}:{operation} does not look read-only"
            )


def test_the_allowlist_is_exactly_the_five_documented_operations(policy: Policy) -> None:
    flat = {
        f"{service}:{operation}"
        for service, operations in policy.aws.allowed_operations.items()
        for operation in operations
    }
    assert flat == {
        "sagemaker:ListEndpoints",
        "sagemaker:DescribeEndpoint",
        "sagemaker:DescribeEndpointConfig",
        "cloudwatch:GetMetricData",
        "application-autoscaling:DescribeScalableTargets",
    }


def test_last_verified_is_an_iso_date(policy: Policy) -> None:
    assert dt.date.fromisoformat(policy.last_verified).year >= 2024


def test_prices_record_their_publication_date(prices: Prices) -> None:
    assert prices.publication_date
    assert prices.source.startswith("https://")
    assert len(prices.regions) > 10


def test_prices_cover_common_instance_types(prices: Prices) -> None:
    for region in ("eu-west-1", "us-east-1"):
        assert prices.instance_hour(region, "ml.m5.large") is not None
        assert prices.instance_hour(region, "ml.m5.xlarge") is not None


def test_an_unknown_price_is_none_rather_than_zero(prices: Prices) -> None:
    assert prices.instance_hour("eu-west-1", "ml.nope.xlarge") is None
    assert prices.instance_hour("mars-north-1", "ml.m5.large") is None


def test_serverless_prices_cover_every_memory_size(prices: Prices) -> None:
    """Serverless offers 1 GB to 6 GB; a gap would make an estimate silently unavailable."""
    for memory_gb in range(1, 7):
        assert prices.serverless_second("eu-west-1", memory_gb) is not None


# ------------------------------------------------------------------- loader failures


def test_a_missing_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(CatalogError, match="file not found"):
        load_policy(tmp_path / "absent.yaml")
    with pytest.raises(CatalogError, match="file not found"):
        load_prices(tmp_path / "absent.yaml")


def test_invalid_yaml_is_reported(tmp_path: Path) -> None:
    target = tmp_path / "policy.yaml"
    target.write_text("key: [unclosed\n", encoding="utf-8")
    with pytest.raises(CatalogError, match="not valid YAML"):
        load_policy(target)


def _write(tmp_path: Path, document: dict[str, Any]) -> Path:
    target = tmp_path / "policy.yaml"
    target.write_text(yaml.safe_dump(document), encoding="utf-8")
    return target


def test_an_unsupported_schema_version_is_rejected(
    tmp_path: Path, raw_policy: dict[str, Any]
) -> None:
    raw_policy["schema_version"] = 99
    with pytest.raises(CatalogError, match="unsupported schema_version"):
        load_policy(_write(tmp_path, raw_policy))


def test_an_aws_fact_without_a_source_is_rejected(
    tmp_path: Path, raw_policy: dict[str, Any]
) -> None:
    del raw_policy["aws"]["metric_namespace"]["source"]
    with pytest.raises(CatalogError, match="must carry a 'source' URL"):
        load_policy(_write(tmp_path, raw_policy))


def test_the_allowlist_without_a_source_is_rejected(
    tmp_path: Path, raw_policy: dict[str, Any]
) -> None:
    del raw_policy["aws"]["allowed_operations"]["source"]
    with pytest.raises(CatalogError, match="must carry a 'source' URL"):
        load_policy(_write(tmp_path, raw_policy))


def test_an_empty_allowlist_is_rejected(tmp_path: Path, raw_policy: dict[str, Any]) -> None:
    raw_policy["aws"]["allowed_operations"]["value"] = {}
    with pytest.raises(CatalogError, match="must not be empty"):
        load_policy(_write(tmp_path, raw_policy))


def test_a_threshold_without_a_rationale_is_rejected(
    tmp_path: Path, raw_policy: dict[str, Any]
) -> None:
    del raw_policy["thresholds"]["underused_invocations_per_instance_hour"]["rationale"]
    with pytest.raises(CatalogError, match="must carry a non-empty 'rationale'"):
        load_policy(_write(tmp_path, raw_policy))


def test_a_missing_section_is_reported(tmp_path: Path, raw_policy: dict[str, Any]) -> None:
    del raw_policy["limits"]
    with pytest.raises(CatalogError, match="limits is missing"):
        load_policy(_write(tmp_path, raw_policy))


def test_empty_prices_are_rejected(tmp_path: Path, raw_prices: dict[str, Any]) -> None:
    raw_prices["hosting_instance_hour_usd"] = {}
    target = tmp_path / "prices.yaml"
    target.write_text(yaml.safe_dump(raw_prices), encoding="utf-8")
    with pytest.raises(CatalogError, match="must not be empty"):
        load_prices(target)


def test_a_non_numeric_price_is_rejected(tmp_path: Path, raw_prices: dict[str, Any]) -> None:
    raw_prices["hosting_instance_hour_usd"]["eu-west-1"]["ml.m5.large"] = "cheap"
    target = tmp_path / "prices.yaml"
    target.write_text(yaml.safe_dump(raw_prices), encoding="utf-8")
    with pytest.raises(CatalogError, match="must be a number"):
        load_prices(target)


def test_errors_name_the_file(tmp_path: Path, raw_policy: dict[str, Any]) -> None:
    raw_policy["schema_version"] = 42
    target = _write(tmp_path, raw_policy)
    with pytest.raises(CatalogError, match=str(target.name)):
        load_policy(target)
