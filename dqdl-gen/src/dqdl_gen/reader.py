"""Read a CSV or Parquet file into an Arrow table.

The only module in the package that touches the filesystem. Everything downstream works on
the :class:`~dqdl_gen.models.DatasetProfile` this produces, so profiling and rule generation
are testable without a file on disk.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import pyarrow as pa
import pyarrow.csv as pa_csv
import pyarrow.parquet as pq


class ReadError(ValueError):
    """Raised when a file cannot be read or its format is not supported."""


class SourceFormat(StrEnum):
    """The file formats this tool can profile."""

    CSV = "csv"
    PARQUET = "parquet"


_SUFFIXES = {
    ".csv": SourceFormat.CSV,
    ".tsv": SourceFormat.CSV,
    ".txt": SourceFormat.CSV,
    ".parquet": SourceFormat.PARQUET,
    ".pq": SourceFormat.PARQUET,
}


@dataclass(frozen=True)
class LoadedTable:
    """An Arrow table plus what is known about where it came from."""

    table: pa.Table
    source_name: str
    source_format: SourceFormat
    sampled: bool
    #: Rows in the whole file, when that is known without reading it all.
    total_row_count: int | None


def detect_format(path: Path) -> SourceFormat:
    """Infer the format from the file extension."""
    try:
        return _SUFFIXES[path.suffix.lower()]
    except KeyError as exc:
        known = ", ".join(sorted(_SUFFIXES))
        raise ReadError(
            f"cannot tell the format of {path.name!r} from its extension. Known: {known}"
        ) from exc


def read_table(
    path: Path,
    sample: int | None = None,
    source_format: SourceFormat | None = None,
) -> LoadedTable:
    """Read `path`, optionally stopping after `sample` rows.

    When sampling, the true row count of the file is recorded if the format can report it
    cheaply, and left as ``None`` otherwise. Nothing downstream invents a row count: a rule
    about row count is simply not generated when the real figure is unknown.
    """
    if not path.is_file():
        raise ReadError(f"file not found: {path}")
    if sample is not None and sample < 1:
        raise ValueError(f"sample must be at least 1 row, got {sample}")

    resolved = source_format or detect_format(path)
    if resolved is SourceFormat.PARQUET:
        return _read_parquet(path, sample)
    return _read_csv(path, sample)


def _read_parquet(path: Path, sample: int | None) -> LoadedTable:
    try:
        parquet_file = pq.ParquetFile(path)
    except (pa.ArrowInvalid, OSError) as exc:
        raise ReadError(f"{path.name} is not readable as Parquet: {exc}") from exc

    # Parquet keeps the row count in its footer, so it is known even when sampling.
    total = parquet_file.metadata.num_rows
    if sample is None or sample >= total:
        return LoadedTable(parquet_file.read(), path.name, SourceFormat.PARQUET, False, total)

    batches: list[pa.RecordBatch] = []
    seen = 0
    for batch in parquet_file.iter_batches(batch_size=min(sample, 65_536)):
        remaining = sample - seen
        batches.append(batch.slice(0, remaining) if batch.num_rows > remaining else batch)
        seen += min(batch.num_rows, remaining)
        if seen >= sample:
            break
    table = pa.Table.from_batches(batches, schema=parquet_file.schema_arrow)
    return LoadedTable(table, path.name, SourceFormat.PARQUET, True, total)


def _read_csv(path: Path, sample: int | None) -> LoadedTable:
    parse_options = pa_csv.ParseOptions(delimiter="\t" if path.suffix.lower() == ".tsv" else ",")
    try:
        if sample is None:
            table = pa_csv.read_csv(path, parse_options=parse_options)
            return LoadedTable(table, path.name, SourceFormat.CSV, False, table.num_rows)

        with pa_csv.open_csv(path, parse_options=parse_options) as reader:
            batches: list[pa.RecordBatch] = []
            seen = 0
            for batch in reader:
                remaining = sample - seen
                batches.append(batch.slice(0, remaining) if batch.num_rows > remaining else batch)
                seen += min(batch.num_rows, remaining)
                if seen >= sample:
                    break
            schema = reader.schema
    except pa.ArrowInvalid as exc:
        raise ReadError(f"{path.name} is not readable as CSV: {exc}") from exc

    table = pa.Table.from_batches(batches, schema=schema) if batches else schema.empty_table()
    # A CSV footer does not exist, so after sampling the true row count is unknown unless
    # the sample happened to reach the end of the file.
    reached_end = sample is not None and table.num_rows < sample
    total = table.num_rows if reached_end else None
    return LoadedTable(table, path.name, SourceFormat.CSV, not reached_end, total)
