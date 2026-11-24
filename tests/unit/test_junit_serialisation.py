"""The XML character guarantee ``foundry.reporting.junit`` owes a CI reader (AAP 0.4.6).

Finding qa04-junit-control-chars: AAP 0.4.6 makes ``junit.xml`` the document any CI system reads,
yet :mod:`xml.etree.ElementTree` escapes only ``&``, ``<``, ``>``, ``"`` and ``'`` and passes every
other code point through as it found it. A failure message, a test id or a run-error message
carrying a character outside XML 1.0's ``Char`` production -- an ANSI escape, NUL, a vertical tab,
a form feed, a backspace, a lone surrogate, ``#xFFFE`` or ``#xFFFF`` -- therefore reached the
document raw, and both parsers rejected the whole file (``not well-formed (invalid token)``): the
run's machine-readable result was destroyed while ``results.json`` escaped the same value
correctly. :func:`foundry.reporting.junit.render` now reduces every record-derived string to the
``Char`` production before serialising it, removing the offending characters rather than
substituting a replacement character that would widen an already-clipped failure message.

These cases pin that reduction in each of the three places the finding reported it, the characters
XML 1.0 does allow (``#x7F`` and the C1 range ``#x80-#x9F``, which must survive), and the fields it
must leave alone: the four fixed properties and five artifact references of AAP 0.4.6, and the
escaping of a ``<script>`` node id that AAP 0.4.7 requires.

Determinism and isolation (AAP 0.14.3): records are built inline from fixed timestamps, so no
assertion reads the wall clock; a written document goes under ``tmp_path``; nothing sleeps and
nothing touches the network.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from xml.dom import minidom
from xml.etree import ElementTree

import pytest

from foundry.reporting import junit
from foundry.reporting.record import (
    ArtifactRecord,
    FailureRecord,
    Outcome,
    QuarantineRecord,
    ResultRecord,
    RunError,
)

# --- Fixed fixtures --------------------------------------------------------
#
# A run id of the shape AAP 0.4.3 publishes, a profile from AAP 0.4.5's four, a node id carrying a
# module path, and two fixed instants, so no assertion here depends on the host's clock.

_RUN_ID = "20250102T030405-abc123"
_PROFILE = "ci"
_STARTED_AT = datetime(2025, 1, 2, 3, 4, 5, 0, tzinfo=UTC)
_FINISHED_AT = datetime(2025, 1, 2, 3, 4, 5, 250_000, tzinfo=UTC)

_MODULE = "tests/test_cart.py"
_NODEID = f"{_MODULE}::test_total"

_ANSI_ESCAPE = "\x1b"
"""The escape a suite run with colour forced puts in a collected message."""

_FORBIDDEN_CHARACTERS: tuple[tuple[str, str], ...] = (
    ("ansi-escape", _ANSI_ESCAPE),
    ("nul", "\x00"),
    ("vertical-tab", "\x0b"),
    ("form-feed", "\x0c"),
    ("backspace", "\x08"),
    ("lone-surrogate", "\ud800"),
    ("noncharacter-fffe", "\ufffe"),
    ("noncharacter-ffff", "\uffff"),
)
"""One label and one character for each class of code point XML 1.0's ``Char`` production excludes.

