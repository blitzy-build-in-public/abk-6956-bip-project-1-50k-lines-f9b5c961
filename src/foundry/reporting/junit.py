"""The JUnit XML writer: the document a CI system reads as Foundry's result surface.

``junit.xml`` is written to the invocation directory beside ``results.json`` and ``report.html``
and holds one ``<testcase>`` per result record plus one ``<system-err>`` per run-level error, in
exactly the shape AAP 0.4.6 fixes. A testcase's ``classname`` is the node id's module path with its
``.py`` suffix removed and its separators turned into dots, its ``name`` is the test id -- a
parametrised id keeps its ``[...]`` suffix, which is what a CI system displays -- and its ``time``
is ``duration_ms / 1000`` to three decimals. Every testcase carries the four properties
``foundry.profile``, ``foundry.outcome``, ``foundry.attempts`` and ``foundry.quarantine``, then a
``foundry.artifact.<kind>`` reference for each of the five artifacts AAP 0.4.6 names, whose value is
the path exactly as the record holds it: relative to the run directory, so a reference survives the
checkout being moved. ``log_tail`` and ``manifest`` references are deliberately absent -- the
manifest describes the artifacts, and the log tail is rendered by ``report.html``.

Outcome mapping (AAP 0.4.6), the reason this writer exists rather than a generic serialiser:

* ``passed`` -- a testcase with no child element.
* ``failed`` -- a ``<failure message="...">`` whose text carries every failing phase, counted in the
  suite's ``failures``.
* ``error`` -- an ``<error message="...">``: JUnit tells an unexpected exception from a failed
  assertion, and a CI reader expects the element. Counted in the suite's ``errors``.
* ``skipped`` -- ``<skipped message="foundry: skipped">``.
* ``xfailed`` -- ``<skipped message="foundry: expected failure">``, because an expected failure is
  not a CI failure.
* ``xpassed`` -- a testcase plus a ``<system-out>`` note that the test passed unexpectedly.
* ``quarantined_failed`` -- ``<skipped message="foundry: quarantined failure">``, the exact string
  AAP 0.4.6 fixes, so a CI system does not read a quarantined failure as a failure and the document
  agrees with the run's exit code of ``0``.
* ``quarantine_passed`` -- a testcase plus a ``<system-out>`` note recommending the
  ``foundry_quarantine`` marker be removed.
* ``not_run`` -- ``<skipped message="foundry: not run">`` for an item that never executed because
  the session stopped or a worker was lost (assumption A12).

``skipped``, ``xfailed``, ``quarantined_failed`` and ``not_run`` are the four that count in the
suite's ``skipped`` attribute. A run error counts in none of the suite's counters, because it
describes a failure of the run rather than of a test: it becomes a ``<system-err>`` on the
``<testsuite>``, prefixed with the worker id it came from.

Escaping and determinism
    Every value is written through :mod:`xml.etree.ElementTree`, so a test id containing
    ``<script>`` reaches the document as ``&lt;script&gt;`` and a message holding ``&``, ``"`` or
    ``'`` round-trips through a parser unchanged; nothing here joins markup by hand. Escaping alone
    cannot carry a code point outside XML 1.0's ``Char`` production -- a C0 control other than tab,
    newline and carriage return, a lone surrogate, ``#xFFFE`` or ``#xFFFF`` -- and ElementTree
    passes such a point through as it found it, so every record-derived string is reduced to that
    production first (:func:`_sanitise_tree`). Those characters are removed rather than replaced:
    XML text carries no meaning for them, and a replacement character would push a clipped
    ``<failure>`` message past :data:`FAILURE_MESSAGE_BYTES`. The document carries no timestamp, no
    hostname and no wall clock -- a ``<testsuite>`` timestamp attribute would make two runs of one
    suite differ -- so it is byte-identical for identical records, its sanitised strings included.
    Records are sorted by node id, run errors by worker id and message, no set is ever iterated,
    and durations are summed with :func:`math.fsum`, whose result does not depend on the order the
    terms arrive in.

Redaction and failure isolation
    Every failure message, run-error message and artifact path passes through
    :class:`foundry.obs.redaction.Redactor` before it reaches the document (AAP 0.3.2). A record's
    node id, outcome, profile, attempt count, quarantine state, duration and the run's id are
    contract values and are left exactly as captured. This module renders text and writes nothing:
    the write goes through :func:`foundry.reporting.store.write_text`, the package's only writer
    (AAP 0.4.8), which reports a refused filesystem write at ``WARNING`` and returns ``None``, so a
    report that cannot be written never changes an outcome or an exit code (AAP 0.14.3).

Import direction and import-time discipline (AAP 0.4.8): the standard library,
:mod:`foundry.core`, :mod:`foundry.obs` and this package's own :mod:`foundry.reporting.record` and
:mod:`foundry.reporting.store`. Importing this module defines constants and functions only -- no
I/O, no environment read, no logger configuration and no mutable module state.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Final
from xml.etree import ElementTree

from foundry.core.exceptions import ElementTypeError
from foundry.core.types import ArtifactKind
from foundry.obs.redaction import Redactor, default_redactor
from foundry.reporting.record import Outcome, ResultElement, ResultRecord, RunError
from foundry.reporting.store import write_text

__all__: Final[tuple[str, ...]] = (
    "ARTIFACT_PROPERTY_NAMES",
    "ARTIFACT_PROPERTY_ORDER",
    "FAILURE_MESSAGE_BYTES",
    "JUNIT_SUITE_NAME",
    "NOT_RUN_SKIP_MESSAGE",
    "NO_FAILURE_DETAIL_MESSAGE",
    "PROPERTY_ARTIFACT_REQUEST",
    "PROPERTY_ARTIFACT_RESPONSE",
    "PROPERTY_ARTIFACT_STDERR",
    "PROPERTY_ARTIFACT_STDOUT",
    "PROPERTY_ARTIFACT_TRACEBACK",
    "PROPERTY_ATTEMPTS",
    "PROPERTY_OUTCOME",
    "PROPERTY_PROFILE",
    "PROPERTY_QUARANTINE",
    "PROPERTY_RUN_ID",
    "QUARANTINE_PASSED_NOTE",
    "QUARANTINE_SKIP_MESSAGE",
    "SKIPPED_SKIP_MESSAGE",
    "XFAIL_SKIP_MESSAGE",
    "XPASSED_NOTE",
    "render",
    "split_nodeid",
    "write",
)

# --- Document shape --------------------------------------------------------

_XML_DECLARATION: Final[str] = '<?xml version="1.0" encoding="utf-8"?>'
"""The declaration the serialised document opens with, before the indented ``<testsuite>``."""

_NEWLINE: Final[str] = "\n"
"""The document's line terminator, so a report written on any platform is the same bytes."""

