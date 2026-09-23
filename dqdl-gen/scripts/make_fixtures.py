"""Regenerate the test fixtures.

The fixtures are committed rather than built during the test run, because the golden files
assert exact output and a fixture that drifted would make those tests lie. Everything here
is deterministic: no randomness, no timestamps.

    .venv/bin/python scripts/make_fixtures.py
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

STATUSES = ("NEW", "PAID", "SHIPPED", "VOID")
REGIONS = ("ap-south-1", "eu-west-1", "us-east-1")


def orders_rows() -> list[str]:
    """A realistic mixed-type table: a key, two categories, a measure with nulls, free text."""
    rows = ["order_id,status,region,amount,customer_note"]
    for index in range(500):
        order_id = 100_000 + index
        status = STATUSES[index % 4]
        region = REGIONS[index % 3]
        # Every hundredth amount is missing, giving a completeness of 0.99 exactly.
        amount = "" if index % 100 == 0 else f"{(index * 7) % 900 + 5}.50"
        note = "note-" + "x" * (index % 12)
        rows.append(f"{order_id},{status},{region},{amount},{note}")
    return rows


def tiny_rows() -> list[str]:
    """Fewer rows than min_rows_for_value_set, so no allowed-value rule should appear."""
    rows = ["code,label"]
    for index in range(12):
        rows.append(f"{index},{'AB'[index % 2]}")
    return rows


def sensors_table() -> pa.Table:
    """Numeric and boolean columns, including one constant and one with nulls."""
    readings = [round(20 + (index % 50) * 0.5, 2) for index in range(300)]
    battery = [None if index % 30 == 0 else (index % 100) for index in range(300)]
    return pa.table(
        {
            "sensor_id": pa.array([f"S{index % 8:03d}" for index in range(300)]),
            "reading_c": pa.array(readings, type=pa.float64()),
            "battery_pct": pa.array(battery, type=pa.int64()),
            "calibrated": pa.array([index % 2 == 0 for index in range(300)]),
            "firmware": pa.array(["4.2.1"] * 300),
        }
    )


def main() -> None:
    """Write every fixture."""
    FIXTURES.mkdir(parents=True, exist_ok=True)
    (FIXTURES / "orders.csv").write_text("\n".join(orders_rows()) + "\n", encoding="utf-8")
    (FIXTURES / "tiny.csv").write_text("\n".join(tiny_rows()) + "\n", encoding="utf-8")
    pq.write_table(sensors_table(), FIXTURES / "sensors.parquet", compression="snappy")
    for path in sorted(FIXTURES.iterdir()):
        print(f"wrote {path.name} ({path.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
