"""The rendered artefacts of a real Foundry run: results.json, report.html and junit.xml.

Evidence for AAP 0.14.1 criterion 9 (reporting) and implicit requirement I10: "opens offline" is a
property of the rendered file, so the document itself is scanned for an external asset reference
rather than the intent being read out of the template. Every assertion here reads what a real run
left behind, from four child-process runs driven through pytest's own entry point: the main project,
a pass-only project, a directory with no tests, and a run whose artifact directory cannot be
created. AAP 0.13.1 places the rendered artefacts of a real run in this module, and the per-module
detail -- record validation, JUnit escaping rules, the comparator -- in
``tests/unit/test_reporting.py``.

The report layout under test is AAP 0.4.6 and AAP 0.7.4: a header carrying the run id, the profile,
``finished_at`` and the test count; a per-outcome summary strip; the results table sorted by node id
with each test's outcome, duration, profile and markers; the Quarantined section; one panel per
failing phase carrying the message, the phase, a link to the request dump, a link to the response
dump, the last ``logging.capture_lines`` captured log lines in a monospace block and the traceback
path; and an Errors section only when the run itself failed.

**Why the asset scan is scoped, and not a blanket absence of ``http://``.** A failure panel
legitimately quotes the request dump of a request made to ``http://127.0.0.1:<port>/items``, so a
``"http://" not in html`` assertion would fail a correct report. What I10 forbids is an *asset*
fetched over the network, which only a URL in ``src=``, ``href=``, ``url(`` or ``@import`` position
can cause, so those four positions are the ones scanned (AAP 0.6 governs the presentation surface).

**Run budget.** The suite's wall clock is capped at 360 s (AAP 0.14.2), so this module runs pytest
four times in total and no more: one run per state, with every assertion about that state grouped in
the test that made it.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from subprocess import CompletedProcess
from typing import Final

# --- The sample project the runs are made against ---------------------------

SAMPLE_CONFTEST: Final[str] = '''"""Loopback echo server, warning log and the sample base URL."""

from __future__ import annotations

import http.server
import logging
import pathlib
import threading
from typing import Iterator

import pytest

WARNINGS = pathlib.Path(__file__).with_name("foundry-warnings.log")
HANDLER = logging.FileHandler(WARNINGS, mode="w", encoding="utf-8")
HANDLER.setLevel(logging.WARNING)
logging.getLogger("foundry").addHandler(HANDLER)


class Echo(http.server.BaseHTTPRequestHandler):
    """Answer every GET with one small JSON document."""

    def do_GET(self) -> None:
        body = b'{"items": [1, 2, 3]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Stay silent: the run's own capture is the only thing a report reads."""