_INDENT_SPACE: Final[str] = "  "
"""Indent width of the document: two spaces, one level per nesting depth."""

JUNIT_SUITE_NAME: Final[str] = "foundry"
"""The ``name`` of the one ``<testsuite>``: the product, not the project under test."""

FAILURE_MESSAGE_BYTES: Final[int] = 500
"""UTF-8 byte budget of a ``<failure>``/``<error>`` ``message`` attribute.

A CI system shows that attribute in a list, so it is bounded: the element's text carries every
failing phase in full, and the attribute carries the first failure's message, redacted first and
then clipped by :meth:`foundry.obs.redaction.Redactor.text`, which appends its own truncation
marker rather than splitting a character.
"""

# --- The exact strings AAP 0.4.6 fixes ------------------------------------


QUARANTINE_SKIP_MESSAGE: Final[str] = "foundry: quarantined failure"
"""The ``<skipped>`` message of a quarantined failure; AAP 0.4.6 quotes this string verbatim."""

SKIPPED_SKIP_MESSAGE: Final[str] = "foundry: skipped"
"""The ``<skipped>`` message of a test pytest skipped through a marker or a ``skip`` call."""

XFAIL_SKIP_MESSAGE: Final[str] = "foundry: expected failure"
"""The ``<skipped>`` message of a failure pytest reported as expected, which CI must not fail on."""

NOT_RUN_SKIP_MESSAGE: Final[str] = "foundry: not run"
"""The ``<skipped>`` message of an item that never executed (assumption A12)."""

QUARANTINE_PASSED_NOTE: Final[str] = (
    "foundry: quarantined test passed; remove the foundry_quarantine marker"
)
"""The ``<system-out>`` note of a marked test that passed, so the marker can come off."""

XPASSED_NOTE: Final[str] = "foundry: unexpected pass of an xfail test"
"""The ``<system-out>`` note of an ``xfail`` test that passed, which no CI element can carry."""

