"""Read log files from a directory, and hand each record to the parser.

Bedrock writes gzipped JSON to S3, but the copies people actually analyse have been
through a download, a re-export or a support ticket. So the reader accepts what turns up:
gzipped or plain, and one JSON object per file, a JSON array, or one object per line.
Guessing the layout from the bytes is more useful than demanding the canonical one, and
every guess that fails is counted rather than raised.

Each record is tagged with where it came from — ``path#line`` — which is how a finding can
be traced back to its source without this module keeping anything but the number.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator, Sequence
from pathlib import Path

from bedrock_log_lens.models import IssueKind, ParseIssue, ParseOutcome
from bedrock_log_lens.parse import parse_record

#: Extensions worth opening. Bedrock writes .json.gz; the rest turn up in exports.
LOG_SUFFIXES = (".json", ".gz", ".jsonl", ".ndjson", ".log")

#: Read in this many bytes to tell gzip from text, rather than trusting the extension.
_GZIP_MAGIC = b"\x1f\x8b"


def find_log_files(root: Path, suffixes: Sequence[str] = LOG_SUFFIXES) -> list[Path]:
    """Every log file under `root`, in a stable order.

    Sorted so that two runs over the same directory produce the same report, which matters
    for anything comparing one day's output with another's.
    """
    if root.is_file():
        return [root]
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in {suffix.lower() for suffix in suffixes}
    )


def read_directory(root: Path, suffixes: Sequence[str] = LOG_SUFFIXES) -> ParseOutcome:
    """Parse every log file under `root`."""
    outcome = ParseOutcome()
    for path in find_log_files(root, suffixes):
        outcome = outcome.merged_with(read_file(path))
    return outcome


def read_file(path: Path) -> ParseOutcome:
    """Parse one log file, whatever shape it is in."""
    try:
        text = decode_file(path)
    except (OSError, EOFError, gzip.BadGzipFile, UnicodeDecodeError) as exc:
        return ParseOutcome(issues=(ParseIssue(IssueKind.UNREADABLE_FILE, str(path), str(exc)),))
    return read_text(text, source=str(path))


def read_text(text: str, source: str = "") -> ParseOutcome:
    """Parse the contents of one log file."""
    records = []
    issues = []
    for raw, where in _decoded_records(text, source):
        if isinstance(raw, ParseIssue):
            issues.append(raw)
            continue
        result = parse_record(raw, where)
        if isinstance(result, ParseIssue):
            issues.append(result)
        else:
            records.append(result)
    return ParseOutcome(records=tuple(records), issues=tuple(issues))


def _decoded_records(text: str, source: str) -> Iterator[tuple[object | ParseIssue, str]]:
    """Yield each record in the file, however the file is laid out.

    A whole-file parse is tried first: that covers a single record and a JSON array, which
    is what an export usually produces. Falling back to line-by-line covers what Bedrock
    itself writes, and means one corrupt line costs one record rather than the file.
    """
    stripped = text.strip()
    if not stripped:
        return

    try:
        document = json.loads(stripped)
    except json.JSONDecodeError:
        pass
    else:
        if isinstance(document, list):
            for index, item in enumerate(document, start=1):
                yield (item, f"{source}#{index}")
        else:
            yield (document, source)
        return

    for number, line in enumerate(text.splitlines(), start=1):
        candidate = line.strip()
        if not candidate:
            continue
        where = f"{source}#{number}"
        try:
            yield (json.loads(candidate), where)
        except json.JSONDecodeError as exc:
            yield (ParseIssue(IssueKind.BAD_JSON, where, str(exc)), where)


def decode_file(path: Path) -> str:
    """Read a file as text, decompressing when it is actually gzipped.

    The magic bytes decide, not the extension: a `.json` that is really gzipped is common
    after a download, and so is a `.gz` that is not.
    """
    raw = path.read_bytes()
    if raw[:2] == _GZIP_MAGIC:
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", errors="replace")