@pytest.fixture(scope="session")
def echo_base_url() -> Iterator[str]:
    """Serve 127.0.0.1 on a kernel-assigned port, so no fixed port is ever taken (AAP 0.14.1)."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Echo)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


@pytest.fixture(scope="session")
def foundry_config(foundry_config, echo_base_url):
    """Shadow the public fixture with the ephemeral base URL, as AAP 0.8.1 documents."""
    http = foundry_config.http.model_copy(update={"base_url": echo_base_url})
    return foundry_config.model_copy(update={"http": http})
'''

PASSING_TESTS: Final[str] = '''"""Two tests that pass."""

from __future__ import annotations


def test_one(foundry_run_id: str) -> None:
    assert foundry_run_id


def test_two(foundry_profile: str) -> None:
    assert foundry_profile == "default"
'''

FAILING_API_TEST: Final[str] = '''"""One request, 250 log records, then a deliberate failure."""

from __future__ import annotations

import logging

from foundry.http import FoundrySession


def test_api_fails(foundry_api: FoundrySession) -> None:
    log = logging.getLogger("foundry")
    for index in range(250):
        log.info("foundry-log-%03d", index)
    print("a stdout line the report records as an artifact")
    response = foundry_api.get("/items")
    assert response.status_code == 200
    assert False, "deliberate failure after one request"
'''

QUARANTINED_TEST: Final[str] = '''"""One known-flaky failure, quarantined in report mode."""

from __future__ import annotations

import pytest


@pytest.mark.foundry_quarantine
def test_quarantined() -> None:
    assert False, "known flaky"
'''

PARAMETRISED_TEST: Final[str] = '''"""A parametrised id carrying markup, which no name could."""

from __future__ import annotations

import pytest


@pytest.mark.parametrize("value", [1], ids=["<script>alert(1)</script>"])
def test_ids_case(value: int) -> None:
    assert value == 1
'''

MAIN_PROJECT_FILES: Final[Mapping[str, str]] = {
    "test_api.py": FAILING_API_TEST,
    "test_ids.py": PARAMETRISED_TEST,
    "test_passing.py": PASSING_TESTS,
    "test_quarantined.py": QUARANTINED_TEST,
}
"""The main project: two passes, one plain failure, one quarantined failure, one markup id."""

PASS_ONLY_FILES: Final[Mapping[str, str]] = {"test_passing.py": PASSING_TESTS}
"""The pass-only project: every test passes, so no panel and no Quarantined section."""

# --- What the runs are expected to produce ----------------------------------

API_NODEID: Final[str] = "test_api.py::test_api_fails"
"""The node id of the plain failure, the one record a failure panel is built for."""

QUARANTINED_NODEID: Final[str] = "test_quarantined.py::test_quarantined"
"""The node id of the quarantined failure."""

MARKUP_ID: Final[str] = "<script>alert(1)</script>"
"""The parametrisation id carrying raw markup: the escaping probe of AAP 0.3.2."""

MARKUP_NODEID: Final[str] = f"test_ids.py::test_ids_case[{MARKUP_ID}]"
"""The node id that carries :data:`MARKUP_ID` through the record, the table, the JUnit case."""

LOG_RECORDS: Final[int] = 250
"""Records the failing test logs, five times the size of the ring AAP 0.14.2 budgets at 200."""

STDOUT_LINE: Final[str] = "a stdout line the report records as an artifact"
"""The line ``test_api_fails`` prints: ``stdout.log`` carries it once, ``stderr.log`` nothing."""

CAPTURE_LINES: Final[int] = 200
"""``logging.capture_lines``: the default and the number of lines a panel must show."""

FIRST_RETAINED_LOG: Final[str] = f"foundry-log-{LOG_RECORDS - CAPTURE_LINES:03d}"
"""The oldest line the ring can still hold: ``foundry-log-050`` for 250 records and a 200 ring."""

LAST_LOG: Final[str] = f"foundry-log-{LOG_RECORDS - 1:03d}"
"""The newest line: ``foundry-log-249``."""

DISABLED_BANNER: Final[str] = "artifact capture is disabled for this run"
"""The disabled banner's opening words (report.html.j2), the AAP 0.4.7 state's visible proof."""

FAILURE_EXIT_CODE: Final[int] = 1
"""The exit code one non-quarantined failure earns, with the quarantined one not counted."""

NO_TESTS_EXIT_CODE: Final[int] = 5
"""pytest's own no-tests-collected code, which Foundry preserves unchanged (assumption A4)."""

EXTERNAL_ASSET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"""(?:src|href)\s*=\s*["']?\s*https?://""", re.IGNORECASE),
    re.compile(r"""url\(\s*["']?\s*https?://""", re.IGNORECASE),
    re.compile(r"""@import\s+(?:url\()?["']?https?://""", re.IGNORECASE),
)
"""The only positions a document can fetch an asset from (AAP 0.6, requirement I10).

Everything else that names a URL in the report is quoted run data -- a request dump's own
``http://127.0.0.1:<port>/items``, for instance -- and escaping it as text is what makes it safe
rather than absent.
"""

LOG_BLOCK: Final[re.Pattern[str]] = re.compile(
    r"""<pre class="log">(?P<lines>.*?)</pre>""", re.DOTALL
)
"""The failure panel's monospace log block, whose contents are the captured tail."""

# The rendered markers each assertion locates: the results table and its cells, the Quarantined
# section's heading, the failure panel's open tag and the run-error section's heading. They are read
# from the shipped template rather than restated, so a template that renames one fails this module.
TABLE_OPEN: Final[str] = '<table class="results">'
TABLE_CLOSE: Final[str] = "</table>"
ROW_OPEN: Final[str] = "<tr>"
QUARANTINE_HEADING: Final[str] = 'id="quarantined-heading"'
PANEL_OPEN: Final[str] = '<details class="panel"'
ERRORS_HEADING: Final[str] = 'id="errors-heading"'
DURATION_CELL: Final[re.Pattern[str]] = re.compile(
    r"""<td class="num">(?:\d+\.\d ms|\d+\.\d{2} s)</td>"""
)
"""One duration cell: ``12.4 ms`` under a second, ``1.25 s`` above it (``_format_duration``).

Each unit carries the decimal precision the formatter gives it -- one for milliseconds, two for
seconds -- so the pattern accepts both documented forms and nothing else: a cell with no number, a
cell with a number and no unit, or a unit at the other unit's precision all fail to match.
"""


