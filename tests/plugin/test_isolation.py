"""Failure isolation of Foundry's own plumbing (AAP 0.1.2): one ``WARNING``, the same outcome."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Final

import pytest

REACHES: tuple[str, ...] = ("fixture:foundry_log_capture",)

_SENTINEL: Final[str] = "SENTINEL-ISOLATION"
_DOCS: Final[tuple[str, ...]] = ("results.json", "report.html", "junit.xml")
_LOG: Final[str] = "foundry.log"
_ARTIFACTS: Final[str] = "artifacts"
_DOCUMENTED: Final[str] = "documented.json"
_MARKERS: Final[tuple[str, ...]] = ("plumbing_failure", "html_fallback")

_PASSING: Final[str] = (
    "import logging\n\n\ndef test_ok() -> None:\n    assert True\n\n\n"
    "def test_ring(foundry_log_capture):\n"
    '    logging.getLogger("foundry").warning("foundry log capture fixture probe")\n'
    "    tail = foundry_log_capture.tail(1)\n"
    '    assert len(tail) == 1 and tail[0].startswith("WARNING") and "fixture probe" in tail[0]\n'
    "    assert len(foundry_log_capture.records) >= 1\n"
)
_FAILING: Final[str] = (
    f'SENTINEL = "{_SENTINEL}"\n\n\ndef test_fails() -> None:\n'
    '    assert f"x-api-key: {SENTINEL}" == "no"\n'
)
_DOCUMENTING: Final[str] = (
    'import json, pathlib\nFACTS = pathlib.Path(__file__).with_name("documented.json")\n\n\n'
    "def test_the_published_members_answer_for_this_test(foundry_artifacts):\n"
    '    reference = foundry_artifacts.write("stdout", None, "published write\\n")\n'
    '    facts = {"manifest": len(foundry_artifacts.manifest), "written": reference is not None}\n'
    '    FACTS.write_text(json.dumps(facts, sort_keys=True), encoding="utf-8")\n'
    '    assert reference is None or reference.kind == "stdout"\n'
)
_SECRET: Final[str] = f'[http]\nheaders = {{ x-api-key = "{_SENTINEL}" }}\n'
_ARTIFACT_CASES: Final[tuple[pytest.ParameterSet, ...]] = (
    pytest.param("test_probe", "test_ok", _PASSING, 0, "passed", id="passing"),
    pytest.param("test_failing", "test_fails", _FAILING, 1, "failed", id="failing"),
)


# A patch in this process outlives its test, so every run goes through the child's conftest.
_CONFTEST: Final[str] = """import logging, pathlib
LOG_PATH = pathlib.Path(__file__).with_name("foundry.log")
class _FileHandler(logging.Handler):
    def emit(self, record):
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"{record.levelname}|{record.name}|{record.getMessage()}\\n")
def pytest_configure(config):
    logger = logging.getLogger("foundry"); logger.setLevel(logging.DEBUG)
    logger.addHandler(_FileHandler())
"""

_INJECTIONS: Final[dict[str, str]] = {
    "redactor": """    from foundry.obs.redaction import Redactor
    def _raising_text(self, text, *, limit=None): raise RuntimeError("injected redaction failure")
    Redactor.text = _raising_text
""",
    "html": """    import foundry.reporting.html as html
    broken = LOG_PATH.with_name("broken.html.j2")
    broken.write_text("{% for row in %}{% endfor %}", encoding="utf-8")
    html.template_path = lambda: broken
""",
    "artifact_write": """    from foundry.obs.artifacts import ArtifactWriter
    def _raising_finalize(self, *, passed): raise RuntimeError("injected write failure")
    ArtifactWriter.finalize = _raising_finalize
""",
    "terminal": """    from _pytest.terminal import TerminalReporter
    write_sep = TerminalReporter.write_sep
    def _raising_write_sep(self, sep, title, **markup):
        if title.startswith("foundry: profile="): raise OSError(5, "I/O error", "closed")
        return write_sep(self, sep, title, **markup)
    TerminalReporter.write_sep = _raising_write_sep
""",
    "terminal_sink": """    import foundry.obs.capture as capture
    def _raising_scrub(writer, redactor):
        raise RuntimeError("injected terminal sink failure")
    capture.scrub_terminal_writer = _raising_scrub
