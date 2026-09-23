# tools-aws

Small, focused command-line tools for working with AWS. Each one lives in its own
directory with its own package, tests, documentation and CI workflow, and can be installed
on its own.

| Tool | What it does | Touches AWS? |
|---|---|---|
| [`sagemaker-inference-picker`](sagemaker-inference-picker/) | Recommends the right Amazon SageMaker inference option for a workload and explains, with a citation, why every other option was ruled out. | No — no API calls, no credentials |
| [`dqdl-gen`](dqdl-gen/) | Profiles a CSV or Parquet file and generates a starter AWS Glue Data Quality ruleset in DQDL. | No, except the opt-in `push` command |

## Shared principles

These hold across every tool here.

- **Read-only by default.** No tool calls an API that creates, modifies or deletes
  anything. Where a tool can write (only `dqdl-gen push`), it is a separate command behind
  an explicit `--confirm` flag, in its own module, with its IAM permissions documented, and
  a test asserts the rest of the tool cannot reach AWS at all.
- **No magic numbers.** Every AWS limit, quota or threshold a tool depends on lives in one
  data file with the documentation URL it was verified against and the date it was checked.
  Loading that file fails if a value arrives without a source.
- **Claims are enforced, not asserted.** Where the documentation makes a promise — no AWS
  calls, generated files in step with their source, two engines agreeing — a test fails the
  build when it stops being true.
- Python 3.11+, `mypy --strict`, ruff, pytest above 85% coverage, and no test ever reaches
  a real AWS endpoint.

## Layout

```
.github/workflows/     one workflow per tool, scoped with a paths filter
docs/                  what GitHub Pages serves
sagemaker-inference-picker/
dqdl-gen/
```

GitHub only reads workflow files from `.github/workflows` at the repository root, so
per-tool CI is done with a `paths:` filter and a `working-directory` rather than nested
workflow directories. A change to one tool does not run the other tool's checks.

## Development

Each tool is a self-contained project. Work in its directory:

```bash
cd sagemaker-inference-picker
uv venv
uv pip install -e . --group dev
.venv/bin/pytest
```

## License

MIT — see [LICENSE](LICENSE).

Built by [@1mad-elmakaoui](https://github.com/1mad-elmakaoui).
