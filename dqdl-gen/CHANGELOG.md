# Changelog

All notable changes to `dqdl-gen` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-09-23

First release.

### Added

#### Profiling

- Reads CSV and Parquet through pyarrow, with `--sample N` for large files.
- Profiles every column: inferred type, null count and rate, distinct count, min and max,
  mean and standard deviation for numerics, string length range, and the complete value set
  for low-cardinality columns.
- A statistic that cannot be computed stays unset rather than being given a stand-in, so a
  missing measurement produces a missing rule rather than a guessed one.

#### Rule generation

- Generates `RowCount`, `ColumnCount`, `IsComplete`, `Completeness`, `IsPrimaryKey`,
  `IsUnique`, `Uniqueness`, `ColumnValues` (allowed-value sets and numeric ranges),
  `ColumnLength`, `ColumnDataType` and `DistinctValuesCount`.
- Every rule carries a `#` comment stating the evidence it came from, including whether the
  profile was sampled.
- `--strictness strict|balanced|lenient` controls how much room rules leave for normal
  variation. An observed completeness of 0.992 becomes a rule at 0.99, 0.98 or 0.94.
- All rule templates live in one module, and every threshold is declared in the data file.
- Sampling a CSV leaves the true row count unknown, so **no** `RowCount` rule is generated
  in that case, and the output says why. Parquet records its row count in the footer, so a
  sampled Parquet still gets an exact one.
- String columns get no `ColumnDataType` rule, because DQDL does not accept `String` as a
  value for that rule type.
- A distinct float column is not treated as a key: values that all differ in one file are
  not evidence of a key.

#### Checking

- Generated DQDL is validated before it is written, and nothing invalid is ever sent to AWS.
- The checker lexes per AWS's lexer rules and then checks every rule against the catalogue.
  AWS's grammar defines a rule type as a bare identifier, so a grammar-level parse alone
  would accept an invented rule name; the catalogue check is what catches it.
- Enforces that a file ends with a newline, since AWS's lexer only treats `#` as a comment
  on a newline-terminated line.

#### Data

- `dqdl.yaml` holds the rule types, accepted data types and tolerances. Facts about DQDL
  must carry a `source` URL and judgement calls must carry a `rationale`, or loading fails.
- Verified 2026-09-23 against `awslabs/dqdl`, AWS's own ANTLR grammar and parser
  configuration for DQDL.

#### CLI

- `profile`, `generate`, `validate`, `rules` and `push`, all supporting `--output json`.
- `--explain` prints the profile table the rules were derived from.
- `--catalog-file` runs against your own copy of the rule definitions.
- Exit codes: 0 success, 1 invalid DQDL, 2 usage error, 3 unusable catalogue, 4 push failed
  or unconfirmed.

#### Tests

- Golden-file tests asserting exact output for three committed fixtures across all three
  strictness levels, plus a sampled CSV and a sampled Parquet.
- `push` is covered with a botocore `Stubber`; no test builds a real client or reads
  credentials.

### Security

- Profiling and generating make no AWS API calls and need no credentials. `push` is the only
  command that writes, calls only `glue:CreateDataQualityRuleset`, requires `--confirm`, and
  keeps `boto3` as an optional extra imported inside the function.
- `tests/test_no_aws_calls.py` fails the build if any module other than `push.py` imports an
  AWS SDK or network client, if `boto3` becomes a core dependency, or if importing the
  package pulls `boto3` in.

[0.1.0]: https://github.com/imadelmakaoui-UJ/tools-aws/releases/tag/dqdl-gen-v0.1.0
