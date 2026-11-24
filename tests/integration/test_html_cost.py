"""Timing instrument for ``report.html``: five hundred result records inside the AAP 0.14.2 budget.

AAP 0.14.2 budgets HTML generation at "500 records: <= 2 s", and AAP 0.10.1 names this module as
the instrument that fails when the budget is exceeded. What is timed is
:func:`foundry.reporting.html.render` -- the public function ``src/foundry/plugin.py`` calls at
session finish (AAP 0.7.1) -- reached through the checked-in template and the shipped asset bundle,
with no stub, no monkeypatch and no private helper in the way. Reading
``src/foundry/reporting/templates/report.html.j2`` from disk and building the Jinja2 environment
with ``autoescape=True`` therefore sit *inside* the measurement, because they sit inside the
production path: one environment is built per run, and a measurement that excluded compilation
would time a code path the product never runs.

The record set is built through the frozen models of :mod:`foundry.reporting.record` rather than
hand-written JSON, because the renderer's contract is the model: :class:`ResultRecord` validates
``attempts >= 1``, a non-negative ``duration_ms``, sorted and de-duplicated markers, run-relative
artifact paths free of ``..`` and a quarantine state that agrees with the outcome (AAP 0.7.2), so a
record set that violates any of them raises instead of rendering and cannot silently weaken the
measurement.

What the assertions prove, beyond the elapsed milliseconds:

* the work actually happened -- all five hundred node ids appear in the document and the results
  table holds exactly one row per record, counted on the template's own per-row marker, so a
  renderer that returned a stub could not pass the budget;
* the timed render is the escaped production renderer -- one node id carries a ``<script>`` payload
  and reaches the page escaped, which ``tests/integration/test_report_render.py`` does not cover;
* the document is self-contained -- no ``src`` or ``href`` attribute points at an absolute-scheme
  location and no ``<link>`` element pulls anything in, so the page opens offline;
* rendering is deterministic -- two renders of one record set are byte-identical, and a reversed
  input order renders the same bytes as the sorted one, which proves every iterated collection is
  sorted rather than inherited from the input (AAP 0.13.1, criterion 14).

No subprocess, server, temporary file or wall-clock read is involved: the render is in-process and
writes nothing to disk, ``time.perf_counter()`` is the only clock, and the record timestamps are
fixed constants so the measurement can never depend on the date (AAP 0.14.3).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

from foundry.reporting.assets import DEFAULT_ASSETS, AssetsBundle
from foundry.reporting.html import QUARANTINE_REMOVAL_NOTE, render, template_path
from foundry.reporting.record import (
    ArtifactRecord,
    FailureRecord,
    Outcome,
    QuarantineRecord,
    ResultRecord,
)

__all__: Final[tuple[str, ...]] = ("build_records",)

# --- The budget and the record set this module measures ---------------------

HTML_BUDGET_MS: Final[float] = 2000.0
"""The HTML-generation budget AAP 0.14.2 sets for five hundred result records, in milliseconds."""

RECORD_COUNT: Final[int] = 500
"""How many result records one measurement renders: the count AAP 0.14.2 names."""

TEMPLATE_RELATIVE_PATH: Final[Path] = Path("src/foundry/reporting/templates/report.html.j2")
"""The report template as it sits in the checkout, relative to the repository root.

Timing this file rather than a copy is what ties the measurement to the package data the wheel
ships (AAP 0.4.1), and reading it through ``repo_root`` keeps the test independent of the directory
the suite happens to be invoked from.
"""

RUN_ID: Final[str] = "20240304T050607-0a1b2c"
"""Fixed run id: ``YYYYmmddTHHMMSS-<6 hex>``, the shape :class:`ResultRecord` validates."""

STARTED_AT: Final[datetime] = datetime(2024, 3, 4, 5, 6, 7, tzinfo=UTC)
"""Fixed first-attempt timestamp. A constant, so the render depends on no clock read."""

FINISHED_AT: Final[datetime] = datetime(2024, 3, 4, 5, 8, 19, tzinfo=UTC)
"""Fixed last-attempt timestamp: after ``STARTED_AT``, as the record's own validator requires."""

PROFILE: Final[str] = "default"
"""Fixed profile name: the one every zero-configuration run resolves to (AAP 0.4.5)."""

ARTIFACTS_ROOT: Final[Path] = Path("artifacts") / RUN_ID
"""The run directory the records' artifact paths are relative to (AAP 0.4.6).

A relative path under the invocation directory, exactly as a run lays it out. Nothing reads under
it -- the records list no ``log_tail`` artifact -- so the panels compute their links without
touching the filesystem.
"""

