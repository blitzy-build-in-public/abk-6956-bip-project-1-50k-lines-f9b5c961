"""Quarantine driven through pytest itself: the two modes, the record and the three surfaces.

AAP 0.10.1 places this module in ``tests/plugin/`` ("``report`` and ``strict`` modes and the report
sections"), so every claim below is made against a real session that ``pytester`` started, read back
from the files that session wrote -- never against a hand-built record, and never by mocking the
plugin, the quarantine registry or a renderer. That is the acceptance evidence AAP 0.14.1's
criterion 6 asks for: a quarantined failure leaves the run green in ``report`` mode (exit 0) and
counts against the run in ``strict`` mode (exit 1), while a marked test that passes is a success
carrying a note that recommends removing the marker (assumption A11).

**Why each surface.** ``results.json`` carries the record's ``outcome``, its ``attempts`` and its
``quarantine.state`` (AAP 0.4.6, 0.7.2). ``junit.xml`` carries the two elements a CI system reads:
the ``<skipped>`` whose ``message`` attribute is exactly ``foundry: quarantined failure``, and the
``<system-out>`` note of a marked test that passed. ``report.html`` carries the Quarantined section,
located by its heading anchor rather than by the bare word "quarantine", which the template's own
comment also carries. All three are read from ``pytester.path``: the plugin writes them to the
invocation directory, which is that path because ``pytester`` runs the session from there
(assumption A5).

**The strict-mode spelling.** The brief for this module writes the override as
"``yet--foundry-execution.quarantine_mode strict``". AAP 0.4.4's generic override is
``--foundry-<dotted.key>``, so the option asserted here is ``--foundry-execution.quarantine_mode``.

**The removal note, as observed.** ``foundry: quarantined test passed; remove the
foundry_quarantine marker``. Two properties of that text are asserted rather than the sentence: it
names the marker, and it asks for the marker's removal. A rewording keeps these tests honest, while
a note that merely reports the pass fails them.

**Determinism** (AAP 0.14.3): no sleep, no wall-clock reading, every document read is compared by
key rather than by position, and each test drives its own project in a directory ``pytester`` owns.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Final
from xml.etree import ElementTree

import pytest

REACHES: tuple[str, ...] = ()
"""No enumerated public surface is reached here.

``tests/plugin/test_reachability.py`` requires a real-entry-point test per public fixture, CLI
subcommand, data loader and report format. Quarantine is none of those: the marker is registered by
the plugin and the outcomes are result-record values, both of which this module exercises through
pytest itself, so the reachability ledger gains nothing it could name.
"""

# --- The contract values AAP 0.4.2, 0.4.4, 0.4.6 and 0.4.7 fix -------------

_MARKER: Final[str] = "foundry_quarantine"
"""The marker AAP 0.4.2 registers and AAP 0.4.9 freezes, written as a consumer writes it."""

_SKIP_MESSAGE: Final[str] = "foundry: quarantined failure"
"""The ``<skipped>`` ``message`` of a quarantined failure in ``junit.xml`` (AAP 0.4.6)."""

_NOTE_ACTION: Final[str] = "remove"
"""The word the removal note has to carry, so the note recommends an action rather than a state."""

_STRICT_OPTION: Final[str] = "--foundry-execution.quarantine_mode"
"""The generic override AAP 0.4.4 publishes for ``execution.quarantine_mode``."""

_STRICT_TOML: Final[str] = '[execution]\nquarantine_mode = "strict"\n'
"""The same key set through the file layer, so the two layers are proved to reach one registry."""

_STRICT_MARKERS_INI: Final[str] = "[pytest]\naddopts = -ra --strict-markers\n"
"""An ini that turns an unregistered marker into a hard error, which is what AAP 0.4.2 needs."""

_QUARANTINE_HEADING: Final[str] = 'id="quarantined-heading"'
"""The report's Quarantined section anchor: unique in the document, unlike the word quarantine."""

# --- The generated projects ------------------------------------------------
# Each is a whole project: a module and nothing else, so a run's configuration comes from the
# defaults (or from the one ``foundry.toml`` a case adds) and the quarantine mode is the only
# difference between the runs that must disagree about the exit code.

