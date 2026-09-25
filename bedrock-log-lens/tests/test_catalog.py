"""The data files, and the discipline they are supposed to enforce.

A number without a source is how a tool starts being confidently wrong, and a threshold
without a rationale is an opinion wearing a fact's clothes. Breaking the files on purpose
is the only way to know the loader would notice.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from bedrock_log_lens.catalog import CatalogError, Policy, Prices, load_policy, load_prices
from bedrock_log_lens.match import InferenceScope


def _raw(name: str) -> dict[str, Any]:
    from importlib import resources

    text = resources.files("bedrock_log_lens").joinpath(f"data/{name}").read_text(encoding="utf-8")
    loaded: dict[str, Any] = yaml.safe_load(text)
    return loaded


@pytest.fixture
def raw_policy() -> dict[str, Any]:
    return _raw("policy.yaml")


@pytest.fixture
def raw_prices() -> dict[str, Any]:
    return _raw("prices.yaml")


def _write(tmp_path: Path, document: dict[str, Any], name: str = "policy.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


# ------------------------------------------------------------- the files as shipped


def test_every_aws_fact_cites_a_source(raw_policy: dict[str, Any]) -> None:
    for key, entry in raw_policy["aws"].items():
        source = entry.get("source")
        assert isinstance(source, str) and source.startswith("http"), f"aws.{key}"


def test_every_threshold_carries_a_rationale(raw_policy: dict[str, Any]) -> None:
    for key, entry in raw_policy["thresholds"].items():
        rationale = entry.get("rationale")
        assert isinstance(rationale, str) and rationale.strip(), f"thresholds.{key}"


def test_no_threshold_pretends_to_be_an_aws_fact(raw_policy: dict[str, Any]) -> None:
    """A source URL on a judgement call would dress an opinion up as documentation."""
    for key, entry in raw_policy["thresholds"].items():
        assert "source" not in entry, f"thresholds.{key} cites a source; it is our choice"


def test_the_content_fields_are_the_two_the_schema_uses(policy: Policy) -> None:
    """If this list is wrong, the parser drops the wrong thing and keeps a prompt."""
    assert set(policy.aws.content_fields) == {"inputBodyJson", "outputBodyJson"}


def test_the_content_field_list_matches_the_one_the_parser_uses(policy: Policy) -> None:
    from bedrock_log_lens.models import CONTENT_FIELDS

    assert set(policy.aws.content_fields) == set(CONTENT_FIELDS)


def test_the_allowlist_holds_only_reads(policy: Policy) -> None:
    forbidden = ("Put", "Delete", "Create", "Replicate", "Restore", "Write")
    for service, operations in policy.aws.read_only_operations.items():
        for operation in operations:
            assert not operation.startswith(forbidden), f"{service}:{operation} is not a read"


def test_every_allowed_operation_has_an_iam_action(policy: Policy) -> None:
    """Otherwise the printed policy would be missing a permission the tool needs."""
    for operation in policy.aws.read_only_operations["s3"]:
        assert operation in policy.aws.iam_actions


def test_last_verified_is_an_iso_date(policy: Policy) -> None:
    import datetime as dt

    dt.date.fromisoformat(policy.last_verified)


def test_prices_record_their_publication_date(prices: Prices) -> None:
    assert prices.publication_date
    assert prices.source.startswith("http")


def test_prices_cover_the_common_regions_and_models(prices: Prices) -> None:
    for region in ("us-east-1", "us-west-2", "eu-west-1"):
        assert region in prices.rates, region
    rates = prices.rates_for(
        "anthropic.claude-sonnet-4-20250514-v1:0", "us-east-1", InferenceScope.REGIONAL
    )
    assert rates is not None
    assert rates.input and rates.output


def test_an_unknown_model_has_no_rates(prices: Prices) -> None:
    assert prices.rates_for("acme.nothing-v1:0", "us-east-1", InferenceScope.REGIONAL) is None


# --------------------------------------------------------------- breaking them on purpose


def test_an_aws_fact_without_a_source_is_rejected(
    tmp_path: Path, raw_policy: dict[str, Any]
) -> None:
    del raw_policy["aws"]["schema_type"]["source"]
    with pytest.raises(CatalogError, match="source"):
        load_policy(_write(tmp_path, raw_policy))


def test_a_threshold_without_a_rationale_is_rejected(
    tmp_path: Path, raw_policy: dict[str, Any]
) -> None:
    del raw_policy["thresholds"]["repeated_shape_count"]["rationale"]
    with pytest.raises(CatalogError, match="rationale"):
        load_policy(_write(tmp_path, raw_policy))


def test_the_iam_action_map_needs_a_source_too(tmp_path: Path, raw_policy: dict[str, Any]) -> None:
    del raw_policy["aws"]["iam_actions"]["source"]
    with pytest.raises(CatalogError, match="source"):
        load_policy(_write(tmp_path, raw_policy))


def test_an_empty_allowlist_is_rejected(tmp_path: Path, raw_policy: dict[str, Any]) -> None:
    raw_policy["aws"]["read_only_operations"]["value"] = {}
    with pytest.raises(CatalogError, match="must not be empty"):
        load_policy(_write(tmp_path, raw_policy))


def test_a_non_numeric_threshold_is_rejected(tmp_path: Path, raw_policy: dict[str, Any]) -> None:
    raw_policy["thresholds"]["burst_request_count"]["value"] = "sixty"
    with pytest.raises(CatalogError, match="must be a number"):
        load_policy(_write(tmp_path, raw_policy))


def test_an_unsupported_schema_version_is_rejected(
    tmp_path: Path, raw_policy: dict[str, Any]
) -> None:
    raw_policy["schema_version"] = 99
    with pytest.raises(CatalogError, match="schema_version"):
        load_policy(_write(tmp_path, raw_policy))


def test_a_missing_section_is_reported(tmp_path: Path, raw_policy: dict[str, Any]) -> None:
    del raw_policy["thresholds"]
    with pytest.raises(CatalogError, match="thresholds is missing"):
        load_policy(_write(tmp_path, raw_policy))


def test_a_price_override_without_a_source_is_rejected(
    tmp_path: Path, raw_prices: dict[str, Any]
) -> None:
    """An override decides what a report says a call cost, so it has to be accountable."""
    raw_prices["model_name_overrides"] = {"acme.thing-v1:0": {"value": "Made Up (Bedrock)"}}
    with pytest.raises(CatalogError, match="source"):
        load_prices(_write(tmp_path, raw_prices, "prices.yaml"))


def test_a_valid_price_override_is_honoured(tmp_path: Path, raw_prices: dict[str, Any]) -> None:
    name = "Claude Sonnet 4 (Amazon Bedrock Edition)"
    raw_prices["model_name_overrides"] = {
        "acme.rebadged-v1:0": {"value": name, "source": "https://aws.amazon.com/bedrock/pricing/"}
    }
    prices = load_prices(_write(tmp_path, raw_prices, "prices.yaml"))

    rates = prices.rates_for("acme.rebadged-v1:0", "us-east-1", InferenceScope.REGIONAL)
    assert rates is not None
    assert rates.service_name == name


def test_empty_prices_are_rejected(tmp_path: Path, raw_prices: dict[str, Any]) -> None:
    raw_prices["rates_usd_per_million_tokens"] = {}
    with pytest.raises(CatalogError, match="must not be empty"):
        load_prices(_write(tmp_path, raw_prices, "prices.yaml"))


def test_a_missing_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(CatalogError, match="file not found"):
        load_policy(tmp_path / "nope.yaml")


def test_invalid_yaml_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "policy.yaml"
    path.write_text("aws: [unclosed\n", encoding="utf-8")
    with pytest.raises(CatalogError, match="not valid YAML"):
        load_policy(path)


def test_errors_name_the_file(tmp_path: Path, raw_policy: dict[str, Any]) -> None:
    del raw_policy["aws"]["schema_type"]["source"]
    path = _write(tmp_path, raw_policy)
    with pytest.raises(CatalogError, match=path.name):
        load_policy(path)