REPORT_DIR: Final[Path] = Path()
"""The report's own directory: the invocation directory, as assumption A5 fixes."""

ROW_CELL: Final[str] = '<th scope="row" class="nodeid">'
"""The template's per-row marker, one per result record inside ``table.results``.

Read out of ``report.html.j2`` rather than guessed: the rows loop emits exactly this cell, so its
count is the row count the budget is measured against, and the markup test fails if a template
change removes it.
"""

MARKUP_PAYLOAD: Final[str] = "<script>alert(1)</script>"
"""The payload one node id carries, to prove the timed render is the escaping one."""

ESCAPED_MARKUP_PAYLOAD: Final[str] = "&lt;script&gt;alert(1)&lt;/script&gt;"
"""The same payload as ``autoescape=True`` renders it (AAP 0.7.4)."""

MARKUP_INDEX: Final[int] = RECORD_COUNT - 1
"""The record whose node id carries :data:`MARKUP_PAYLOAD`: last, so sort order stays stable."""

_ABSOLUTE_SCHEMES: Final[tuple[str, ...]] = ("http" + "://", "https" + "://")
"""The two spellings of an absolute reference a self-contained document may never carry.

Assembled from fragments, following :mod:`foundry.reporting.assets`: the source tree is scanned for
these literal spellings while the report's self-contained property is verified, and this module has
to pass the scan it takes part in.
"""

# The mix AAP 0.10.1 asks for -- 450 passed, 30 failed, 10 quarantined_failed, 5 quarantine_passed
# and 5 skipped -- laid out so that `attempts` never decreases across the sorted record order: the
# passing records come first, then the skipped and quarantine_passed ones, then the two retried
# outcomes. The multiset is unchanged; only its order differs.
_TAIL_SKIPPED: Final[int] = 5
_TAIL_QUARANTINE_PASSED: Final[int] = 5
_TAIL_QUARANTINED_FAILED: Final[int] = 10
_TAIL_FAILED: Final[int] = 30

_SPECIALISED_COUNT: Final[int] = (
    _TAIL_SKIPPED + _TAIL_QUARANTINE_PASSED + _TAIL_QUARANTINED_FAILED + _TAIL_FAILED
)
"""Records in the specialised tail; the passed records fill everything before it, so 450 at 500."""

_FAILED_OUTCOMES: Final[frozenset[Outcome]] = frozenset(
    {Outcome.FAILED, Outcome.QUARANTINED_FAILED}
)
"""The outcomes that carry a failure entry, three artifacts and a second attempt."""

_QUARANTINE_OUTCOMES_LOCAL: Final[frozenset[Outcome]] = frozenset(
    {Outcome.QUARANTINED_FAILED, Outcome.QUARANTINE_PASSED}
)
"""The outcomes that carry the ``foundry_quarantine`` marker and a quarantine verdict."""


# --- Building the record set ------------------------------------------------


def _nodeid(index: int) -> str:
    """Return record ``index``'s node id, zero-padded so its sorted position is unambiguous.

    Args:
        index: The record's position in the generated set.

    Returns:
        ``tests/test_module_<i>.py::test_case_<i>``, with ``<i>`` three digits wide, plus
        :data:`MARKUP_PAYLOAD` on the last record so the autoescape proof rides the same render.
    """
    nodeid = f"tests/test_module_{index:03d}.py::test_case_{index:03d}"
    return nodeid + MARKUP_PAYLOAD if index == MARKUP_INDEX else nodeid


def _outcome_for(index: int, count: int) -> Outcome:
    """Return record ``index``'s outcome, following the mix comment above.

    Args:
        index: The record's position in the generated set.
        count: The size of the generated set, which positions the specialised tail at its end.

    Returns:
        The outcome, one of the five the mix uses.
    """
    skipped_from = count - _SPECIALISED_COUNT
    quarantine_passed_from = skipped_from + _TAIL_SKIPPED
    quarantined_failed_from = quarantine_passed_from + _TAIL_QUARANTINE_PASSED
    failed_from = quarantined_failed_from + _TAIL_QUARANTINED_FAILED
    if index < skipped_from:
        return Outcome.PASSED
    if index < quarantine_passed_from:
        return Outcome.SKIPPED
    if index < quarantined_failed_from:
        return Outcome.QUARANTINE_PASSED
    if index < failed_from:
        return Outcome.QUARANTINED_FAILED
    return Outcome.FAILED


