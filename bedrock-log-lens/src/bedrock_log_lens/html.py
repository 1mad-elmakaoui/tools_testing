"""Render a report as one self-contained HTML file.

One file, no network, no scripts: everything is inline so the result can be attached to a
ticket or opened from a laptop with no internet. There is no JavaScript at all, which also
means there is nothing that could fetch anything from a page built out of log data.

Every value is escaped on the way in. The report carries no prompt content, so nothing here
can render one, but the identifiers it does carry — ARNs, model IDs, request IDs — come
from a file this tool did not write, and are treated as untrusted text.
"""

from __future__ import annotations

import datetime as dt
from html import escape

from bedrock_log_lens.models import GroupTotals, Report

_STYLE = """
:root {
  color-scheme: light dark;
  --bg: #ffffff; --fg: #17171a; --muted: #6b6b76; --line: #e3e3e8;
  --accent: #1f6feb; --warn: #9a6700; --bad: #b42318; --good: #116329;
  --card: #fafafb;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #131316; --fg: #ececf1; --muted: #a0a0ab; --line: #2b2b31;
    --accent: #6ea8fe; --warn: #e3b341; --bad: #ff7b72; --good: #5fcf80;
    --card: #1a1a1f;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 2.5rem 1rem 4rem; background: var(--bg); color: var(--fg);
  font: 15px/1.55 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
}
main { max-width: 68rem; margin: 0 auto; }
h1 { font-size: 1.5rem; margin: 0 0 .25rem; }
h2 { font-size: 1.05rem; margin: 2.25rem 0 .6rem; }
.sub { color: var(--muted); margin: 0 0 1.75rem; }
.cards { display: flex; flex-wrap: wrap; gap: .75rem; margin-bottom: .5rem; }
.card {
  flex: 1 1 9rem; padding: .8rem .9rem; border: 1px solid var(--line);
  border-radius: 8px; background: var(--card);
}
.card .k { color: var(--muted); font-size: .78rem; text-transform: uppercase;
  letter-spacing: .04em; }
.card .v { font-size: 1.35rem; font-variant-numeric: tabular-nums; margin-top: .15rem; }
table { width: 100%; border-collapse: collapse; margin: .4rem 0 .2rem; font-size: .9rem; }
th, td { text-align: left; padding: .4rem .55rem; border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-weight: 600; font-size: .78rem; text-transform: uppercase;
  letter-spacing: .04em; }
td.n, th.n { text-align: right; font-variant-numeric: tabular-nums; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .86em; }
.note { color: var(--muted); font-size: .85rem; margin: .5rem 0; }
.warn { color: var(--warn); }
.bad { color: var(--bad); }
.good { color: var(--good); }
.finding { border-left: 3px solid var(--line); padding: .1rem 0 .1rem .8rem; margin: .8rem 0; }
.finding.burst { border-color: var(--warn); }
.finding.rate_jump { border-color: var(--accent); }
.finding.repeated_shape { border-color: var(--bad); }
.finding .head { font-weight: 600; }
.privacy {
  margin: 2.5rem 0 0; padding: .9rem 1rem; border: 1px solid var(--line);
  border-radius: 8px; background: var(--card); color: var(--muted); font-size: .87rem;
}
@media (max-width: 40rem) { body { padding: 1.5rem 1rem 3rem; } .card { flex-basis: 100%; } }
"""


def render_html(report: Report, disclaimer: str, generated_at: dt.datetime) -> str:
    """The whole report as one self-contained HTML document."""
    covered = "no records"
    if report.window_start and report.window_end:
        covered = f"{report.window_start:%Y-%m-%d %H:%M} to {report.window_end:%Y-%m-%d %H:%M} UTC"

    parts: list[str] = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>Bedrock invocation log report</title>",
        f"<style>{_STYLE}</style>",
        "</head><body><main>",
        "<h1>Bedrock invocation log report</h1>",
        f'<p class="sub">{escape(covered)} · {report.sources_read:,} file(s) · '
        f"generated {generated_at:%Y-%m-%d %H:%M} UTC</p>",
        _cards(report),
    ]

    if report.unpriced_models:
        parts.append(
            f'<p class="note warn">{report.totals.unpriced_requests:,} request(s) across '
            f"{len(report.unpriced_models)} model(s) had no published price, so the cost "
            f"total is a floor: <code>"
            + "</code>, <code>".join(escape(model) for model in report.unpriced_models[:6])
            + "</code></p>"
        )

    parts.append(_table("By model", report.by_model, "Model"))
    parts.append(_table("By caller identity", report.by_identity, "Identity"))
    parts.append(_table("By day", report.by_day, "Day"))
    parts.append(_table("By hour", report.by_hour, "Hour"))
    parts.append(_percentiles(report))
    parts.append(_anomalies(report))
    parts.append(_expensive(report))
    parts.append(_issues(report))
    parts.append(
        '<p class="privacy"><strong>Privacy.</strong> This report is built from log '
        "metadata only. The prompt and response fields of every record are discarded when "
        "the record is parsed, before any analysis or output sees them, so nothing on this "
        "page can contain what a user typed or what a model replied.<br>"
        f"{escape(disclaimer)}</p>"
    )
    parts.append("</main></body></html>")
    return "\n".join(part for part in parts if part)


