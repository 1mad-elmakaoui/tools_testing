"""Reading files and profiling their columns."""

from __future__ import annotations

from pathlib import Path

import pytest

from dqdl_gen.catalog import Catalog
from dqdl_gen.models import ColumnKind, ColumnProfile, DatasetProfile
from dqdl_gen.profile import profile_table
from dqdl_gen.reader import ReadError, SourceFormat, detect_format, read_table


def _profile(path: Path, catalog: Catalog, sample: int | None = None) -> DatasetProfile:
    return profile_table(
        read_table(path, sample=sample),
        max_value_set=catalog.shape.max_allowed_value_set,
        top_values_shown=catalog.shape.top_values_shown,
    )


def _column(profile: DatasetProfile, name: str) -> ColumnProfile:
    return next(column for column in profile.columns if column.name == name)


# ------------------------------------------------------------------------------ reader


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("a.csv", SourceFormat.CSV),
        ("a.TSV", SourceFormat.CSV),
        ("a.parquet", SourceFormat.PARQUET),
        ("a.PQ", SourceFormat.PARQUET),
    ],
)
def test_format_detection(name: str, expected: SourceFormat) -> None:
    assert detect_format(Path(name)) is expected


def test_an_unknown_extension_is_reported(tmp_path: Path) -> None:
    target = tmp_path / "data.json"
    target.write_text("{}", encoding="utf-8")
    with pytest.raises(ReadError, match="cannot tell the format"):
        read_table(target)


def test_a_missing_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ReadError, match="file not found"):
        read_table(tmp_path / "absent.csv")


def test_a_zero_sample_is_rejected(orders_csv: Path) -> None:
    with pytest.raises(ValueError, match="sample must be at least 1 row"):
        read_table(orders_csv, sample=0)


def test_unreadable_parquet_is_reported(tmp_path: Path) -> None:
    target = tmp_path / "broken.parquet"
    target.write_bytes(b"this is not parquet")
    with pytest.raises(ReadError, match="not readable as Parquet"):
        read_table(target)


# ---------------------------------------------------------------------------- sampling


def test_reading_a_whole_csv_knows_its_row_count(orders_csv: Path) -> None:
    loaded = read_table(orders_csv)
    assert loaded.table.num_rows == 500
    assert loaded.sampled is False
    assert loaded.total_row_count == 500


def test_sampling_a_csv_leaves_the_true_row_count_unknown(orders_csv: Path) -> None:
    """A CSV has no footer, so after sampling the total genuinely is not known."""
    loaded = read_table(orders_csv, sample=50)
    assert loaded.table.num_rows == 50
    assert loaded.sampled is True
    assert loaded.total_row_count is None


def test_a_sample_larger_than_the_csv_is_not_a_sample(orders_csv: Path) -> None:
    loaded = read_table(orders_csv, sample=10_000)
    assert loaded.sampled is False
    assert loaded.total_row_count == 500


def test_sampling_parquet_still_knows_the_total_from_the_footer(
    sensors_parquet: Path,
) -> None:
    loaded = read_table(sensors_parquet, sample=25)
    assert loaded.table.num_rows == 25
    assert loaded.sampled is True
    assert loaded.total_row_count == 300


def test_a_sample_larger_than_the_parquet_is_not_a_sample(sensors_parquet: Path) -> None:
    loaded = read_table(sensors_parquet, sample=10_000)
    assert loaded.sampled is False
    assert loaded.total_row_count == 300


# --------------------------------------------------------------------------- profiling


def test_profiling_a_csv(orders_csv: Path, catalog: Catalog) -> None:
    profile = _profile(orders_csv, catalog)
    assert profile.row_count == 500
    assert profile.column_count == 5
    assert profile.source_format == "csv"
    assert [column.name for column in profile.columns] == [
        "order_id",
        "status",
        "region",
        "amount",
        "customer_note",
    ]


def test_a_key_column_is_seen_as_distinct_and_complete(orders_csv: Path, catalog: Catalog) -> None:
    column = _column(_profile(orders_csv, catalog), "order_id")
    assert column.kind is ColumnKind.INTEGER
    assert column.null_count == 0
    assert column.distinct_count == 500
    assert column.is_distinct
    assert column.is_complete
    assert column.minimum == 100_000
    assert column.maximum == 100_499


def test_a_categorical_column_gets_its_complete_value_set(
    orders_csv: Path, catalog: Catalog
) -> None:
    column = _column(_profile(orders_csv, catalog), "status")
    assert column.kind is ColumnKind.STRING
    assert column.distinct_count == 4
    assert len(column.top_values) == 4
    assert {value for value, _ in column.top_values} == {"NEW", "PAID", "SHIPPED", "VOID"}
    assert sum(count for _, count in column.top_values) == 500


def test_a_column_with_nulls_reports_its_completeness(orders_csv: Path, catalog: Catalog) -> None:
    column = _column(_profile(orders_csv, catalog), "amount")
    assert column.null_count == 5
    assert column.completeness == pytest.approx(0.99)
    assert column.kind is ColumnKind.FLOAT
    assert column.mean is not None
    assert column.stddev is not None


def test_string_lengths_are_measured(orders_csv: Path, catalog: Catalog) -> None:
    column = _column(_profile(orders_csv, catalog), "customer_note")
    assert column.min_length == 5
    assert column.max_length == 16


def test_profiling_parquet(sensors_parquet: Path, catalog: Catalog) -> None:
    profile = _profile(sensors_parquet, catalog)
    assert profile.source_format == "parquet"
    assert profile.row_count == 300
    assert _column(profile, "calibrated").kind is ColumnKind.BOOLEAN
    assert _column(profile, "reading_c").kind is ColumnKind.FLOAT
    assert _column(profile, "battery_pct").null_count == 10


def test_a_constant_column_has_no_span(sensors_parquet: Path, catalog: Catalog) -> None:
    column = _column(_profile(sensors_parquet, catalog), "firmware")
    assert column.distinct_count == 1
    assert column.min_length == column.max_length == 5


def test_a_high_cardinality_column_yields_no_value_set(orders_csv: Path, catalog: Catalog) -> None:
    """Above the cap the profiler returns nothing rather than a partial set."""
    column = _column(_profile(orders_csv, catalog), "order_id")
    assert column.distinct_count == 500
    assert column.top_values == ()


def test_a_sampled_profile_records_that_it_was_sampled(orders_csv: Path, catalog: Catalog) -> None:
    profile = _profile(orders_csv, catalog, sample=100)
    assert profile.sampled is True
    assert profile.row_count == 100
    assert profile.row_count_is_known is False
    assert profile.known_row_count is None