NO_FAILURE_DETAIL_MESSAGE: Final[str] = "foundry: no failure detail was recorded"
"""What a failing record with an empty ``failures`` tuple reports, so no element is ever blank."""

# --- Property names -------------------------------------------------------


PROPERTY_PROFILE: Final[str] = "foundry.profile"
"""Name of the property carrying the active profile (AAP 0.4.6)."""

PROPERTY_OUTCOME: Final[str] = "foundry.outcome"
"""Name of the property carrying the record's outcome, as its bare value."""

PROPERTY_ATTEMPTS: Final[str] = "foundry.attempts"
"""Name of the property carrying the total attempt count, the first attempt included."""

PROPERTY_QUARANTINE: Final[str] = "foundry.quarantine"
"""Name of the property carrying the quarantine state: ``none``, ``failed`` or ``passed``."""

PROPERTY_RUN_ID: Final[str] = "foundry.run_id"
"""Name of the ``<testsuite>`` property carrying the run's identifier (AAP 0.4.6)."""

PROPERTY_ARTIFACT_REQUEST: Final[str] = "foundry.artifact.request"
"""Name of the property referencing the dumped request, relative to the run directory."""

PROPERTY_ARTIFACT_RESPONSE: Final[str] = "foundry.artifact.response"
"""Name of the property referencing the dumped response, relative to the run directory."""

PROPERTY_ARTIFACT_STDOUT: Final[str] = "foundry.artifact.stdout"
"""Name of the property referencing the captured stdout, relative to the run directory."""

PROPERTY_ARTIFACT_STDERR: Final[str] = "foundry.artifact.stderr"
"""Name of the property referencing the captured stderr, relative to the run directory."""

PROPERTY_ARTIFACT_TRACEBACK: Final[str] = "foundry.artifact.traceback"
"""Name of the property referencing the traceback artifact, relative to the run directory."""

ARTIFACT_PROPERTY_NAMES: Final[Mapping[ArtifactKind, str]] = MappingProxyType(
    {
        "request": PROPERTY_ARTIFACT_REQUEST,
        "response": PROPERTY_ARTIFACT_RESPONSE,
        "stdout": PROPERTY_ARTIFACT_STDOUT,
        "stderr": PROPERTY_ARTIFACT_STDERR,
        "traceback": PROPERTY_ARTIFACT_TRACEBACK,
    }
)
"""The five artifact kinds AAP 0.4.6 references, each with its property name.

``log_tail`` and ``manifest`` are absent on purpose: the manifest lists the artifacts, and the log
tail is the HTML report's panel rather than a JUnit reference.
"""

ARTIFACT_PROPERTY_ORDER: Final[tuple[ArtifactKind, ...]] = (
    "request",
    "response",
    "stdout",
    "stderr",
    "traceback",
)
"""The kinds above in the order AAP 0.4.6 lists them, so the properties read the same every run."""

# --- Element names and the outcome dispatch -------------------------------

_SUITE: Final[str] = "testsuite"
"""Element name of the document's single suite, the root of the tree."""

_TESTCASE: Final[str] = "testcase"
"""Element name of one test."""

_PROPERTIES: Final[str] = "properties"
"""Element name of the container holding a testcase's or the suite's properties."""

_PROPERTY: Final[str] = "property"
"""Element name of one property; its ``name`` and ``value`` attributes are fixed by JUnit."""

_FAILURE: Final[str] = "failure"
"""Element name of a failed assertion, which is a CI failure."""

_ERROR: Final[str] = "error"
"""Element name of an unexpected exception, which a CI reader reports apart from a failure."""

_SKIPPED: Final[str] = "skipped"
"""Element name of a test that did not count against the run."""

_SYSTEM_OUT: Final[str] = "system-out"
"""Element name of the note a passing quarantined or ``xfail``-passing test carries."""

_SYSTEM_ERR: Final[str] = "system-err"
"""Element name of a run error on the ``<testsuite>``, as AAP 0.4.6 requires."""

_FAILURE_ELEMENTS: Final[Mapping[Outcome, str]] = MappingProxyType(
    {Outcome.FAILED: _FAILURE, Outcome.ERROR: _ERROR}
)
"""The two outcomes that produce a failing child element: a failure and an unexpected error."""

