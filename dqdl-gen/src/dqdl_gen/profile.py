"""Turn an Arrow table into a :class:`~dqdl_gen.models.DatasetProfile`.

Every statistic is optional. When one cannot be computed for a column — an unusual type, a
kernel that does not apply — it is left as ``None`` rather than given a stand-in, because a
rule generated from an invented number is worse than a missing rule.
"""

from __future__ import annotations

import pyarrow as pa
import pyarrow.compute as pc

from dqdl_gen.models import ColumnKind, ColumnProfile, DatasetProfile
from dqdl_gen.reader import LoadedTable


def profile_table(
    loaded: LoadedTable,
    max_value_set: int,
    top_values_shown: int,
) -> DatasetProfile:
    """Profile every column of a loaded table.

    `max_value_set` bounds how many distinct values are collected in full. A column at or
    under it gets its complete value set, which is what lets an allowed-value rule be
    generated honestly; above it, only the most frequent values are kept, for display.
    """
    table = loaded.table
    columns = tuple(
        _profile_column(
            name=name,
            column=table.column(name),
            row_count=table.num_rows,
            max_value_set=max_value_set,
            top_values_shown=top_values_shown,
        )
        for name in table.schema.names
    )
    return DatasetProfile(
        source_name=loaded.source_name,
        source_format=loaded.source_format.value,
        row_count=table.num_rows,
        columns=columns,
        sampled=loaded.sampled,
        total_row_count=loaded.total_row_count,
    )


def classify(data_type: pa.DataType) -> ColumnKind:
    """Reduce an Arrow type to the category the rules care about."""
    if pa.types.is_boolean(data_type):
        return ColumnKind.BOOLEAN
    if pa.types.is_integer(data_type):
        return ColumnKind.INTEGER
    if pa.types.is_floating(data_type) or pa.types.is_decimal(data_type):
        return ColumnKind.FLOAT
    if pa.types.is_string(data_type) or pa.types.is_large_string(data_type):
        return ColumnKind.STRING
    if pa.types.is_date(data_type):
        return ColumnKind.DATE
    if pa.types.is_timestamp(data_type):
        return ColumnKind.TIMESTAMP
    return ColumnKind.OTHER


def _profile_column(
    name: str,
    column: pa.ChunkedArray,
    row_count: int,
    max_value_set: int,
    top_values_shown: int,
) -> ColumnProfile:
    kind = classify(column.type)
    null_count = column.null_count
    distinct_count = _distinct_count(column)

    minimum = maximum = mean = stddev = None
    if kind.is_numeric:
        minimum, maximum = _min_max(column)
        mean = _scalar(pc.mean, column)
        stddev = _scalar(pc.stddev, column)

    min_length = max_length = None
    if kind is ColumnKind.STRING:
        lengths = _try(pc.binary_length, column)
        if lengths is not None:
            low, high = _min_max(lengths)
            min_length = None if low is None else int(low)
            max_length = None if high is None else int(high)

    return ColumnProfile(
        name=name,
        kind=kind,
        source_type=str(column.type),
        row_count=row_count,
        null_count=null_count,
        distinct_count=distinct_count,
        minimum=minimum,
        maximum=maximum,
        mean=mean,
        stddev=stddev,
        min_length=min_length,
        max_length=max_length,
        top_values=_top_values(column, kind, distinct_count, max_value_set, top_values_shown),
    )


def _distinct_count(column: pa.ChunkedArray) -> int | None:
    result = _try(pc.count_distinct, column)
    if result is None:
        return None
    value = result.as_py()
    return None if value is None else int(value)


def _min_max(column: pa.ChunkedArray) -> tuple[float | None, float | None]:
    result = _try(pc.min_max, column)
    if result is None:
        return None, None
    values = result.as_py()
    if not isinstance(values, dict):  # pragma: no cover - min_max always returns a struct
        return None, None
    return _as_float(values.get("min")), _as_float(values.get("max"))


def _scalar(kernel: object, column: pa.ChunkedArray) -> float | None:
    result = _try(kernel, column)
    return None if result is None else _as_float(result.as_py())


def _as_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):  # pragma: no cover - guards exotic types
        return None


def _top_values(
    column: pa.ChunkedArray,
    kind: ColumnKind,
    distinct_count: int | None,
    max_value_set: int,
    top_values_shown: int,
) -> tuple[tuple[str, int], ...]:
    """Collect value counts, in full when the column is small enough to enumerate.

    A complete set is only returned when the column has at most `max_value_set` distinct
    values; that is what lets the generator tell "this is the whole set" from "these are the
    common ones".
    """
    if kind not in {ColumnKind.STRING, ColumnKind.INTEGER, ColumnKind.BOOLEAN}:
        return ()
    if distinct_count is None or distinct_count == 0:
        return ()

    wanted = max(max_value_set, top_values_shown)
    if distinct_count > wanted:
        return ()

    counts = _try(pc.value_counts, column)
    if counts is None:
        return ()

    collected: list[tuple[str, int]] = []
    for entry in counts.to_pylist():
        value = entry.get("values")
        if value is None:
            continue
        collected.append((_render_value(value), int(entry.get("counts", 0))))
    # Most frequent first, ties broken by value so the output is deterministic.
    collected.sort(key=lambda item: (-item[1], item[0]))
    return tuple(collected)


def _render_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _try(kernel: object, column: pa.ChunkedArray) -> pa.Scalar | pa.Array | None:
    """Run a compute kernel, returning None rather than raising on an unsupported type."""
    try:
        return kernel(column)  # type: ignore[operator]
    except (pa.ArrowNotImplementedError, pa.ArrowInvalid, pa.ArrowTypeError):
        return None
