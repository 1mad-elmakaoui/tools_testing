"""Regenerate docs/demo.svg from a real run of the tool.

Exported from actual output rather than mocked up, so it cannot drift from what the tool
prints. Run it after any change to the renderers or the templates:

    .venv/bin/python scripts/make_demo.py
"""

from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.syntax import Syntax
from rich.text import Text

from dqdl_gen.catalog import load_catalog
from dqdl_gen.emit import emit
from dqdl_gen.generate import generate
from dqdl_gen.models import Strictness
from dqdl_gen.profile import profile_table
from dqdl_gen.reader import read_table
from dqdl_gen.render import render_profile

TOOL_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = TOOL_ROOT / "tests" / "fixtures" / "orders.csv"
OUTPUT = TOOL_ROOT / "docs" / "demo.svg"
WIDTH = 104

#: How many lines of the generated ruleset to show. The whole thing is longer than a
#: screenshot should be.
RULE_LINES = 26


def main() -> None:
    """Render the demo and write the SVG."""
    console = Console(record=True, width=WIDTH)
    prompt = Text()
    prompt.append("$ ", style="bold green")
    prompt.append("dqdl-gen generate orders.csv --explain --stdout")
    console.print(prompt)

    catalog = load_catalog()
    profile = profile_table(
        read_table(FIXTURE),
        max_value_set=catalog.shape.max_allowed_value_set,
        top_values_shown=catalog.shape.top_values_shown,
    )
    render_profile(console, profile, catalog)

    text = emit(generate(profile, catalog, Strictness.BALANCED), catalog)
    shown = "\n".join(text.splitlines()[:RULE_LINES])
    console.print()
    console.print(Syntax(shown, "python", background_color="default", word_wrap=True))
    console.print(Text("    ... (truncated for the demo)", style="dim"))

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    console.save_svg(str(OUTPUT), title="dqdl-gen")
    print(f"wrote {OUTPUT.relative_to(TOOL_ROOT)} ({OUTPUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
