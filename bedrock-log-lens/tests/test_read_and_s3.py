"""Reading logs from disk and from S3.

These files arrive gzipped from Bedrock and in every other shape from exports and support
tickets, so the reader guesses the layout from the bytes and counts what it cannot use.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import pytest

from bedrock_log_lens.models import IssueKind
from bedrock_log_lens.read import find_log_files, read_directory, read_file, read_text
from bedrock_log_lens.s3 import (
    ReadOnlyS3,
    ReadOnlyViolationError,
    S3Error,
    S3Location,
    read_s3,
)
from tests.conftest import CANARY, make_raw, write_log_dir

# ------------------------------------------------------------------------ local files


def test_a_gzipped_log_directory_is_read(tmp_path: Path) -> None:
    write_log_dir(tmp_path, [make_raw(), make_raw()], gzipped=True)
    outcome = read_directory(tmp_path)
    assert len(outcome.records) == 2
    assert outcome.issues == ()


def test_a_plain_log_directory_is_read(tmp_path: Path) -> None:
    write_log_dir(tmp_path, [make_raw() for _ in range(3)])
    assert len(read_directory(tmp_path).records) == 3


def test_gzip_is_detected_by_content_not_by_extension(tmp_path: Path) -> None:
    """A .json that is really gzipped is common after a download."""
    path = tmp_path / "invocations.json"
    path.write_bytes(gzip.compress(json.dumps(make_raw()).encode()))
    assert len(read_file(path).records) == 1


@pytest.mark.parametrize(
    "layout",
    ["one_object", "json_array", "json_lines"],
)
def test_every_file_layout_is_read(layout: str) -> None:
    records = [make_raw(request_id=f"r-{index}") for index in range(3)]
    if layout == "one_object":
        text = json.dumps(records[0])
        expected = 1
    elif layout == "json_array":
        text = json.dumps(records)
        expected = 3
    else:
        text = "\n".join(json.dumps(record) for record in records)
        expected = 3

    outcome = read_text(text, source="test")
    assert len(outcome.records) == expected


def test_one_corrupt_line_costs_one_record_not_the_file() -> None:
    """A truncated line is normal in a log export and must not end the run."""
    good = json.dumps(make_raw(request_id="fine"))
    text = f"{good}\n{{ this is not json\n{good}\n"

    outcome = read_text(text, source="logs.json")

    assert len(outcome.records) == 2
    assert outcome.issue_counts == {IssueKind.BAD_JSON: 1}
    assert outcome.usable_fraction == pytest.approx(2 / 3)


def test_records_are_tagged_with_where_they_came_from() -> None:
    text = "\n".join(json.dumps(make_raw(request_id=f"r-{i}")) for i in range(2))
    outcome = read_text(text, source="logs.json")
    assert outcome.records[0].source == "logs.json#1"
    assert outcome.records[1].source == "logs.json#2"


def test_an_unreadable_file_is_counted_not_raised(tmp_path: Path) -> None:
    path = tmp_path / "broken.json.gz"
    path.write_bytes(b"\x1f\x8b not actually gzip")
    outcome = read_file(path)
    assert outcome.records == ()
    assert outcome.issue_counts == {IssueKind.UNREADABLE_FILE: 1}


def test_an_empty_file_produces_nothing_at_all(tmp_path: Path) -> None:
    path = tmp_path / "empty.json"
    path.write_text("   \n")
    outcome = read_file(path)
    assert outcome.total_seen == 0


def test_files_are_read_in_a_stable_order(tmp_path: Path) -> None:
    """Two runs over the same directory must produce the same report."""
    for name in ("b.json", "a.json", "c.json"):
        (tmp_path / name).write_text(json.dumps(make_raw()))
    assert find_log_files(tmp_path) == sorted(find_log_files(tmp_path))


def test_unrelated_files_are_ignored(tmp_path: Path) -> None:
    write_log_dir(tmp_path, [make_raw()])
    (tmp_path / "README.md").write_text("not a log")
    (tmp_path / "notes.txt").write_text("nor this")
    assert len(read_directory(tmp_path).records) == 1


# ------------------------------------------------------------------------------ S3


class FakeS3:
    """A botocore-shaped S3 double."""

    def __init__(self, objects: dict[str, bytes], pages: int = 1) -> None:
        self.objects = objects
        self.pages = pages
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("list_objects_v2", kwargs))
        keys = sorted(self.objects)
        if self.pages > 1 and "ContinuationToken" not in kwargs:
            return {
                "Contents": [{"Key": key} for key in keys[:1]],
                "IsTruncated": True,
                "NextContinuationToken": "page-2",
            }
        remainder = keys[1:] if self.pages > 1 else keys
        return {"Contents": [{"Key": key} for key in remainder], "IsTruncated": False}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_object", kwargs))
        import io

        return {"Body": io.BytesIO(self.objects[kwargs["Key"]])}


def _objects(count: int = 2, gzipped: bool = True) -> dict[str, bytes]:
    payload = json.dumps(make_raw()).encode()
    if gzipped:
        payload = gzip.compress(payload)
    return {
        f"AWSLogs/123/BedrockModelInvocationLogs/us-east-1/2026/09/24/12/f{i}.json.gz": payload
        for i in range(count)
    }


def test_an_s3_prefix_is_read() -> None:
    fake = FakeS3(_objects(3))
    outcome, keys = read_s3(ReadOnlyS3(fake), S3Location("logs", "AWSLogs/"))
    assert keys == 3
    assert len(outcome.records) == 3


def test_listing_follows_every_page() -> None:
    fake = FakeS3(_objects(3), pages=2)
    _outcome, keys = read_s3(ReadOnlyS3(fake), S3Location("logs"))
    assert keys == 3


@pytest.mark.parametrize(
    "operation",
    ["PutObject", "DeleteObject", "DeleteObjects", "CreateBucket", "PutBucketPolicy"],
)
def test_a_write_is_refused_before_it_reaches_s3(operation: str) -> None:
    fake = FakeS3({})
    with pytest.raises(ReadOnlyViolationError, match="only reads"):
        ReadOnlyS3(fake).call(operation, Bucket="logs")
    assert fake.calls == []


def test_the_refusal_names_what_is_allowed() -> None:
    with pytest.raises(ReadOnlyViolationError, match="GetObject, ListObjectsV2"):
        ReadOnlyS3(FakeS3({})).call("DeleteBucket")


def test_an_unreadable_object_is_counted_not_fatal() -> None:
    class Broken(FakeS3):
        def get_object(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("AccessDenied")

    outcome, keys = read_s3(ReadOnlyS3(Broken(_objects(2))), S3Location("logs"))
    assert keys == 2
    assert outcome.issue_counts == {IssueKind.UNREADABLE_FILE: 2}


def test_s3_errors_name_the_operation() -> None:
    class Broken(FakeS3):
        def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("NoSuchBucket")

    with pytest.raises(S3Error, match="ListObjectsV2"):
        list(ReadOnlyS3(Broken({})).list_keys(S3Location("nope")))


@pytest.mark.parametrize(
    ("uri", "bucket", "prefix"),
    [
        ("s3://logs", "logs", ""),
        ("s3://logs/AWSLogs/123/", "logs", "AWSLogs/123/"),
        ("  s3://logs/a/b  ", "logs", "a/b"),
    ],
)
def test_s3_uris_are_parsed(uri: str, bucket: str, prefix: str) -> None:
    location = S3Location.parse(uri)
    assert location.bucket == bucket
    assert location.prefix == prefix


@pytest.mark.parametrize("uri", ["logs", "https://logs", "s3://", "s3:///prefix"])
def test_a_bad_s3_uri_is_refused(uri: str) -> None:
    with pytest.raises(ValueError, match=r"s3://|bucket"):
        S3Location.parse(uri)


def test_the_package_does_not_import_boto3() -> None:
    """Analysing a local directory must need no AWS SDK at all."""
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import bedrock_log_lens, bedrock_log_lens.read, bedrock_log_lens.cli, sys; "
            "sys.exit(1 if 'boto3' in sys.modules else 0)",
        ],
        check=False,
        capture_output=True,
    )
    assert result.returncode == 0, "importing the package pulled boto3 in"


def test_reading_s3_never_surfaces_content() -> None:
    """The same guarantee applies whatever the records came from."""
    outcome, _keys = read_s3(ReadOnlyS3(FakeS3(_objects(2))), S3Location("logs"))
    assert CANARY not in repr(outcome.records)
