"""Unit tests for the report's narrow-window layout and its numeric cells (AAP 0.7.4)."""

from datetime import UTC, datetime

from foundry.reporting import assets, html
from foundry.reporting.record import Outcome, ResultRecord

_RUN_ID = "20250102T030405-abc123"
_PROFILE = "ci"
_STARTED_AT = datetime(2025, 1, 2, 3, 4, 5, 0, tzinfo=UTC)
_FINISHED_AT = datetime(2025, 1, 2, 3, 4, 5, 250_000, tzinfo=UTC)
_NARROW = "@media (max-width: 768px)"
_PRINT = "@media print"
_EXTERNAL = ("http" + "://", "https" + "://", "url" + "(", "@" + "import")
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


def _render(records: list[ResultRecord]) -> str:
    document = html.render(records, run_id=_RUN_ID, profile=_PROFILE)
    assert document is not None, "the report fell back to no document"
    return document


def _rule(css: str, selector: str) -> str:
    start = css.index(f"{selector} {{")
    return css[start : css.index("}", start) + 1]


def _narrow() -> str:
    start = assets.CSS.index(_NARROW)
    end = assets.CSS.find("@media", start + len(_NARROW))
    assert end > start, "the narrow-window block must be followed by another media query"
    return assets.CSS[start:end]


def test_the_results_table_sits_inside_its_own_scroll_container() -> None:
    document = _render([_RECORD])
    wrapper_at = document.index('<div class="table-scroll">')
    table_at = document.index('<table class="results">')
    table_end = document.index("</table>")
    assert wrapper_at < table_at < table_end < document.index("</div>", table_end)
    assert document.count('<div class="table-scroll">') == 1
    assert document.count('<th scope="col"') == 5


def test_a_run_with_no_records_renders_no_table_and_no_scroll_container() -> None:
    document = _render([])
    assert "no tests collected" in document and "<table" not in document
    assert 'class="table-scroll"' not in document


def test_the_narrow_window_block_floors_the_identifier_column_and_scrolls_the_table() -> None:
    assert assets.CSS.count(_NARROW) == 1
    block = _narrow()
    assert "overflow-x: auto" in _rule(block, ".table-scroll")
    assert "min-width:" in _rule(block, "table.results .nodeid")
    assert "text-align: left" in _rule(block, "table.results caption")


def test_a_duration_never_breaks_between_the_number_and_its_unit() -> None:
    assert "white-space: nowrap" in _rule(assets.CSS, "table.results .num")


def test_the_narrow_window_block_keeps_the_report_offline_openable() -> None:
    assets.assert_self_contained(assets.CSS)
    block = _narrow()
    leaked = [marker for marker in _EXTERNAL if marker in block]
    assert leaked == [], f"the narrow-window block references {leaked}"
    assert not assets.contains_external_reference(block)


def test_the_header_pins_inside_its_scroll_box_below_the_breakpoint() -> None:
    assert "position: sticky" in _rule(assets.CSS, "table.results thead th")
    block = _narrow()
    assert "position: sticky" in _rule(block, "table.results thead th")
    assert "max-height: 80vh" in _rule(block, ".table-scroll")
    print_block = assets.CSS[assets.CSS.index(_PRINT) :]
    assert "max-height: none" in _rule(print_block, ".table-scroll")


def test_the_duration_header_sits_over_the_values_it_labels() -> None:
    document = _render([_RECORD])
    assert '<th scope="col" class="num">duration</th>' in document
    assert '<td class="num">' in document
    assert "text-align: right" in _rule(assets.CSS, "table.results .num")


def test_a_zero_tally_is_neutral_and_a_non_zero_tally_keeps_its_outcome_colour() -> None:
    document = _render([_RECORD])
    assert '<li class="summary-passed">' in document, "the passing tally lost its colour"
    assert '<li class="summary-failed is-zero">' in document, "a zero tally is not neutral"
    assert "color: var(--muted)" in _rule(assets.CSS, ".summary li.is-zero .count")


def test_the_summary_and_the_artifact_links_reach_the_forty_four_pixel_target() -> None:
    assert "min-height: 44px" in _rule(assets.CSS, ".panel summary")
    assert "min-height: 44px" in _rule(assets.CSS, ".panel dd a")
    assert "align-items: baseline" in _rule(assets.CSS, ".panel dl")


def test_the_caption_text_is_pinned_inside_the_narrow_scroll_box() -> None:
    assert _render([_RECORD]).count('<span class="caption-text">') == 1
    block = _narrow()
    assert "position: sticky" in _rule(block, "table.results .caption-text")
    assert "background-attachment: local, local, scroll, scroll" in block


def test_the_quarantined_section_keeps_the_page_rhythm_below_the_table() -> None:
    rule = _rule(assets.CSS, ".table-scroll + .quarantined")
    assert "margin-top: 20px" in rule, f"the table abuts the Quarantined section: {rule}"


def test_the_drawn_disclosure_triangle_survives_a_forced_colours_palette() -> None:
    block = assets.CSS[assets.CSS.index("@media (forced-colors: active)") :]
    rule = _rule(block, ".panel summary::before")
    assert "border-width: 0" in rule, f"the squared block is not replaced: {rule}"
    assert "clip-path: polygon(" in rule, f"the block is not clipped to a triangle: {rule}"