_QUARANTINED_FAILURE: Final[str] = '''"""A quarantined test that fails on every attempt."""

import pytest


@pytest.mark.foundry_quarantine
def test_known_flake() -> None:
    """Fail unconditionally, so the quarantine mode alone decides the exit code."""
    assert False, "quarantine sentinel"
'''

_QUARANTINED_PASS: Final[str] = '''"""A quarantined test that passes, so the marker is stale."""

import pytest


@pytest.mark.foundry_quarantine
def test_recovered() -> None:
    """Pass unconditionally, so the marker is the only thing to report."""
    assert True
'''

_PLAIN_FAILURE: Final[str] = '''"""A failure no marker covers: the control for the green run."""

import pytest


def test_plain_failure() -> None:
    """Fail with no quarantine marker, so the run has to fail."""
    assert False, "plain sentinel"
'''

_MIXED_FAILURES: Final[str] = '''"""One plain failure beside one quarantined failure."""

import pytest


def test_plain_failure() -> None:
    """Fail with no marker, so this failure fails the run whatever the mode is."""
    assert False, "plain sentinel"


@pytest.mark.foundry_quarantine
def test_known_flake() -> None:
    """Fail with the marker, so this failure is downgraded in report mode alone."""
    assert False, "quarantine sentinel"
'''

# --- Reading what a run wrote ----------------------------------------------


def _nodeid(stem: str, test: str) -> str:
    """Return the node id of ``test`` in ``<stem>.py``, as pytest reports it under the rootdir."""
    return f"{stem}.py::{test}"


def _read(pytester: pytest.Pytester, name: str) -> str:
    """Return the text of one report file the run wrote, naming the directory when it is absent.

    A missing document is the interesting failure, so the assertion says what the run did leave
    behind instead of raising a bare ``FileNotFoundError``.
    """
    path = pytester.path / name
    assert path.is_file(), (
        f"{name} was not written to {pytester.path}; the directory holds "
        f"{sorted(entry.name for entry in pytester.path.iterdir())}"
    )
    return path.read_text(encoding="utf-8")


def _records(pytester: pytest.Pytester) -> dict[str, dict[str, Any]]:
    """Return the run's result records, keyed by node id, from the ``results.json`` it wrote.

    AAP 0.4.6 makes the document a JSON array discriminated by ``record_type``; a run of these
    projects records results only, so an element that is anything else is a contract breach rather
    than something to skip over.
    """
    document = json.loads(_read(pytester, "results.json"))
    assert isinstance(document, list), (
        f"results.json is not a JSON array but {type(document).__name__}"
    )
    records: dict[str, dict[str, Any]] = {}
    for element in document:
        assert isinstance(element, dict), f"results.json holds a non-object element: {element!r}"
        assert element.get("record_type") == "result", f"unexpected element kind: {element!r}"
        records[str(element["nodeid"])] = element
    return records


def _only_record(pytester: pytest.Pytester) -> dict[str, Any]:
    """Return the run's single result record, refusing a document that holds more or fewer."""
    records = _records(pytester)
    assert len(records) == 1, f"expected one result record, got {sorted(records)}"
    return next(iter(records.values()))


def _assert_record_invariants(record: Mapping[str, Any]) -> None:
    """Assert the record invariants AAP 0.7.2 fixes, which every outcome has to keep.

    ``attempts`` counts the first attempt, a duration is never negative, markers are sorted and
    de-duplicated, and ``quarantine.state`` agrees with ``outcome`` -- the last one is what stops
    the report's Quarantined section and the exit code from disagreeing about a test.
    """
    assert record["attempts"] >= 1
    assert record["duration_ms"] >= 0
    markers = record["markers"]
    assert markers == sorted(set(markers))
    expected = {"quarantined_failed": "failed", "quarantine_passed": "passed"}
    assert record["quarantine"] == {"state": expected.get(record["outcome"], "none")}


