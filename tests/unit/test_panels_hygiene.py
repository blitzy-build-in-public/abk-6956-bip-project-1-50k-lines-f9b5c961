"""The ``foundry.reporting.panels`` behaviours the findings on this surface fix.

Finding F02 (``panels-zero-capture-warning``): ``logging.capture_lines = 0`` is a supported
configuration -- :data:`foundry.obs.capture.MIN_CAPTURE_LINES` documents it as a clean no-capture
state -- yet ``read_log_tail`` announced it through ``emit_plumbing_failure`` at ``WARNING``, a
level AAP 0.13.1 reserves for the failure-isolation cases and the HTML fallback. A zero limit now
reads nothing and reports nothing, while the three genuine faults (a traversal escape, a path that
is not a regular file and a read fault) still report exactly one ``WARNING`` each.

Finding F09 (``cfg-phase-order-index-mutable``): ``_PHASE_ORDER_INDEX`` was a plain dict bound at
module scope, and a ``Final`` annotation stops a rebinding but not a write into the dict, which
AAP 0.4.8's module-level-state rule forbids. It is now a read-only mapping that still orders every
phase.

Finding cr05-capture-prefix-duplication (``format_record`` prefix): every Foundry lifecycle line is
rendered by ``foundry.obs.events.event_line``, which already begins with ``foundry: ``, and
``foundry.obs.capture.format_record`` prefixed the logger name a second time, so the duplication was
persisted into ``log_tail.log`` and shown in this panel. The last case drives the ``foundry``
logger, the artifact writer and :func:`panels.read_log_tail` over one line and requires the prefix
to appear once.

Findings cr0bac-empty-log-tail-wording and cr1b36-empty-log-tail-wording (``log tail unavailable``):
a failing test that logged nothing makes ``ArtifactWriter.write_log_tail`` write no file, so its
record lists no ``log_tail`` artifact and ``_log_tail_for`` reported the tail as unavailable -- a
capture fault that never happened, while the middle state the template documents ("captured no
lines") rendered nowhere. Capture enabled with no listed tail now reports a tail that was captured
and held none, and two cases below pin that state beside the artifacts-disabled one and the genuine
read fault the earlier cases cover.

Finding cr1b36-raw-control-characters (raw control bytes in ``report.html``): autoescaping covers
``& < > ' "`` and nothing else, so a NUL or a bell a failing test wrote into its message reached the
document as a raw byte -- invisible on screen and binary to any tool that reads the file as text.
``visible_control_characters`` now spells every such character out as visible escape text on the
panel message and on every captured log line alike, and the three cases at the end of this module
pin that escaping, the newline and tab that must survive it, and the log line it also covers.

No other test module covers ``foundry.reporting``, so these cases are the guard against any of
these behaviours returning silently.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from foundry.core.types import Phase
from foundry.obs import artifacts, capture, events
from foundry.obs.redaction import REDACTED, default_redactor
from foundry.reporting import panels
from foundry.reporting.record import FailureRecord, Outcome, ResultRecord

_TAIL_NAME = "log_tail.log"
"""Name of the artifact the panels read, as ``foundry.obs.artifacts`` writes it."""

_OLDER_LINE = "the older captured line"
"""The first of the two lines every tail file here holds, so a stray read is observable."""

_DENIED_LINE = "x-api-key: " + "sentinel-value"
"""A captured line carrying a header name ``secrets.redact_keys`` denies by default.

The value is assembled at run time so the literal never reads as a credential to a scanner.
"""

_REDACTED_DENIED_LINE = f"x-api-key: {REDACTED}"
"""What :func:`foundry.obs.redaction.Redactor.text` makes of :data:`_DENIED_LINE`."""

_CONTROL_CHARACTERS: tuple[tuple[str, str], ...] = (
    ("nul", "\x00"),
    ("bel", "\x07"),
    ("esc", "\x1b"),
    ("del", "\x7f"),
)
"""One label and one character for each class of control character a report must never embed raw.