# --- What one run left behind ----------------------------------------------


def _mapping(value: object) -> Mapping[str, object]:
    """Return ``value`` as a JSON object, failing the test when the document is shaped otherwise."""
    assert isinstance(value, dict), f"expected a JSON object, got {type(value).__name__}"
    return {str(key): entry for key, entry in value.items()}


def _sequence(value: object) -> tuple[object, ...]:
    """Return ``value`` as a JSON array, failing the test when the document is shaped otherwise."""
    assert isinstance(value, list), f"expected a JSON array, got {type(value).__name__}"
    return tuple(value)


def _escaped(text: str) -> str:
    """Return ``text`` escaped as the report template's autoescaping escapes it.

    The same five replacements ``markupsafe.escape`` makes, so a node id carrying markup is looked
    for in the document in the form the renderer actually wrote (AAP 0.3.2).
    """
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&#34;")
        .replace("'", "&#39;")
    )


def _section(document: str, start: str, end: str) -> str:
    """Return the slice of ``document`` between ``start`` and the first ``end`` after it."""
    begin = document.find(start)
    assert begin >= 0, f"the report has no {start!r}, so the marker this test reads is missing"
    stop = document.find(end, begin)
    assert stop > begin, f"the report has no {end!r} after {start!r}"
    return document[begin:stop]


