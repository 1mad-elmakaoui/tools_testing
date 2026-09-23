# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-09-23

First release.

### Added

#### Decision engine

- Recommends one of real-time endpoint, serverless inference, asynchronous inference or
  batch transform for a described workload.
- Hard constraints eliminate options first: request payload size, response size, processing
  time, GPU support, scale-to-zero, whether the option answers the caller at all, and native
  completion notification. Limits are inclusive, so a payload of exactly 4 MB passes on
  serverless and 4.000001 MB does not.
- Weighted soft preferences then rank whatever survived, and surviving options that were not
  chosen are reported rather than silently dropped.
- Secondary advice on multi-model endpoints, inference components, inference-component
  scale-to-zero and serverless provisioned concurrency.
- When every option is eliminated the result names the requirements that cannot hold
  together and which options each one ruled out. There is no fallback guess.

#### Data

- `limits.yaml` holds every AWS-documented limit the tool relies on, each with the
  documentation URL it was verified against and, where useful, a caveat quoted from that
  page. Judgement calls such as preference weights and thresholds live there too, each with
  a written rationale, so no magic number appears in the decision code.
- The loader enforces this: a value without a `source` is a load error, and a heuristic
  without a `rationale` is a load error. Tests assert both against the shipped file.
- Values verified 2026-09-22 against the SageMaker AI Developer Guide and API Reference.

#### CLI

- `recommend`, taking the workload from flags or, with `--interactive`, by asking. Omitted
  required flags are a usage error naming what is missing rather than an assumed default,
  and invalid interactive answers are re-asked.
- `limits`, printing the documented limits behind every decision with their sources and the
  date they were last verified.
- `--output json` on every command, rendered from the same result object as the terminal
  output so the two cannot drift. In JSON mode stdout carries nothing but the document.
- `--limits-file` to run against your own verified copy of the limits data.
- Exit codes: `0` recommendation produced, `1` nothing satisfies the requirements, `2` usage
  error, `3` limits file unusable.

#### Web

- A single static page, `docs/index.html`, for GitHub Pages. It is generated from
  `limits.yaml` by `scripts/build_page.py` and inlines everything it needs, so it makes no
  network requests and works opened from disk.
- The page's JavaScript engine is a second implementation of the rules, so
  `tests/test_web_parity.py` runs the CLI's scenario corpus through both engines under Node
  and fails the build on any disagreement, and `tests/test_web_page.py` fails the build if
  the committed page has gone stale against `limits.yaml`.

#### Project

- MIT licence, README covering purpose, demo, install, usage, how it works, limitations and
  the (empty) IAM policy.
- `docs/demo.svg`, exported from a real run by `scripts/make_demo.py` rather than mocked up.
- GitHub Actions CI running ruff, `mypy --strict` and pytest on Python 3.11 and 3.12, plus a
  packaging job that builds the wheel, asserts `limits.yaml` is inside it and runs the
  installed console script.

### Security

- The tool calls no AWS API, opens no network connection and needs no credentials or IAM
  permissions. `tests/test_no_aws_calls.py` enforces this by parsing every source file and
  failing the build if an AWS SDK or network client is imported or declared as a dependency.

[0.1.0]: https://github.com/imadelmakaoui-UJ/tools-aws/releases/tag/v0.1.0
