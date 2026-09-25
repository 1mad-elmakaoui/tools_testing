# Changelog

All notable changes to `kinesis-skew` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-09-24

First release.

### Added

#### Diagnosis

- Tells partition key skew apart from under-provisioning, which look identical in
  stream-level metrics and have opposite fixes. Skew is throttling with traffic piled onto
  one shard while the stream has room to spare; adding shards cannot help, because the same
  key still hashes to one shard.
- Verdicts: `skew`, `capacity`, `mixed`, `bursty`, `healthy`, `no_shard_metrics` and
  `no_traffic`. Each carries a headline, the evidence behind it and a specific remedy.
- `bursty` is the honest answer to throttling with traffic spread evenly and no shard near
  its limit in any minute: AWS decides throttling per second and these metrics arrive per
  minute, so the spikes are inside a datapoint. Naming it under-provisioning would claim a
  cause the numbers do not show.
- Utilisation is scored on each shard's busiest minute, not its average. A shard pinned for
  two minutes an hour averages to about 3% over a day, and that shard is the point.
- Skew statistics: busiest-to-mean ratio, coefficient of variation, hottest shard's share
  and the Gini coefficient, computed over the shards open across the whole window.
- The diagnosis is a pure function over per-shard numbers, tested directly for skewed,
  under-provisioned, healthy, bursty, resharded, single-shard, idle and
  no-shard-metrics cases.

#### Collection

- One stream, several, or every stream in a region.
- Shard-level `IncomingBytes`, `IncomingRecords` and `WriteProvisionedThroughputExceeded`
  over a configurable lookback, default 24 hours, at the 60-second period Kinesis emits
  them on, batched inside both documented `GetMetricData` caps.
- Everything is requested as `Sum`. CloudWatch's `Maximum` on these metrics is the largest
  single put operation, not the busiest period, so using it as a peak would understate a
  hot shard by orders of magnitude.
- Enhanced monitoring is checked before any metric is requested. A stream that cannot
  answer the question is never asked it, and an empty metric response is never mistaken for
  an idle stream. The remedy prints the exact `enable-enhanced-monitoring` command; the
  tool never runs it.
- Shards are listed from the start of the window rather than as currently open, so a shard
  closed by a split partway through still contributes the traffic it took. Shards open for
  less than half the window are shown but kept out of the statistics, so a split does not
  read as skew.
- A lookback beyond CloudWatch's 15-day retention for one-minute data is refused. Past it
  CloudWatch returns nothing rather than an error, which would read as an idle stream.
- Throttling is retried with exponential backoff; a stream that cannot be read is reported
  alongside the rest rather than ending the run.

#### On-demand streams

- Handled as their own case and never told to add shards, since they have no shard count to
  set. Their capacity verdict describes the real constraint: an on-demand stream carries up
  to twice its peak write throughput of the last 30 days and throttles if traffic more than
  doubles inside 15 minutes.
- Skew on an on-demand stream is still skew. Auto-scaling cannot help when one key hashes
  to one shard.

#### Output

- A per-shard bar chart, busiest first, with the verdict, the evidence and the remedy. Skew
  is a shape, and the bars show it before any statistic is read.
- `--output json` rendered from the same objects as the terminal view, so the two cannot
  drift apart.
- `--all` includes healthy streams; thresholds are overridable with `--hot`, `--skew-ratio`
  and `--gini`.
- Exit codes: 0 nothing throttled, 1 a problem found, 2 usage error, 3 unusable data file,
  4 the scan could not complete.

#### Optional key sampling

- `--sample-keys` reads a bounded sample from the busiest shard and ranks its partition
  keys, so a hot key can be named rather than inferred.
- Bounded to 10 `GetRecords` calls and 5,000 records, paced under the documented five calls
  per second per shard, with a warning printed before it runs.
- It samples the records in the shard now, which are not the ones rejected earlier. The
  output says so every time: evidence about which keys dominate, not proof of which key
  caused a throttle.

#### Data

- `limits.yaml` holds every number. An AWS fact must carry a `source` URL and a judgement
  call must carry a `rationale`, or loading fails. There are no bare numbers in the code.
- Verified 2026-09-24 against the Kinesis Data Streams developer guide and the Kinesis and
  CloudWatch API models shipped in botocore.

### Security

- **The tool is read-only.** It calls `kinesis:ListStreams`, `DescribeStreamSummary` and
  `ListShards`, and `cloudwatch:GetMetricData`, and nothing else.
- The two sampling operations are added to the allowlist only when `--sample-keys` is
  passed, so an ordinary run cannot reach `GetRecords` even if the code tried. They also get
  their own IAM statement, scoped to stream ARNs, so nobody has to grant record reads to run
  a normal scan.
- The allowlist is enforced at runtime around every client, including each page of a
  paginated call. Tests attempt writes through the guard — including `EnableEnhancedMonitoring`,
  the very thing the tool advises — and assert they are refused before reaching the client.
- `kinesis-skew policy` generates the IAM document from that same allowlist, so the
  documentation and the enforcement cannot drift apart.

[0.1.0]: https://github.com/imadelmakaoui-UJ/tools-aws/releases/tag/kinesis-skew-v0.1.0
