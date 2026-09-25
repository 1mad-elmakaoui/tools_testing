# Changelog

All notable changes to `bedrock-log-lens` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-09-24

First release.

### Added

#### Privacy

- **The tool never reads a prompt.** `inputBodyJson` and `outputBodyJson` are dropped when
  a record is parsed, and the type every report is built from has no field capable of
  holding either. The guarantee is structural: there is no rule for a future edit to
  forget, because there is nowhere to put the content.
- A test asserts the record type stays that way, so adding a content-bearing field fails
  the build.
- Fixtures carry a canary string inside every prompt and response. Tests run the real
  commands over them and assert it appears in none of the terminal output, the JSON, the
  HTML file on disk, or the records. Another test asserts the canary is still in the
  fixtures, so a passing suite cannot be a suite testing nothing.
- `show-content` is a separate command that re-reads the files for named request ids. It
  refuses without an explicit `--show-content` flag and prints an unsuppressible warning
  either way. Keeping it out of the pipeline is what makes the guarantee hold.
- The parser reaches inside a content field exactly once, for the four integers in
  `amazon-bedrock-invocationMetrics`, and copies nothing else out.

#### Reading

- A local directory or a single file, gzipped or plain, with the layout detected from the
  bytes rather than the extension: a single JSON object, a JSON array, or one object per
  line are all read.
- An S3 prefix, read-only, behind an allowlist checked before each call. `boto3` is an
  optional extra imported inside `s3.py`, so local analysis needs no AWS SDK and no
  credentials; a test starts a fresh interpreter to confirm importing the package does not
  pull it in.
- Parsed against the documented `ModelInvocationLog` schema, version 1.x. A 2.x record is
  skipped rather than guessed at, since the same field names could mean anything.
- **Tolerance is a requirement.** A record that cannot be used is counted and described,
  never raised. One corrupt line costs one record rather than the file, and skipped records
  are reported by reason so a run that mostly failed looks obviously wrong.
- A record with no token counts is reported rather than counted as zero, which would
  quietly shrink every total it belongs to. An error record with no counts is kept: a
  throttled call really happened and really cost nothing.

#### Reporting

- Totals for requests, input, output and cache tokens, and estimated cost, grouped by
  model, by caller identity, by hour and by day.
- Per-model p50/p95/p99 for input and output tokens by nearest rank, so every figure is a
  token count that actually occurred. A model with too few requests is marked a thin sample
  rather than presented as a distribution.
- Requests above their model's p99 are flagged as outliers.
- The most expensive requests, metadata only.
- Rich terminal output, `--output json`, and `--html` for one self-contained file with no
  scripts and no external references, so it can be attached to a ticket and opened offline.
  Identifiers from the logs are escaped, because a model ID or an ARN comes from a file
  this tool did not write.
- Exit code 1 when an anomaly is found, so it can run as a scheduled check.

#### Anomaly heuristics

- Labelled as heuristics wherever they appear, in the terminal, the JSON and the HTML.
- `burst`: many calls from one identity inside a short window.
- `rate_jump`: a window far above that identity's own median rate, rather than a global
  one, so a steady high-volume caller is not flagged for being busy.
- `repeated_shape`: one identity, one model, an identical input token count, repeating.
  This is the one written for agents — a stuck agent re-sends the same context, so its
  input size stops changing, while a healthy conversation grows every turn. Visible in the
  metadata, with no access to a prompt.
- Every finding names the identity, the window, the call count and the cost, because a
  nightly batch job and a runaway agent are indistinguishable by call rate alone.

#### Costing

- `prices.yaml` is generated from the AWS Price List bulk API: 1,482 on-demand token rates
  across 35 regions, carrying AWS's own publication date. Batch, latency-optimized and
  provisioned-throughput rates are excluded, since a log record does not say which applied.
- AWS publishes no mapping from Bedrock model IDs to its price list, which keys prices by
  marketing name. Rather than hand-type model IDs, `match.py` states a rule: both sides
  reduce to lowercase tokens and a price applies only when the two lists are equal. A
  near-miss is reported as unpriced rather than priced from the nearest guess.
- Inference profile prefixes are understood: `global.` selects the global rate, and `us.`,
  `eu.` and `apac.` resolve to the same model as the bare ID.
- Cache reads and writes are priced at their own rates rather than folded into the input
  count, since a cached read is about a tenth of a fresh input token.
- A total containing an unpriced request says so and is presented as a floor.
- `--prices` overrides everything for negotiated pricing.

#### Data

- An AWS fact must carry a `source` URL and a judgement call must carry a `rationale`, or
  loading fails. Tests delete one of each and assert the file is refused.
- Verified 2026-09-24.

### Security

- Reading from S3 calls `s3:ListBucket` (the `ListObjectsV2` API) and `s3:GetObject`, and
  nothing else. The allowlist is enforced before each call, and tests attempt writes
  through the guard and assert they are refused before anything reaches S3.
- `bedrock-log-lens policy` generates the IAM document from that allowlist plus a sourced
  mapping of API operation to IAM action, so the documented policy cannot drift from the
  calls the code makes.

[0.1.0]: https://github.com/imadelmakaoui-UJ/tools-aws/releases/tag/bedrock-log-lens-v0.1.0
