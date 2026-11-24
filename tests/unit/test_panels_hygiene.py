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

No other test module covers ``foundry.reporting``, so these cases are the guard against any of
these behaviours returning silently.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from foundry.core.types import Phase
from foundry.obs import artifacts, capture, events
from foundry.obs.redaction import REDACTED, default_redactor
from foundry.reporting import panels

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
    """A zero limit is the supported no-capture state: ``((), False)`` and no ``WARNING``.

    The tail file holds real lines, so a change that starts reading at limit ``0`` fails the
    return-value assertion as well as the level one.
    """
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
    """``_PHASE_ORDER_INDEX`` rejects mutation and still maps every phase to its position.

    AAP 0.4.8 forbids mutable module-level state, so the mapping is not a ``dict`` and a write
    into it raises :exc:`TypeError`; the completeness assertion keeps that read-only change from
    silently losing the deterministic panel order :func:`panels._panel_sort_key` depends on.
    """
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


def test_a_captured_event_line_carries_its_prefix_once_end_to_end(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """One lifecycle line reaches ``log_tail.log`` and this reader with one ``foundry: `` prefix.

    ``foundry.obs.events.event_line`` renders the logger name as the prefix, and
    :func:`foundry.obs.capture.format_record` adds the logger name only to a message that does not
    already carry it, so the ring, the artifact and the panel agree on one line.
    """
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