_SKIP_MESSAGES: Final[Mapping[Outcome, str]] = MappingProxyType(
    {
        Outcome.SKIPPED: SKIPPED_SKIP_MESSAGE,
        Outcome.XFAILED: XFAIL_SKIP_MESSAGE,
        Outcome.QUARANTINED_FAILED: QUARANTINE_SKIP_MESSAGE,
        Outcome.NOT_RUN: NOT_RUN_SKIP_MESSAGE,
    }
)
"""The four outcomes that produce a ``<skipped>`` element, each with its exact message."""

_NOTE_MESSAGES: Final[Mapping[Outcome, str]] = MappingProxyType(
    {Outcome.XPASSED: XPASSED_NOTE, Outcome.QUARANTINE_PASSED: QUARANTINE_PASSED_NOTE}
)
"""The two passing outcomes that carry a ``<system-out>`` note instead of a child element."""


def split_nodeid(nodeid: str) -> tuple[str, str]:
    """Split a pytest node id into the JUnit ``classname`` and ``name`` pair.

    ``tests/test_api.py::TestClient::test_get[200-ok]`` becomes
    ``("tests.test_api.TestClient", "test_get[200-ok]")``: the module path loses its ``.py`` suffix
    and its separators become dots, the class segments join that name, and the last segment is the
    test id itself. A parametrised id keeps its ``[...]`` suffix, which is what a CI system
    displays and what distinguishes one case of one test from another. A bare path with no ``::``
    is reported as both class and name, which is what a JUnit reader needs to list a collected file
    that produced no item.

    Args:
        nodeid: The record's node id, unsanitised as pytest reports it -- sanitising is the
            artifact writer's job, and a class name reads better with its slashes turned into dots.

    Returns:
        The ``(classname, name)`` pair. A node id whose module segment is empty yields an empty
        classname rather than a name taken from the wrong segment.
    """
    segments = nodeid.split("::")
    module = segments[0].strip().replace("\\", "/")
    while module.startswith("./"):
        module = module[2:]
    if module.endswith(".py"):
        module = module[: -len(".py")]
    classname = module.replace("/", ".")
    parts = segments[1:]
    if not parts:
        return classname, classname
    if len(parts) == 1:
        return classname, parts[0]
    return f"{classname}.{'.'.join(parts[:-1])}", parts[-1]


def _seconds(duration_ms: float) -> str:
    """Return a duration in milliseconds as the ``time`` attribute's seconds text.

    Three decimals are always written, so a fast run renders ``0.001`` rather than a shorter float
    repr and two runs of one suite produce the same text.

    Args:
        duration_ms: The duration in milliseconds, never negative.

    Returns:
        The duration in seconds, formatted with exactly three decimals.
    """
    return f"{duration_ms / 1000.0:.3f}"


# --- XML character safety --------------------------------------------------

_XML_FORBIDDEN: Final[re.Pattern[str]] = re.compile(
    r"[^\t\n\r\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]"
)
"""Every code point XML 1.0's ``Char`` production excludes, as one compiled pattern.

That production is ``#x9 | #xA | #xD | [#x20-#xD7FF] | [#xE000-#xFFFD] | [#x10000-#x10FFFF]``;
ElementTree escapes only ``& < > " '``, so ``#x7F`` and the C1 range ``#x80-#x9F`` are kept and a
C0 control, a lone surrogate, ``#xFFFE`` or ``#xFFFF`` is not.
"""


def _sanitise(text: str) -> str:
    """Return the already-redacted ``text`` carrying only characters XML 1.0 admits.

    Args:
        text: A string a record contributed to the document.

    Returns:
        The text with every code point outside the ``Char`` production removed.
    """
    return _XML_FORBIDDEN.sub("", text)


def _sanitise_tree(root: ElementTree.Element) -> None:
    """Reduce every attribute value and text node of ``root`` to characters XML admits.

    One walk over the built tree covers each record-derived string once -- the testcase
    ``classname`` and ``name``, every property value, the failure or error ``message`` attribute
    and body, and a run error's ``<system-err>`` text -- and it covers a field a later record
    adds. Only :func:`_sanitise` changes a value, so no order changes and two renders of one record
    set stay byte-identical (AAP 0.13.1).

    Args:
        root: The suite element built from the run's records, before it is indented.
    """
    for element in root.iter():
        if element.text is not None:
            element.text = _sanitise(element.text)
        for name, value in tuple(element.attrib.items()):
            element.attrib[name] = _sanitise(value)


# --- Building the tree -----------------------------------------------------


