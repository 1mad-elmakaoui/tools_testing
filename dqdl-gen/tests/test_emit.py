"""Rendering a ruleset as DQDL text."""

from __future__ import annotations

from dqdl_gen.catalog import Catalog
from dqdl_gen.emit import COMMENT_WIDTH, emit, render_rule
from dqdl_gen.models import ColumnKind, ColumnProfile, DatasetProfile, Rule, Ruleset, Strictness


def _ruleset(*rules: Rule, **overrides: object) -> Ruleset:
    column = ColumnProfile(
        name="col", kind=ColumnKind.STRING, source_type="string", row_count=10, null_count=0
    )
    base: dict[str, object] = {
        "source_name": "fixture.csv",
        "source_format": "csv",
        "row_count": 10,
        "columns": (column,),
    }
    base.update(overrides)
    profile = DatasetProfile(**base)  # type: ignore[arg-type]
    return Ruleset(profile=profile, strictness=Strictness.BALANCED, rules=rules)


def test_rendering_a_rule_without_a_condition() -> None:
    assert render_rule(Rule("IsComplete", ("order id",))) == 'IsComplete "order id"'


def test_rendering_a_rule_with_a_condition() -> None:
    assert render_rule(Rule("RowCount", (), "between 1 and 2")) == "RowCount between 1 and 2"


def test_column_names_are_quoted_and_escaped() -> None:
    assert render_rule(Rule("IsComplete", ('od"d',))) == 'IsComplete "od\\"d"'


def test_output_always_ends_with_a_newline(catalog: Catalog) -> None:
    """Required: AWS's lexer only skips a comment that is newline-terminated."""
    assert emit(_ruleset(Rule("RowCount", (), "> 0", "evidence")), catalog).endswith("\n")


def test_an_empty_ruleset_emits_an_empty_list(catalog: Catalog) -> None:
    text = emit(_ruleset(), catalog)
    assert "Rules = []" in text
    assert text.endswith("\n")


def test_the_header_names_the_source_and_strictness(catalog: Catalog) -> None:
    text = emit(_ruleset(Rule("RowCount", (), "> 0", "e")), catalog)
    assert "from fixture.csv (csv)" in text
    assert "Strictness: balanced" in text
    assert catalog.last_verified in text


def test_the_header_reports_sampling(catalog: Catalog) -> None:
    ruleset = _ruleset(Rule("RowCount", (), "> 0", "e"), sampled=True, total_row_count=99)
    assert "10 of 99 rows profiled from a sample" in emit(ruleset, catalog)


def test_the_header_admits_when_the_total_is_unknown(catalog: Catalog) -> None:
    ruleset = _ruleset(Rule("RowCount", (), "> 0", "e"), sampled=True, total_row_count=None)
    assert "file total unknown" in emit(ruleset, catalog)


def test_long_evidence_is_wrapped_across_comment_lines(catalog: Catalog) -> None:
    evidence = "word " * 60
    text = emit(_ruleset(Rule("RowCount", (), "> 0", evidence)), catalog)
    comment_lines = [line for line in text.splitlines() if line.strip().startswith("#")]
    assert len(comment_lines) > 5
    assert all(len(line) <= COMMENT_WIDTH + 4 for line in comment_lines)


def test_hyphenated_words_are_not_split_across_lines(catalog: Catalog) -> None:
    evidence = "padding " * 20 + "allowed-value rule"
    text = emit(_ruleset(Rule("RowCount", (), "> 0", evidence)), catalog)
    assert "allowed-\n" not in text


def test_rules_are_separated_by_commas_but_the_last_is_not(catalog: Catalog) -> None:
    text = emit(
        _ruleset(
            Rule("RowCount", (), "> 0", "a"),
            Rule("ColumnCount", (), "= 1", "b"),
        ),
        catalog,
    )
    rule_lines = [
        line.strip()
        for line in text.splitlines()
        if line.startswith("    ") and not line.strip().startswith("#")
    ]
    assert rule_lines == ["RowCount > 0,", "ColumnCount = 1"]
