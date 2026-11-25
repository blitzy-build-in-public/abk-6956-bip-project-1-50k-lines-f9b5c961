"""The rendered artefacts of a real run: results.json, report.html and junit.xml (AAP 0.14.1 c9).

Criterion 9 and I10: the rendered document is scanned for an asset a browser would fetch; four
child runs -- main, pass-only, empty, blocked -- assert the AAP 0.4.6/0.4.7/0.7.4 layout."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable, Mapping
from pathlib import Path
from subprocess import CompletedProcess
from typing import Final, TypeAlias

SAMPLE_CONFTEST: Final[str] = """import http.server, logging, pathlib, pytest, threading
LOG = pathlib.Path(__file__).with_name("foundry-warnings.log")
HANDLER = logging.FileHandler(LOG, mode="w", encoding="utf-8"); HANDLER.setLevel(logging.WARNING)
logging.getLogger("foundry").addHandler(HANDLER)
class Echo(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"items": [1, 2, 3]}'; self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
    def log_message(self, format, *args): return None
@pytest.fixture(scope="session")
def foundry_config(foundry_config):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Echo)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    resolved = foundry_config.http.model_copy(update={"base_url": url})
    yield foundry_config.model_copy(update={"http": resolved})
    server.shutdown(); server.server_close()
"""

PASSING_TESTS: Final[str] = """
def test_one(foundry_run_id): assert foundry_run_id
def test_two(foundry_profile): assert foundry_profile == "default"
"""

FAILING_API_TEST: Final[str] = """
import logging
def test_api_fails(foundry_api):
    log = logging.getLogger("foundry")
    for index in range(250): log.info("foundry-log-%03d", index)
    print("a stdout line the report records as an artifact")
    response = foundry_api.get("/items"); assert response.status_code == 200
    assert False, "deliberate failure after one request"
"""

QUARANTINED_TEST: Final[str] = """
import pytest
@pytest.mark.foundry_quarantine
def test_quarantined(): assert False, "known flaky"
"""

PARAMETRISED_TEST: Final[str] = """
import pytest
@pytest.mark.parametrize("value", [1], ids=["<script>alert(1)</script>"])
def test_ids_case(value): assert value == 1
"""

MAIN_PROJECT_FILES: Final[Mapping[str, str]] = {
    "test_api.py": FAILING_API_TEST,
    "test_ids.py": PARAMETRISED_TEST,
    "test_passing.py": PASSING_TESTS,
    "test_quarantined.py": QUARANTINED_TEST,
}
# The failing sample test's one printed line; ``stderr.log`` stays empty.
STDOUT_LINE: Final[str] = "a stdout line the report records as an artifact"

API_NODEID: Final[str] = "test_api.py::test_api_fails"
QUARANTINED_NODEID: Final[str] = "test_quarantined.py::test_quarantined"
MARKUP_ID: Final[str] = "<script>alert(1)</script>"
MARKUP_NODEID: Final[str] = f"test_ids.py::test_ids_case[{MARKUP_ID}]"
LOG_RECORDS: Final[int] = 250
CAPTURE_LINES: Final[int] = 200
FIRST_RETAINED_LOG: Final[str] = f"foundry-log-{LOG_RECORDS - CAPTURE_LINES:03d}"
LAST_LOG: Final[str] = f"foundry-log-{LOG_RECORDS - 1:03d}"
# The seven artifact kinds a failing record keeps, in sorted order (AAP 0.4.6, 0.4.5).
FAILING_KINDS: Final[str] = "log_tail manifest request response stderr stdout traceback"
# The positions a document fetches an asset from (AAP 0.6, I10); a bare "http://" scan would fail,
# because a failure panel quotes http://127.0.0.1:<port>/items. Only these positions may not.
EXTERNAL_ASSET: Final[re.Pattern[str]] = re.compile(
    r"""(?:(?:src|href)\s*=\s*["']?\s*|url\(\s*["']?\s*|@import\s+(?:url\()?["']?)https?://""",
    re.IGNORECASE,
)
LOG_BLOCK: Final[re.Pattern[str]] = re.compile(r'<pre class="log">(?P<lines>.*?)</pre>', re.DOTALL)
# One duration cell: "12.4 ms" under a second, "1.25 s" above it, each at its documented precision.
DURATION: Final[re.Pattern[str]] = re.compile(r'<td class="num">\d+\.\d(?: ms|\d s)</td>')
# Markers read back from the shipped template, so a template that renames one fails this module.
TABLE_OPEN: Final[str] = '<table class="results">'
QUARANTINE_HEADING: Final[str] = 'id="quarantined-heading"'
PANEL_OPEN: Final[str] = '<details class="panel"'
ERRORS_HEADING: Final[str] = 'id="errors-heading"'
Runner: TypeAlias = Callable[..., CompletedProcess[str]]
Writer: TypeAlias = Callable[..., Path]