The lone surrogate is the shape ``os.fsdecode`` gives an undecodable byte in a path. ``#x7F`` is
deliberately absent, and :func:`test_the_characters_xml_allows_survive_unchanged` covers it and the
C1 range.
"""

_LEGAL_CHARACTERS: tuple[tuple[str, str], ...] = (
    ("del", "\x7f"),
    ("c1-open", "\x80"),
    ("c1-close", "\x9f"),
)
"""DEL and the first and last C1 controls: outside ASCII and still legal XML 1.0 content."""

_LOCATIONS: tuple[str, ...] = ("failure-message", "nodeid", "run-error-message")
"""The three record-derived strings the finding observed a forbidden character in."""

_ARTIFACTS: tuple[ArtifactRecord, ...] = tuple(
    ArtifactRecord(kind=kind, path=f"tests__test_cart.py__test_total/{name}", bytes=len(name))
    for kind, name in (
        ("request", "request.json"),
        ("response", "response.json"),
        ("stdout", "stdout.log"),
        ("stderr", "stderr.log"),
        ("traceback", "traceback.txt"),
    )
)
"""One artifact of each kind AAP 0.4.6 references, in the order the document lists them."""

_EXPECTED_PROPERTIES: tuple[tuple[str, str], ...] = (
    ("foundry.profile", _PROFILE),
    ("foundry.outcome", "passed"),
    ("foundry.attempts", "3"),
    ("foundry.quarantine", "none"),
    ("foundry.artifact.request", "tests__test_cart.py__test_total/request.json"),
    ("foundry.artifact.response", "tests__test_cart.py__test_total/response.json"),
    ("foundry.artifact.stdout", "tests__test_cart.py__test_total/stdout.log"),
    ("foundry.artifact.stderr", "tests__test_cart.py__test_total/stderr.log"),
    ("foundry.artifact.traceback", "tests__test_cart.py__test_total/traceback.txt"),
)
"""The four fixed properties then the five artifact references, as AAP 0.4.6 orders them."""


# --- Building the fixtures -------------------------------------------------


def _record(**overrides: object) -> ResultRecord:
    """Return a failing result record whose every field the caller did not override is fixed."""
    fields: dict[str, object] = {
        "run_id": _RUN_ID,
        "started_at": _STARTED_AT,
        "finished_at": _FINISHED_AT,
        "profile": _PROFILE,
        "nodeid": _NODEID,
        "outcome": Outcome.FAILED,
        "attempts": 1,
        "duration_ms": 12.5,
    }
    fields.update(overrides)
    return ResultRecord.model_validate(fields)


def _run_error(message: str) -> RunError:
    """Return a run-level error element, the second kind ``results.json`` holds."""
    return RunError(run_id=_RUN_ID, recorded_at=_STARTED_AT, worker_id="gw1", message=message)


def _elements(location: str, forbidden: str) -> tuple[list[ResultRecord | RunError], str]:
    """Return the run elements carrying ``forbidden`` at ``location``, and the text that must live.

    Args:
        location: Which of :data:`_LOCATIONS` holds the character.
        forbidden: The code point XML 1.0 cannot carry.

    Returns:
        The elements to render, and the surrounding text a reader of the document must still find.

    Raises:
        ValueError: ``location`` is not one of :data:`_LOCATIONS`, which a new location added to
            that tuple without a case here would pass silently otherwise.
    """
    if location == "failure-message":
        failures = (FailureRecord(phase="call", message=f"AssertionError: nope{forbidden}"),)
        return [_record(failures=failures)], "AssertionError: nope"
    if location == "nodeid":
        return [_record(nodeid=f"{_MODULE}::test_{forbidden}total")], "test_total"
    if location == "run-error-message":
        return [_record(), _run_error(f"worker lost{forbidden}")], "worker lost"
    raise ValueError(f"unknown location: {location}")


def _properties(element: ElementTree.Element) -> list[tuple[str, str]]:
    """Return an element's ``<properties>`` as ``(name, value)`` pairs, in document order."""
    holder = element.find("properties")
    assert holder is not None
    return [(prop.get("name", ""), prop.get("value", "")) for prop in holder.findall("property")]


def _rendered_text(root: ElementTree.Element, location: str) -> str:
    """Return the text of the field ``location`` names, as the parsed document holds it.

    Args:
        root: The parsed ``<testsuite>``.
        location: Which of :data:`_LOCATIONS` the character was placed in.

    Returns:
        The message attribute and body of the failure, the testcase ``name``, or every
        ``<system-err>`` text, whichever ``location`` names.
    """
    if location == "failure-message":
        failure = root.find("testcase/failure")
        assert failure is not None
        return f"{failure.get('message', '')}\n{failure.text or ''}"
    if location == "nodeid":
        testcase = root.find("testcase")
        assert testcase is not None
        return testcase.get("name", "")
    return "\n".join(element.text or "" for element in root.findall("system-err"))


