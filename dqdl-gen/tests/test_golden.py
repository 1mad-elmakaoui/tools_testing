"""Golden-file tests: exact output for the committed fixtures.

These are the tests that notice an unintended change to a template, a tolerance or the
emitter. When one fails, look at the diff: if the change was intended, regenerate with
``python scripts/make_golden.py`` and commit the new output deliberately.
"""

from __future__ import annotations

import difflib
from pathlib import Path

import pytest
from scripts.make_golden import CASES, build, golden_name

from dqdl_gen.catalog import Catalog
from dqdl_gen.models import Strictness
from dqdl_gen.validate import validate

GOLDEN = Path(__file__).resolve().parent / "golden"

IDS = [golden_name(fixture, level, sample) for fixture, level, sample in CASES]


def test_the_case_list_is_covered() -> None:
    assert len(CASES) >= 7
    assert len(set(IDS)) == len(IDS)


@pytest.mark.parametrize(("fixture", "strictness", "sample"), CASES, ids=IDS)
def test_output_matches_the_golden_file(
    fixture: str, strictness: Strictness, sample: int | None
) -> None:
    name = golden_name(fixture, strictness, sample)
    path = GOLDEN / name
    if not path.is_file():
        pytest.fail(f"{name} is missing; run python scripts/make_golden.py")

    expected = path.read_text(encoding="utf-8")
    actual = build(fixture, strictness, sample)
    if actual != expected:
        diff = "\n".join(
            difflib.unified_diff(
                expected.splitlines(),
                actual.splitlines(),
                fromfile=f"golden/{name}",
                tofile="generated",
                lineterm="",
            )
        )
        pytest.fail(
            f"generated DQDL no longer matches {name}. If this change was intended, run "
            f"python scripts/make_golden.py and commit the result.\n{diff}"
        )


@pytest.mark.parametrize(("fixture", "strictness", "sample"), CASES, ids=IDS)
def test_every_golden_file_is_valid_dqdl(
    fixture: str, strictness: Strictness, sample: int | None, catalog: Catalog
) -> None:
    path = GOLDEN / golden_name(fixture, strictness, sample)
    assert validate(path.read_text(encoding="utf-8"), catalog)


@pytest.mark.parametrize(("fixture", "strictness", "sample"), CASES, ids=IDS)
def test_every_rule_in_a_golden_file_has_a_comment_above_it(
    fixture: str, strictness: Strictness, sample: int | None
) -> None:
    """Every rule must carry the evidence it came from."""
    path = GOLDEN / golden_name(fixture, strictness, sample)
    lines = path.read_text(encoding="utf-8").splitlines()
    start = lines.index("Rules = [") + 1
    end = lines.index("]")
    previous_was_comment = False
    for line in lines[start:end]:
        stripped = line.strip()
        if not stripped:
            previous_was_comment = False
            continue
        if stripped.startswith("#"):
            previous_was_comment = True
            continue
        assert previous_was_comment, f"rule without evidence in {path.name}: {stripped}"
        previous_was_comment = False


def test_generation_is_reproducible() -> None:
    """Byte-identical on a second run: nothing time- or order-dependent leaks in."""
    fixture, strictness, sample = CASES[1]
    assert build(fixture, strictness, sample) == build(fixture, strictness, sample)


def test_golden_files_have_no_timestamp() -> None:
    """A timestamp in the header would make every regeneration a diff."""
    for name in IDS:
        text = (GOLDEN / name).read_text(encoding="utf-8")
        assert "generated at" not in text.lower()


def test_strictness_widens_the_numeric_range() -> None:
    """The same column should get a wider range as strictness relaxes."""
    ranges = {}
    for level in (Strictness.STRICT, Strictness.BALANCED, Strictness.LENIENT):
        text = (GOLDEN / golden_name("orders.csv", level, None)).read_text(encoding="utf-8")
        line = next(
            item for item in text.splitlines() if item.strip().startswith('ColumnValues "amount"')
        )
        parts = line.strip().rstrip(",").split()
        ranges[level] = (float(parts[3]), float(parts[5]))
    assert ranges[Strictness.STRICT][0] > ranges[Strictness.BALANCED][0]
    assert ranges[Strictness.BALANCED][0] > ranges[Strictness.LENIENT][0]
    assert ranges[Strictness.STRICT][1] < ranges[Strictness.BALANCED][1]
    assert ranges[Strictness.BALANCED][1] < ranges[Strictness.LENIENT][1]


def test_a_sampled_csv_golden_has_no_row_count_rule() -> None:
    text = (GOLDEN / golden_name("orders.csv", Strictness.BALANCED, 120)).read_text(
        encoding="utf-8"
    )
    assert "RowCount" not in text
    assert "file total unknown" in text


def test_a_sampled_parquet_golden_keeps_its_row_count_rule() -> None:
    """Parquet reports its row count in the footer, so the rule is not a guess."""
    text = (GOLDEN / golden_name("sensors.parquet", Strictness.BALANCED, 60)).read_text(
        encoding="utf-8"
    )
    assert "RowCount between" in text
    assert "60 of 300 rows" in text


def test_a_small_file_gets_no_allowed_value_rule() -> None:
    text = (GOLDEN / golden_name("tiny.csv", Strictness.BALANCED, None)).read_text(encoding="utf-8")
    assert 'ColumnValues "label" in [' not in text


def test_no_golden_file_declares_a_string_column_data_type() -> None:
    """ColumnDataType does not accept String, so it must never appear."""
    for name in IDS:
        text = (GOLDEN / name).read_text(encoding="utf-8")
        assert '= "String"' not in text
