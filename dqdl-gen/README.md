# dqdl-gen

Profiles a CSV or Parquet file and generates a starter AWS Glue Data Quality ruleset in
DQDL, with every rule carrying a comment stating the evidence it came from.

[![CI](https://github.com/imadelmakaoui-UJ/tools-aws/actions/workflows/dqdl-gen.yml/badge.svg)](https://github.com/imadelmakaoui-UJ/tools-aws/actions/workflows/dqdl-gen.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

---

## What this touches in AWS

**Profiling and generating are entirely local. Nothing is read from or written to AWS, and
no credentials are needed.**

| Command | AWS APIs called | Credentials |
|---|---|---|
| `profile` | **none** | none |
| `generate` | **none** | none |
| `validate` | **none** | none |
| `rules` | **none** | none |
| `push` | `glue:CreateDataQualityRuleset` — **and nothing else** | yes |

`push` is the one command that writes, and it is kept deliberately apart: `boto3` is an
optional install, only `push.py` imports it, the import happens inside the function, and the
command refuses to run without `--confirm`. It only ever **creates** a ruleset — it never
updates or deletes one, so a name clash stops rather than overwrites.

That separation is enforced, not promised. `tests/test_no_aws_calls.py` parses every source
file and fails the build if any module other than `push.py` imports an AWS SDK or a network
client, asserts `boto3` is not a core dependency, and starts a fresh interpreter to check
that importing the package does not pull `boto3` in.

See [AWS permissions](#aws-permissions) for the exact IAM policy.

---

## Demo

```console
Rules = [
    # 500 rows in the file, allowing 20% either way at balanced strictness
    RowCount between 400 and 600,

    # 5 columns in the profiled file
    ColumnCount = 5,

    # no nulls in 500 rows
    IsComplete "order_id",

    # all 500 values distinct and non-null, so this looks like a key
    IsPrimaryKey "order_id",

    # observed 100000 to 100499, widened by 5% at balanced strictness
    ColumnValues "order_id" between 99975 and 100524,

    # read as int64
    ColumnDataType "order_id" = "Long",

    # no nulls in 500 rows
    IsComplete "status",
```

<details>
<summary>The profile table <code>--explain</code> prints</summary>

```console
orders.csv · 500 rows · 5 columns
┏━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━┳━━━━━━━━━━━━━┓
┃ Column        ┃ Type    ┃    Nulls ┃ Distinct ┃        Range ┃    Mean / sd ┃  Len ┃ Top values  ┃
┡━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━╇━━━━━━━━━━━━━┩
│ order_id      │ integer │        0 │      500 │   100,000 to │  100,249.5 / │    - │ -           │
│               │         │          │          │      100,499 │       144.34 │      │             │
│ status        │ string  │        0 │        4 │            - │            - │  3-7 │ NEW (125),  │
│               │         │          │          │              │              │      │ PAID (125), │
│               │         │          │          │              │              │      │ SHIPPED     │
│               │         │          │          │              │              │      │ (125), VOID │
│               │         │          │          │              │              │      │ (125)       │
│ region        │ string  │        0 │        3 │            - │            - │ 9-10 │ ap-south-1  │
│               │         │          │          │              │              │      │ (167),      │
│               │         │          │          │              │              │      │ eu-west-1   │
│               │         │          │          │              │              │      │ (167),      │
│               │         │          │          │              │              │      │ us-east-1   │
│               │         │          │          │              │              │      │ (166)       │
│ amount        │ float   │ 5 (1.0%) │      495 │ 7.5 to 904.5 │     444.59 / │    - │ -           │
│               │         │          │          │              │        254.2 │      │             │
│ customer_note │ string  │        0 │       12 │            - │            - │ 5-16 │ note- (42), │
│               │         │          │          │              │              │      │ note-x      │
│               │         │          │          │              │              │      │ (42),       │
│               │         │          │          │              │              │      │ note-xx     │
│               │         │          │          │              │              │      │ (42),       │
│               │         │          │          │              │              │      │ note-xxx    │
│               │         │          │          │              │              │      │ (42),       │
│               │         │          │          │              │              │      │ note-xxxx   │
│               │         │          │          │              │              │      │ (42), +7    │
│               │         │          │          │              │              │      │ more        │
└───────────────┴─────────┴──────────┴──────────┴──────────────┴──────────────┴──────┴─────────────┘
```

</details>

The demo image at [`docs/demo.svg`](docs/demo.svg) is exported from a real run by
[`scripts/make_demo.py`](scripts/make_demo.py), not mocked up.

---

## Install

```bash
pipx install ./dqdl-gen
```

With the optional AWS push command:

```bash
pipx install "./dqdl-gen[push]"
```

For development, with [uv](https://docs.astral.sh/uv/):

```bash
cd dqdl-gen
uv venv
uv pip install -e . --group dev
```

Python 3.11 or newer.

---

## Usage

### Generate a ruleset

```bash
dqdl-gen generate orders.csv                      # writes orders.dqdl
dqdl-gen generate orders.parquet --out rules.dqdl
dqdl-gen generate orders.csv --stdout             # print instead of writing
dqdl-gen generate orders.csv --explain            # also show the profile it used
```

### Control how tolerant the rules are

```bash
dqdl-gen generate orders.csv --strictness strict     # hugs the observed data
dqdl-gen generate orders.csv --strictness balanced   # default
dqdl-gen generate orders.csv --strictness lenient    # only obvious breakage fails
```

An observed completeness of 0.992 becomes `Completeness "col" >= 0.99` at strict,
`>= 0.98` at balanced and `>= 0.94` at lenient. Every margin is declared in the data file
with a written rationale.

### Large files

```bash
dqdl-gen generate huge.parquet --sample 100000
```

Sampling changes one thing beyond speed. Parquet records its row count in the file footer,
so a row-count rule is still exact. A CSV has no footer, so after sampling the real total is
genuinely unknown — and rather than derive a rule from the sample size, **no `RowCount` rule
is generated at all**, and the output says so. Every rule's evidence comment records that it
came from a sample.

### Check an existing ruleset

```bash
dqdl-gen validate rules.dqdl
```

### See what it can emit

```bash
dqdl-gen rules              # rule types, sources, and the strictness levels
dqdl-gen rules --output json
```

```console
DQDL rule types dqdl-gen emits · verified 2026-09-23
┏━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Rule type           ┃ Params ┃ Condition ┃ Scope  ┃ What it checks                               ┃
┡━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ ColumnCount         │   0    │ number    │ table  │ Checks the number of columns in the dataset  │
│                     │        │           │        │ https://docs.aws.amazon.com/glue/latest/dg/d │
│                     │        │           │        │ qdl-rule-types-ColumnCount.html              │
├─────────────────────┼────────┼───────────┼────────┼──────────────────────────────────────────────┤
│ ColumnDataType      │   1    │ string    │ column │ Check that a column's values cast to the     │
│                     │        │           │        │ given Spark type                             │
│                     │        │           │        │ https://docs.aws.amazon.com/glue/latest/dg/d │
│                     │        │           │        │ qdl-rule-types-ColumnDataType.html           │
├─────────────────────┼────────┼───────────┼────────┼──────────────────────────────────────────────┤
│ ColumnLength        │   1    │ number    │ column │ Constrain the length of the values in a      │
│                     │        │           │        │ column                                       │
│                     │        │           │        │ https://docs.aws.amazon.com/glue/latest/dg/d │
│                     │        │           │        │ qdl-rule-types-ColumnLength.html             │
├─────────────────────┼────────┼───────────┼────────┼──────────────────────────────────────────────┤
│ ColumnValues        │   1    │ any       │ column │ Constrain the values a column may hold       │
```

### JSON

Every command supports `--output json`, rendered from the same objects as the terminal
output. The document includes the profile, every rule with its evidence, and the rendered
DQDL:

```bash
dqdl-gen generate orders.csv --output json | jq '.rules[3]'
```

```json
{
  "rule_type": "IsPrimaryKey",
  "parameters": [
    "order_id"
  ],
  "condition": null,
  "evidence": "all 500 values distinct and non-null, so this looks like a key",
  "dqdl": "IsPrimaryKey \"order_id\""
}
```

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | The DQDL is not valid |
| `2` | Usage error — bad flag, unreadable file, unknown format |
| `3` | The catalogue file is missing, malformed, or missing a source or rationale |
| `4` | `push` failed, or was run without `--confirm` |

---

## How it works

```
reader  ->  profile  ->  generate(strictness)  ->  emit  ->  validate
  I/O         pure            pure               pure        pure
```

Only `reader.py` touches the filesystem. Everything after it works on plain frozen
dataclasses, which is why rule generation is tested against hand-built profiles with no
files involved.

### What gets profiled

Per column: inferred type, null count and rate, distinct count, min and max, mean and
standard deviation for numerics, string length range, and the complete value set for
low-cardinality columns.

Every statistic is optional. When one cannot be computed it stays `None` rather than getting
a stand-in, and **a missing statistic means a missing rule, never a guessed one**.

### What gets generated

| Evidence | Rule |
|---|---|
| Row count (when known) | `RowCount between N and M` |
| Column count | `ColumnCount = N` |
| No nulls | `IsComplete "col"` |
| Some nulls | `Completeness "col" >= 0.98` |
| Distinct and complete, and a plausible key type | `IsPrimaryKey "col"` |
| Distinct with nulls | `IsUnique "col"` |
| Nearly distinct | `Uniqueness "col" >= 0.98` |
| Low cardinality, enough rows | `ColumnValues "col" in ["A", "B"]` |
| Low cardinality | `DistinctValuesCount "col" between 1 and N` |
| Numeric | `ColumnValues "col" between min and max` |
| String | `ColumnLength "col" between a and b` |
| Inferred type | `ColumnDataType "col" = "Long"` |

Some deliberate omissions:

- **String columns get no `ColumnDataType` rule.** DQDL's `ColumnDataType` accepts
  `Boolean, Date, Timestamp, Integer, Double, Float, Long` — `String` is not among them, so
  emitting one would be invalid.
- **A distinct float column is not treated as a key.** Amounts in a single file often all
  differ by accident; a uniqueness rule on one would fail the first time two rows shared a
  value. Only the column kinds listed in the catalogue are eligible.
- **A small file gets no allowed-value rule.** Below the row threshold there is not enough
  evidence that the observed values are the whole set.

### Checking the output

Generated DQDL is validated before it is written, and nothing invalid is ever sent to AWS.

The check is more than a parse, for a specific reason. AWS's own grammar defines a rule type
as a bare identifier (`ruleType: IDENTIFIER`), so a grammar-level parse accepts
`Frobnicate "x" > 1` without complaint. The checker therefore lexes per AWS's lexer rules,
parses the subset this tool emits, and then checks every rule against the catalogue: the
name must be one we may emit, the parameter count must match, and the condition must be of a
kind that rule type accepts.

It also enforces a detail that is easy to miss: AWS's lexer defines a comment as
`'#' .*? '\r'? '\n'`, so a comment on an unterminated final line does not lex as a comment
and the ruleset fails to parse. Generated files always end with a newline.

---

## Where the rule definitions come from

Everything lives in
[`src/dqdl_gen/data/dqdl.yaml`](src/dqdl_gen/data/dqdl.yaml), split into two kinds of entry
that are validated differently:

- **Facts about DQDL** — rule types, parameter counts, accepted data types, comment syntax.
  Each must carry a `source` URL, or loading fails.
- **Judgement calls** — strictness margins, cardinality thresholds, which column kinds can
  be keys. Each must carry a `rationale`, because no AWS document says what a sensible
  tolerance is.

Verified **2026-09-23** against [`awslabs/dqdl`](https://github.com/awslabs/dqdl) — AWS's own
ANTLR grammar and parser configuration for DQDL, which is the parser Glue Data Quality uses.
That is a stricter source than the prose documentation: it is where the `String`-is-not-a-
data-type detail and the comment-needs-a-newline detail come from.

---

## AWS permissions

Only `dqdl-gen push` needs any. Everything else needs none at all.

The minimal policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["glue:CreateDataQualityRuleset"],
      "Resource": "arn:aws:glue:<region>:<account-id>:dataQualityRuleset/*"
    }
  ]
}
```

That is the complete list of actions this tool can perform. It grants no read, update or
delete permission, and a test asserts the documented policy contains none.

```bash
dqdl-gen push rules.dqdl --name orders-rules --confirm \
    --database sales --table orders
```

Without `--confirm` the command prints what it *would* do and exits 4 without contacting
AWS.

---

## Limitations

- **A ruleset is a starting point, not a specification.** It describes one profile of one
  file. Read it before committing it to a pipeline; the tool cannot know which of the
  patterns it found are intentional.
- **The tolerances are judgement, not fact.** The rule types and syntax come from AWS's
  parser. How much room to leave is this tool's opinion, stated with a rationale in the data
  file so you can disagree and change it.
- **Profiling reads the whole file into memory** unless you pass `--sample`. There is no
  streaming profile.
- **Only CSV and Parquet.** No JSON, Avro, ORC or database sources.
- **Types come from the reader.** A CSV column of digits is profiled as an integer even if
  it is really a zero-padded identifier, and pyarrow keeps an empty CSV field as an empty
  string rather than a null, which shows up as a length of 0 rather than a null.
- **No cross-column or cross-dataset rules.** DQDL supports `ColumnCorrelation`,
  `ReferentialIntegrity`, `DatasetMatch` and others; none can be inferred from a single
  file's profile, so none are generated.
- **`ColumnValues` sets are generated for string columns only.** A low-cardinality numeric
  column gets a range rule instead, which is a weaker constraint.
- **`push` does not verify the target exists.** If the database or table is wrong, Glue
  accepts the ruleset and it simply never matches anything.

---

## Development

```bash
uv venv
uv pip install -e . --group dev

.venv/bin/ruff check .          # lint
.venv/bin/ruff format --check . # formatting
.venv/bin/mypy                  # strict type checking
.venv/bin/pytest                # tests, with a coverage floor of 85%

.venv/bin/python scripts/make_fixtures.py   # regenerate tests/fixtures
.venv/bin/python scripts/make_golden.py     # regenerate tests/golden
.venv/bin/python scripts/make_demo.py       # regenerate docs/demo.svg
```

### Golden files

`tests/golden/` holds the exact expected output for each committed fixture at each
strictness level, including a sampled CSV and a sampled Parquet. They are what notices an
unintended change to a template, a tolerance or the emitter.

Output is deterministic and carries no timestamp, which is what makes that work: a
regenerated ruleset only appears in a diff when something real changed. When a golden test
fails, read the diff first, then regenerate deliberately.

### Layout

```
src/dqdl_gen/
├── data/dqdl.yaml   rule types, data types and tolerances   <- single source of truth
├── catalog.py       loads and validates that file
├── models.py        frozen dataclasses, no behaviour
├── reader.py        CSV/Parquet -> Arrow table              <- the only I/O
├── profile.py       table -> DatasetProfile                 ┐
├── templates.py     every rule template, in one module      │
├── generate.py      profile + strictness -> Ruleset         ├ pure
├── emit.py          Ruleset -> DQDL text                    │
├── syntax.py        quoting and number rendering            │
├── validate.py      lexer, parser and catalogue checks      ┘
├── render.py        Rich and JSON renderers
├── cli.py           Typer wiring
└── push.py          the ONLY module that talks to AWS
```

---

## License

MIT — see [LICENSE](LICENSE).

Built by [@1mad-elmakaoui](https://github.com/1mad-elmakaoui).
