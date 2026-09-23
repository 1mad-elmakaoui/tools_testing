"""Plain data types shared by the profiler, the generator and the emitter.

Frozen dataclasses and enums only: no I/O, no AWS, nothing that needs a file on disk to
construct. Rule generation can therefore be tested against hand-built profiles.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ColumnKind(StrEnum):
    """What a column holds, reduced to the categories the rules care about."""

    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    STRING = "string"
    DATE = "date"
    TIMESTAMP = "timestamp"
    OTHER = "other"

    @property
    def is_numeric(self) -> bool:
        """Whether range and mean rules make sense for this kind."""
        return self in {ColumnKind.INTEGER, ColumnKind.FLOAT}


class Strictness(StrEnum):
    """How much room generated rules leave for normal variation."""

    STRICT = "strict"
    BALANCED = "balanced"
    LENIENT = "lenient"


@dataclass(frozen=True)
class ColumnProfile:
    """What profiling observed about one column.

    Every optional field is ``None`` when it could not be computed rather than being given a
    stand-in value, so a rule is never generated from a number nobody measured.
    """

    name: str
    kind: ColumnKind
    source_type: str
    row_count: int
    null_count: int
    distinct_count: int | None = None
    minimum: float | None = None
    maximum: float | None = None
    mean: float | None = None
    stddev: float | None = None
    min_length: int | None = None
    max_length: int | None = None
    top_values: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        if self.row_count < 0:
            raise ValueError(f"row_count must not be negative, got {self.row_count}")
        if not 0 <= self.null_count <= self.row_count:
            raise ValueError(f"null_count {self.null_count} must be between 0 and {self.row_count}")

    @property
    def non_null_count(self) -> int:
        """How many values were actually present."""
        return self.row_count - self.null_count

    @property
    def completeness(self) -> float:
        """Fraction of rows that are not null. An empty column counts as complete."""
        if self.row_count == 0:
            return 1.0
        return self.non_null_count / self.row_count

    @property
    def uniqueness(self) -> float | None:
        """Distinct values as a fraction of non-null values, or None if not counted."""
        if self.distinct_count is None or self.non_null_count == 0:
            return None
        return self.distinct_count / self.non_null_count

    @property
    def is_complete(self) -> bool:
        """Whether the column contains no nulls at all."""
        return self.null_count == 0

    @property
    def is_distinct(self) -> bool:
        """Whether every non-null value is different."""
        return (
            self.distinct_count is not None
            and self.non_null_count > 0
            and self.distinct_count == self.non_null_count
        )


@dataclass(frozen=True)
class DatasetProfile:
    """What profiling observed about a whole file."""

    source_name: str
    source_format: str
    row_count: int
    columns: tuple[ColumnProfile, ...]
    sampled: bool = False
    total_row_count: int | None = None

    def __post_init__(self) -> None:
        if self.row_count < 0:
            raise ValueError(f"row_count must not be negative, got {self.row_count}")
        names = [column.name for column in self.columns]
        if len(names) != len(set(names)):
            raise ValueError("column names must be unique within a profile")

    @property
    def column_count(self) -> int:
        """How many columns the file has."""
        return len(self.columns)

    @property
    def row_count_is_known(self) -> bool:
        """Whether the true number of rows in the file is known.

        False after sampling a format whose total cannot be read cheaply, in which case no
        row-count rule is generated rather than one based on the sample size.
        """
        return not self.sampled or self.total_row_count is not None

    @property
    def known_row_count(self) -> int | None:
        """The file's true row count, or None when only a sample was seen."""
        if not self.sampled:
            return self.row_count
        return self.total_row_count


@dataclass(frozen=True)
class Rule:
    """One generated DQDL rule, with the evidence that produced it."""

    rule_type: str
    parameters: tuple[str, ...] = ()
    condition: str | None = None
    comment: str = ""

    def __post_init__(self) -> None:
        if not self.rule_type:
            raise ValueError("rule_type must not be empty")


@dataclass(frozen=True)
class Ruleset:
    """A generated ruleset, ready to be emitted as DQDL."""

    profile: DatasetProfile
    strictness: Strictness
    rules: tuple[Rule, ...]

    @property
    def rule_count(self) -> int:
        """How many rules were generated."""
        return len(self.rules)

    def rules_for(self, column: str) -> tuple[Rule, ...]:
        """Every rule whose first parameter is `column`."""
        return tuple(rule for rule in self.rules if rule.parameters[:1] == (column,))