def _cards(report: Report) -> str:
    totals = report.totals
    cards = [
        ("requests", f"{totals.requests:,}"),
        ("input tokens", f"{totals.input_tokens:,}"),
        ("output tokens", f"{totals.output_tokens:,}"),
        ("est. cost", _money(totals.cost_usd)),
    ]
    if totals.cache_read_tokens or totals.cache_write_tokens:
        cards.insert(
            3, ("cache tokens", f"{totals.cache_read_tokens + totals.cache_write_tokens:,}")
        )
    inner = "".join(
        f'<div class="card"><div class="k">{escape(key)}</div>'
        f'<div class="v">{escape(value)}</div></div>'
        for key, value in cards
    )
    return f'<div class="cards">{inner}</div>'


def _table(title: str, groups: tuple[GroupTotals, ...], label: str) -> str:
    if not groups:
        return ""
    rows = "".join(
        "<tr>"
        f"<td><code>{escape(group.key)}</code></td>"
        f'<td class="n">{group.requests:,}</td>'
        f'<td class="n">{group.input_tokens:,}</td>'
        f'<td class="n">{group.output_tokens:,}</td>'
        f'<td class="n">{escape(_money(group.cost_usd))}'
        f"{'+' if not group.cost_is_complete else ''}</td>"
        "</tr>"
        for group in groups
    )
    return (
        f"<h2>{escape(title)}</h2><table><thead><tr>"
        f"<th>{escape(label)}</th><th class='n'>Requests</th><th class='n'>Input</th>"
        f"<th class='n'>Output</th><th class='n'>Est. cost</th>"
        f"</tr></thead><tbody>{rows}</tbody></table>"
    )


def _percentiles(report: Report) -> str:
    if not report.percentiles_input and not report.percentiles_output:
        return ""
    rows = []
    for label, summaries in (
        ("input", report.percentiles_input),
        ("output", report.percentiles_output),
    ):
        for item in summaries:
            thin = "" if item.is_meaningful else '<span class="note"> thin sample</span>'
            rows.append(
                "<tr>"
                f"<td><code>{escape(item.model_id)}</code></td>"
                f"<td>{escape(label)}</td>"
                f'<td class="n">{item.count:,}{thin}</td>'
                f'<td class="n">{item.p50:,}</td>'
                f'<td class="n">{item.p95:,}</td>'
                f'<td class="n">{item.p99:,}</td>'
                f'<td class="n">{item.maximum:,}</td>'
                "</tr>"
            )
    return (
        "<h2>Token distribution per model</h2><table><thead><tr>"
        "<th>Model</th><th>Tokens</th><th class='n'>n</th><th class='n'>p50</th>"
        "<th class='n'>p95</th><th class='n'>p99</th><th class='n'>max</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )


def _anomalies(report: Report) -> str:
    if not report.anomalies:
        return '<h2>Anomalies</h2><p class="note good">Nothing matched the heuristics.</p>'
    findings = "".join(
        f'<div class="finding {escape(item.kind.value)}">'
        f'<div class="head">{escape(item.kind.value.replace("_", " "))} · '
        f"<code>{escape(item.identity_arn)}</code></div>"
        f'<div class="note">{item.window_start:%Y-%m-%d %H:%M:%S} to '
        f"{item.window_end:%H:%M:%S} UTC · {item.request_count:,} requests · "
        f"{escape(_money(item.cost_usd))}</div>"
        f'<div class="note">{escape(item.detail)}</div>'
        "</div>"
        for item in report.anomalies
    )
    return (
        "<h2>Anomalies</h2>"
        '<p class="note">These are heuristics, not proof. A scheduled batch job and a '
        "runaway agent look alike by call rate, so each finding names the identity and the "
        "window for a person to judge.</p>" + findings
    )


def _expensive(report: Report) -> str:
    if not report.most_expensive:
        return ""
    rows = "".join(
        "<tr>"
        f"<td><code>{escape(record.request_id)}</code></td>"
        f"<td><code>{escape(record.model_id)}</code></td>"
        f"<td><code>{escape(record.identity_arn)}</code></td>"
        f'<td class="n">{record.input_tokens or 0:,}</td>'
        f'<td class="n">{record.output_tokens or 0:,}</td>'
        f'<td class="n">{escape(_money(cost.usd or 0.0))}</td>'
        "</tr>"
        for record, cost in report.most_expensive
    )
    return (
        "<h2>Most expensive requests</h2>"
        '<p class="note">Metadata only. No prompt is read to build this list.</p>'
        "<table><thead><tr><th>Request</th><th>Model</th><th>Identity</th>"
        "<th class='n'>Input</th><th class='n'>Output</th><th class='n'>Est. cost</th>"
        f"</tr></thead><tbody>{rows}</tbody></table>"
    )


def _issues(report: Report) -> str:
    counts = report.issue_counts
    if not counts:
        return ""
    total = sum(counts.values())
    rows = "".join(
        f'<tr><td>{escape(kind.value.replace("_", " "))}</td><td class="n">{count:,}</td></tr>'
        for kind, count in sorted(counts.items(), key=lambda item: -item[1])
    )
    css = "bad" if total > report.totals.requests else "warn"
    return (
        f'<h2>Records that could not be used</h2><p class="note {css}">{total:,} record(s) '
        "are not included in any total above.</p>"
        "<table><thead><tr><th>Reason</th><th class='n'>Records</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def _money(amount: float) -> str:
    if amount and abs(amount) < 0.01:
        return f"${amount:,.4f}"
    return f"${amount:,.2f}"