def _mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, dict), f"expected a JSON object, got {type(value).__name__}"
    return {str(key): entry for key, entry in value.items()}


def _sequence(value: object) -> tuple[object, ...]:
    assert isinstance(value, list), f"expected a JSON array, got {type(value).__name__}"
    return tuple(value)


def _escaped(text: str) -> str:
    # markupsafe.escape's replacements: a node id's markup is found as the renderer wrote it.
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _table_rows(document: str) -> tuple[str, ...]:
    begin = document.find(TABLE_OPEN)
    assert begin >= 0, "the report renders no results table"
    table = document[begin : document.find("</table>", begin)]
    return tuple(row for row in table.split("<tr>")[1:] if '"nodeid"' in row)


def _records(results: Path) -> tuple[Mapping[str, object], ...]:
    elements = json.loads(results.read_text(encoding="utf-8"))
    assert isinstance(elements, list), results
    entries = [_mapping(element) for element in elements]
    return tuple(entry for entry in entries if entry.get("record_type") == "result")


def _record(records: tuple[Mapping[str, object], ...], nodeid: str) -> Mapping[str, object]:
    found = [record for record in records if record.get("nodeid") == nodeid]
    assert found, f"no result record for {nodeid}"
    return found[0]


def _artifacts(record: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    return tuple(_mapping(entry) for entry in _sequence(record.get("artifacts", ())))


def _artifact_text(record: Mapping[str, object], run_dir: Path, kind: str) -> str:
    path = next(str(one["path"]) for one in _artifacts(record) if one["kind"] == kind)
    return (run_dir / path).read_text(encoding="utf-8")


def _warnings(project: Path) -> tuple[str, ...]:
    # The sample conftest's file handler is the only stable way to see a fault raised during
    # pytest_configure, which belongs to no item (AAP 0.4.7).
    path = project / "foundry-warnings.log"
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    return tuple(line for line in text.splitlines() if line.strip())


def _run(
    run_pytest: Runner, project: Path, output: Path, artifacts: Path | None = None
) -> CompletedProcess[str]:
    # A5: --foundry-json-report relocates all three documents; every output stays in the test's dir.
    return run_pytest(
        f"--foundry-json-report={output / 'results.json'}",
        f"--foundry-artifacts.dir={artifacts or output / 'artifacts'}",
        f"--foundry-cassette.dir={output / 'cassettes'}",
        cwd=project,
    )


def test_reports_render_a_real_run(
    tmp_path: Path, write_project: Writer, run_pytest: Runner
) -> None:
    project = write_project(tmp_path / "main", files=MAIN_PROJECT_FILES, conftest=SAMPLE_CONFTEST)
    output = tmp_path / "out-main"
    completed = _run(run_pytest, project, output)
    # Criterion 9: the run wrote all three files under their fixed names and changed no exit code.
    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert all((output / name).is_file() for name in ("results.json", "report.html", "junit.xml"))
    records = _records(output / "results.json")
    # AAP 0.4.7: the quarantined failure is reported as such, and only one failure counts.
    quarantined = _record(records, QUARANTINED_NODEID)
    assert quarantined["outcome"] == "quarantined_failed"
    assert _mapping(quarantined["quarantine"])["state"] == "failed"
    assert [o["outcome"] for o in records if o["outcome"] in {"failed", "error"}] == ["failed"]
    # AAP 0.4.6: a failing record keeps exchange, streams and log tail; a passer only its manifest.
    failing = _record(records, API_NODEID)
    failures = tuple(_mapping(entry) for entry in _sequence(failing["failures"]))
    assert [failure["phase"] for failure in failures] == ["call"]
    traceback_path = str(failures[0]["traceback_path"])
    run_dir = output / "artifacts" / str(failing["run_id"])
    document = (output / "report.html").read_text(encoding="utf-8")
    assert (run_dir / traceback_path).is_file() and traceback_path in document, "no traceback link"
    assert {str(one["kind"]) for one in _artifacts(failing)} == set(FAILING_KINDS.split())
    assert _artifact_text(failing, run_dir, "stdout") == f"{STDOUT_LINE}\n"
    assert _artifact_text(failing, run_dir, "stderr") == ""
    others = [one for one in records if one["nodeid"] != API_NODEID and one["outcome"] == "passed"]
    assert [{a["kind"] for a in _artifacts(o)} for o in others] == [{"manifest"}] * len(others)
    # Criterion 9 and I10: nothing is fetchable over a network; the stylesheet is inlined.
    assert EXTERNAL_ASSET.search(document) is None, document[:4000]
    assert "<style>" in document and "<link" not in document
    # AAP 0.7.4: one row per test in node-id order, carrying outcome, duration, profile and markers.
    rows = _table_rows(document)
    rendered = [_escaped(str(record["nodeid"])) for record in records]
    assert len(rows) == len(records), f"{len(rows)} rows for {len(records)} records"
    by_nodeid = dict(zip(rendered, rows, strict=True))
    assert all(name in row for name, row in by_nodeid.items()), "a row omits its node id"
    failing_row = by_nodeid[_escaped(API_NODEID)]
    assert "outcome-failed" in failing_row and DURATION.search(failing_row) is not None
    assert ">default<" in failing_row, failing_row
    marked_row = by_nodeid[_escaped(QUARANTINED_NODEID)]
    assert "foundry_quarantine" in marked_row and "quarantined_failed" in marked_row
    # AAP 0.7.4/0.14.2: one panel per failure, both dumps, exactly capture_lines lines, oldest kept.
    assert document.count(PANEL_OPEN) == 2
    summary = f'<span class="summary-text">{API_NODEID}: failed (call)</span>'
    assert f"<summary>{summary}</summary>" in document, "no panel for the failure"
    assert "request dump" in document and "response dump" in document
    block = LOG_BLOCK.search(document)
    assert block is not None, "the panel renders no log block"
    lines = block.group("lines").split("\n")
    assert len(lines) == CAPTURE_LINES, f"the panel shows {len(lines)} lines"
    assert FIRST_RETAINED_LOG in lines[0] and LAST_LOG in lines[-1]
    evicted = [f"foundry-log-{index:03d}" for index in range(LOG_RECORDS - CAPTURE_LINES)]
    assert [name for name in evicted if name in block.group("lines")] == []
    # AAP 0.3.2: a node id carrying markup renders as text, in the report and in the JUnit case.
    assert "&lt;script&gt;" in document, "the escaped form of the node id is missing"
    assert "<script>alert(1)</script>" not in document, "raw markup reached the document"
    junit = (output / "junit.xml").read_text(encoding="utf-8")
    assert "&lt;script&gt;" in junit, "the JUnit document does not escape the node id"
    suite = ElementTree.parse(output / "junit.xml").getroot()
    cases = [(one.get("classname", ""), one.get("name", "")) for one in suite.iter("testcase")]
    matching = [case for case in cases if MARKUP_ID in case[1]]
    assert [f"{name}.py::{case}" for name, case in matching] == [MARKUP_NODEID], cases
    # AAP 0.7.4: the Errors section exists only when the run itself failed, and this run did not.
    assert ERRORS_HEADING not in document and _warnings(project) == (), _warnings(project)


def test_pass_only_run_renders_no_panel_and_no_quarantined_section(
    tmp_path: Path, write_project: Writer, run_pytest: Runner
) -> None:
    project = write_project(
        tmp_path / "pass", files={"test_passing.py": PASSING_TESTS}, conftest=SAMPLE_CONFTEST
    )
    output = tmp_path / "out-pass"
    completed = _run(run_pytest, project, output)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    records = _records(output / "results.json")
    assert [record["outcome"] for record in records] == ["passed", "passed"]
    document = (output / "report.html").read_text(encoding="utf-8")
    assert PANEL_OPEN not in document, "a run with no failure rendered a failure panel"
    assert QUARANTINE_HEADING not in document and ERRORS_HEADING not in document
    assert len(_table_rows(document)) == 2
    # AAP 0.4.5: artifacts.keep_passed is false, so a passing test keeps only its manifest.
    assert [[a["kind"] for a in _artifacts(r)] for r in records] == [["manifest"]] * len(records)
    assert _warnings(project) == (), _warnings(project)


def test_directory_with_no_tests_writes_an_empty_document_and_a_no_tests_state(
    tmp_path: Path, write_project: Writer, run_pytest: Runner
) -> None:
    output = tmp_path / "out-empty"
    completed = _run(run_pytest, write_project(tmp_path / "empty"), output)
    # AAP 0.4.7: zero collected tests keep pytest's own code, an empty document and a state.
    assert completed.returncode == 5, completed.stdout + completed.stderr
    assert json.loads((output / "results.json").read_text(encoding="utf-8")) == [], "not empty"
    document = (output / "report.html").read_text(encoding="utf-8")
    assert TABLE_OPEN not in document, "the report rendered an empty table instead of a state"
    assert "no tests" in document.lower(), "the report states no reason for having no results"


def test_unwritable_artifacts_directory_disables_capture_and_keeps_the_exit_code(
    tmp_path: Path, write_project: Writer, run_pytest: Runner
) -> None:
    # Blocked structurally, not with chmod: the parent is a regular file, so mkdir fails for root.
    project = write_project(tmp_path / "block", files=MAIN_PROJECT_FILES, conftest=SAMPLE_CONFTEST)
    blocker = tmp_path / "blocker"
    blocker.write_text("a regular file, so nothing can be created below it\n", encoding="utf-8")
    artifacts_dir = blocker / "artifacts"
    output = tmp_path / "out-blocked"
    completed = _run(run_pytest, project, output, artifacts_dir)
    # AAP 0.4.7: capture disabled is one warning and nothing else; every test is still recorded.
    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "INTERNALERROR" not in completed.stdout + completed.stderr, "the fault escaped"
    records = _records(output / "results.json")
    assert len(records) == 5, f"the run recorded {len(records)} of 5 tests"
    assert [_artifacts(r) for r in records] == [()] * len(records), "an artifact is listed"
    failures = [one for record in records for one in _sequence(record.get("failures", ()))]
    assert [_mapping(one)["traceback_path"] for one in failures] == [None] * len(failures)
    document = (output / "report.html").read_text(encoding="utf-8")
    assert "artifact capture is disabled for this run" in document, "no disabled banner"
    assert "request dump" not in document, "the report links a dump that was never written"
    assert str(artifacts_dir) not in document, "the report names the directory it could not write"
    warnings = _warnings(project)
    assert len(warnings) == 1 and "artifact capture disabled" in warnings[0], warnings
