"""Regenerate the golden .dqdl files.

The golden tests assert exact output for the committed fixtures, which is what stops a
change to the templates or the tolerances slipping through unnoticed. Run this after an
intentional change, and read the resulting diff before committing it.

    .venv/bin/python scripts/make_golden.py
"""

from __future__ import annotations

from pathlib import Path

from dqdl_gen.catalog import load_catalog
from dqdl_gen.emit import emit
from dqdl_gen.generate import generate
from dqdl_gen.models import Strictness
from dqdl_gen.profile import profile_table
from dqdl_gen.reader import read_table
from dqdl_gen.validate import validate

TESTS = Path(__file__).resolve().parent.parent / "tests"
FIXTURES = TESTS / "fixtures"
GOLDEN = TESTS / "golden"

#: fixture file, strictness, and the sample size to read (None for the whole file).
CASES: tuple[tuple[str, Strictness, int | None], ...] = (
    ("orders.csv", Strictness.STRICT, None),
    ("orders.csv", Strictness.BALANCED, None),
    ("orders.csv", Strictness.LENIENT, None),
    ("orders.csv", Strictness.BALANCED, 120),
    ("sensors.parquet", Strictness.BALANCED, None),
    ("sensors.parquet", Strictness.BALANCED, 60),
    ("tiny.csv", Strictness.BALANCED, None),
)


def golden_name(fixture: str, strictness: Strictness, sample: int | None) -> str:
    """The golden file name for one case."""
    stem = Path(fixture).stem
    suffix = "" if sample is None else f".sample{sample}"
    return f"{stem}.{strictness.value}{suffix}.dqdl"


def build(fixture: str, strictness: Strictness, sample: int | None) -> str:
    """Run the whole pipeline for one case and return the DQDL."""
    catalog = load_catalog()
    profile = profile_table(
        read_table(FIXTURES / fixture, sample=sample),
        max_value_set=catalog.shape.max_allowed_value_set,
        top_values_shown=catalog.shape.top_values_shown,
    )
    text = emit(generate(profile, catalog, strictness), catalog)
    # Never commit a golden file the checker would reject.
    validate(text, catalog)
    return text


def main() -> None:
    """Write every golden file."""
    GOLDEN.mkdir(parents=True, exist_ok=True)
    for fixture, strictness, sample in CASES:
        name = golden_name(fixture, strictness, sample)
        (GOLDEN / name).write_text(build(fixture, strictness, sample), encoding="utf-8")
        print(f"wrote {name}")


if __name__ == "__main__":
    main()
