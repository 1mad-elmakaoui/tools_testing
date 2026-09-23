"""Check generated DQDL before it is written out.

This is deliberately more than a parse. AWS's own grammar defines a rule type as a bare
identifier (``ruleType: IDENTIFIER``), so a grammar-level parse accepts ``Frobnicate "x" > 1``
without complaint. A check that only parsed would therefore give false confidence about the
thing most likely to be wrong — a rule type that does not exist, or one given the wrong
number of parameters.

So the tokenizer follows AWS's lexer rules, the parser follows the subset of the grammar
this tool emits, and every rule is then checked against the catalogue: the name must be one
we are allowed to emit, the parameter count must match, and the condition must be of a kind
that rule type accepts.

Scope: the subset ``dqdl-gen`` generates. It does not accept the whole language — no
Metadata or DataSources sections, no variables, composites, where clauses or thresholds —
and says so rather than pretending otherwise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum, auto

from dqdl_gen.catalog import Catalog, ConditionKind

RULES_KEYWORD = "Rules"


class DqdlSyntaxError(ValueError):
    """Raised when generated DQDL is not valid."""


class TokenKind(StrEnum):
    """Token categories, mirroring AWS's CommonLexerRules."""

    IDENTIFIER = auto()
    NUMBER = auto()
    STRING = auto()
    OPERATOR = auto()
    PUNCTUATION = auto()


@dataclass(frozen=True)
class Token:
    """One lexed token, with the line it came from for error messages."""

    kind: TokenKind
    text: str
    line: int
    #: For a STRING token, the value with its escapes resolved.
    value: str = ""


# Longest first, so ">=" is not lexed as ">" followed by "=".
_OPERATORS = (">=", "<=", "!=", "=", ">", "<")
_PUNCTUATION = ("[", "]", ",", "(", ")")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_IDENTIFIER = re.compile(r"[a-zA-Z0-9_.]+")
_COMMENT = re.compile(r"#[^\n]*\n")


def tokenize(text: str) -> list[Token]:
    """Lex DQDL into tokens, discarding comments and whitespace.

    Comments are stripped here rather than with a regex over the whole document, because a
    ``#`` inside a quoted string is part of the string, not the start of a comment.
    """
    tokens: list[Token] = []
    position = 0
    line = 1
    length = len(text)

    while position < length:
        char = text[position]

        if char in " \t\r\n":
            if char == "\n":
                line += 1
            position += 1
            continue

        if char == "#":
            match = _COMMENT.match(text, position)
            if match is None:
                raise DqdlSyntaxError(
                    f"line {line}: a comment must end with a newline. AWS's lexer defines "
                    f"LINE_COMMENT as '#' .*? '\\r'? '\\n', so a comment on an unterminated "
                    f"final line is not skipped and the ruleset fails to parse."
                )
            position = match.end()
            line += 1
            continue

        if char == '"':
            token, position = _lex_string(text, position, line)
            tokens.append(token)
            continue

        number = _NUMBER.match(text, position)
        if number is not None:
            tokens.append(Token(TokenKind.NUMBER, number.group(), line))
            position = number.end()
            continue

        identifier = _IDENTIFIER.match(text, position)
        if identifier is not None:
            tokens.append(Token(TokenKind.IDENTIFIER, identifier.group(), line))
            position = identifier.end()
            continue

        operator = next((op for op in _OPERATORS if text.startswith(op, position)), None)
        if operator is not None:
            tokens.append(Token(TokenKind.OPERATOR, operator, line))
            position += len(operator)
            continue

        if char in _PUNCTUATION:
            tokens.append(Token(TokenKind.PUNCTUATION, char, line))
            position += 1
            continue

        raise DqdlSyntaxError(f"line {line}: unexpected character {char!r}")

    return tokens


def _lex_string(text: str, position: int, line: int) -> tuple[Token, int]:
    """Lex one quoted string, honouring the only two escapes DQDL recognises."""
    cursor = position + 1
    parts: list[str] = []
    while cursor < len(text):
        char = text[cursor]
        if char == "\\" and cursor + 1 < len(text) and text[cursor + 1] in '"\\':
            parts.append(text[cursor + 1])
            cursor += 2
            continue
        if char == '"':
            raw = text[position : cursor + 1]
            return Token(TokenKind.STRING, raw, line, "".join(parts)), cursor + 1
        if char == "\n":
            break
        parts.append(char)
        cursor += 1
    raise DqdlSyntaxError(f"line {line}: unterminated quoted string")