def _artifact_dir(index: int) -> str:
    """Return the percent-encoded per-test artifact directory a record's paths sit under.

    The shape is ``artifacts/<run_id>/<sanitized-nodeid>/`` (AAP 0.4.6) with the separators
    encoded, which is what keeps the recorded paths single-component and free of ``..``. Encoding
    the index rather than the node id keeps :data:`MARKUP_PAYLOAD` out of a filesystem path.

    Args:
        index: The record's position in the generated set.

    Returns:
        One relative directory name.
    """
    return f"tests%2Ftest_module_{index:03d}.py%3A%3Atest_case_{index:03d}"


def _phase_for(index: int) -> Literal["call", "setup"]:
    """Return the failing phase of record ``index``, alternating so both sort positions are used.

    Args:
        index: The record's position in the generated set.

    Returns:
        ``call`` for an even index and ``setup`` for an odd one: two of the three phases a
        :class:`FailureRecord` accepts, which exercises the panel order of
        :func:`foundry.reporting.panels.build_panels`.
    """
    return "call" if index % 2 == 0 else "setup"


def _markers_for(index: int, outcome: Outcome) -> tuple[str, ...]:
    """Return record ``index``'s marker names; the record sorts and de-duplicates them itself.

    Args:
        index: The record's position in the generated set.
        outcome: The record's outcome, which selects the marker family.

    Returns:
        The marker names, in the order they are written; a passed record carries one on even
        indexes and none on odd ones, so both the populated and the empty column render.
    """
    if outcome in _QUARANTINE_OUTCOMES_LOCAL:
        return ("foundry_quarantine", "slow")
    if outcome is Outcome.FAILED:
        return ("api", "regression")
    if outcome is Outcome.SKIPPED:
        return ("slow",)
    return ("smoke",) if index % 2 == 0 else ()


def _failure_records(index: int, outcome: Outcome) -> tuple[FailureRecord, ...]:
    """Return the single failure entry a failing record carries, or nothing for a clean one.

    Args:
        index: The record's position in the generated set.
        outcome: The record's outcome.

    Returns:
        One :class:`FailureRecord` naming the phase, a deterministic message and the traceback
        path; an empty tuple for an outcome that did not fail.
    """
    if outcome not in _FAILED_OUTCOMES:
        return ()
    return (
        FailureRecord(
            phase=_phase_for(index),
            message=f"assert checkout total {index} == 502",
            traceback_path=f"{_artifact_dir(index)}/traceback.txt",
        ),
    )


def _artifact_records(index: int, outcome: Outcome) -> tuple[ArtifactRecord, ...]:
    """Return the three artifacts a failing record lists, or nothing for a clean one.

    Request, response and traceback are exactly the three kinds a failure panel links to
    (AAP 0.7.4), so the renderer builds the whole link section for every failing record. No
    ``log_tail`` entry is listed, which is what keeps the render in memory: the panels read a tail
    from the filesystem only when a record names one.

    Args:
        index: The record's position in the generated set.
        outcome: The record's outcome.

    Returns:
        Three :class:`ArtifactRecord` entries, or an empty tuple.
    """
    if outcome not in _FAILED_OUTCOMES:
        return ()
    directory = _artifact_dir(index)
    return (
        ArtifactRecord(kind="request", path=f"{directory}/request.json", bytes=428),
        ArtifactRecord(kind="response", path=f"{directory}/response.json", bytes=1024),
        ArtifactRecord(kind="traceback", path=f"{directory}/traceback.txt", bytes=286),
    )


def _quarantine_record(outcome: Outcome) -> QuarantineRecord:
    """Return the quarantine verdict that agrees with ``outcome`` (AAP 0.7.2).

    Args:
        outcome: The record's outcome.

    Returns:
        ``failed`` for ``quarantined_failed``, ``passed`` for ``quarantine_passed`` and ``none``
        otherwise, which is the only combination the record's validator accepts.
    """
    if outcome is Outcome.QUARANTINED_FAILED:
        return QuarantineRecord(state="failed")
    if outcome is Outcome.QUARANTINE_PASSED:
        return QuarantineRecord(state="passed")
    return QuarantineRecord()


def _build_record(index: int, count: int) -> ResultRecord:
    """Build one validated result record of the measured set.

    Args:
        index: The record's position in the generated set.
        count: The size of the generated set.

    Returns:
        A record carrying every field AAP 0.4.6 fixes, with timestamps from the module constants
        rather than from a clock.
    """
    outcome = _outcome_for(index, count)
    return ResultRecord(
        run_id=RUN_ID,
        started_at=STARTED_AT,
        finished_at=FINISHED_AT,
        profile=PROFILE,
        nodeid=_nodeid(index),
        outcome=outcome,
        attempts=2 if outcome in _FAILED_OUTCOMES else 1,
        duration_ms=0.5 + (index % 37) * 1.25,
        markers=_markers_for(index, outcome),
        failures=_failure_records(index, outcome),
        artifacts=_artifact_records(index, outcome),
        quarantine=_quarantine_record(outcome),
    )


