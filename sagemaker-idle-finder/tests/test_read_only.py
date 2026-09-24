"""The promise the README makes: this tool cannot change anything in an account.

Asserted three ways, because one would not be enough: only one module may reach AWS, the
enforced allowlist must match the documented one, and the IAM policy the tool prints must
be generated from that same allowlist rather than typed out beside it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from sagemaker_idle_finder.catalog import Policy
from sagemaker_idle_finder.cli import _iam_policy

SOURCE_ROOT = Path(__file__).resolve().parent.parent / "src" / "sagemaker_idle_finder"

#: The one module allowed to import an AWS SDK.
AWS_MODULE = "aws.py"
#: cli.py builds a session to read the configured region, which is not an API call.
REGION_LOOKUP_MODULE = "cli.py"

SDK_MODULES = frozenset({"boto3", "botocore", "aiobotocore"})

WRITE_VERBS = (
    "Create",
    "Delete",
    "Update",
    "Put",
    "Invoke",
    "Start",
    "Stop",
    "Register",
    "Deregister",
    "Modify",
    "Attach",
    "Detach",
    "Tag",
    "Untag",
)


def _sources() -> list[Path]:
    return sorted(SOURCE_ROOT.rglob("*.py"))


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


def test_there_are_sources_to_check() -> None:
    assert _sources(), "found no sources; the guard below would pass vacuously"


@pytest.mark.parametrize(
    "path",
    [p for p in _sources() if p.name not in {AWS_MODULE, REGION_LOOKUP_MODULE}],
    ids=lambda p: p.name,
)
def test_only_the_aws_module_imports_an_sdk(path: Path) -> None:
    offenders = sorted(_imports(path) & SDK_MODULES)
    assert not offenders, f"{path.name} imports {offenders}; only {AWS_MODULE} may"


def test_the_sdk_is_imported_lazily(policy: Policy) -> None:
    """Imported inside a function so nothing loads boto3 just to parse the data files."""
    del policy
    for name in (AWS_MODULE, REGION_LOOKUP_MODULE):
        tree = ast.parse((SOURCE_ROOT / name).read_text(encoding="utf-8"))
        top_level = {
            alias.name.split(".")[0]
            for node in tree.body
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert "boto3" not in top_level, f"{name} imports boto3 at module scope"


def test_no_allowed_operation_is_a_write(policy: Policy) -> None:
    for service, operations in policy.aws.allowed_operations.items():
        for operation in operations:
            assert not operation.startswith(WRITE_VERBS), f"{service}:{operation} writes"


def test_the_printed_iam_policy_is_generated_from_the_enforced_allowlist(
    policy: Policy,
) -> None:
    """The documentation and the enforcement cannot drift, because they are one list."""
    document = _iam_policy(policy)
    statements = document["Statement"]
    assert isinstance(statements, list)
    printed = set(statements[0]["Action"])
    enforced = {
        f"{service}:{operation}"
        for service, operations in policy.aws.allowed_operations.items()
        for operation in operations
    }
    assert printed == enforced


def test_the_iam_policy_grants_nothing_that_writes(policy: Policy) -> None:
    statements = _iam_policy(policy)["Statement"]
    assert isinstance(statements, list)
    for action in statements[0]["Action"]:
        _, operation = action.split(":", 1)
        assert not operation.startswith(WRITE_VERBS)


def test_importing_the_package_does_not_import_boto3() -> None:
    import subprocess
    import sys

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, sagemaker_idle_finder; "
            "assert 'boto3' not in sys.modules, sorted(sys.modules); print('clean')",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "clean" in completed.stdout