A C0 control, the escape sequence a colourised traceback carries, and DEL: every one of them has no
visible form, so a document holding it renders nothing where it sits and greps as binary.
"""

# Fixed run id, profile, node id and instants, so no assertion depends on the host's clock.

_STARTED_AT = datetime(2025, 1, 2, 3, 4, 5, 0, tzinfo=UTC)
_FINISHED_AT = datetime(2025, 1, 2, 3, 4, 6, tzinfo=UTC)


def _logless_record() -> ResultRecord:
    """Return a failing record that lists no ``log_tail`` artifact.

    That listing is what the artifact writer produces for a failing test that logged nothing: an
    empty sequence writes no file, so the artifact is absent rather than empty.
    """
    return ResultRecord.model_validate(
        {
            "run_id": "20250102T030405-abc123",
            "started_at": _STARTED_AT,
            "finished_at": _FINISHED_AT,
            "profile": "ci",
            "nodeid": "tests/test_probe.py::test_logless",
            "outcome": Outcome.FAILED,
            "attempts": 1,
            "duration_ms": 4.0,
            "failures": (FailureRecord(phase="call", message="AssertionError: boom"),),
        }
    )


def _record_with_message(message: str) -> ResultRecord:
    """Return a failing record whose call-phase failure carries ``message`` verbatim."""
    return ResultRecord.model_validate(
        {
            "run_id": "20250102T030405-abc123",
            "started_at": _STARTED_AT,
            "finished_at": _FINISHED_AT,
            "profile": "ci",
            "nodeid": "tests/test_probe.py::test_message",
            "outcome": Outcome.FAILED,
            "attempts": 1,
            "duration_ms": 4.0,
            "failures": (FailureRecord(phase="call", message=message),),
        }
    )


def _panel_message(message: str) -> str:
    """Build the one panel of a failing record and return the message it carries.

    Args:
        message: The failure message the record reports.

    Returns:
        The panel's message after redaction and control-character normalisation, which is what the
        template interpolates into the report.
    """
    (panel,) = panels.build_panels([_record_with_message(message)])
    return panel.message


def _write_tail(directory: Path, *lines: str) -> Path:
    """Write a ``log_tail.log`` holding ``lines`` into ``directory`` and return its path.

    Args:
        directory: The directory to write into, normally pytest's ``tmp_path``.
        lines: The captured lines, oldest first, each written with a trailing newline.

    Returns:
        The path of the written tail, which is a regular file :func:`panels.read_log_tail` reads.
    """
    path = directory / _TAIL_NAME
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8", newline="\n")
    return path


def test_zero_limit_reads_nothing_and_reports_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A zero limit is the supported no-capture state: ``((), False)`` and no ``WARNING``."""
    tail = _write_tail(tmp_path, _OLDER_LINE, _DENIED_LINE)
    with caplog.at_level(logging.DEBUG, logger="foundry"):
        lines, available = panels.read_log_tail(tail, limit=0, redactor=default_redactor())
    assert (lines, available) == ((), False)
    warnings = [
        record
        for record in caplog.records
        if record.name.startswith("foundry") and record.levelno >= logging.WARNING
    ]
    assert warnings == [], [record.getMessage() for record in warnings]