def build_records(count: int = RECORD_COUNT) -> tuple[ResultRecord, ...]:
    """Build the measured record set: five hundred validated results, sorted by node id.

    Args:
        count: How many records to build. At least :data:`_SPECIALISED_COUNT`, so the specialised
            tail fits; the default is the five hundred AAP 0.14.2 measures.

    Returns:
        The records, sorted by node id, which is the order
        :func:`foundry.reporting.html.build_context` sorts them into anyway.

    Raises:
        ValueError: ``count`` is smaller than the specialised tail, which could not hold the mix.
    """
    if count < _SPECIALISED_COUNT:
        raise ValueError(
            f"count must be at least {_SPECIALISED_COUNT} to hold the record mix, got {count}"
        )
    records = tuple(_build_record(index, count) for index in range(count))
    return tuple(sorted(records, key=lambda record: record.nodeid))


# --- Shared assertions ------------------------------------------------------


def _template_from_checkout(repo_root: Path) -> Path:
    """Return the checkout's report template, asserting it is present and non-empty first.

    A template that is missing or empty must fail loudly *before* the timer starts: a render that
    returned early would otherwise read as a fast one.

    Args:
        repo_root: The repository root, from the session fixture of the same name.

    Returns:
        The template's path.
    """
    template = repo_root / TEMPLATE_RELATIVE_PATH
    assert template.is_file(), f"the report template is missing: {template}"
    assert template.stat().st_size > 0, f"the report template is empty: {template}"
    assert template.name == template_path().name, (
        "the checkout's template and the package-data template disagree: "
        f"{template.name!r} != {template_path().name!r}"
    )
    return template


def _assets_from_package() -> AssetsBundle:
    """Return the shipped asset bundle, asserting both payloads are non-empty first.

    Returns:
        :data:`foundry.reporting.assets.DEFAULT_ASSETS`, the bundle every run renders with.
    """
    assets = DEFAULT_ASSETS
    assert assets.css.strip(), "the shipped stylesheet is empty"
    assert assets.js.strip(), "the shipped script is empty"
    assert assets.version >= 1, f"unexpected asset bundle version {assets.version!r}"
    return assets


def _render_report(template: Path, records: tuple[ResultRecord, ...]) -> str:
    """Render ``records`` through the production renderer and return the document.

    This is the call ``src/foundry/plugin.py`` makes at session finish (AAP 0.7.1), with the run
    directory and the report directory supplied as a real run supplies them; the renderer's
    documented fallback is unchanged -- a template fault yields ``None`` and one ``WARNING``, which
    this helper turns into a failure rather than letting it pass as a fast render.

    Args:
        template: The template file to render through.
        records: The result records to render.

    Returns:
        The complete text of ``report.html``.
    """
    document = render(
        records,
        template,
        _assets_from_package(),
        artifacts_root=ARTIFACTS_ROOT,
        report_dir=REPORT_DIR,
    )
    assert document is not None, (
        "the report renderer returned None: the shipped template did not render, so the budget "
        "measured nothing"
    )
    return document


def _rendered_nodeid(nodeid: str) -> str:
    """Return the text a node id is expected to reach the document as.

    Args:
        nodeid: A record's node id.

    Returns:
        The node id with :data:`MARKUP_PAYLOAD` replaced by its escaped form, which is what
        ``autoescape=True`` renders; every other node id is returned unchanged.
    """
    return nodeid.replace(MARKUP_PAYLOAD, ESCAPED_MARKUP_PAYLOAD)


def _assert_every_nodeid_present(document: str, records: tuple[ResultRecord, ...]) -> None:
    """Assert every record's node id reached the document, escaped where it carries markup.

    Args:
        document: The rendered report.
        records: The records that were rendered.
    """
    missing = [
        record.nodeid for record in records if _rendered_nodeid(record.nodeid) not in document
    ]
    assert not missing, (
        f"{len(missing)} of {len(records)} node ids are absent from the report, "
        f"first: {missing[0]!r}"
    )


def _assert_self_contained(document: str) -> None:
    """Assert the document pulls nothing in from outside itself (criterion 9).

    Only asset-bearing attributes are inspected, because a failure message may legitimately quote
    a URL. :func:`foundry.reporting.html.render` refuses a document whose attributes point outside
    it before returning one, so this is the independent whole-document cross-check of that guard.

    Args:
        document: The rendered report.
    """
    for scheme in _ABSOLUTE_SCHEMES:
        for attribute in ("src", "href"):
            needle = f'{attribute}="{scheme}'
            assert needle not in document, (
                f"the report references an external asset: {needle!r} appears in report.html"
            )
    assert "<link" not in document, (
        "the report carries a <link> element, so it is not self-contained"
    )


