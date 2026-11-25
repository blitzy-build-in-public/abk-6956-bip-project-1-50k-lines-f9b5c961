"""Who creates ``artifacts/<run_id>/`` and what a blocked root does: the verdict is the writer's and
the session asks for it, so a blocked root costs one ``WARNING`` and disables capture for the run.
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

_RUN_ID: Final[str] = "20250102T030405-0abc12"
_NODEID: Final[str] = "tests/test_api.py::test_cart[1]"
_DISABLED_MESSAGE: Final[str] = "artifact capture disabled: cannot create"
_NO_QUARANTINE: Final[QuarantineDecision] = QuarantineDecision(
    state="none", outcome="passed", fails_run=False, listed=False
)
# Every capture input an item that observed nothing carries.
_EMPTY = {"markers": (), "reports": (), "attempts": (), "artifacts": (), "log_tail": ()}


def _writer(root: Path) -> ArtifactWriter:
    return ArtifactWriter(
        root, run_id=_RUN_ID, profile="ci", redactor=default_redactor(), nodeid=_NODEID
    )


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    tree = obs_logging.LOGGER_NAME
    ours = [record for record in caplog.records if record.name.split(".")[0] == tree]
    return [record.getMessage() for record in ours if record.levelno == logging.WARNING]


def test_a_writable_root_creates_the_run_directory_and_reports_capture_on(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    writer = _writer(tmp_path)
    caplog.set_level(logging.WARNING, logger=obs_logging.LOGGER_NAME)
    assert writer.ensure_run_dir() is True
    assert (writer.run_dir, writer.disabled) == ((tmp_path / _RUN_ID).resolve(), False)
    assert (writer.run_dir.is_dir(), _warnings(caplog)) == (True, [])
    # The session asks the writer for the verdict instead of creating the tree itself.
    state = session.create_state(FoundryConfig(), invocation_dir=tmp_path)
    assert state.artifacts_enabled is True
    assert state.artifacts_root == (tmp_path / "artifacts" / state.run_id).resolve()


def test_a_blocked_root_disables_capture_once_and_every_record_reports_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / "artifacts").write_text("a regular file, not a directory\n", encoding="utf-8")
    writer = _writer(tmp_path / "artifacts")
    caplog.set_level(logging.WARNING, logger=obs_logging.LOGGER_NAME)
    assert (writer.ensure_run_dir(), writer.disabled) == (False, True)
    lines = _warnings(caplog)
    assert len(lines) == 1 and _DISABLED_MESSAGE in lines[0] and "component=artifacts" in lines[0]
    # Nothing after the fault reports again: the verdict is the writer's shared fault state.
    assert not writer.dir_for(_NODEID).exists()
    assert writer.write("request", None, '{"cart": []}') is None and _warnings(caplog) == lines
    caplog.clear()
    state = session.create_state(FoundryConfig(), invocation_dir=tmp_path)
    assert (state.artifacts_enabled, state.writer.disabled) == (False, True)
    blocked = _warnings(caplog)
    assert len(blocked) == 1 and _DISABLED_MESSAGE in blocked[0]
    observed = {**_EMPTY, "state": state, "nodeid": _NODEID, "quarantine": _NO_QUARANTINE}
    observed |= {"started_at": state.started_at, "finished_at": state.started_at}
    payloads = [session.assemble_payload(**observed), session.not_run_payload(state, _NODEID)]
    assert [(p.artifacts_disabled, p.artifacts) for p in payloads] == [(True, ()), (True, ())]
    assert _warnings(caplog) == blocked
