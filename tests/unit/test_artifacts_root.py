"""Unit tests for the run artifact directory: who creates it and what a blocked root does.

Two AAP requirements, each pinned at both surfaces that reach the directory.

* AAP 0.4.8's filesystem-write row permits a filesystem write only in ``foundry.obs.artifacts``,
  ``foundry.cassette.store`` and ``foundry.reporting.store``, so ``artifacts/<run_id>/`` is created
  by :meth:`foundry.obs.artifacts.ArtifactWriter.ensure_run_dir`; ``foundry.execution.session``
  asks the writer for the verdict instead of creating the tree itself.
* AAP 0.4.7's "artifacts directory not writable" row is what a blocked root produces: one
  ``WARNING`` on the ``foundry`` logger, capture disabled for the run so every record reports
  ``artifacts_disabled: true`` with no artifact paths, and a run that continues. The verdict is the
  writer's shared fault state, so a second attempt adds no second warning.

Both surfaces are driven through the public API only: the writer is built from the artifacts root
and the run's identity, and the session state comes from ``create_state`` with a default
``FoundryConfig``. Determinism and isolation (AAP 0.14.3): every directory lives in ``tmp_path``,
nothing sleeps, reads a wall clock, opens a socket or spawns a process, and the run id the session
mints is used only to build the expected path, never asserted on.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Final

import pytest

from foundry.config.models import FoundryConfig
from foundry.execution import session
from foundry.execution.quarantine import QuarantineDecision
from foundry.obs import logging as obs_logging
from foundry.obs.artifacts import ArtifactWriter
from foundry.obs.redaction import default_redactor

# A run id of the shape AAP 0.4.3 publishes, a profile from AAP 0.4.5's four, and a realistic node
# id carrying "/", "::" and "[".
_RUN_ID: Final[str] = "20250102T030405-0abc12"
_PROFILE: Final[str] = "ci"
_NODEID: Final[str] = "tests/test_api.py::test_cart[1]"

# The opening of the one message a blocked artifacts root produces.
_DISABLED_MESSAGE: Final[str] = "artifact capture disabled: cannot create"

# The verdict of a passing test no quarantine marker decided anything about.
_NO_QUARANTINE: Final[QuarantineDecision] = QuarantineDecision(
    state="none", outcome="passed", fails_run=False, listed=False
)


def _foundry_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Return the records the ``foundry`` logger tree emitted during the current test."""
    prefix = obs_logging.LOGGER_NAME + "."
    return [
        record
        for record in caplog.records
        if record.name == obs_logging.LOGGER_NAME or record.name.startswith(prefix)
    ]


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return the rendered ``WARNING`` lines the ``foundry`` tree emitted during this test."""
    return [
        record.getMessage()
        for record in _foundry_records(caplog)
        if record.levelno == logging.WARNING
    ]


def _writer(root: Path) -> ArtifactWriter:
    """Return a writer rooted at ``root`` and bound to one test, built from the public API."""
    return ArtifactWriter(
        root,
        run_id=_RUN_ID,
        profile=_PROFILE,
        redactor=default_redactor(),
        nodeid=_NODEID,
    )


def _blocked_root(tmp_path: Path) -> Path:
    """Write a regular file where the default ``artifacts.dir`` belongs, and return that path."""
    root = tmp_path / "artifacts"
    root.write_text("a regular file where the artifacts directory belongs\n", encoding="utf-8")
    return root


def test_a_writable_root_yields_the_run_directory_and_capture_on(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A writable root creates ``<root>/<run_id>``, reports capture on and warns about nothing."""
    writer = _writer(tmp_path)
    caplog.set_level(logging.WARNING, logger=obs_logging.LOGGER_NAME)

    assert writer.ensure_run_dir() is True
    assert writer.run_dir == (tmp_path / _RUN_ID).resolve()
    assert writer.run_dir.is_dir()
    assert writer.disabled is False
    assert _warnings(caplog) == []


def test_a_blocked_root_disables_capture_with_one_warning_only(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A blocked root disables capture once, and every later call adds no second warning."""
    writer = _writer(_blocked_root(tmp_path))
    caplog.set_level(logging.WARNING, logger=obs_logging.LOGGER_NAME)

    assert writer.ensure_run_dir() is False
    assert writer.disabled is True

    lines = _warnings(caplog)
    assert len(lines) == 1
    assert _DISABLED_MESSAGE in lines[0]
    assert "component=artifacts" in lines[0]

    # Nothing after the fault reports again: the verdict is the writer's shared fault state.
    assert writer.ensure_run_dir() is False
    assert not writer.dir_for(_NODEID).exists()
    assert writer.write("request", None, '{"cart": []}') is None
    assert writer.write_text("stdout", "cart priced\n") is None
    assert _warnings(caplog) == lines


def test_create_state_creates_the_run_directory_and_enables_capture(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A writable invocation directory gives an enabled state whose run directory exists."""
    caplog.set_level(logging.WARNING, logger=obs_logging.LOGGER_NAME)

    state = session.create_state(FoundryConfig(), invocation_dir=tmp_path)

    assert state.artifacts_enabled is True
    assert state.artifacts_root == (tmp_path / "artifacts" / state.run_id).resolve()
    assert state.artifacts_root.is_dir()
    assert state.writer.run_dir == state.artifacts_root
    assert _warnings(caplog) == []


def test_create_state_disables_capture_when_the_root_cannot_be_created(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A blocked root leaves the state reporting every record as artifacts-disabled."""
    _blocked_root(tmp_path)
    caplog.set_level(logging.WARNING, logger=obs_logging.LOGGER_NAME)

    state = session.create_state(FoundryConfig(), invocation_dir=tmp_path)

    assert state.artifacts_enabled is False
    assert state.writer.disabled is True
    lines = _warnings(caplog)
    assert len(lines) == 1
    assert _DISABLED_MESSAGE in lines[0]

    assembled = session.assemble_payload(
        state=state,
        nodeid=_NODEID,
        markers=(),
        reports=(),
        attempts=(),
        quarantine=_NO_QUARANTINE,
        artifacts=(),
        log_tail=(),
        started_at=state.started_at,
        finished_at=state.started_at,
    )
    not_run = session.not_run_payload(state, _NODEID)

    for payload in (assembled, not_run):
        assert payload.artifacts_disabled is True
        assert payload.artifacts == ()
    assert _warnings(caplog) == lines
