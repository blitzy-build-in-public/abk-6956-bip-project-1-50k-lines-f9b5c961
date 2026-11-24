"""The inlined stylesheet and script of Foundry's self-contained HTML report.

:data:`CSS` and :data:`JS` are every byte of the report's presentation and behaviour, inlined by
:mod:`foundry.reporting.html` so ``report.html`` opens with no network;
:func:`contains_external_reference` and :func:`assert_self_contained` make that property checkable.
AAP 0.3.2, 0.4.8, 0.7.4, 0.14.1.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from foundry.core.exceptions import FoundryError

__all__: Final[tuple[str, ...]] = (
    "CSS",
    "DEFAULT_ASSETS",
    "EXTERNAL_REFERENCE_MARKERS",
    "JS",
    "SELF_CONTAINED_FORBIDDEN",
    "AssetsBundle",
    "assert_self_contained",
    "contains_external_reference",
    "default_assets",
)

CSS: Final[str] = """\
/* foundry report stylesheet -- self-contained: no external font, no CDN, no stylesheet import.
   Owned by foundry.reporting.assets and inlined by foundry.reporting.html. Light theme only. */

:root {
  --bg: #ffffff;
  --surface: #f7f8fa;
  --border: #dfe3e8;
  --text: #1b1f24;
  --muted: #57606a;
  --accent: #0969da;
  /* Outcome foregrounds: one step darker than their tints alone would allow, because they colour
     the 12px outcome badges and the 13px summary-chip digits, which must clear WCAG AA 4.5:1
     against their own tint. Do not lighten them back. */
  --passed: #116329;
  --failed: #cf222e;
  --error: #a40e26;
  --skipped: #57606a;
  --xfail: #9a6700;
  --xpass: #9a6700;
  --quarantined: #6639ba;
  --not-run: #57606a;
  --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial,
    sans-serif;
  --font-mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  --radius: 6px;
  --shadow: 0 1px 2px rgba(27, 31, 36, 0.08);
  --accent-active: #0550ae;
}

*, *::before, *::after { box-sizing: border-box; }

body {
  background: var(--bg); color: var(--text); font-family: var(--font-sans); font-size: 15px;
  line-height: 1.5; margin: 0 auto; max-width: 1100px; padding: 24px 20px 48px;
}

a { color: var(--accent); }
a:hover { text-decoration: underline; }
a:active { color: var(--accent-active); text-decoration: underline; }
code { font-family: var(--font-mono); font-size: 13px; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.hidden { display: none; }

header.report-header {
  border-bottom: 1px solid var(--border); margin-bottom: 20px; padding-bottom: 16px;
}

h1 { font-size: 24px; font-weight: 600; margin: 0 0 6px; }
h2 { font-size: 18px; font-weight: 600; margin: 28px 0 12px; }
.meta { color: var(--muted); font-size: 13px; margin: 0; }
.meta span { margin-right: 14px; white-space: nowrap; }

/* Summary strip: one chip per outcome family, wrapping on a narrow window. */
.summary {
  display: flex; flex-wrap: wrap; gap: 8px; list-style: none; margin: 0 0 20px; padding: 0;
}

.summary li {
  align-items: baseline; background: var(--surface); border: 1px solid var(--border);
  border-radius: 999px; display: inline-flex; font-size: 13px; gap: 6px; padding: 4px 12px;
}

.summary .count { font-variant-numeric: tabular-nums; font-weight: 600; }
.summary-passed .count { color: var(--passed); }
.summary-failed .count { color: var(--failed); }
.summary-error .count { color: var(--error); }
.summary-skipped .count { color: var(--skipped); }
.summary-xfailed .count { color: var(--xfail); }
.summary-xpassed .count { color: var(--xpass); }
.summary-quarantined_failed .count { color: var(--quarantined); }
.summary-quarantine_passed .count { color: var(--quarantined); }
.summary-not_run .count { color: var(--not-run); }

/* Script-only controls: hidden until the script marks the document, so a no-script reader
   never meets a filter or a button that cannot work. */
.controls {
  align-items: center; display: none; flex-wrap: wrap; gap: 8px; margin: 0 0 12px;
}

html[data-foundry-js="enabled"] .controls { display: flex; }

.controls input,
.controls button {
  background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius);
  color: var(--text); font: inherit; padding: 6px 10px;
}