def _testcase(pytester: pytest.Pytester, test: str) -> ElementTree.Element:
    """Return the ``<testcase>`` of one test from ``junit.xml``, located by its JUnit ``name``."""
    root = ElementTree.parse(pytester.path / "junit.xml").getroot()
    names = [case.get("name") or "" for case in root.iter("testcase")]
    for case in root.iter("testcase"):
        if case.get("name") == test:
            return case
    raise AssertionError(f"junit.xml carries no <testcase> named {test!r}: {names}")


def _skipped_messages(case: ElementTree.Element) -> list[str]:
    """Return the ``message`` attribute of every ``<skipped>`` child of ``case``, in order."""
    return [element.get("message", "") for element in case.iter("skipped")]


def _system_out(case: ElementTree.Element) -> str:
    """Return the text of ``case``'s ``<system-out>`` note, or the empty string when it has none."""
    return "".join(element.text or "" for element in case.iter("system-out"))


def _quarantined_section(pytester: pytest.Pytester) -> str:
    """Return the report's Quarantined section, from its heading anchor to the closing tag.

    Anchoring on :data:`_QUARANTINE_HEADING` is what makes this the section rather than the
    template's own comment about it, which mentions quarantine first in the file.
    """
    text = _read(pytester, "report.html")
    start = text.find(_QUARANTINE_HEADING)
    if start < 0:
        raise AssertionError("report.html carries no Quarantined section (AAP 0.4.6)")
    end = text.find("</section>", start)
    return text[start:end]


def _has_quarantined_section(pytester: pytest.Pytester) -> bool:
    """Return whether the report carries a Quarantined section at all."""
    return _QUARANTINE_HEADING in _read(pytester, "report.html")


# --- The two modes (AAP 0.4.5, 0.4.7) --------------------------------------