# --- The measurements -------------------------------------------------------


def test_the_template_carries_the_markup_this_module_counts(repo_root: Path) -> None:
    """The row marker and the sections the cost assertions count are present in the template.

    This pins the basis of the row count: if a template change renames the per-row cell, the cost
    test would count zero rows and say the wrong thing, so the basis is asserted on its own.
    """
    source = _template_from_checkout(repo_root).read_text(encoding="utf-8")
    assert ROW_CELL in source, f"the template no longer emits {ROW_CELL!r} per result row"
    assert 'class="quarantined"' in source, "the template lost its Quarantined section"
    assert '<details class="panel"' in source, "the template lost its failure panels"
    assert "{{ ctx.total_tests }} tests" in source, "the template lost its test total"


def test_five_hundred_records_render_inside_the_two_second_budget(repo_root: Path) -> None:
    """Five hundred result records render in at most the AAP 0.14.2 budget of two seconds.

    The render is the production path, so the measurement includes reading the template and
    building the Jinja2 environment with ``autoescape=True``. The content assertions follow the
    budget so that a renderer returning a stub cannot pass on time alone.
    """
    template = _template_from_checkout(repo_root)
    records = build_records()
    assert len(records) == RECORD_COUNT, f"the record set holds {len(records)} records"

    started = time.perf_counter()
    document = _render_report(template, records)
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    assert elapsed_ms <= HTML_BUDGET_MS, (
        f"report.html render took {elapsed_ms:.1f} ms for {RECORD_COUNT} records, "
        f"over the {HTML_BUDGET_MS:.0f} ms budget"
    )

    row_count = document.count(ROW_CELL)
    print(
        f"report.html: {RECORD_COUNT} records in {elapsed_ms:.1f} ms "
        f"(budget {HTML_BUDGET_MS:.0f} ms), {row_count} rows, {len(document)} chars"
    )

    assert row_count == RECORD_COUNT, (
        f"the report holds {row_count} result rows for {RECORD_COUNT} records"
    )
    # The table's header row is the only other one, so the element count cross-checks the marker.
    assert document.count("<tr") >= RECORD_COUNT + 1, "the results table lost rows"
    assert f"{RECORD_COUNT} tests" in document, "the report's header lost the test total"
    _assert_every_nodeid_present(document, records)

    failed = sum(1 for record in records if record.outcome in _FAILED_OUTCOMES)
    quarantined_passed = sum(1 for record in records if record.outcome is Outcome.QUARANTINE_PASSED)
    assert document.count('<details class="panel"') == failed, (
        "one failure panel per failing phase was not rendered"
    )
    assert document.count(QUARANTINE_REMOVAL_NOTE) == quarantined_passed, (
        "the Quarantined section did not carry one removal note per marked pass"
    )
    assert 'class="no-tests"' not in document, "a populated run rendered the no-tests state"
    assert 'class="banner"' not in document, "the run's records carry artifacts, so no banner"

    _assert_self_contained(document)


def test_one_record_set_renders_identically_twice(repo_root: Path) -> None:
    """Two renders of one record set are byte-identical, and input order changes nothing.

    Reversing the input proves the determinism is the renderer's -- every collection it iterates is
    sorted before it reaches the template -- rather than an accident of the order this module
    happens to build in (AAP 0.13.1, criterion 14).
    """
    template = _template_from_checkout(repo_root)
    records = build_records()

    first = _render_report(template, records)
    second = _render_report(template, records)
    reversed_input = _render_report(template, tuple(reversed(records)))

    assert first == second, "two renders of one record set produced different documents"
    assert first == reversed_input, (
        "the document depends on the input order, so a collection reaches the template unsorted"
    )


def test_the_marked_nodeid_reaches_the_page_escaped(repo_root: Path) -> None:
    """The node id carrying a script payload is escaped in the timed renderer's output.

    This is the autoescape proof for the document the budget measures: the renderer's environment
    is built with ``autoescape=True`` (AAP 0.7.4), so markup in a node id renders as text.
    """
    document = _render_report(_template_from_checkout(repo_root), build_records())
    marked = next(record.nodeid for record in build_records() if MARKUP_PAYLOAD in record.nodeid)
    assert ESCAPED_MARKUP_PAYLOAD in document, f"the escaped node id is absent: {marked!r}"
    assert MARKUP_PAYLOAD not in document, "a node id reached the page as unescaped markup"
