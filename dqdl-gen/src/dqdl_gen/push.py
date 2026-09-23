"""Create a ruleset in AWS Glue. The only module in this package that writes to AWS.

Everything else in ``dqdl-gen`` is local and read-only. This module is kept apart on purpose:

* ``boto3`` is an optional dependency (``pip install "dqdl-gen[push]"``) and is imported
  inside the function, so the core tool neither needs nor loads it;
* the command refuses to run without an explicit ``--confirm``;
* ``tests/test_no_aws_calls.py`` fails the build if any other module imports an AWS SDK.

It calls exactly one API: ``CreateDataQualityRuleset``. It never updates or deletes an
existing ruleset — if the name is taken, it reports that and stops.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - imported only for type checking
    # botocore builds client methods at runtime, so the generated Glue stubs are what give
    # create_data_quality_ruleset a checkable signature.
    from mypy_boto3_glue.client import GlueClient

#: The single AWS API this tool can call. Anything else is out of scope by design.
API_ACTION = "glue:CreateDataQualityRuleset"

#: The minimal IAM policy needed, documented in the README.
IAM_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": ["glue:CreateDataQualityRuleset"],
            "Resource": "arn:aws:glue:<region>:<account-id>:dataQualityRuleset/*",
        }
    ],
}


class PushError(RuntimeError):
    """Raised when a ruleset cannot be created in Glue."""


@dataclass(frozen=True)
class PushRequest:
    """What to create in Glue."""

    name: str
    ruleset: str
    description: str | None = None
    database_name: str | None = None
    table_name: str | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("a ruleset name is required")
        if not self.ruleset.strip():
            raise ValueError("the ruleset must not be empty")
        if (self.database_name is None) != (self.table_name is None):
            raise ValueError(
                "a target needs both --database and --table, or neither: Glue identifies a "
                "ruleset's dataset by database and table together"
            )


@dataclass(frozen=True)
class PushResult:
    """What Glue reported back."""

    name: str
    region: str | None


def build_arguments(request: PushRequest) -> dict[str, Any]:
    """Build the CreateDataQualityRuleset arguments.

    Pure, so the exact request can be asserted in tests without a client.
    """
    arguments: dict[str, Any] = {"Name": request.name, "Ruleset": request.ruleset}
    if request.description:
        arguments["Description"] = request.description
    if request.database_name and request.table_name:
        arguments["TargetTable"] = {
            "DatabaseName": request.database_name,
            "TableName": request.table_name,
        }
    return arguments


def create_ruleset(request: PushRequest, client: GlueClient | None = None) -> PushResult:
    """Create the ruleset in Glue.

    `client` exists so tests can pass a stubbed Glue client; when it is None a real one is
    built, which is the only point at which this package touches AWS.
    """
    glue = client if client is not None else _build_client()
    try:
        glue.create_data_quality_ruleset(**build_arguments(request))
    # botocore builds its exception classes at runtime from the service model, so
    # there is no importable AlreadyExistsException to catch by type here.
    except Exception as exc:
        raise PushError(_describe(exc, request)) from exc
    return PushResult(name=request.name, region=_region_of(glue))


def _build_client() -> GlueClient:
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - exercised by the install docs
        raise PushError(
            "boto3 is not installed. `dqdl-gen push` is an optional extra: "
            'install it with `pip install "dqdl-gen[push]"`.'
        ) from exc
    return boto3.client("glue")


def _describe(exc: Exception, request: PushRequest) -> str:
    name = type(exc).__name__
    if name == "AlreadyExistsException":
        return (
            f"a Glue data quality ruleset named {request.name!r} already exists. "
            f"This tool only ever creates, never updates, so nothing was changed."
        )
    if name in {"AccessDeniedException", "AccessDeniedError"}:
        return f"access denied. {API_ACTION} is required; see the README for the policy."
    return f"Glue refused to create the ruleset: {exc}"


def _region_of(client: GlueClient) -> str | None:
    meta = getattr(client, "meta", None)
    return getattr(meta, "region_name", None) if meta is not None else None