@dataclass(frozen=True)
class ParsedRule:
    """One rule recovered from generated text, for checking against the catalogue."""

    rule_type: str
    parameters: tuple[str, ...]
    condition: ConditionKind
    condition_text: str
    string_values: tuple[str, ...]
    line: int


def validate(text: str, catalog: Catalog) -> tuple[ParsedRule, ...]:
    """Validate a generated ruleset, returning its rules.

    Raises :class:`DqdlSyntaxError` on the first problem found.
    """
    if not text.endswith("\n"):
        raise DqdlSyntaxError(
            "the ruleset must end with a newline, otherwise a trailing comment does not "
            "lex as a comment"
        )

    tokens = tokenize(text)
    if not tokens:
        raise DqdlSyntaxError("the ruleset is empty")

    cursor = _expect_header(tokens)
    rules: list[ParsedRule] = []

    if _at(tokens, cursor, "]"):
        cursor += 1
    else:
        while True:
            rule, cursor = _parse_rule(tokens, cursor, catalog)
            rules.append(rule)
            if _at(tokens, cursor, ","):
                cursor += 1
                continue
            if _at(tokens, cursor, "]"):
                cursor += 1
                break
            raise DqdlSyntaxError(f"{_where(tokens, cursor)}: expected ',' or ']' after a rule")

    if cursor != len(tokens):
        raise DqdlSyntaxError(f"{_where(tokens, cursor)}: unexpected content after the closing ']'")
    return tuple(rules)


def _expect_header(tokens: list[Token]) -> int:
    if tokens[0].text != RULES_KEYWORD:
        raise DqdlSyntaxError(
            f"line {tokens[0].line}: a ruleset must start with {RULES_KEYWORD!r}, "
            f"got {tokens[0].text!r}. This checker covers the subset dqdl-gen emits, "
            f"which has no Metadata or DataSources section."
        )
    if not _at(tokens, 1, "="):
        raise DqdlSyntaxError(f"{_where(tokens, 1)}: expected '=' after {RULES_KEYWORD!r}")
    if not _at(tokens, 2, "["):
        raise DqdlSyntaxError(f"{_where(tokens, 2)}: expected '[' after '='")
    return 3


def _parse_rule(tokens: list[Token], cursor: int, catalog: Catalog) -> tuple[ParsedRule, int]:
    if cursor >= len(tokens) or tokens[cursor].kind is not TokenKind.IDENTIFIER:
        raise DqdlSyntaxError(f"{_where(tokens, cursor)}: expected a rule type name")
    name_token = tokens[cursor]
    cursor += 1

    parameters: list[str] = []
    while cursor < len(tokens) and tokens[cursor].kind is TokenKind.STRING:
        parameters.append(tokens[cursor].value)
        cursor += 1

    condition, condition_text, strings, cursor = _parse_condition(tokens, cursor)

    spec = catalog.rule_types.get(name_token.text)
    if spec is None:
        known = ", ".join(sorted(catalog.rule_types))
        raise DqdlSyntaxError(
            f"line {name_token.line}: {name_token.text!r} is not a rule type this tool "
            f"emits. AWS's grammar accepts any identifier here, so this check is the only "
            f"thing standing between a typo and an invalid ruleset. Known: {known}"
        )
    if len(parameters) != spec.parameters:
        raise DqdlSyntaxError(
            f"line {name_token.line}: {spec.name} takes {spec.parameters} parameter(s), "
            f"got {len(parameters)}"
        )
    if not spec.accepts(condition):
        raise DqdlSyntaxError(
            f"line {name_token.line}: {spec.name} does not accept a "
            f"{condition.value} condition (it takes: {spec.condition.value})"
        )
    if spec.name == "ColumnDataType":
        _check_data_type(strings, catalog, name_token.line)

    return (
        ParsedRule(
            rule_type=spec.name,
            parameters=tuple(parameters),
            condition=condition,
            condition_text=condition_text,
            string_values=strings,
            line=name_token.line,
        ),
        cursor,
    )