def _add_properties(parent: ElementTree.Element, pairs: Sequence[tuple[str, str]]) -> None:
    """Attach a ``<properties>`` container to ``parent`` when there is a property to record.

    An empty ``pairs`` attaches nothing, so a suite that knows no run id does not carry an empty
    container and the zero-record document stays the bare element AAP 0.4.6 describes.

    Args:
        parent: The element -- a testcase or the testsuite -- the container is attached to.
        pairs: The ``(name, value)`` pairs, already redacted, in the order the document lists them.
    """
    if not pairs:
        return
    holder = ElementTree.SubElement(parent, _PROPERTIES)
    for name, value in pairs:
        ElementTree.SubElement(holder, _PROPERTY, {"name": name, "value": value})


def _artifact_pairs(record: ResultRecord, scrub: Redactor) -> list[tuple[str, str]]:
    """Return the ``foundry.artifact.<kind>`` references a record's artifacts produce.

    The five kinds are emitted in :data:`ARTIFACT_PROPERTY_ORDER`, so two runs that captured the
    same artifacts produce the same property order. A kind the record does not carry is omitted
    rather than emitted empty: a reference to a file that was never written would be a dead link,
    and a passed test with ``artifacts.keep_passed`` off carries no artifacts at all.

    Args:
        record: The result record whose artifact list is read.
        scrub: The redactor every path passes through before it reaches the document.

    Returns:
        The ``(property name, relative path)`` pairs, each path scrubbed, in document order.
    """
    recorded: dict[ArtifactKind, str] = {}
    for artifact in record.artifacts:
        recorded.setdefault(artifact.kind, artifact.path)
    pairs: list[tuple[str, str]] = []
    for kind in ARTIFACT_PROPERTY_ORDER:
        path = recorded.get(kind)
        if path is not None:
            pairs.append((ARTIFACT_PROPERTY_NAMES[kind], scrub.text(path)))
    return pairs


def _testcase_properties(record: ResultRecord, scrub: Redactor) -> list[tuple[str, str]]:
    """Return the four fixed properties plus the artifact references of one testcase.

    The fixed four are the values AAP 0.4.6 names, taken from the record exactly as captured: a
    profile, an outcome value, an attempt count and a quarantine state are contract values, so
    redaction must not touch them.

    Args:
        record: The result record the testcase renders.
        scrub: The redactor the artifact paths pass through.

    Returns:
        The ``(name, value)`` pairs in the order the document lists them.
    """
    pairs: list[tuple[str, str]] = [
        (PROPERTY_PROFILE, record.profile),
        (PROPERTY_OUTCOME, record.outcome.value),
        (PROPERTY_ATTEMPTS, str(record.attempts)),
        (PROPERTY_QUARANTINE, record.quarantine.state),
    ]
    pairs.extend(_artifact_pairs(record, scrub))
    return pairs


def _failure_payload(record: ResultRecord, scrub: Redactor) -> tuple[str, str]:
    """Return the ``message`` attribute and the element text of a failing record.

    The attribute is the first failure's message, redacted and then clipped to
    :data:`FAILURE_MESSAGE_BYTES`, because a CI list shows it. The text carries every failing
    phase, each prefixed with its phase name in brackets, so a test that failed in ``setup`` and
    again in ``teardown`` reports both instead of only the first.

    Args:
        record: The failing result record.
        scrub: The redactor every message passes through.

    Returns:
        The ``(message attribute, element text)`` pair. A failing record that carries no failure
        entry -- which a run produces only when the failure text was lost with a worker -- reports
        :data:`NO_FAILURE_DETAIL_MESSAGE` in both, so no element is ever blank.
    """
    if not record.failures:
        return NO_FAILURE_DETAIL_MESSAGE, NO_FAILURE_DETAIL_MESSAGE
    lines = [f"[{failure.phase}] {scrub.text(failure.message)}" for failure in record.failures]
    headline = scrub.text(record.failures[0].message, limit=FAILURE_MESSAGE_BYTES).strip()
    return headline or NO_FAILURE_DETAIL_MESSAGE, "\n".join(lines)


def _add_outcome(testcase: ElementTree.Element, record: ResultRecord, scrub: Redactor) -> None:
    """Attach the child element the record's outcome maps to, if it has one.

    Args:
        testcase: The testcase element the child is attached to.
        record: The result record being rendered.
        scrub: The redactor a failing message passes through.
    """
    failing = _FAILURE_ELEMENTS.get(record.outcome)
    if failing is not None:
        headline, body = _failure_payload(record, scrub)
        element = ElementTree.SubElement(testcase, failing, {"message": headline})
        element.text = body
        return
    skipped = _SKIP_MESSAGES.get(record.outcome)
    if skipped is not None:
        ElementTree.SubElement(testcase, _SKIPPED, {"message": skipped})
        return
    note = _NOTE_MESSAGES.get(record.outcome)
    if note is not None:
        ElementTree.SubElement(testcase, _SYSTEM_OUT).text = note


