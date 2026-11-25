"""Failure isolation of Foundry's own plumbing, driven through real pytest sessions.

AAP 0.1.2 is the rule this module executes: a failure in Foundry's own capture, redaction or
reporting is one ``WARNING`` on the ``foundry`` logger and changes neither a test's outcome nor
the run's exit code -- ``report.html``'s documented fallback apart, and even that leaves the
exit code alone. The two AAP 0.4.7 rows exercised here are "artifacts directory not writable",
which warns once, lets the run continue and puts no artifact path on the record, and "HTML
template render failure", which skips ``report.html``, keeps ``results.json``, produces no
``junit.xml``, logs one warning and leaves the exit code alone. AAP 0.14.3 names this module as
that constraint's instrument, and it is the one place AAP 0.14.5's ban on mocking the plugin,
the CLI and the renderer admits a fault.

**The faults and where they are planted.** Each goes into a project ``pytester`` writes, through
an attribute looked up when the call is made and never a reference captured earlier:

* capture -- a regular file named ``artifacts`` where the run's artifact directory belongs, so
  ``ArtifactWriter.ensure_run_dir`` raises ``NotADirectoryError`` from its own ``mkdir`` and
  takes its documented ``_disable`` path;
* redaction -- ``Redactor.text`` replaced at class level, the entry point every sink scrubs
  through;
* the report -- a syntactically invalid Jinja2 template that ``foundry.reporting.html``'s
  ``template_path`` is pointed at, so the real ``_render_document`` raises
  ``TemplateSyntaxError`` inside its guarded region;
* and nothing at all for the ``foundry_log_capture`` case, which observes the public fixture.

Each patch goes into the *child's* ``conftest.py`` through ``pytester.makeconftest`` and every
run is ``pytester.runpytest_subprocess``: the handler recording Foundry's log lines has to live
where the records are emitted, and a renderer left broken here would fail every later test in
this process.

**What "unchanged" means.** Each case runs one project twice -- a control run, then the run with
the fault planted -- and asserts the exit code and the outcome counts are equal, that the
injected run reported a plumbing ``WARNING`` and no ``ERROR``, and that the fault's own
observable holds. The counts come from pytest's terminal summary rather than ``results.json``,
so a report Foundry could not write cannot hide a changed outcome.

**Deviations this tree forces.** Patching ``ArtifactWriter.write`` cannot fire, because no
run-path call site writes per-test artifacts yet -- a control run records ``artifacts: []`` --
so it would leave this module verifying nothing; patching ``ensure_run_dir`` to raise is the
mirror-image mistake, because the real method catches ``OSError`` itself, while a directory it
cannot create is exactly the fault AAP 0.4.7 describes. The redaction case runs a failing test
beside the passing one, because a passing test redacts nothing: Foundry scrubs failure messages,
artifact paths and run-error text, which is where a secret could reach disk. Nothing here
sleeps, reads a clock or needs order.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest

REACHES: tuple[str, ...] = ("fixture:foundry_log_capture",)
"""The public surface this module drives through a real entry point (AAP 0.14.1).

``tests/plugin/test_reachability.py`` parses this declaration out of the module and counts
``foundry_log_capture`` as reached, which the last case makes true by logging inside a child run.
"""

_SENTINEL: Final[str] = "SENTINEL-ISOLATION"
"""A credential-shaped value, configured as a secret header and quoted by a failing test.

It must be absent from everything Foundry writes in a run where the redactor works, which is what
makes the control run an assertion that the sentinel is a real leak channel, and absent again in the
run where the redactor raises, which is what makes a redaction fault unable to become a leak.
"""

_LOG_FILENAME: Final[str] = "foundry.log"
"""The file the child's handler writes one ``LEVELNAME|logger|message`` line per record into."""

_RESULTS_FILENAME: Final[str] = "results.json"
_REPORT_FILENAME: Final[str] = "report.html"
_JUNIT_FILENAME: Final[str] = "junit.xml"
_ARTIFACTS_DIRNAME: Final[str] = "artifacts"
_FACTS_FILENAME: Final[str] = "capture.json"

_OUTCOME_KEYS: Final[frozenset[str]] = frozenset(
    {"passed", "failed", "error", "errors", "skipped", "xfailed", "xpassed", "deselected"}
)
"""The outcome counters two runs are compared on, taken from pytest's own terminal summary."""

_PLUMBING_MARKERS: Final[tuple[str, ...]] = ("plumbing_failure", "html_fallback")
"""The two event names ``foundry.obs.events`` writes a fault in Foundry's plumbing under."""

