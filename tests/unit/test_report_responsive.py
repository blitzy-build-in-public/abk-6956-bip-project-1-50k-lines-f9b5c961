"""Unit tests for the report's narrow-window layout and its numeric cells.

The results table keeps its five columns inside its own scroll container, the identifier column
keeps a readable floor below the narrow breakpoint, a duration never breaks from its unit, and the
document stays offline-openable.
"""

from __future__ import annotations

from datetime import UTC, datetime

from foundry.reporting import assets, html
from foundry.reporting.record import Outcome, ResultRecord

_RUN_ID = "20250102T030405-abc123"
_PROFILE = "ci"
_STARTED_AT = datetime(2025, 1, 2, 3, 4, 5, 0, tzinfo=UTC)
_FINISHED_AT = datetime(2025, 1, 2, 3, 4, 5, 250_000, tzinfo=UTC)
_NARROW_QUERY = "@media (max-width: 768px)"
_EXTERNAL_MARKERS = ("http" + "://", "https" + "://", "url" + "(", "@" + "import")

_RECORD = ResultRecord(
    run_id=_RUN_ID,
    started_at=_STARTED_AT,
    finished_at=_FINISHED_AT,
    profile=_PROFILE,
    nodeid="tests/test_cart.py::test_total[case-3]",
    outcome=Outcome.PASSED,
    attempts=1,
    duration_ms=12.5,
)
"""One validated passing record; frozen, so both tests below render the same fixed bytes."""


def _render(records: list[ResultRecord]) -> str:
    """Render the report and fail rather than accept the documented template fallback."""
    document = html.render(records, run_id=_RUN_ID, profile=_PROFILE)
    assert document is not None
    return document


def _rule(css: str, selector: str) -> str:
    """Return one rule of ``css``, from its opening brace to its closing brace."""
    start = css.index(f"{selector} {{")
    return css[start : css.index("}", start) + 1]


def _responsive_block() -> str:
    """Return the narrow-window block: its media query up to the next media query."""
    start = assets.CSS.index(_NARROW_QUERY)
    end = assets.CSS.find("@media", start + len(_NARROW_QUERY))
    assert end > start, "the narrow-window block must be followed by another media query"
    return assets.CSS[start:end]


def test_the_results_table_sits_inside_its_own_scroll_container() -> None:
    document = _render([_RECORD])
    wrapper_at = document.index('<div class="table-scroll">')
    table_at = document.index('<table class="results">')
    table_end = document.index("</table>")
    assert wrapper_at < table_at < table_end < document.index("</div>", table_end)
    assert document.count('<div class="table-scroll">') == 1
    assert document.count('<th scope="col">') == 5


def test_a_run_with_no_records_renders_no_table_and_no_scroll_container() -> None:
    document = _render([])
    assert "no tests collected" in document
    assert "<table" not in document
    assert 'class="table-scroll"' not in document


def test_the_narrow_window_block_floors_the_identifier_column_and_scrolls_the_table() -> None:
    assert assets.CSS.count(_NARROW_QUERY) == 1
    block = _responsive_block()
    assert "overflow-x: auto" in _rule(block, ".table-scroll")
    assert "min-width:" in _rule(block, "table.results .nodeid")
    assert "text-align: left" in _rule(block, "table.results caption")


def test_a_duration_never_breaks_between_the_number_and_its_unit() -> None:
    assert "white-space: nowrap" in _rule(assets.CSS, "table.results .num")


def test_the_narrow_window_block_keeps_the_report_offline_openable() -> None:
    assets.assert_self_contained(assets.CSS)
    block = _responsive_block()
    leaked = [marker for marker in _EXTERNAL_MARKERS if marker in block]
    assert leaked == [], f"the narrow-window block references {leaked}"
    assert not assets.contains_external_reference(block)


def test_the_header_pins_above_the_breakpoint_only() -> None:
    """The sticky header survives on a wide window and is stated static where the table scrolls."""
    assert "position: sticky" in _rule(assets.CSS, "table.results thead th")
    assert "position: static" in _rule(_responsive_block(), "table.results thead th")