def _parse_condition(
    tokens: list[Token], cursor: int
) -> tuple[ConditionKind, str, tuple[str, ...], int]:
    """Parse the condition after a rule's parameters, if there is one."""
    if cursor >= len(tokens) or _at_any(tokens, cursor, (",", "]")):
        return ConditionKind.NONE, "", (), cursor

    start = cursor
    token = tokens[cursor]
    strings: list[str] = []

    if token.kind is TokenKind.OPERATOR:
        cursor += 1
        cursor, kind = _parse_operand(tokens, cursor, strings)
    elif token.kind is TokenKind.IDENTIFIER and token.text == "between":
        cursor += 1
        cursor, kind = _parse_operand(tokens, cursor, strings)
        if not (
            cursor < len(tokens)
            and tokens[cursor].kind is TokenKind.IDENTIFIER
            and tokens[cursor].text == "and"
        ):
            raise DqdlSyntaxError(f"{_where(tokens, cursor)}: expected 'and' in a range")
        cursor += 1
        cursor, _ = _parse_operand(tokens, cursor, strings)
    elif token.kind is TokenKind.IDENTIFIER and token.text == "in":
        cursor += 1
        cursor, kind = _parse_array(tokens, cursor, strings)
    else:
        raise DqdlSyntaxError(f"{_where(tokens, cursor)}: expected a condition, got {token.text!r}")

    text = " ".join(item.text for item in tokens[start:cursor])
    return kind, text, tuple(strings), cursor


def _parse_operand(
    tokens: list[Token], cursor: int, strings: list[str]
) -> tuple[int, ConditionKind]:
    if cursor >= len(tokens):
        raise DqdlSyntaxError("unexpected end of ruleset: expected a value")
    token = tokens[cursor]
    if token.kind is TokenKind.NUMBER:
        return cursor + 1, ConditionKind.NUMBER
    if token.kind is TokenKind.STRING:
        strings.append(token.value)
        return cursor + 1, ConditionKind.STRING
    raise DqdlSyntaxError(
        f"{_where(tokens, cursor)}: expected a number or quoted string, got {token.text!r}"
    )


def _parse_array(tokens: list[Token], cursor: int, strings: list[str]) -> tuple[int, ConditionKind]:
    if not _at(tokens, cursor, "["):
        raise DqdlSyntaxError(f"{_where(tokens, cursor)}: expected '[' after 'in'")
    cursor += 1
    if _at(tokens, cursor, "]"):
        raise DqdlSyntaxError(f"{_where(tokens, cursor)}: an 'in' list must not be empty")
    cursor, kind = _parse_operand(tokens, cursor, strings)
    while _at(tokens, cursor, ","):
        cursor += 1
        cursor, item = _parse_operand(tokens, cursor, strings)
        if item is not kind:
            raise DqdlSyntaxError(
                f"{_where(tokens, cursor)}: an 'in' list must not mix numbers and strings"
            )
    if not _at(tokens, cursor, "]"):
        raise DqdlSyntaxError(f"{_where(tokens, cursor)}: expected ']' to close an 'in' list")
    return cursor + 1, kind


def _check_data_type(strings: tuple[str, ...], catalog: Catalog, line: int) -> None:
    for value in strings:
        if value not in catalog.data_types:
            allowed = ", ".join(catalog.data_types)
            raise DqdlSyntaxError(
                f"line {line}: {value!r} is not a ColumnDataType DQDL accepts. "
                f"Allowed: {allowed}. Note that 'String' is deliberately not among them."
            )


def _at(tokens: list[Token], cursor: int, text: str) -> bool:
    return cursor < len(tokens) and tokens[cursor].text == text


def _at_any(tokens: list[Token], cursor: int, options: tuple[str, ...]) -> bool:
    return any(_at(tokens, cursor, option) for option in options)


def _where(tokens: list[Token], cursor: int) -> str:
    if cursor >= len(tokens):
        return "end of ruleset"
    return f"line {tokens[cursor].line}"