_FAILURE_LEVELS: Final[tuple[str, ...]] = ("ERROR|", "CRITICAL|")
"""The levels library code may never log at, so a run reporting one has broken the rule outright."""

_PASSING_TEST: Final[str] = '''"""A test that passes without touching anything Foundry captures."""


def test_ok() -> None:
    """Pass, so the only fault in the run is Foundry's own plumbing."""
    assert True
'''

_FAILING_TEST: Final[str] = f'''"""A test that fails while quoting a credential-shaped value."""

SENTINEL: str = "{_SENTINEL}"


def test_fails() -> None:
    """Fail with a header line in the message, which every sink must redact before writing."""
    assert f"x-api-key: {{SENTINEL}}" == "a different key"
'''

_SECRET_CONFIG: Final[str] = f"""[http]
headers = {{ x-api-key = "{_SENTINEL}" }}
"""

_CAPTURE_TEST: Final[
    str
] = '''"""Read the public ``foundry_log_capture`` ring from inside a real test."""

import json
import logging
import pathlib

MESSAGE = "foundry log capture fixture probe"
FACTS = pathlib.Path(__file__).with_name("capture.json")


def test_the_ring_returns_the_record_it_just_logged(foundry_log_capture: object) -> None:
    """A WARNING logged on the ``foundry`` logger is the ring's newest line, and is retained."""
    logging.getLogger("foundry").warning(MESSAGE)
    tail = foundry_log_capture.tail(1)
    facts = {"records": len(foundry_log_capture.records), "tail": list(tail)}
    FACTS.write_text(json.dumps(facts, sort_keys=True), encoding="utf-8")
    assert len(tail) == 1
    assert MESSAGE in tail[0]
    assert len(foundry_log_capture.records) >= 1
'''

_CONFTEST_HEAD: Final[
    str
] = '''"""Child project: record every foundry log line, then plant the fault under test."""

import logging
import pathlib

import pytest

LOG_PATH = pathlib.Path(__file__).with_name("foundry.log")


class _FileHandler(logging.Handler):
    """Write one "LEVELNAME|logger|message" line per record, so the outer test can read them.

    The line carries the level, the logger name and the message and nothing else: a timestamp would
    make two runs of one project differ.
    """

    def __init__(self, path: pathlib.Path) -> None:
        super().__init__(logging.DEBUG)
        self._path = path

    def emit(self, record: logging.LogRecord) -> None:
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(f"{record.levelname}|{record.name}|{record.getMessage()}\\n")


def pytest_configure(config: pytest.Config) -> None:
    """Attach the handler, then plant the fault, before foundry.plugin's trylast configure runs."""
    logger = logging.getLogger("foundry")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(_FileHandler(LOG_PATH))
'''

_INJECT_REDACTOR: Final[str] = '''    from foundry.obs.redaction import Redactor

    def _raising_text(self: Redactor, text: str, *, limit: int | None = None) -> str:
        """Stand in for Redactor.text and raise the fault this case injects."""
        raise RuntimeError("injected redaction failure")

    Redactor.text = _raising_text
'''

_INJECT_HTML: Final[str] = """    import foundry.reporting.html as html_module

    broken = LOG_PATH.with_name("broken.html.j2")
    broken.write_text("{% for row in %}{% endfor %}", encoding="utf-8")
    html_module.template_path = lambda: broken
"""

_INJECTIONS: Final[Mapping[str, str]] = {
    "redactor": _INJECT_REDACTOR,
    "html": _INJECT_HTML,
}
"""The faults this module plants, by the name each case asks for."""


@dataclass(frozen=True, slots=True)
class _Observation:
    """What one child run reported: its exit code, its pytest outcomes, its Foundry log lines."""

    returncode: int
    outcomes: dict[str, int]
    warnings: tuple[str, ...]
    errors: tuple[str, ...]

    def plumbing(self) -> tuple[str, ...]:
        """Return the records Foundry's own fault emitters wrote, in the order they were logged."""
        return tuple(
            line for line in self.warnings if any(marker in line for marker in _PLUMBING_MARKERS)
        )


def _conftest_source(injection: str | None) -> str:
    """Return the child's ``conftest.py``: the log handler, plus the fault ``injection`` names.

    Written into the child rather than applied in this process, because the handler has to be
    attached where the records are emitted and because a patch left behind here would outlive the
    test that made it.
    """
    return _CONFTEST_HEAD + ("" if injection is None else _INJECTIONS[injection])


def _observe(project: Path, result: pytest.RunResult) -> _Observation:
    """Return what the child run ``result`` reported about the project in ``project``."""
    lines = _log_lines(project)
    return _Observation(
        returncode=result.ret,
        outcomes={
            name: count
            for name, count in sorted(result.parseoutcomes().items())
            if name in _OUTCOME_KEYS
        },
        warnings=tuple(line for line in lines if line.startswith("WARNING|")),
        errors=tuple(line for line in lines if line.startswith(_FAILURE_LEVELS)),
    )


