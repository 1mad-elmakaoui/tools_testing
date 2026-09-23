"""The one command that writes to AWS, exercised against a stubbed Glue client.

No test here builds a real client, reads credentials or reaches the network. The stub
asserts the exact request that would be sent, which is the part worth pinning down for a
command that creates something in someone's account.
"""

from __future__ import annotations

from typing import Any

import boto3
import pytest
from botocore.config import Config
from botocore.stub import ANY, Stubber

from dqdl_gen.push import (
    API_ACTION,
    IAM_POLICY,
    PushError,
    PushRequest,
    build_arguments,
    create_ruleset,
)

RULESET = 'Rules = [\n    IsComplete "order_id"\n]\n'


@pytest.fixture
def glue() -> Any:
    """A Glue client that cannot reach AWS: no credentials, no retries, no network."""
    return boto3.client(
        "glue",
        region_name="eu-west-2",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
        aws_session_token="testing",
        config=Config(retries={"max_attempts": 0}),
    )


# ---------------------------------------------------------------------------- request


def test_the_minimal_request() -> None:
    arguments = build_arguments(PushRequest(name="orders", ruleset=RULESET))
    assert arguments == {"Name": "orders", "Ruleset": RULESET}


def test_a_description_is_included_when_given() -> None:
    arguments = build_arguments(PushRequest(name="orders", ruleset=RULESET, description="nightly"))
    assert arguments["Description"] == "nightly"


def test_a_target_table_is_included_when_given() -> None:
    arguments = build_arguments(
        PushRequest(name="orders", ruleset=RULESET, database_name="sales", table_name="orders")
    )
    assert arguments["TargetTable"] == {"DatabaseName": "sales", "TableName": "orders"}


def test_a_half_specified_target_is_rejected() -> None:
    """Glue identifies a dataset by database and table together, so half of one is a bug."""
    with pytest.raises(ValueError, match="both --database and --table"):
        PushRequest(name="orders", ruleset=RULESET, database_name="sales")
    with pytest.raises(ValueError, match="both --database and --table"):
        PushRequest(name="orders", ruleset=RULESET, table_name="orders")


def test_an_empty_name_is_rejected() -> None:
    with pytest.raises(ValueError, match="ruleset name is required"):
        PushRequest(name="  ", ruleset=RULESET)


def test_an_empty_ruleset_is_rejected() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        PushRequest(name="orders", ruleset="   ")


# ------------------------------------------------------------------------ stubbed call


def test_creating_a_ruleset_sends_exactly_one_create_call(glue: Any) -> None:
    request = PushRequest(name="orders", ruleset=RULESET, description="nightly")
    with Stubber(glue) as stubber:
        stubber.add_response(
            "create_data_quality_ruleset",
            {"Name": "orders"},
            {"Name": "orders", "Ruleset": RULESET, "Description": "nightly"},
        )
        result = create_ruleset(request, client=glue)
        stubber.assert_no_pending_responses()
    assert result.name == "orders"
    assert result.region == "eu-west-2"


def test_an_existing_name_is_reported_without_overwriting(glue: Any) -> None:
    """This tool only ever creates. A name clash stops, it does not update."""
    with Stubber(glue) as stubber:
        stubber.add_client_error(
            "create_data_quality_ruleset",
            service_error_code="AlreadyExistsException",
            http_status_code=400,
        )
        with pytest.raises(PushError, match="already exists"):
            create_ruleset(PushRequest(name="orders", ruleset=RULESET), client=glue)


def test_access_denied_points_at_the_required_permission(glue: Any) -> None:
    with Stubber(glue) as stubber:
        stubber.add_client_error(
            "create_data_quality_ruleset",
            service_error_code="AccessDeniedException",
            http_status_code=403,
        )
        with pytest.raises(PushError, match="access denied"):
            create_ruleset(PushRequest(name="orders", ruleset=RULESET), client=glue)


def test_any_other_failure_is_surfaced(glue: Any) -> None:
    with Stubber(glue) as stubber:
        stubber.add_client_error(
            "create_data_quality_ruleset",
            service_error_code="InvalidInputException",
            service_message="ruleset is malformed",
            http_status_code=400,
        )
        with pytest.raises(PushError, match="Glue refused to create the ruleset"):
            create_ruleset(PushRequest(name="orders", ruleset=RULESET), client=glue)


def test_the_target_table_reaches_the_api(glue: Any) -> None:
    request = PushRequest(
        name="orders", ruleset=RULESET, database_name="sales", table_name="orders"
    )
    with Stubber(glue) as stubber:
        stubber.add_response(
            "create_data_quality_ruleset",
            {"Name": "orders"},
            {
                "Name": "orders",
                "Ruleset": ANY,
                "TargetTable": {"DatabaseName": "sales", "TableName": "orders"},
            },
        )
        create_ruleset(request, client=glue)
        stubber.assert_no_pending_responses()


# --------------------------------------------------------------------------------- iam


def test_the_documented_policy_grants_exactly_one_action() -> None:
    statements = IAM_POLICY["Statement"]
    assert isinstance(statements, list)
    assert len(statements) == 1
    assert statements[0]["Action"] == ["glue:CreateDataQualityRuleset"]
    assert API_ACTION == "glue:CreateDataQualityRuleset"


def test_the_policy_grants_no_read_or_delete_action() -> None:
    """The tool never reads or removes anything, so the policy must not allow it to."""
    statements = IAM_POLICY["Statement"]
    assert isinstance(statements, list)
    for action in statements[0]["Action"]:
        assert not any(
            action.startswith(f"glue:{verb}") for verb in ("Delete", "Update", "Get", "List")
        )