@dataclass(frozen=True)
class RunOutput:
    """One run's exit code, its captured output and the three report files it wrote.

    Everything is read from disk on demand rather than cached, so a reader sees exactly the bytes
    the run left behind, and a document that is not shaped as Foundry promises fails in the
    accessor that first reads it.
    """

    exit_code: int
    stdout: str
    stderr: str
    results_path: Path
    html_path: Path
    junit_path: Path
    artifacts_dir: Path
    warnings_path: Path

    @property
    def results(self) -> tuple[object, ...]:
        """Return ``results.json`` decoded into the elements it holds, in document order."""
        return _sequence(json.loads(self.results_path.read_text(encoding="utf-8")))

    @property
    def records(self) -> tuple[Mapping[str, object], ...]:
        """Return the ``record_type == "result"`` elements, which is every per-test entry."""
        found: list[Mapping[str, object]] = []
        for element in self.results:
            entry = _mapping(element)
            if entry.get("record_type") == "result":
                found.append(entry)
        return tuple(found)

    @property
    def html(self) -> str:
        """Return ``report.html`` as text."""
        return self.html_path.read_text(encoding="utf-8")

    @property
    def junit(self) -> str:
        """Return ``junit.xml`` as text."""
        return self.junit_path.read_text(encoding="utf-8")

    @property
    def warnings(self) -> tuple[str, ...]:
        """Return the ``WARNING`` lines the run logged on the ``foundry`` logger, in order.

        The sample project's ``conftest.py`` attaches one file handler at ``WARNING`` to the
        ``foundry`` logger, which is the only stable way to count Foundry's own plumbing faults from
        outside the process: pytest shows captured log for a failing item, and a bootstrap fault
        logged during ``pytest_configure`` belongs to no item at all (AAP 0.4.7).
        """
        if not self.warnings_path.is_file():
            return ()
        text = self.warnings_path.read_text(encoding="utf-8")
        return tuple(line for line in text.splitlines() if line.strip())

    @property
    def output(self) -> str:
        """Return the run's combined terminal output, which no assertion may find a traceback in."""
        return f"{self.stdout}\n{self.stderr}"

    def record_for(self, nodeid: str) -> Mapping[str, object]:
        """Return the result record of ``nodeid``, failing when the run wrote none."""
        for record in self.records:
            if record.get("nodeid") == nodeid:
                return record
        raise AssertionError(
            f"no result record for {nodeid}: {[r.get('nodeid') for r in self.records]}"
        )

    def artifacts_of(self, record: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
        """Return ``record``'s artifact entries, each naming a kind, a path and a byte count."""
        return tuple(_mapping(entry) for entry in _sequence(record.get("artifacts", ())))

    def file_of(self, record: Mapping[str, object], relative_path: str) -> Path:
        """Return the file a record's run-relative artifact path names, under ``<dir>/<run_id>``.

        AAP 0.4.6 makes every recorded path relative to the *run* directory, so the run id is the
        missing segment between ``artifacts.dir`` and the path the record carries.
        """
        return self.artifacts_dir / str(record["run_id"]) / relative_path

    def file_of_kind(self, record: Mapping[str, object], kind: str) -> Path:
        """Return the file ``record``'s artifact of ``kind`` names, failing when it lists none."""
        for artifact in self.artifacts_of(record):
            if artifact["kind"] == kind:
                return self.file_of(record, str(artifact["path"]))
        raise AssertionError(
            f"{record['nodeid']} lists no {kind} artifact: "
            f"{[artifact['kind'] for artifact in self.artifacts_of(record)]}"
        )


def _run(
    run_pytest: Callable[..., CompletedProcess[str]],
    project: Path,
    output: Path,
    artifacts_dir: Path,
) -> RunOutput:
    """Run pytest in ``project`` and return what the run wrote into ``output``.

    ``--foundry-json-report`` relocates ``results.json`` and the other two report files follow it
    beside it (assumption A5), while ``--foundry-artifacts.dir`` and ``--foundry-cassette.dir`` keep
    every run output inside the test's own directory, so nothing is written beside the project.
    """
    results = output / "results.json"
    completed = run_pytest(
        "-q",
        "-p",
        "no:cacheprovider",
        "--foundry-json-report",
        str(results),
        "--foundry-artifacts.dir",
        str(artifacts_dir),
        "--foundry-cassette.dir",
        str(output / "cassettes"),
        cwd=project,
    )
    return RunOutput(
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        results_path=results,
        html_path=output / "report.html",
        junit_path=output / "junit.xml",
        artifacts_dir=artifacts_dir,
        warnings_path=project / "foundry-warnings.log",
    )


# --- Assertions over one real run -------------------------------------------


def _assert_reports_written(run: RunOutput) -> None:
    """AAP 0.14.1 criterion 9: the run wrote all three files, and changed no exit code for it."""
    assert run.exit_code == FAILURE_EXIT_CODE, run.output
    for path in (run.results_path, run.html_path, run.junit_path):
        assert path.is_file(), f"the run wrote no {path.name}"


def _assert_result_document(run: RunOutput) -> None:
    """AAP 0.4.6: every element is discriminated, records sorted by node id, paths relative."""
    assert run.results, "results.json holds no element at all"
    nodeids = [str(record["nodeid"]) for record in run.records]
    assert nodeids == sorted(nodeids), f"records are not sorted by node id: {nodeids}"
    for element in run.results:
        assert _mapping(element).get("record_type") in {"result", "run_error"}
    for record in run.records:
        for artifact in run.artifacts_of(record):
            path = str(artifact["path"])
            assert not path.startswith("/"), f"artifact path is absolute: {path}"
            assert ".." not in path.split("/"), f"artifact path escapes the run: {path}"


def _assert_quarantine_verdicts(run: RunOutput) -> None:
    """AAP 0.4.7: the quarantine outcomes, and the fact that only one failure counts."""
    quarantined = run.record_for(QUARANTINED_NODEID)
    assert quarantined["outcome"] == "quarantined_failed"
    assert _mapping(quarantined["quarantine"])["state"] == "failed"
    counted = [
        record["outcome"] for record in run.records if record["outcome"] in {"failed", "error"}
    ]
    assert counted == ["failed"], f"exactly one non-quarantined failure fails the run: {counted}"


def _assert_failure_record(run: RunOutput) -> None:
    """AAP 0.4.6: the plain failure carries the failing phase and a traceback artifact."""
    record = run.record_for(API_NODEID)
    assert record["outcome"] == "failed"
    failures = tuple(_mapping(entry) for entry in _sequence(record["failures"]))
    assert [failure["phase"] for failure in failures] == ["call"]
    traceback_path = failures[0]["traceback_path"]
    assert isinstance(traceback_path, str), f"the failure names no traceback: {failures[0]}"
    assert run.file_of(record, traceback_path).is_file(), traceback_path


def _assert_records_carry_their_artifacts(run: RunOutput) -> None:
    """AAP 0.4.6: the failing test captured the exchange, both streams and the bounded tail.

    ``stdout.log`` and ``stderr.log`` are written for every record that is not a pass, whether or
    not the test wrote to that stream, because the layout publishes both file names and
    ``junit.xml`` references them as ``foundry.artifact.stdout`` and ``foundry.artifact.stderr``.
    The failing test prints exactly one line and writes nothing to stderr, so its ``stdout.log`` is
    that line once and its ``stderr.log`` is the empty file the reference needs.
    ``artifacts.keep_passed`` is ``false``, so a passing record is pruned to its manifest, and every
    file a record names exists under ``artifacts/<run_id>/<sanitized-nodeid>/``.
    """
    record = run.record_for(API_NODEID)
    kinds = {str(artifact["kind"]) for artifact in run.artifacts_of(record)}
    assert kinds == {
        "log_tail",
        "manifest",
        "request",
        "response",
        "stderr",
        "stdout",
        "traceback",
    }, kinds
    for artifact in run.artifacts_of(record):
        path = run.file_of(record, str(artifact["path"]))
        assert path.is_file(), f"the record names {path} but the run wrote no such file"
    stdout = run.file_of_kind(record, "stdout").read_text(encoding="utf-8")
    assert stdout == f"{STDOUT_LINE}\n", f"stdout.log holds {stdout!r}, not the one printed line"
    stderr = run.file_of_kind(record, "stderr").read_text(encoding="utf-8")
    assert stderr == "", f"stderr.log holds {stderr!r} although the test wrote nothing to stderr"
    for other in run.records:
        if other["nodeid"] == API_NODEID:
            continue
        kinds_here = {str(artifact["kind"]) for artifact in run.artifacts_of(other)}
        if other["outcome"] == "passed":
            assert kinds_here == {"manifest"}, f"{other['nodeid']} kept {kinds_here}"
        else:
            # A record that did not pass may carry the streams and the traceback as well.
            assert kinds_here <= {"manifest", "stdout", "stderr", "traceback"}, (
                f"{other['nodeid']}: {kinds_here}"
            )


def _assert_no_external_asset(run: RunOutput) -> None:
    """Criterion 9 and requirement I10: nothing in the document can be fetched over a network."""
    document = run.html
    for pattern in EXTERNAL_ASSET_PATTERNS:
        found = pattern.search(document)
        assert found is None, f"the report references an external asset: {found.group(0)!r}"
    assert "<style>" in document, "the stylesheet is not inlined into the document"
    assert "<link" not in document, "the document links a stylesheet or a font instead of inlining"


def _table_rows(document: str) -> tuple[str, ...]:
    """Return the results table's body rows, in the order the report renders them."""
    body = _section(document, TABLE_OPEN, TABLE_CLOSE)
    return tuple(row for row in body.split(ROW_OPEN)[1:] if '"nodeid"' in row)


def _assert_test_table(run: RunOutput) -> None:
    """AAP 0.7.4: one row per test, sorted by node id, carrying outcome, duration and profile."""
    document = run.html
    rows = _table_rows(document)
    nodeids = [str(record["nodeid"]) for record in run.records]
    rendered = [_escaped(nodeid) for nodeid in nodeids]
    assert len(rows) == len(nodeids), f"{len(rows)} rows for {len(nodeids)} records"
    for row, nodeid in zip(rows, rendered, strict=True):
        assert nodeid in row, f"row does not name {nodeid}: {row[:120]}"
    positions = [document.index(nodeid) for nodeid in rendered]
    assert positions == sorted(positions), f"the table is not sorted by node id: {rendered}"
    failing = next(row for row, nodeid in zip(rows, rendered, strict=True) if API_NODEID in nodeid)
    assert "outcome-failed" in failing
    assert DURATION_CELL.search(failing) is not None, f"the row carries no duration: {failing}"
    assert ">default<" in failing, f"the row carries no profile: {failing}"
    quarantined = next(
        row for row, nodeid in zip(rows, rendered, strict=True) if QUARANTINED_NODEID in nodeid
    )
    assert "foundry_quarantine" in quarantined, "the marked row lists no marker name"
    assert "quarantined_failed" in quarantined


def _assert_quarantine_section(run: RunOutput) -> None:
    """AAP 0.4.6: the Quarantined section lists the quarantined failure by node id."""
    document = run.html
    assert QUARANTINE_HEADING in document, "the report has no Quarantined section"
    section = _section(document, QUARANTINE_HEADING, "</section>")
    assert _escaped(QUARANTINED_NODEID) in section, section[:200]


def _assert_failure_panel(run: RunOutput) -> None:
    """AAP 0.7.4: the panel links both dumps and shows exactly ``capture_lines`` log lines."""
    document = run.html
    panels = document.count(PANEL_OPEN)
    assert panels == 2, f"one panel per failing record, expected 2, found {panels}"
    assert f"<summary>{API_NODEID}: failed (call)</summary>" in document, "no panel for the failure"
    assert "request dump" in document, "the panel links no request dump"
    assert "response dump" in document, "the panel links no response dump"
    for name in ("request.json", "response.json"):
        assert name in document, f"the panel names no {name}"
    match = LOG_BLOCK.search(document)
    assert match is not None, "the panel renders no log block"
    lines = match.group("lines").split("\n")
    assert len(lines) == CAPTURE_LINES, f"the panel shows {len(lines)} lines, not {CAPTURE_LINES}"
    assert FIRST_RETAINED_LOG in lines[0], f"the oldest retained line is {lines[0]!r}"
    assert LAST_LOG in lines[-1], f"the newest line is {lines[-1]!r}"
    evicted = [f"foundry-log-{index:03d}" for index in range(LOG_RECORDS - CAPTURE_LINES)]
    assert not any(line in match.group("lines") for line in evicted), "the ring did not evict"


def _assert_markup_escaped(run: RunOutput) -> None:
    """AAP 0.3.2: a node id carrying markup renders as text, in the report and in the JUnit case."""
    document = run.html
    assert "&lt;script&gt;" in document, "the escaped form of the node id is missing"
    assert "<script>alert(1)</script>" not in document, "raw markup reached the document"
    junit = run.junit
    assert "&lt;script&gt;" in junit, "the JUnit document does not escape the node id"
    suite = ElementTree.parse(run.junit_path).getroot()
    cases = [(case.get("classname", ""), case.get("name", "")) for case in suite.iter("testcase")]
    matching = [case for case in cases if MARKUP_ID in case[1]]
    assert len(matching) == 1, f"expected one markup testcase, got {cases}"
    classname, name = matching[0]
    assert name == f"test_ids_case[{MARKUP_ID}]", name
    assert f"{classname}.py::{name}" == MARKUP_NODEID, (classname, name)


# --- The four runs ---------------------------------------------------------


def test_reports_render_a_real_run(
    tmp_path: Path,
    write_project: Callable[..., Path],
    run_pytest: Callable[..., CompletedProcess[str]],
) -> None:
    """Criterion 9: one real run, and every property the rendered artefacts owe a reader.

    The assertions are grouped by the artefact each one reads -- the result document, the HTML
    document, the JUnit document -- and each group is one helper, so a failure names the property
    rather than the assertion's line.
    """
    project = write_project(tmp_path / "main", files=MAIN_PROJECT_FILES, conftest=SAMPLE_CONFTEST)
    run = _run(run_pytest, project, tmp_path / "out-main", tmp_path / "out-main" / "artifacts")
    _assert_reports_written(run)
    _assert_result_document(run)
    _assert_quarantine_verdicts(run)
    _assert_failure_record(run)
    _assert_records_carry_their_artifacts(run)
    _assert_no_external_asset(run)
    _assert_test_table(run)
    _assert_quarantine_section(run)
    _assert_failure_panel(run)
    _assert_markup_escaped(run)
    # AAP 0.7.4: the Errors section exists only when the run itself failed, and this run did not.
    assert ERRORS_HEADING not in run.html, "a run with no run-level error listed an Errors section"
    # AAP 0.4.7: Foundry's own plumbing stayed quiet on a run that captured everything it needed.
    assert run.warnings == (), f"the run logged plumbing warnings: {run.warnings}"


def test_pass_only_run_renders_no_panel_and_no_quarantined_section(
    tmp_path: Path,
    write_project: Callable[..., Path],
    run_pytest: Callable[..., CompletedProcess[str]],
) -> None:
    """AAP 0.4.6 and 0.7.4: a run where every test passes renders neither panels nor quarantine."""
    project = write_project(tmp_path / "pass", files=PASS_ONLY_FILES, conftest=SAMPLE_CONFTEST)
    run = _run(run_pytest, project, tmp_path / "out-pass", tmp_path / "out-pass" / "artifacts")
    assert run.exit_code == 0, run.output
    assert [record["outcome"] for record in run.records] == ["passed", "passed"]
    document = run.html
    assert PANEL_OPEN not in document, "a run with no failure rendered a failure panel"
    assert QUARANTINE_HEADING not in document, "a run with no quarantine rendered the section"
    assert ERRORS_HEADING not in document, "a run with no run-level error rendered Errors"
    assert len(_table_rows(document)) == 2, "the table does not carry one row per test"
    assert run.warnings == (), f"the run logged plumbing warnings: {run.warnings}"
    # AAP 0.4.5: `artifacts.keep_passed` is false, so a passing test keeps only its manifest.
    for record in run.records:
        kinds = [str(artifact["kind"]) for artifact in run.artifacts_of(record)]
        assert kinds == ["manifest"], f"{record['nodeid']} kept {kinds}"


def test_directory_with_no_tests_writes_an_empty_document_and_a_no_tests_state(
    tmp_path: Path,
    write_project: Callable[..., Path],
    run_pytest: Callable[..., CompletedProcess[str]],
) -> None:
    """AAP 0.4.7: zero collected tests keep pytest's code, an empty document and a state."""
    project = write_project(tmp_path / "empty")
    run = _run(run_pytest, project, tmp_path / "out-empty", tmp_path / "out-empty" / "artifacts")
    assert run.exit_code == NO_TESTS_EXIT_CODE, run.output
    assert run.results == (), f"results.json is not the empty array: {run.results_path.read_text()}"
    document = run.html
    assert TABLE_OPEN not in document, "the report rendered an empty table instead of a state"
    assert "no tests" in document.lower(), "the report states no reason for having no results"
    assert run.warnings == (), f"the run logged plumbing warnings: {run.warnings}"


def test_unwritable_artifacts_directory_disables_capture_and_keeps_the_exit_code(
    tmp_path: Path,
    write_project: Callable[..., Path],
    run_pytest: Callable[..., CompletedProcess[str]],
) -> None:
    """AAP 0.4.7: capture disabled by an unwritable directory is one warning and no other change.

    The directory is made unwritable structurally rather than with ``chmod``: the run's artifact
    directory is placed under a path whose parent is a **regular file**, so ``mkdir`` fails for
    every user, a root runner included. A ``chmod 0o500`` would be undone by the privileges CI
    runs with, and the state would silently pass without ever being exercised.
    """
    project = write_project(
        tmp_path / "blocked", files=MAIN_PROJECT_FILES, conftest=SAMPLE_CONFTEST
    )
    blocker = tmp_path / "blocker"
    blocker.write_text("a regular file, so nothing can be created below it\n", encoding="utf-8")
    artifacts_dir = blocker / "artifacts"
    run = _run(run_pytest, project, tmp_path / "out-blocked", artifacts_dir)
    assert run.exit_code == FAILURE_EXIT_CODE, run.output
    assert "INTERNALERROR" not in run.output, "the fault reached pytest's own error path"
    assert len(run.records) == 5, f"the run recorded {len(run.records)} of 5 tests"
    for record in run.records:
        assert run.artifacts_of(record) == (), f"{record['nodeid']} lists an artifact"
        for failure in _sequence(record.get("failures", ())):
            assert _mapping(failure)["traceback_path"] is None, f"{record['nodeid']} names a path"
    document = run.html
    assert DISABLED_BANNER in document, "the report states no disabled banner"
    assert "request dump" not in document, "the report links a dump that was never written"
    assert str(artifacts_dir) not in document, "the report names the directory it could not write"
    # AAP 0.4.7: exactly one warning for the run, and it names what was disabled.
    assert len(run.warnings) == 1, f"expected one warning, got {run.warnings}"
    assert "artifact capture disabled" in run.warnings[0], run.warnings[0]
