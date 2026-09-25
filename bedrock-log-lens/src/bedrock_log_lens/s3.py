"""Read log objects from S3, and nothing else.

The only module that imports boto3, and it does so inside the function so that analysing a
local directory needs no AWS SDK at all. A test asserts that importing the package does not
pull boto3 in.

Two operations, both reads, checked against an allowlist before the call is made rather
than promised in a README. The allowlist comes from policy.yaml, and the IAM policy the
README documents is generated from the same list.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from bedrock_log_lens.models import IssueKind, ParseIssue, ParseOutcome
from bedrock_log_lens.read import LOG_SUFFIXES, read_text

#: What the guard permits. Matches aws.read_only_operations in policy.yaml.
READ_ONLY_OPERATIONS = frozenset({"ListObjectsV2", "GetObject"})


class ReadOnlyViolationError(RuntimeError):
    """Raised when something tries an S3 operation outside the allowlist.

    A programming error, not a runtime condition: it means the tool tried to do something
    it promises never to do.
    """


class S3Error(RuntimeError):
    """Raised when S3 cannot be read and the caller should hear about it."""


@dataclass(frozen=True)
class S3Location:
    """A bucket and key prefix to read from."""

    bucket: str
    prefix: str = ""

    @classmethod
    def parse(cls, uri: str) -> S3Location:
        """Read an ``s3://bucket/prefix`` URI."""
        text = uri.strip()
        if not text.startswith("s3://"):
            raise ValueError("an S3 location must start with s3://")
        remainder = text[len("s3://") :]
        if not remainder or remainder.startswith("/"):
            raise ValueError("an S3 location must name a bucket")
        bucket, _, prefix = remainder.partition("/")
        return cls(bucket=bucket, prefix=prefix)

    def __str__(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}"


class ReadOnlyS3:
    """An S3 client that refuses to do anything but list and get.

    Operations are named as the API names them, so the allowlist, the guard and the IAM
    policy are comparable by eye.
    """

    def __init__(self, client: Any, allowed: frozenset[str] = READ_ONLY_OPERATIONS) -> None:  # noqa: ANN401
        self._client = client
        self._allowed = allowed

    def call(self, operation: str, **kwargs: Any) -> Mapping[str, Any]:  # noqa: ANN401
        """Invoke `operation`, refusing anything not on the allowlist."""
        if operation not in self._allowed:
            allowed = ", ".join(sorted(self._allowed))
            raise ReadOnlyViolationError(
                f"s3:{operation} is not allowed. This tool only reads, and may call: {allowed}"
            )
        method = getattr(self._client, _method_name(operation))
        try:
            result: Mapping[str, Any] = method(**kwargs)
        except ReadOnlyViolationError:
            raise
        except Exception as exc:  # botocore builds its error classes at runtime
            raise S3Error(f"s3:{operation} failed: {exc}") from exc
        return result

    def list_keys(self, location: S3Location) -> Iterator[str]:
        """Every log object key under the prefix, paginated by hand.

        Hand-rolled so each page passes the same guard; a botocore paginator would call the
        service directly and slip past it.
        """
        token: str | None = None
        wanted = {suffix.lower() for suffix in LOG_SUFFIXES}
        while True:
            arguments: dict[str, Any] = {"Bucket": location.bucket}
            if location.prefix:
                arguments["Prefix"] = location.prefix
            if token:
                arguments["ContinuationToken"] = token
            response = self.call("ListObjectsV2", **arguments)
            for entry in response.get("Contents") or []:
                key = str(entry.get("Key", ""))
                if key and not key.endswith("/") and _suffix_of(key) in wanted:
                    yield key
            if not response.get("IsTruncated"):
                return
            token = response.get("NextContinuationToken")
            if not token:
                return

    def read_object(self, bucket: str, key: str) -> str:
        """Fetch one object and decode it, decompressing when it is gzipped."""
        import gzip

        response = self.call("GetObject", Bucket=bucket, Key=key)
        body = response.get("Body")
        raw = body.read() if body is not None else b""
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        return bytes(raw).decode("utf-8", errors="replace")


def read_s3(client: ReadOnlyS3, location: S3Location) -> tuple[ParseOutcome, int]:
    """Parse every log object under the prefix, and say how many objects that was."""
    outcome = ParseOutcome()
    keys = 0
    for key in client.list_keys(location):
        keys += 1
        source = f"s3://{location.bucket}/{key}"
        try:
            text = client.read_object(location.bucket, key)
        except S3Error as exc:
            outcome = outcome.merged_with(
                ParseOutcome(issues=(ParseIssue(IssueKind.UNREADABLE_FILE, source, str(exc)),))
            )
            continue
        outcome = outcome.merged_with(read_text(text, source=source))
    return outcome, keys


def build_client(
    region: str | None = None,
    profile: str | None = None,
    allowed: frozenset[str] = READ_ONLY_OPERATIONS,
) -> ReadOnlyS3:
    """Build a guarded S3 client. The only place a real AWS client is constructed."""
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - depends on how it was installed
        raise S3Error(
            "reading from S3 needs the optional boto3 dependency: "
            "pipx install 'bedrock-log-lens[s3]', or pip install boto3"
        ) from exc

    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    return ReadOnlyS3(session.client("s3", region_name=region), allowed)


def _method_name(operation: str) -> str:
    """Turn an API operation name into the boto3 method name."""
    out: list[str] = []
    for index, character in enumerate(operation):
        if character.isupper() and index and not operation[index - 1].isupper():
            out.append("_")
        out.append(character.lower())
    return "".join(out)


def _suffix_of(key: str) -> str:
    _, _, tail = key.rpartition("/")
    _, dot, suffix = tail.rpartition(".")
    return f".{suffix.lower()}" if dot else ""