def _testcase(record: ResultRecord, scrub: Redactor) -> ElementTree.Element:
    """Build the ``<testcase>`` element of one result record.

    Args:
        record: The result record to render.
        scrub: The redactor the record's messages and artifact paths pass through.

    Returns:
        The element, carrying ``classname``, ``name`` and ``time``, then its properties, then the
        single child element its outcome maps to.
    """
    classname, name = split_nodeid(record.nodeid)
    testcase = ElementTree.Element(
        _TESTCASE,
        {"classname": classname, "name": name, "time": _seconds(record.duration_ms)},
    )
    _add_properties(testcase, _testcase_properties(record, scrub))
    _add_outcome(testcase, record, scrub)
    return testcase


def _suite_identity(
    results: Sequence[ResultRecord], errors: Sequence[RunError]
) -> list[tuple[str, str]]:
    """Return the ``foundry.run_id`` and ``foundry.profile`` properties of the run.

    Both are read from the node-id-sorted first result record, so two runs of one suite attribute
    the document identically whichever worker finished first. A run that recorded only run errors
    still names its run id, and reports no profile, because no test carried one.

    Args:
        results: The run's result records, already sorted by node id.
        errors: The run's run-error elements, already sorted by worker id and message.

    Returns:
        The ``(name, value)`` pairs, empty for a run that recorded nothing.
    """
    if results:
        return [(PROPERTY_RUN_ID, results[0].run_id), (PROPERTY_PROFILE, results[0].profile)]
    if errors:
        return [(PROPERTY_RUN_ID, errors[0].run_id)]
    return []


def _suite(
    results: Sequence[ResultRecord], errors: Sequence[RunError], scrub: Redactor
) -> ElementTree.Element:
    """Build the ``<testsuite>`` element of a run.

    The four counters are exact and never overlap: ``failures`` counts ``failed`` alone, ``errors``
    counts ``error`` alone and ``skipped`` counts the four outcomes that render a ``<skipped>``.
    A quarantined failure is therefore counted as skipped, never as a failure, which is what keeps
    the document agreeing with a run that exits ``0`` under ``quarantine_mode = "report"``.

    Args:
        results: The run's result records, already sorted by node id.
        errors: The run's run-error elements, already sorted by worker id and message.
        scrub: The redactor every message passes through.

    Returns:
        The element, carrying the suite properties, then one testcase per result record, then one
        ``<system-err>`` per run error.
    """
    suite = ElementTree.Element(
        _SUITE,
        {
            "name": JUNIT_SUITE_NAME,
            "tests": str(len(results)),
            "failures": str(sum(1 for record in results if record.outcome is Outcome.FAILED)),
            "errors": str(sum(1 for record in results if record.outcome is Outcome.ERROR)),
            "skipped": str(sum(1 for record in results if record.outcome in _SKIP_MESSAGES)),
            "time": _seconds(math.fsum(record.duration_ms for record in results)),
        },
    )
    _add_properties(suite, _suite_identity(results, errors))
    for record in results:
        suite.append(_testcase(record, scrub))
    for error in errors:
        ElementTree.SubElement(
            suite, _SYSTEM_ERR
        ).text = f"{error.worker_id}: {scrub.text(error.message)}"
    return suite


# --- Ordering --------------------------------------------------------------


def _record_sort_key(record: ResultRecord) -> str:
    """Return the node id a result record is sorted by, which is unique within a session.

    Args:
        record: The record to key.

    Returns:
        The node id, so the order is independent of worker count and finish order.
    """
    return record.nodeid


def _run_error_sort_key(error: RunError) -> tuple[str, str]:
    """Return the ``(worker_id, message)`` pair a run error is sorted by.

    ``recorded_at`` is deliberately not the key: the determinism criterion normalises the record
    timestamps and not this element's, so ordering by it would make two runs differ.

    Args:
        error: The run error to key.

    Returns:
        The sort pair, both values of which the document renders.
    """
    return (error.worker_id, error.message)