# --- A character XML cannot carry ------------------------------------------


@pytest.mark.parametrize("location", _LOCATIONS)
@pytest.mark.parametrize(
    ("label", "forbidden"),
    _FORBIDDEN_CHARACTERS,
    ids=[label for label, _ in _FORBIDDEN_CHARACTERS],
)
def test_a_forbidden_character_never_reaches_the_document(
    label: str, forbidden: str, location: str, tmp_path: Path
) -> None:
    """A character outside the ``Char`` production is removed, and the document still parses.

    The two parsers are independent (``xml.etree`` and ``xml.dom.minidom``), so agreement between
    them is what makes the document readable by "any CI system" rather than by one library, and
    the same check is repeated on the file :func:`foundry.reporting.junit.write` produces.
    """
    elements, survivor = _elements(location, forbidden)
    document = junit.render(elements)
    assert forbidden not in document
    assert survivor in document
    root = ElementTree.fromstring(document)
    minidom.parseString(document.encode("utf-8"))
    assert survivor in _rendered_text(root, location)
    target = tmp_path / f"{label}-{location}.xml"
    assert junit.write(elements, target) == target
    assert target.read_text(encoding="utf-8") == document
    ElementTree.parse(target)
    minidom.parseString(target.read_bytes())


@pytest.mark.parametrize(
    ("label", "legal"), _LEGAL_CHARACTERS, ids=[label for label, _ in _LEGAL_CHARACTERS]
)
def test_the_characters_xml_allows_survive_unchanged(label: str, legal: str) -> None:
    """DEL and the C1 controls are legal XML content, so removing them would be the defect."""
    message = f"keep{legal}inside"
    document = junit.render([_record(failures=(FailureRecord(phase="call", message=message),))])
    assert legal in document
    root = ElementTree.fromstring(document)
    minidom.parseString(document.encode("utf-8"))
    failure = root.find("testcase/failure")
    assert failure is not None
    assert failure.get("message") == message


def test_a_forbidden_character_inside_a_property_value_is_removed() -> None:
    """The walk covers a property value as well as an element's text and a failure's message.

    An artifact reference is the property value a run corrupts most easily: the path is derived
    from a node id, and a node id collected from a coloured terminal carries escape sequences.
    """
    path = f"tests__test_cart.py__test_total/request{_ANSI_ESCAPE}[31m.json"
    record = _record(artifacts=(ArtifactRecord(kind="request", path=path, bytes=10),))
    document = junit.render([record])
    assert _ANSI_ESCAPE not in document
    root = ElementTree.fromstring(document)
    minidom.parseString(document.encode("utf-8"))
    testcase = root.find("testcase")
    assert testcase is not None
    assert (
        "foundry.artifact.request",
        "tests__test_cart.py__test_total/request[31m.json",
    ) in _properties(testcase)


# --- The fields sanitisation must not disturb ------------------------------


def test_the_documented_properties_and_artifact_references_are_unchanged() -> None:
    """Sanitising a document leaves AAP 0.4.6's four properties and five references as they were."""
    record = _record(
        outcome=Outcome.PASSED,
        attempts=3,
        artifacts=_ARTIFACTS,
        quarantine=QuarantineRecord(state="none"),
    )
    root = ElementTree.fromstring(junit.render([record]))
    testcase = root.find("testcase")
    assert testcase is not None
    assert _properties(testcase) == list(_EXPECTED_PROPERTIES)


def test_a_markup_node_id_is_still_escaped() -> None:
    """AAP 0.4.7: a ``<script>`` node id is escaped, and sanitisation keeps that escaping."""
    nodeid = f"{_MODULE}::test_<script>alert(1)</script>"
    document = junit.render([_record(nodeid=nodeid)])
    assert "&lt;script&gt;" in document
    assert "<script" not in document
    testcase = ElementTree.fromstring(document).find("testcase")
    assert testcase is not None
    assert testcase.get("name") == "test_<script>alert(1)</script>"
