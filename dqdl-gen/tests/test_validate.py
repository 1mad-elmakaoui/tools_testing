"""The DQDL checker: what it accepts, and the mistakes it must not let through."""

from __future__ import annotations

import pytest

from dqdl_gen.catalog import Catalog, ConditionKind
from dqdl_gen.validate import DqdlSyntaxError, TokenKind, tokenize, validate

VALID = """# a generated ruleset
Rules = [
    # evidence
    RowCount between 1 and 10,
    IsComplete "order_id",
    IsPrimaryKey "order_id",
    Completeness "amount" >= 0.98,
    ColumnValues "status" in ["NEW", "PAID"],
    ColumnValues "amount" between -1.5 and 99,
    ColumnLength "note" between 0 and 40,
    ColumnDataType "amount" = "Double",
    DistinctValuesCount "status" between 1 and 2
]
"""


def test_a_generated_ruleset_validates(catalog: Catalog) -> None:
    rules = validate(VALID, catalog)
    assert len(rules) == 9
    assert rules[0].rule_type == "RowCount"
    assert rules[1].parameters == ("order_id",)
    assert rules[4].condition is ConditionKind.STRING
    assert rules[5].condition is ConditionKind.NUMBER


def test_an_empty_rule_list_is_valid(catalog: Catalog) -> None:
    assert validate("Rules = []\n", catalog) == ()


# --------------------------------------------------------------------------- tokenizer


def test_a_hash_inside_a_string_is_not_a_comment(catalog: Catalog) -> None:
    """Stripping comments with a regex over the document would corrupt this ruleset."""
    text = 'Rules = [\n    ColumnValues "tag" in ["#1", "#2"]\n]\n'
    rules = validate(text, catalog)
    assert rules[0].parameters == ("tag",)
    assert rules[0].string_values == ("#1", "#2")


def test_escapes_are_resolved(catalog: Catalog) -> None:
    text = 'Rules = [\n    ColumnValues "c" in ["a\\"b", "c\\\\d"]\n]\n'
    assert validate(text, catalog)[0].string_values == ('a"b', "c\\d")


def test_operators_are_lexed_longest_first() -> None:
    tokens = tokenize("Rules = [ RowCount >= 5 ]\n")
    operators = [token.text for token in tokens if token.kind is TokenKind.OPERATOR]
    assert operators == ["=", ">="]


def test_line_numbers_are_tracked() -> None:
    tokens = tokenize("Rules = [\n\n    RowCount > 1\n]\n")
    assert next(token for token in tokens if token.text == "RowCount").line == 3


def test_an_unterminated_string_is_reported() -> None:
    with pytest.raises(DqdlSyntaxError, match="unterminated quoted string"):
        tokenize('Rules = [ IsComplete "oops\n]\n')


def test_an_unexpected_character_is_reported() -> None:
    with pytest.raises(DqdlSyntaxError, match="unexpected character"):
        tokenize("Rules = [ RowCount @ 5 ]\n")


# ------------------------------------------------------------------ structural errors


def test_a_missing_trailing_newline_is_rejected(catalog: Catalog) -> None:
    """AWS's lexer only skips a comment that ends in a newline."""
    with pytest.raises(DqdlSyntaxError, match="must end with a newline"):
        validate(VALID.rstrip("\n"), catalog)


def test_a_comment_without_a_newline_is_rejected() -> None:
    """A file ending mid-comment does not lex, which is why emitted files end in a newline."""
    with pytest.raises(DqdlSyntaxError, match="comment must end with a newline"):
        tokenize("Rules = [\n    RowCount > 1\n]\n# dangling")


def test_an_empty_document_is_rejected(catalog: Catalog) -> None:
    with pytest.raises(DqdlSyntaxError, match="the ruleset is empty"):
        validate("\n", catalog)


def test_a_missing_rules_keyword_is_rejected(catalog: Catalog) -> None:
    with pytest.raises(DqdlSyntaxError, match="must start with 'Rules'"):
        validate("Analyzers = [ RowCount ]\n", catalog)


def test_a_missing_equals_is_rejected(catalog: Catalog) -> None:
    with pytest.raises(DqdlSyntaxError, match="expected '='"):
        validate("Rules [ RowCount > 1 ]\n", catalog)


def test_a_missing_bracket_is_rejected(catalog: Catalog) -> None:
    with pytest.raises(DqdlSyntaxError, match=r"expected '\['"):
        validate("Rules = RowCount > 1\n", catalog)