def _log_lines(project: Path) -> tuple[str, ...]:
    """Return the Foundry records the child logged, or ``()`` when it logged nothing."""
    path = project / _LOG_FILENAME
    if not path.is_file():
        return ()
    return tuple(path.read_text(encoding="utf-8").splitlines())


def _records(project: Path) -> list[Mapping[str, object]]:
    """Return the result records the run wrote, in document order, or ``[]`` when it wrote none."""
    path = project / _RESULTS_FILENAME
    if not path.is_file():
        return []
    document = json.loads(path.read_text(encoding="utf-8"))
    return [entry for entry in document if entry.get("record_type") == "result"]


def _record_for(project: Path, nodeid: str) -> Mapping[str, object] | None:
    """Return the result record the run wrote for ``nodeid``, or ``None`` when it wrote none."""
    for record in _records(project):
        if record.get("nodeid") == nodeid:
            return record
    return None


def _forget_outputs(project: Path) -> None:
    """Delete what the previous child run wrote, so the next run's outputs are only its own."""
    for name in (_RESULTS_FILENAME, _REPORT_FILENAME, _JUNIT_FILENAME, _LOG_FILENAME):
        (project / name).unlink(missing_ok=True)
    directory = project / _ARTIFACTS_DIRNAME
    if directory.is_dir():
        shutil.rmtree(directory)


def _foundry_files(project: Path) -> list[Path]:
    """Return every file Foundry itself wrote in ``project``: its documents and its artifacts."""
    candidates = [
        project / name
        for name in (_RESULTS_FILENAME, _REPORT_FILENAME, _JUNIT_FILENAME, _LOG_FILENAME)
    ]
    written = [path for path in candidates if path.is_file()]
    artifacts = project / _ARTIFACTS_DIRNAME
    if artifacts.is_dir():
        written.extend(sorted(path for path in artifacts.rglob("*") if path.is_file()))
    return written


def _sentinel_leaks(project: Path) -> list[str]:
    """Return the files Foundry wrote that still quote the configured secret, as relative paths."""
    leaks: list[str] = []
    for path in _foundry_files(project):
        if _SENTINEL in path.read_text(encoding="utf-8", errors="replace"):
            leaks.append(path.relative_to(project).as_posix())
    return leaks


def test_failing_artifact_writer_does_not_change_a_passing_test(pytester: pytest.Pytester) -> None:
    """An artifact directory that cannot be created costs one warning, not the outcome."""
    project = pytester.path
    pytester.makeconftest(_conftest_source(None))
    pytester.makepyfile(test_probe=_PASSING_TEST)
    control = _observe(project, pytester.runpytest_subprocess("-q"))
    assert control.returncode == 0
    assert control.plumbing() == ()
    assert (project / _ARTIFACTS_DIRNAME).is_dir(), "the control run creates its artifact directory"

    _forget_outputs(project)
    (project / _ARTIFACTS_DIRNAME).write_text("not a directory\n", encoding="utf-8")
    injected = _observe(project, pytester.runpytest_subprocess("-q"))

    assert injected.returncode == control.returncode
    assert injected.outcomes == control.outcomes
    assert injected.errors == (), "a capture fault may never be logged above WARNING"
    fired = injected.plumbing()
    assert len(fired) == 1, "AAP 0.4.7: an unwritable artifact directory is one warning"
    assert "artifact capture disabled" in fired[0]
    records = _records(project)
    assert [record["outcome"] for record in records] == ["passed"]
    # AAP 0.4.7 keeps the disabled flag in manifest.json, which a capture that never ran does not
    # write; the result record's own promise is only that no artifact path reaches it.
    assert records[0]["artifacts"] == []


