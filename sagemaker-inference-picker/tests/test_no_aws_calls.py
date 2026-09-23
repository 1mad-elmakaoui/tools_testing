"""Guard the central promise: this tool never talks to AWS.

The README states that the tool calls no AWS API and needs no credentials. That claim is
only worth making if something enforces it, so these tests fail the build if an AWS SDK or
a network client is ever imported or declared as a dependency.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).resolve().parent.parent / "src" / "sagemaker_inference_picker"
PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"

#: Importing any of these would mean the tool can reach the network.
FORBIDDEN_MODULES = frozenset(
    {
        "aiohttp",
        "boto3",
        "botocore",
        "ftplib",
        "http",
        "httpx",
        "requests",
        "smtplib",
        "socket",
        "ssl",
        "telnetlib",
        "urllib",
        "urllib3",
        "xmlrpc",
    }
)

FORBIDDEN_DEPENDENCIES = ("boto3", "botocore", "aiobotocore", "requests", "httpx", "aiohttp")


def _source_files() -> list[Path]:
    return sorted(SOURCE_ROOT.rglob("*.py"))


def test_there_are_source_files_to_check() -> None:
    assert _source_files(), "found no package sources; the guard below would pass vacuously"


@pytest.mark.parametrize("path", _source_files(), ids=lambda path: path.name)
def test_no_module_imports_a_network_client(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    offenders = sorted(imported & FORBIDDEN_MODULES)
    assert not offenders, f"{path.name} imports {offenders}, which can reach the network"


def test_no_aws_sdk_is_declared_as_a_dependency() -> None:
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    declared = " ".join(project["dependencies"]).lower()
    for forbidden in FORBIDDEN_DEPENDENCIES:
        assert forbidden not in declared, f"{forbidden} must not be a runtime dependency"


def test_the_runtime_dependency_list_stays_small() -> None:
    """A focused tool should not grow dependencies quietly."""
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    assert len(project["dependencies"]) <= 3, project["dependencies"]