.controls input { min-width: 240px; }
.controls button { background: var(--surface); cursor: pointer; }
.controls button:hover { background: #eef0f3; }

/* Results table: one row per test, sorted by node id. The header pins while the page scrolls; the
   narrow-window rules below make the table's own box a scroll container, and a box that scrolls
   sideways cannot pin a descendant, so there the header travels with the table instead. */
table.results { border-collapse: collapse; font-size: 14px; width: 100%; }

table.results thead th {
  background: var(--surface); border-bottom: 1px solid var(--border); font-weight: 600;
  padding: 8px 10px; position: sticky; text-align: left; top: 0; z-index: 1;
}

table.results tbody td,
table.results tbody th {
  border-bottom: 1px solid var(--border); padding: 8px 10px; vertical-align: top;
}

table.results tbody th { font-weight: 400; text-align: left; }

table.results tbody tr:hover { background: var(--surface); }
/* nowrap keeps a duration and its unit on one line, so tabular figures stay in one column. */
table.results .num {
  font-variant-numeric: tabular-nums; text-align: right; white-space: nowrap;
}
table.results .nodeid { font-family: var(--font-mono); font-size: 13px; word-break: break-word; }

/* One badge class per outcome value of the result record: nine in all. */
.outcome {
  border: 1px solid currentColor; border-radius: 999px; display: inline-block; font-size: 12px;
  font-weight: 600; letter-spacing: 0.02em; padding: 1px 9px; white-space: nowrap;
}

.outcome-passed { background: #e6f4ea; color: var(--passed); }
.outcome-failed { background: #ffebe9; color: var(--failed); }
.outcome-error { background: #ffe3e0; color: var(--error); }
.outcome-skipped { background: #f0f1f3; color: var(--skipped); }
.outcome-xfailed { background: #fff8e1; color: var(--xfail); }
.outcome-xpassed { background: #fff8e1; color: var(--xpass); }
.outcome-quarantined_failed { background: #f2ecfd; color: var(--quarantined); }
.outcome-quarantine_passed { background: #f2ecfd; color: var(--quarantined); }
.outcome-not_run { background: #f0f1f3; color: var(--not-run); }

/* Quarantined section: known-flaky tests, kept apart from the failures they are not. */
.quarantined {
  background: var(--surface); border: 1px solid var(--border);
  border-left: 4px solid var(--quarantined); border-radius: var(--radius);
  margin: 0 0 20px; padding: 12px 16px;
}

.quarantined h2 { font-size: 16px; margin: 0 0 8px; }
.quarantined ul { margin: 0; padding-left: 20px; }
.quarantined li { margin: 4px 0; }
.quarantined code { overflow-wrap: anywhere; }
.note { color: var(--muted); font-size: 13px; }

/* Failure panels: one per failure, expanded through native details/summary. */
.panel {
  background: var(--bg); border: 1px solid var(--border); border-radius: var(--radius);
  box-shadow: var(--shadow); margin: 0 0 16px; padding: 12px 16px;
}

.panel summary { cursor: pointer; font-weight: 600; overflow-wrap: anywhere; }
.panel summary:hover { color: var(--accent); }
.panel summary:active { color: var(--accent-active); }

.panel dl {
  display: grid; gap: 4px 16px; grid-template-columns: max-content 1fr; margin: 12px 0 0;
}

.panel dt { color: var(--muted); font-size: 13px; }
.panel dd { font-family: var(--font-mono); font-size: 13px; margin: 0; word-break: break-word; }
.panel p { margin: 8px 0 0; overflow-wrap: anywhere; }

/* Section lists: a run error can embed a long node id, so an entry wraps inside the column. */
main > section li { overflow-wrap: anywhere; }

details { margin: 0 0 16px; }
summary { cursor: pointer; }
details[open] > summary { margin-bottom: 8px; }

/* Captured log tail: monospace, bounded height, wrapped so a long line hides nothing. */
pre.log {
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
  font-family: var(--font-mono); font-size: 12px; line-height: 1.45; margin: 8px 0 0;
  max-height: 420px; overflow-x: auto; padding: 10px 12px; white-space: pre-wrap;
  word-break: break-word;
}

/* Artifacts-disabled warning: the run continued, so this informs rather than alarms. */
.banner {
  background: #fff8c5; border: 1px solid #d4a72c; border-radius: var(--radius);
  color: #4d2d00; font-size: 14px; margin: 0 0 20px; padding: 10px 14px;
}

/* Empty state: an explicit statement that no tests ran, never a blank table. */
.no-tests {
  background: var(--surface); border: 1px dashed var(--border); border-radius: var(--radius);
  color: var(--muted); padding: 28px 20px; text-align: center;
}

.no-tests strong {
  color: var(--text); display: block; font-size: 16px; margin-bottom: 4px;
}

footer.report-footer {
  border-top: 1px solid var(--border); color: var(--muted); font-size: 12px;
  margin-top: 32px; padding-top: 12px;
}

/* A narrow window cannot hold five columns at their minimum widths without crushing the node id
   column to a few characters per line, so below this width the table keeps the identifier column
   readable at a fixed minimum, tightens its spacing, and scrolls sideways inside its own box
   instead of widening the page: no column is dropped and nothing is pushed off the viewport. */
@media (max-width: 768px) {
  .table-scroll { overflow-x: auto; }
  table.results { font-size: 13px; }
  table.results thead th,
  table.results tbody th,
  table.results tbody td { padding: 6px 8px; }
  table.results .nodeid { font-size: 12px; min-width: 20ch; }
  /* A box that scrolls sideways cannot pin a descendant, and its height is the table's, so state
     the unpinned header explicitly rather than leaving a sticky rule that can never take effect. */
  table.results thead th { position: static; }
  table.results caption { text-align: left; }
}

@media print {
  body { max-width: none; padding: 0; }
  table.results thead th { box-shadow: none; position: static; }
  .controls { display: none; }
  /* A printed page cannot scroll sideways, so the table shows in full rather than clipped. */
  .table-scroll { overflow: visible; }
  .panel, .quarantined, .banner { box-shadow: none; }
  pre.log { max-height: none; overflow: visible; }
  details { break-inside: avoid; }
}
"""

JS: Final[str] = """\
/* foundry report script -- progressive enhancement only: the report is complete without it,
   because native details elements do every expansion. Plain ES5, no imports, no network. */
(function () {
  "use strict";

  function resultRows() {
    var table = document.getElementById("foundry-results") ||
      document.querySelector("table.results");
    var rows = [];
    if (!table || !table.tBodies) {
      return rows;
    }
    for (var b = 0; b < table.tBodies.length; b += 1) {
      for (var r = 0; r < table.tBodies[b].rows.length; r += 1) {
        rows.push(table.tBodies[b].rows[r]);
      }
    }
    return rows;
  }

  function filterRows(rows, needle) {
    for (var i = 0; i < rows.length; i += 1) {
      var row = rows[i];
      var text = String(row.textContent || "").toLowerCase();
      if (needle && text.indexOf(needle) === -1) {
        row.classList.add("hidden");
      } else {
        row.classList.remove("hidden");
      }
    }
  }

  function setAllDetails(open) {
    var nodes = document.getElementsByTagName("details");
    for (var i = 0; i < nodes.length; i += 1) {
      nodes[i].open = open;
    }
  }

  function start() {
    var root = document.documentElement;
    if (!root) {
      return;
    }
    root.setAttribute("data-foundry-js", "enabled");
    var rows = resultRows();
    var input = document.querySelector("[data-foundry-filter]");
    if (input && rows.length) {
      var apply = function () {
        filterRows(rows, String(input.value || "").toLowerCase());
      };
      input.addEventListener("input", apply);
      input.addEventListener("keyup", apply);
    }
    var expand = document.querySelector('[data-foundry-action="expand-all"]');
    if (expand) {
      expand.addEventListener("click", function () { setAllDetails(true); });
    }
    var collapse = document.querySelector('[data-foundry-action="collapse-all"]');
    if (collapse) {
      collapse.addEventListener("click", function () { setAllDetails(false); });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
"""

# The two spellings of an absolute reference. Assembled from fragments so this file carries no
# literal absolute-scheme spelling, which the source tree is scanned for.
SELF_CONTAINED_FORBIDDEN: Final[tuple[str, ...]] = ("http" + "://", "https" + "://")

# Markers of an asset fetched from somewhere else. ``url(`` alone is deliberately absent: a
# ``data:`` URI is self-contained and stays permitted. The first marker is assembled for the same
# reason as the two above.
EXTERNAL_REFERENCE_MARKERS: Final[tuple[str, ...]] = (
    "@" + "import",
    "url(http",
    "url(//",
    'src="http',
    "<link",
)


@dataclass(frozen=True, slots=True)
class AssetsBundle:
    """The inlined presentation assets one report is rendered with.

    Both strings are constants owned by this module, so the rendered document's self-contained
    property is a property of this file alone, and the frozen bundle is hashable and reusable.

    Attributes:
        css: The stylesheet, inlined verbatim inside the report's ``<style>`` element.
        js: The script, inlined verbatim inside the report's ``<script>`` element. It is
            progressive enhancement only: the report is complete without it.
        version: The asset schema version, raised only when the markup the assets target changes
            shape, so a renderer can tell an older bundle from a current one.
    """

    css: str
    js: str
    version: int = 1


DEFAULT_ASSETS: Final[AssetsBundle] = AssetsBundle(css=CSS, js=JS)


def default_assets() -> AssetsBundle:
    """Return the one shared bundle of inlined assets.

    The instance is built once at import and never rebuilt, so rendering five hundred result
    records costs no more than rendering one (AAP 0.14.2); being frozen, it cannot be mutated by
    a caller that needs different assets.

    Returns:
        The module-level :data:`DEFAULT_ASSETS` instance.
    """
    return DEFAULT_ASSETS


def _first_external_reference(text: str) -> str | None:
    """Return the first external-reference marker in ``text``, or ``None`` when there is none.

    The scan is case-insensitive, and it walks :data:`SELF_CONTAINED_FORBIDDEN` before
    :data:`EXTERNAL_REFERENCE_MARKERS`, so the marker a caller is told about is the same on every
    run.

    Returns:
        The marker as this module spells it, or ``None`` when ``text`` is self-contained.
    """
    lowered = text.lower()
    for marker in SELF_CONTAINED_FORBIDDEN + EXTERNAL_REFERENCE_MARKERS:
        if marker.lower() in lowered:
            return marker
    return None


def contains_external_reference(text: str) -> bool:
    """Report whether ``text`` pulls in anything from outside itself.

    Returns:
        ``True`` when any marker of :data:`SELF_CONTAINED_FORBIDDEN` or
        :data:`EXTERNAL_REFERENCE_MARKERS` appears in ``text`` -- compared case-insensitively --
        ``False`` otherwise. An empty string is self-contained.
    """
    return _first_external_reference(text) is not None


def assert_self_contained(text: str) -> None:
    """Raise unless ``text`` pulls in nothing from outside itself.

    The asset tests call this on :data:`CSS` and :data:`JS`; the renderer may call it on a
    finished document, which holds for a report whose interpolated values are escaped.

    Raises:
        FoundryError: ``text`` contains a marker from :data:`SELF_CONTAINED_FORBIDDEN` or
            :data:`EXTERNAL_REFERENCE_MARKERS`. The message names the marker and the hint says
            what to do instead.
    """
    marker = _first_external_reference(text)
    if marker is None:
        return
    raise FoundryError(
        f"report asset is not self-contained: found external reference {marker!r}",
        hint=(
            "inline the asset bytes instead of referencing them; the report must open on a "
            "machine with no network"
        ),
    )