def test_genuine_fault_still_warns_once(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A path that is not a regular file reports one ``WARNING`` naming it (AAP 0.14.3)."""
    missing = tmp_path / "missing.log"
    with caplog.at_level(logging.DEBUG, logger="foundry"):
        lines, available = panels.read_log_tail(missing, limit=5, redactor=default_redactor())
    assert (lines, available) == ((), False)
    warnings = [
        record
        for record in caplog.records
        if record.name.startswith("foundry") and record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1, [record.getMessage() for record in warnings]
    assert str(missing) in warnings[0].getMessage()


def test_phase_order_index_is_read_only_and_complete() -> None:
    """``_PHASE_ORDER_INDEX`` rejects mutation and still maps every phase to its position."""
    index = panels._PHASE_ORDER_INDEX
    assert not isinstance(index, dict)
    phase: Phase = panels.PHASE_ORDER[0]
    with pytest.raises(TypeError):
        index[phase] = 0
    assert dict(index) == {member: position for position, member in enumerate(panels.PHASE_ORDER)}


def test_positive_limit_still_returns_the_newest_redacted_lines(tmp_path: Path) -> None:
    """A positive limit still reads the newest lines and redacts them, as before."""
    tail = _write_tail(tmp_path, _OLDER_LINE, _DENIED_LINE)
    lines, available = panels.read_log_tail(tail, limit=1, redactor=default_redactor())
    assert available is True
    assert lines == (_REDACTED_DENIED_LINE,)


def test_a_capture_enabled_record_without_a_tail_reports_it_as_captured(tmp_path: Path) -> None:
    """A record that lists no ``log_tail`` under capture enabled held no lines, not a fault."""
    (panel,) = panels.build_panels(
        [_logless_record()],
        artifacts_root=tmp_path / "artifacts",
        report_dir=tmp_path,
        log_lines=5,
    )
    assert (panel.log_available, panel.has_log, panel.log_lines) == (True, False, ())


def test_disabled_artifacts_keep_the_tail_unavailable(tmp_path: Path) -> None:
    """With capture disabled the panel says the tail is unavailable, not that it held none."""
    (panel,) = panels.build_panels([_logless_record()], artifacts_root=None, report_dir=tmp_path)
    assert (panel.log_available, panel.has_log) == (False, False)


def test_a_captured_event_line_carries_its_prefix_once_end_to_end(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """One lifecycle line reaches ``log_tail.log`` and this reader with one ``foundry: `` prefix."""
    expected = "WARNING foundry: plumbing_failure component=probe message='message 3'"
    ring = capture.LogRing(capacity=1)
    with caplog.at_level(logging.DEBUG, logger="foundry"), ring:
        events.emit_plumbing_failure("probe", "message 3")
    writer = artifacts.ArtifactWriter(
        tmp_path / "artifacts",
        run_id="20250102T030405-abc123",
        profile="default",
        redactor=default_redactor(),
        nodeid="tests/test_probe.py::test_capture",
    )

    reference = writer.write_log_tail(list(ring.tail(1)))

    assert ring.tail(1) == (expected,)
    assert reference is not None
    path = writer.run_dir / reference.path
    assert path.read_text(encoding="utf-8").splitlines() == [expected]
    assert panels.read_log_tail(path, limit=5, redactor=default_redactor()) == ((expected,), True)


@pytest.mark.parametrize(
    ("label", "character"),
    _CONTROL_CHARACTERS,
    ids=[label for label, _ in _CONTROL_CHARACTERS],
)
def test_a_control_character_in_a_failure_message_is_spelled_out(
    label: str, character: str
) -> None:
    """A control character reaches the panel as escape text, and no raw one survives."""
    message = _panel_message(f"{label}-{character}-end")
    assert message == f"{label}-\\x{ord(character):02x}-end"
    raw = [member for member in message if ord(member) < 0x20 or 0x7F <= ord(member) <= 0x9F]
    assert raw == [], repr(message)


def test_a_newline_and_a_tab_survive_in_the_failure_message() -> None:
    """The two characters that carry line structure are left exactly as the failure wrote them."""
    message = "assert total == 3\n\tassert 4 == 3"
    assert _panel_message(message) == message


def test_a_captured_log_line_is_prepared_with_its_control_character_spelled_out() -> None:
    """A captured log line reaches the panel with its control character spelled out, tabs kept."""
    prepared = panels._prepare_line("captured\x07line\twith\ttabs\x7f", default_redactor())
    assert prepared == "captured\\x07line\twith\ttabs\\x7f"