def test_failing_redactor_does_not_change_a_passing_test(pytester: pytest.Pytester) -> None:
    """A redactor that raises costs a warning and a record, never an outcome and never a secret."""
    project = pytester.path
    pytester.makeconftest(_conftest_source(None))
    pytester.makepyfile(test_probe=_PASSING_TEST, test_failing=_FAILING_TEST)
    pytester.makefile(".toml", foundry=_SECRET_CONFIG)

    control = _observe(project, pytester.runpytest_subprocess("-q"))
    assert control.returncode == 1
    assert control.plumbing() == ()
    failing = _record_for(project, "test_failing.py::test_fails")
    assert failing is not None
    assert "***" in str(failing["failures"]), "the working redactor scrubs the header line"
    assert _sentinel_leaks(project) == [], (
        "the sentinel must be a real leak channel for the assertion below to mean anything"
    )

    _forget_outputs(project)
    pytester.makeconftest(_conftest_source("redactor"))
    injected = _observe(project, pytester.runpytest_subprocess("-q"))

    assert injected.returncode == control.returncode
    assert injected.outcomes == control.outcomes
    assert injected.errors == (), "a redaction fault may never be logged above WARNING"
    fired = injected.plumbing()
    assert fired, "the planted fault produced no WARNING: the injection missed its target"
    assert any("error=RuntimeError" in line for line in fired)
    assert _sentinel_leaks(project) == [], (
        "a redaction fault must not become a leak: no file Foundry wrote may quote the secret"
    )
    passing = _record_for(project, "test_probe.py::test_ok")
    assert passing is not None, "a plumbing fault may not cost an unaffected test its record"
    assert passing["outcome"] == "passed"
    failing_after = _record_for(project, "test_failing.py::test_fails")
    assert failing_after is not None, "a redaction fault may not cost the failing test its record"
    assert failing_after["outcome"] == "failed", "the fault degrades a message, never an outcome"
    assert "***" in str(failing_after["failures"])


def test_html_render_failure_uses_the_documented_fallback(pytester: pytest.Pytester) -> None:
    """A template that cannot render skips report.html and junit.xml and keeps results.json."""
    project = pytester.path
    pytester.makeconftest(_conftest_source(None))
    pytester.makepyfile(test_probe=_PASSING_TEST)
    control = _observe(project, pytester.runpytest_subprocess("-q"))
    assert control.returncode == 0
    assert control.plumbing() == ()
    for name in (_RESULTS_FILENAME, _REPORT_FILENAME, _JUNIT_FILENAME):
        assert (project / name).is_file(), f"the control run writes {name}"

    _forget_outputs(project)
    pytester.makeconftest(_conftest_source("html"))
    injected = _observe(project, pytester.runpytest_subprocess("-q"))

    assert injected.returncode == control.returncode
    assert injected.outcomes == control.outcomes
    assert injected.errors == (), "a rendering fault may never be logged above WARNING"
    fired = injected.plumbing()
    assert fired, "the planted fault produced no WARNING: the injection missed its target"
    assert any(_REPORT_FILENAME in line for line in fired)
    assert (project / _RESULTS_FILENAME).is_file()
    assert not (project / _REPORT_FILENAME).exists()
    assert not (project / _JUNIT_FILENAME).exists(), (
        "AAP 0.4.7: 'report.html is skipped and only results.json is written, junit.xml is not "
        "produced'; a writer that emitted junit.xml around a broken report contradicts that row"
    )
    assert [record["outcome"] for record in _records(project)] == ["passed"]


def test_failing_artifact_writer_does_not_mask_a_failing_test(pytester: pytest.Pytester) -> None:
    """The same capture fault on a failing run leaves the failure, and its code, standing."""
    project = pytester.path
    pytester.makeconftest(_conftest_source(None))
    pytester.makepyfile(test_failing=_FAILING_TEST)
    control = _observe(project, pytester.runpytest_subprocess("-q"))
    assert control.returncode == 1
    assert control.outcomes == {"failed": 1}

    _forget_outputs(project)
    (project / _ARTIFACTS_DIRNAME).write_text("not a directory\n", encoding="utf-8")
    injected = _observe(project, pytester.runpytest_subprocess("-q"))

    assert injected.returncode == control.returncode
    assert injected.outcomes == control.outcomes
    assert injected.errors == ()
    assert injected.plumbing(), "the planted fault produced no WARNING: the injection missed"
    record = _record_for(project, "test_failing.py::test_fails")
    assert record is not None
    assert record["outcome"] == "failed"
    assert record["failures"], "the failure the run recorded keeps its phase, message and traceback"


def test_foundry_log_capture_fixture_reports_real_records(pytester: pytest.Pytester) -> None:
    """The public fixture answers a real test with the record that test has just logged."""
    project = pytester.path
    pytester.makeconftest(_conftest_source(None))
    pytester.makepyfile(test_capture=_CAPTURE_TEST)
    observation = _observe(project, pytester.runpytest_subprocess("-q"))
    assert observation.returncode == 0
    assert observation.outcomes == {"passed": 1}
    facts = json.loads((project / _FACTS_FILENAME).read_text(encoding="utf-8"))
    assert len(facts["tail"]) == 1
    assert facts["tail"][0].startswith("WARNING")
    assert "foundry log capture fixture probe" in facts["tail"][0]
    assert facts["records"] >= 1