""",
}


def _conftest_source(plant: str | None) -> str:
    return _CONFTEST + _INJECTIONS.get(plant or "", "")


def _log_lines(project: Path) -> list[str]:
    path = project / _LOG
    return path.read_text(encoding="utf-8").splitlines() if path.is_file() else []


def _plumbing(project: Path) -> list[str]:
    lines = _log_lines(project)
    return [ln for ln in lines if ln.startswith("WARNING|") and any(m in ln for m in _MARKERS)]


def _errors(project: Path) -> list[str]:
    return [line for line in _log_lines(project) if line.startswith(("ERROR|", "CRITICAL|"))]


def _records(project: Path) -> list[dict[str, object]]:
    document = project / _DOCS[0]
    loaded = json.loads(document.read_text(encoding="utf-8")) if document.is_file() else []
    return [item for item in loaded if item.get("record_type") == "result"]


def _record(project: Path, nodeid: str) -> dict[str, object] | None:
    return next((item for item in _records(project) if item.get("nodeid") == nodeid), None)


def _written(project: Path) -> list[Path]:
    found = [project / name for name in (*_DOCS, _LOG)]
    return [p for p in found + sorted((project / _ARTIFACTS).rglob("*")) if p.is_file()]


def _documented(project: Path) -> object:
    return json.loads((project / _DOCUMENTED).read_text(encoding="utf-8"))


def _leaks(project: Path) -> list[str]:
    return [p.name for p in _written(project) if _SENTINEL in p.read_text(encoding="utf-8")]


def _run(pytester: pytest.Pytester, plant: str, control: pytest.RunResult) -> pytest.RunResult:
    """Forget the last run's outputs, plant ``plant``'s fault, and compare with the control."""
    for name in (*_DOCS, _LOG, _DOCUMENTED):
        (pytester.path / name).unlink(missing_ok=True)
    shutil.rmtree(pytester.path / _ARTIFACTS, ignore_errors=True)
    if plant == "artifacts":
        (pytester.path / _ARTIFACTS).write_text("not a directory\n", encoding="utf-8")
    pytester.makeconftest(_conftest_source(plant))
    injected = pytester.runpytest_subprocess("-q")
    assert injected.ret == control.ret and injected.parseoutcomes() == control.parseoutcomes()
    return injected


def _baseline(pytester: pytest.Pytester, code: int, **modules: str) -> pytest.RunResult:
    """Run the project once with no fault planted, asserting the exit code and a clean log."""
    pytester.makeconftest(_conftest_source(None))
    pytester.makepyfile(**modules)
    control = pytester.runpytest_subprocess("-q")
    assert control.ret == code and _plumbing(pytester.path) == [], f"control run exit {code}"
    return control


@pytest.mark.parametrize(("module", "function", "body", "code", "outcome"), _ARTIFACT_CASES)
def test_an_unwritable_artifacts_directory_costs_one_warning(
    pytester: pytest.Pytester, module: str, function: str, body: str, code: int, outcome: str
) -> None:
    control = _baseline(pytester, code, **{module: body})
    assert {item["artifacts_disabled"] for item in _records(pytester.path)} == {False}
    _run(pytester, "artifacts", control)
    assert _errors(pytester.path) == [], "a capture fault may never be logged above WARNING"
    fired = _plumbing(pytester.path)
    assert len(fired) == 1 and "artifact capture disabled" in fired[0], "AAP 0.4.7: one warning"
    record = _record(pytester.path, f"{module}.py::{function}")
    assert record is not None and record["outcome"] == outcome and record["artifacts"] == []
    assert bool(record["failures"]) is (outcome == "failed")
    records = _records(pytester.path)
    assert {item["artifacts_disabled"] for item in records} == {True}
    assert all(f["traceback_path"] is None for item in records for f in item["failures"])


def test_failing_redactor_does_not_change_a_passing_test(pytester: pytest.Pytester) -> None:
    pytester.makefile(".toml", foundry=_SECRET)
    control = _baseline(pytester, 1, test_probe=_PASSING, test_failing=_FAILING)
    before = _record(pytester.path, "test_failing.py::test_fails")
    assert before is not None and "***" in str(before["failures"])
    assert _leaks(pytester.path) == [], "the sentinel must be a real leak channel to prove anything"
    _run(pytester, "redactor", control)
    assert _errors(pytester.path) == [], "a redaction fault may never be logged above WARNING"
    assert any("error=RuntimeError" in line for line in _plumbing(pytester.path))
    assert _leaks(pytester.path) == [], "a redaction fault must not become a leak"
    passing = _record(pytester.path, "test_probe.py::test_ok") or {}
    failing = _record(pytester.path, "test_failing.py::test_fails") or {}
    assert (passing.get("outcome"), failing.get("outcome")) == ("passed", "failed")
    assert "***" in str(failing["failures"]), "the fault degrades a message, never an outcome"


def test_html_render_failure_uses_the_documented_fallback(pytester: pytest.Pytester) -> None:
    control = _baseline(pytester, 0, test_probe=_PASSING)
    assert all((pytester.path / name).is_file() for name in _DOCS)
    _run(pytester, "html", control)
    assert _errors(pytester.path) == [], "a rendering fault may never be logged above WARNING"
    assert any("report.html" in line for line in _plumbing(pytester.path))
    # AAP 0.4.7: report.html is skipped and only results.json is written; junit.xml is not produced.
    assert [name for name in _DOCS if (pytester.path / name).exists()] == [_DOCS[0]]
    assert {record["outcome"] for record in _records(pytester.path)} == {"passed"}


def test_the_published_artifacts_members_never_fail_a_test(pytester: pytest.Pytester) -> None:
    control = _baseline(pytester, 0, test_documents=_DOCUMENTING)
    assert _documented(pytester.path) == {"manifest": 1, "written": True}
    _run(pytester, "artifacts", control)
    assert len(_plumbing(pytester.path)) == 1
    assert _documented(pytester.path) == {"manifest": 0, "written": False}
    rows = [(r["artifacts_disabled"], r["artifacts"]) for r in _records(pytester.path)]
    assert rows == [(True, [])], rows


def test_a_write_path_fault_does_not_mask_a_failing_test(pytester: pytest.Pytester) -> None:
    control = _baseline(pytester, 1, test_failing=_FAILING)
    assert control.parseoutcomes() == {"failed": 1}
    assert {"traceback.txt", "manifest.json"} <= {path.name for path in _written(pytester.path)}
    _run(pytester, "artifact_write", control)
    assert _errors(pytester.path) == [], "a write-path fault may never be logged above WARNING"
    assert any("error=RuntimeError" in line for line in _plumbing(pytester.path))
    after = {path.name for path in _written(pytester.path)}
    assert "traceback.txt" in after and "manifest.json" not in after, f"raise hit finalize: {after}"
    record = _record(pytester.path, "test_failing.py::test_fails")
    assert record is not None and record["outcome"] == "failed" and record["failures"], record


def test_a_terminal_fault_does_not_change_a_passing_test(pytester: pytest.Pytester) -> None:
    control = _baseline(pytester, 0, test_probe=_PASSING)
    assert "foundry: profile=default" in control.stdout.str() + control.stderr.str()
    injected = _run(pytester, "terminal", control)
    assert _errors(pytester.path) == [], "a terminal fault may never be logged above WARNING"
    terminal = injected.stdout.str() + injected.stderr.str()
    assert "INTERNALERROR" not in terminal and "Traceback" not in terminal
    fired = _plumbing(pytester.path)
    assert len(fired) == 1 and "activation banner" in fired[0], fired
    assert (_record(pytester.path, "test_probe.py::test_ok") or {}).get("outcome") == "passed"


def test_a_terminal_sink_install_fault_does_not_change_a_passing_test(
    pytester: pytest.Pytester,
) -> None:
    control = _baseline(pytester, 0, test_probe=_PASSING)
    assert all((pytester.path / name).is_file() for name in _DOCS)
    injected = _run(pytester, "terminal_sink", control)
    assert _errors(pytester.path) == [], "an install fault may never be logged above WARNING"
    terminal = injected.stdout.str() + injected.stderr.str()
    assert "INTERNALERROR" not in terminal, "a sink that cannot install may not end the session"
    fired = _plumbing(pytester.path)
    assert len(fired) == 1 and "terminal redactor" in fired[0], fired
    assert all((pytester.path / name).is_file() for name in _DOCS)
    assert (_record(pytester.path, "test_probe.py::test_ok") or {}).get("outcome") == "passed"