def test_report_mode_keeps_the_run_green(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    """The default mode records a marked failure, lists it, and still exits 0.

    Criterion 6's first half: a known-flaky failure stays visible without failing the build. The run
    reports one failed test while the process exits 0, so the outcomes are asserted beside the exit
    code -- a green exit earned by a passing test would prove nothing about quarantine.
    """
    pytester.makepyfile(test_flake=_QUARANTINED_FAILURE)

    result = pytester.runpytest()

    assert result.ret == 0
    result.assert_outcomes(failed=1)
    nodeid = _nodeid("test_flake", "test_known_flake")
    record = _only_record(pytester)
    assert record["nodeid"] == nodeid
    assert record["outcome"] == "quarantined_failed"
    assert record["quarantine"] == {"state": "failed"}
    assert record["attempts"] == 1
    # The failure itself is kept: the report has something to show a person even though CI is green.
    assert [failure["phase"] for failure in record["failures"]] == ["call"]
    _assert_record_invariants(record)

    assert _skipped_messages(_testcase(pytester, "test_known_flake")) == [_SKIP_MESSAGE]

    section = _quarantined_section(pytester)
    assert nodeid in section
    assert "quarantined_failed" in section


def test_strict_mode_fails_the_run(pytester: pytest.Pytester, clean_foundry_env: None) -> None:
    """The same record counts against the run once the mode is ``strict`` through the CLI layer."""
    pytester.makepyfile(test_flake=_QUARANTINED_FAILURE)

    result = pytester.runpytest(_STRICT_OPTION, "strict")

    assert result.ret == 1
    result.assert_outcomes(failed=1)
    record = _only_record(pytester)
    assert record["outcome"] == "quarantined_failed"
    assert record["quarantine"] == {"state": "failed"}
    _assert_record_invariants(record)
    # AAP 0.4.6 maps an outcome and not a mode, so the CI-visible element is identical either way.
    assert _skipped_messages(_testcase(pytester, "test_known_flake")) == [_SKIP_MESSAGE]


def test_strict_mode_from_the_file_layer(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    """``foundry.toml`` sets the same key through the file layer, and reaches the same registry.

    No option is passed on the command line, so a run that exits 1 here proves the value travelled
    from the file into the resolved configuration and on into the quarantine decision.
    """
    pytester.makepyfile(test_flake=_QUARANTINED_FAILURE)
    pytester.makefile(".toml", foundry=_STRICT_TOML)
    assert (pytester.path / "foundry.toml").is_file()

    result = pytester.runpytest()

    assert result.ret == 1
    record = _only_record(pytester)
    assert record["outcome"] == "quarantined_failed"
    assert record["quarantine"] == {"state": "failed"}
    _assert_record_invariants(record)


# --- The removal note (assumption A11) -------------------------------------


def test_passing_quarantined_test_is_a_success_with_a_removal_note(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    """A marked test that passes succeeds, and every surface recommends dropping the marker.

    The note is asserted on its two semantic elements -- the marker's name and the word asking for
    its removal -- so a rewording survives while a note that only reports the pass does not.
    """
    pytester.makepyfile(test_recovered=_QUARANTINED_PASS)

    result = pytester.runpytest()

    assert result.ret == 0
    result.assert_outcomes(passed=1)
    nodeid = _nodeid("test_recovered", "test_recovered")
    record = _only_record(pytester)
    assert record["nodeid"] == nodeid
    assert record["outcome"] == "quarantine_passed"
    assert record["quarantine"] == {"state": "passed"}
    assert _MARKER in record["markers"]
    _assert_record_invariants(record)

    case = _testcase(pytester, "test_recovered")
    assert _skipped_messages(case) == []
    note = _system_out(case)
    assert _MARKER in note
    assert _NOTE_ACTION in note.lower()

    section = _quarantined_section(pytester).lower()
    assert nodeid.lower() in section
    assert _MARKER in section
    assert _NOTE_ACTION in section


# --- Marker registration (AAP 0.4.2) ---------------------------------------


def test_marker_is_registered_under_strict_markers(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    """The plugin registers the marker, so ``--strict-markers`` leaves it a marker, not an error.

    The inner rootdir is ``pytester.path``, where the outer ``addopts`` do not apply, so the ini is
    written here. The record's outcome shows the marker was honoured rather than merely tolerated.
    """
    pytester.makepyfile(test_flake=_QUARANTINED_FAILURE)
    pytester.makeini(_STRICT_MARKERS_INI)

    result = pytester.runpytest()

    assert result.ret == 0
    assert "unknown mark" not in result.stdout.str().lower()
    record = _only_record(pytester)
    assert _MARKER in record["markers"]
    assert record["outcome"] == "quarantined_failed"


# --- Quarantine never hides another failure --------------------------------


def test_quarantine_does_not_mask_a_real_failure(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    """One unchecked failure beside one marked failure still fails the run, in ``report`` mode.

    Both records keep their own verdict, which is the AAP 0.7.2 consistency rule observed rather
    than asserted about a hand-built record: the plain failure carries no quarantine state and the
    marked one carries ``failed``.
    """
    pytester.makepyfile(test_mixed=_MIXED_FAILURES)

    result = pytester.runpytest()

    assert result.ret == 1
    result.assert_outcomes(failed=2)
    records = _records(pytester)
    plain_nodeid = _nodeid("test_mixed", "test_plain_failure")
    flake_nodeid = _nodeid("test_mixed", "test_known_flake")
    assert set(records) == {plain_nodeid, flake_nodeid}
    plain = records[plain_nodeid]
    flake = records[flake_nodeid]
    assert (plain["outcome"], plain["quarantine"]) == ("failed", {"state": "none"})
    assert (flake["outcome"], flake["quarantine"]) == ("quarantined_failed", {"state": "failed"})
    _assert_record_invariants(plain)
    _assert_record_invariants(flake)


def test_control_unquarantined_failure_fails_the_run(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    """The control for the first case: the same failure with no marker fails the run.

    It also shows the report's Quarantined section is driven by the records rather than printed
    unconditionally, because nothing here is quarantined.
    """
    pytester.makepyfile(test_plain=_PLAIN_FAILURE)

    result = pytester.runpytest()

    assert result.ret == 1
    result.assert_outcomes(failed=1)
    record = _only_record(pytester)
    assert record["nodeid"] == _nodeid("test_plain", "test_plain_failure")
    assert record["outcome"] == "failed"
    assert record["quarantine"] == {"state": "none"}
    assert record["markers"] == []
    _assert_record_invariants(record)
    assert not _has_quarantined_section(pytester)