def _partition(
    records: Sequence[ResultElement],
) -> tuple[list[ResultRecord], list[RunError]]:
    """Split the run's elements into its result records and its run errors, each sorted.

    Args:
        records: The run's elements in any order.

    Returns:
        The result records sorted by node id, and the run errors sorted by worker id and message.

    Raises:
        ElementTypeError: An element is neither a
            :class:`~foundry.reporting.record.ResultRecord` nor a
            :class:`~foundry.reporting.record.RunError`. It is a
            :class:`~foundry.core.exceptions.FoundryError` that subclasses ``TypeError``, so a
            caller defect surfaces instead of being absorbed into a document that would silently
            omit a test.
    """
    results: list[ResultRecord] = []
    errors: list[RunError] = []
    for element in records:
        if isinstance(element, ResultRecord):
            results.append(element)
        elif isinstance(element, RunError):
            errors.append(element)
        else:
            raise ElementTypeError(
                "junit.xml holds only ResultRecord and RunError elements, "
                f"got {type(element).__name__}",
                hint="pass the elements a Foundry run recorded, as results.json lists them",
            )
    results.sort(key=_record_sort_key)
    errors.sort(key=_run_error_sort_key)
    return results, errors


# --- Public API ------------------------------------------------------------


def render(records: Sequence[ResultElement], *, redactor: Redactor | None = None) -> str:
    """Render a run's elements as the exact text of a ``junit.xml`` document.

    Every value goes through :mod:`xml.etree.ElementTree`, so the library escapes text and
    attribute values and a test id containing ``<script>`` can never reach the document as markup
    (AAP 0.3.2); a code point outside XML 1.0's ``Char`` production, which escaping cannot carry,
    is removed from every record-derived string first (:func:`_sanitise_tree`). The outcome mapping
    -- which of the nine outcomes produces a ``<failure>``, an ``<error>``, a ``<skipped>`` or a
    ``<system-out>`` note, and which of the suite's counters it feeds -- is the module's, and one
    testcase per result record plus one ``<system-err>`` per run error is AAP 0.4.6's shape.

    Nothing here depends on the wall clock, the host or the order the caller collected its records
    in: the document is byte-identical for identical records (AAP 0.14.1 criterion 14).

    Args:
        records: The run's elements, in any order: result records, run errors, or both.
        redactor: The run's redactor, built from ``secrets.redact_keys``. ``None`` uses the
            built-in header and query deny-lists, so a caller cannot skip redaction by omitting it.

    Returns:
        The document as text: the XML declaration, a newline, the element indented by two spaces
        and a trailing newline. An empty ``records`` yields a valid ``<testsuite>`` reporting zero
        tests, so the zero-tests state still leaves a document a CI system can read.

    Raises:
        ElementTypeError: An element is neither a
            :class:`~foundry.reporting.record.ResultRecord` nor a
            :class:`~foundry.reporting.record.RunError`.
    """
    scrub = default_redactor() if redactor is None else redactor
    results, errors = _partition(records)
    suite = _suite(results, errors, scrub)
    _sanitise_tree(suite)
    ElementTree.indent(suite, space=_INDENT_SPACE)
    body = ElementTree.tostring(suite, encoding="unicode")
    return f"{_XML_DECLARATION}{_NEWLINE}{body}{_NEWLINE}"


def write(
    records: Sequence[ResultElement],
    path: str | Path,
    *,
    redactor: Redactor | None = None,
) -> Path | None:
    """Write the run's JUnit XML document to ``path``, creating its directory if needed.

    The document is rendered by :func:`render` and the file is written by
    :func:`foundry.reporting.store.write_text`, this package's only writer (AAP 0.4.8), so no
    module here opens a file itself.

    Args:
        records: The run's elements, in any order; :func:`render` imposes the document's order.
        path: The document to write, normally ``junit.xml`` in the invocation directory or beside
            the path ``--foundry-json-report`` named.
        redactor: The run's redactor, or ``None`` for the built-in deny-lists.

    Returns:
        The path written, or ``None`` when the filesystem refused the write. A refusal is a
        plumbing fault: it is reported at ``WARNING`` on the ``foundry`` logger and changes neither
        a test's outcome nor the run's exit code (AAP 0.14.3).

    Raises:
        ElementTypeError: An element is neither a
            :class:`~foundry.reporting.record.ResultRecord` nor a
            :class:`~foundry.reporting.record.RunError`.
    """
    return write_text(render(records, redactor=redactor), path)
