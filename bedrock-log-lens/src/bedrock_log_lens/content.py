"""Show the prompt and response for named requests, on explicit demand.

Nothing else in this program can do this, and that is the design. The analysis pipeline
parses content away before any report sees it, so the only way to show a prompt is to go
back to the file and read it again — which is what this module does, for the specific
request IDs someone asked for, one at a time.

Keeping it here rather than in the pipeline is what makes the guarantee hold. If the
records carried content and a flag decided whether to print it, every future output path
would be one forgotten check away from a leak. They cannot leak what they never had.

Using this prints full prompts and responses to a terminal, and those may contain personal
data, credentials or anything else a user typed. The caller is responsible for where that
output goes.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from bedrock_log_lens.parse import SUPPORTED_SCHEMA_TYPE
from bedrock_log_lens.read import decode_file, find_log_files

#: The warning shown before any content is printed. Not suppressible.
WARNING = (
    "--show-content prints the full prompts and responses for the requests named below. "
    "Bedrock invocation logs contain everything your users typed and everything the model "
    "replied, which may include personal data, credentials and confidential material. "
    "Nothing is redacted."
)


@dataclass(frozen=True)
class RecordContent:
    """The content of one request, fetched deliberately.

    This is the only type in the program that holds a prompt, and it exists only for as
    long as it takes to print it.
    """

    request_id: str
    source: str
    model_id: str
    input_body: object
    output_body: object

    def as_text(self) -> str:
        """The content, formatted for a terminal."""
        return (
            f"request {self.request_id}  ({self.model_id})\n"
            f"  from {self.source}\n"
            f"  input:\n{_indent(self.input_body)}\n"
            f"  output:\n{_indent(self.output_body)}"
        )


def fetch(root: Path, request_ids: Sequence[str]) -> Iterator[RecordContent]:
    """Find the named requests in the logs under `root` and yield their content.

    Re-reads the files rather than keeping anything from the analysis run. That is slower,
    and it is the reason the rest of the program cannot leak.
    """
    wanted = {request_id.strip() for request_id in request_ids if request_id.strip()}
    if not wanted:
        return

    for path in find_log_files(root):
        try:
            text = decode_file(path)
        except OSError:
            continue
        yield from _scan(text, str(path), wanted)
        if not wanted:
            return


def _scan(text: str, source: str, wanted: set[str]) -> Iterator[RecordContent]:
    for raw in _objects(text):
        if not isinstance(raw, dict) or raw.get("schemaType") != SUPPORTED_SCHEMA_TYPE:
            continue
        request_id = str(raw.get("requestId") or "")
        if request_id not in wanted:
            continue
        wanted.discard(request_id)
        input_section = raw.get("input") if isinstance(raw.get("input"), dict) else {}
        output_section = raw.get("output") if isinstance(raw.get("output"), dict) else {}
        yield RecordContent(
            request_id=request_id,
            source=source,
            model_id=str(raw.get("modelId") or ""),
            input_body=input_section.get("inputBodyJson"),  # type: ignore[union-attr]
            output_body=output_section.get("outputBodyJson"),  # type: ignore[union-attr]
        )


def _objects(text: str) -> Iterable[object]:
    """Every JSON object in a log file, whatever its layout."""
    stripped = text.strip()
    if not stripped:
        return []
    try:
        document = json.loads(stripped)
    except json.JSONDecodeError:
        pass
    else:
        return document if isinstance(document, list) else [document]

    found: list[object] = []
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        try:
            found.append(json.loads(candidate))
        except json.JSONDecodeError:
            continue
    return found


def _indent(body: object) -> str:
    if body is None:
        return "    <not recorded: text data delivery was off for this request>"
    rendered = json.dumps(body, indent=2, ensure_ascii=False, default=str)
    return "\n".join(f"    {line}" for line in rendered.splitlines())