def test_a_missing_comma_is_rejected(catalog: Catalog) -> None:
    with pytest.raises(DqdlSyntaxError, match=r"expected ',' or '\]'"):
        validate('Rules = [ RowCount > 1 IsComplete "a" ]\n', catalog)


def test_trailing_content_is_rejected(catalog: Catalog) -> None:
    with pytest.raises(DqdlSyntaxError, match="unexpected content after"):
        validate("Rules = [ RowCount > 1 ] extra\n", catalog)


# ------------------------------------------------------------------- catalogue errors


def test_an_invented_rule_type_is_rejected(catalog: Catalog) -> None:
    """AWS's grammar defines ruleType as a bare identifier, so only this check catches it."""
    with pytest.raises(DqdlSyntaxError, match="not a rule type this tool emits"):
        validate("Rules = [ Frobnicate > 1 ]\n", catalog)


def test_too_many_parameters_are_rejected(catalog: Catalog) -> None:
    with pytest.raises(DqdlSyntaxError, match="takes 1 parameter"):
        validate('Rules = [ IsComplete "a" "b" ]\n', catalog)


def test_too_few_parameters_are_rejected(catalog: Catalog) -> None:
    with pytest.raises(DqdlSyntaxError, match="takes 1 parameter"):
        validate("Rules = [ IsComplete ]\n", catalog)


def test_a_condition_on_a_boolean_rule_is_rejected(catalog: Catalog) -> None:
    with pytest.raises(DqdlSyntaxError, match="does not accept a number condition"):
        validate('Rules = [ IsComplete "a" > 5 ]\n', catalog)


def test_a_missing_condition_is_rejected(catalog: Catalog) -> None:
    with pytest.raises(DqdlSyntaxError, match="does not accept a none condition"):
        validate("Rules = [ RowCount ]\n", catalog)


def test_a_string_condition_on_a_numeric_rule_is_rejected(catalog: Catalog) -> None:
    with pytest.raises(DqdlSyntaxError, match="does not accept a string condition"):
        validate('Rules = [ RowCount = "many" ]\n', catalog)


def test_string_is_rejected_as_a_column_data_type(catalog: Catalog) -> None:
    with pytest.raises(DqdlSyntaxError, match="deliberately not among them"):
        validate('Rules = [ ColumnDataType "a" = "String" ]\n', catalog)


def test_a_valid_column_data_type_is_accepted(catalog: Catalog) -> None:
    assert validate('Rules = [ ColumnDataType "a" = "Timestamp" ]\n', catalog)


# ------------------------------------------------------------------- condition errors


def test_a_range_without_and_is_rejected(catalog: Catalog) -> None:
    with pytest.raises(DqdlSyntaxError, match="expected 'and' in a range"):
        validate("Rules = [ RowCount between 1 10 ]\n", catalog)


def test_an_empty_in_list_is_rejected(catalog: Catalog) -> None:
    with pytest.raises(DqdlSyntaxError, match="must not be empty"):
        validate('Rules = [ ColumnValues "a" in [] ]\n', catalog)


def test_an_unclosed_in_list_is_rejected(catalog: Catalog) -> None:
    with pytest.raises(DqdlSyntaxError, match=r"expected '\]' to close"):
        validate('Rules = [ ColumnValues "a" in ["x" "y"] ]\n', catalog)


def test_a_mixed_in_list_is_rejected(catalog: Catalog) -> None:
    with pytest.raises(DqdlSyntaxError, match="must not mix numbers and strings"):
        validate('Rules = [ ColumnValues "a" in ["x", 1] ]\n', catalog)


def test_in_without_a_bracket_is_rejected(catalog: Catalog) -> None:
    with pytest.raises(DqdlSyntaxError, match=r"expected '\[' after 'in'"):
        validate('Rules = [ ColumnValues "a" in "x" ]\n', catalog)


def test_a_nonsense_condition_is_rejected(catalog: Catalog) -> None:
    with pytest.raises(DqdlSyntaxError, match="expected a condition"):
        validate("Rules = [ RowCount maybe 5 ]\n", catalog)


def test_an_operand_that_is_not_a_value_is_rejected(catalog: Catalog) -> None:
    with pytest.raises(DqdlSyntaxError, match="expected a number or quoted string"):
        validate("Rules = [ RowCount > between ]\n", catalog)
