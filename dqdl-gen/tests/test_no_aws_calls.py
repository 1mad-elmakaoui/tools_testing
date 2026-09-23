"""Guard the boundary: only push.py may reach AWS.

The README says profiling and generating are entirely local. That is only worth stating if
something enforces it, so these tests parse every source file and fail the build if an AWS
SDK or network client appears anywhere outside the one module allowed to have it.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).resolve().parent.parent / "src" / "dqdl_gen"
PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"

#: The single module allowed to talk to AWS.
AWS_MODULE = "push.py"

FORBIDDEN_MODULES = frozenset(
    {
        "aiobotocore",
        "aiohttp",
        "boto3",
        "botocore",
        "ftplib",
        "http",
        "httpx",
        "requests",
        "smtplib",
        "socket",
        "telnetlib",
        "urllib",
        "urllib3",
        "xmlrpc",
    }
)


def _source_files() -> list[Path]:
    return sorted(SOURCE_ROOT.rglob("*.py"))


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    return imported


def test_there_are_source_files_to_check() -> None:
    assert _source_files(), "found no sources; the guard below would pass vacuously"


def test_push_is_present_and_is_the_only_exception() -> None:
    assert (SOURCE_ROOT / AWS_MODULE).is_file()


@pytest.mark.parametrize(
    "path", [p for p in _source_files() if p.name != AWS_MODULE], ids=lambda p: p.name
)
def test_no_other_module_imports_a_network_client(path: Path) -> None:
    offenders = sorted(_imported_modules(path) & FORBIDDEN_MODULES)
    assert not offenders, (
        f"{path.name} imports {offenders}. Only {AWS_MODULE} may reach AWS; everything "
        f"else in this tool is local."
    )


def test_push_imports_boto3_lazily() -> None:
    """boto3 is an optional extra, so importing it at module scope would break the core."""
    source = (SOURCE_ROOT / AWS_MODULE).read_text(encoding="utf-8")
    tree = ast.parse(source)
    top_level = {
        alias.name.split(".")[0]
        for node in tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "boto3" not in top_level, "boto3 must be imported inside the function"
    assert "import boto3" in source, "push.py is expected to import boto3 somewhere"


def test_the_cli_imports_push_lazily() -> None:
    """The push command's import lives in the function so the rest never loads boto3."""
    tree = ast.parse((SOURCE_ROOT / "cli.py").read_text(encoding="utf-8"))
    top_level = {
        node.module for node in tree.body if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "dqdl_gen.push" not in top_level


def test_boto3_is_not_a_core_dependency() -> None:
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    declared = " ".join(project["dependencies"]).lower()
    for forbidden in ("boto3", "botocore", "requests", "httpx"):
        assert forbidden not in declared, f"{forbidden} must not be a core dependency"


def test_boto3_is_an_optional_extra() -> None:
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    extras = project["optional-dependencies"]
    assert "push" in extras
    assert any("boto3" in item for item in extras["push"])


def test_importing_the_package_does_not_import_boto3() -> None:
    """A fresh interpreter importing dqdl_gen must not pull in an AWS SDK."""
    import subprocess
    import sys

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, dqdl_gen; "
            "assert 'boto3' not in sys.modules, sorted(sys.modules); "
            "print('clean')",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "clean" in completed.stdout
