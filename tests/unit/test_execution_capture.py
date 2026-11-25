"""Unit tests for the per-test artifact capture of ``foundry.execution.coordinator``.

The capture path is where a test's own output becomes a file, so these cases hold it to the two
documents that describe it: AAP 0.4.6's artifact layout, in which each captured section reaches
``stdout.log`` or ``stderr.log`` exactly once and in phase order and both logs exist even when
nothing was captured, and AAP 0.1.2's failure isolation, in which an unrenderable traceback costs
one ``WARNING``, that one file and nothing else.

The phase reports a case hands the capture path are stand-ins for the three reports pytest builds
for one item, so no case here drives pytest machinery, sleeps, reads the wall clock or opens a
socket (AAP 0.14.3). The fixed run id, profile and node id are imported from
``tests.unit.test_execution`` rather than restated, so the suite describes one run and not two."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

import pytest
from test_execution import _NODEID, _PROFILE, _RUN_ID

from foundry.execution import coordinator, state
from foundry.obs import artifacts, capture
from foundry.obs.redaction import default_redactor

# --- Per-test artifact capture ----------------------------------------------

_PHASES: Final[tuple[str, ...]] = ("setup", "call", "teardown")
"""The three phases of one attempt, in the order pytest appends their report sections."""


class _PhaseReport:
    """The attributes the capture path reads from one of pytest's phase reports.

    ``sections`` is the accumulated ``(title, content)`` list pytest re-sends on every phase's
    report and ``longrepr`` is what a traceback is rendered from; the stand-in keeps both without a
    runtest protocol behind it."""

    def __init__(
        self,
        when: str,
        sections: Sequence[tuple[str, str]],
        *,
        outcome: str = "failed",
        longrepr: object = None,
    ) -> None:
        self.when = when
        self.sections = list(sections)
        self.outcome = outcome
        self.longrepr = longrepr
        self.wasxfail: object = None


class _StashedItem:
    """The one attribute the capture path reads from a ``pytest.Item``: its typed stash."""

    def __init__(self, entries: Mapping[object, object]) -> None:
        self.stash = dict(entries)


class _Unrenderable:
    """A long representation that cannot be rendered: the one fault the traceback path isolates."""

    def __str__(self) -> str:
        raise RuntimeError("this long representation cannot be rendered")


def _phase_reports(
    sections: Sequence[tuple[str, str, str]],
    *,
    longrepr: object = "AssertionError: deliberate failure",
) -> list[_PhaseReport]:
    """Return the setup, call and teardown reports pytest builds from one item's section list.

    pytest appends a non-empty ``(when, key, content)`` triple as each phase runs and re-sends the
    whole accumulated list on that phase's report, so the setup report carries the setup sections,
    the call report the setup and call sections, and the teardown report all of them. Only the call
    report fails, and ``longrepr`` is what it fails with."""
    accumulated: list[tuple[str, str]] = []
    reports: list[_PhaseReport] = []
    for when in _PHASES:
        accumulated.extend(
            (f"Captured {key} {rwhen}", content)
            for rwhen, key, content in sections
            if rwhen == when
        )
        reports.append(
            _PhaseReport(
                when,
                tuple(accumulated),
                outcome="failed" if when == "call" else "passed",
                longrepr=longrepr if when == "call" else None,
            )
        )
    return reports


def _capture_streams(
    tmp_path: Path,
    sections: Sequence[tuple[str, str, str]],
    *,
    longrepr: object = "AssertionError: deliberate failure",
) -> Path:
    """Capture one item's artifacts from stand-in reports and return the directory it wrote.

    The writer is a real one rooted in ``tmp_path`` and bound to :data:`_NODEID`, the log ring is
    a real :class:`foundry.obs.capture.LogRing`, and the stash is a plain dict, so the capture path
    runs as it does for an item with no runtest protocol behind it."""
    writer = artifacts.ArtifactWriter(
        tmp_path, run_id=_RUN_ID, profile=_PROFILE, redactor=default_redactor(), nodeid=_NODEID
    )
    item = _StashedItem({state.ITEM_ARTIFACTS: writer, state.ITEM_LOG_RING: capture.LogRing()})
    coordinator.capture_artifacts(item, _phase_reports(sections, longrepr=longrepr), final=True)
    return writer.dir_for(_NODEID)


@pytest.mark.parametrize(
    ("sections", "expected_stdout", "expected_stderr"),
    [
        pytest.param(
            (
                ("setup", "stdout", "setup line\n"),
                ("call", "stdout", "call line\n"),
                ("teardown", "stdout", "teardown line\n"),
            ),
            "setup line\ncall line\nteardown line\n",
            "",
            id="every-phase-once-in-order",
        ),
        pytest.param(
            (("call", "stdout", "one call line\n"),), "one call line\n", "", id="call-phase-only"
        ),
        pytest.param(
            (
                ("setup", "stderr", "setup complaint\n"),
                ("call", "stdout", "call output\n"),
                ("call", "stderr", "call complaint\n"),
            ),
            "call output\n",
            "setup complaint\ncall complaint\n",
            id="both-streams",
        ),
        pytest.param((), "", "", id="nothing-captured"),
    ],
)
def test_each_captured_section_is_written_once_in_order(
    tmp_path: Path,
    sections: Sequence[tuple[str, str, str]],
    expected_stdout: str,
    expected_stderr: str,
) -> None:
    """AAP 0.4.6: each captured section lands in its own log exactly once, and both logs exist."""
    directory = _capture_streams(tmp_path, sections)
    assert (directory / "stdout.log").read_text(encoding="utf-8") == expected_stdout
    assert (directory / "stderr.log").read_text(encoding="utf-8") == expected_stderr


def test_an_unrenderable_traceback_is_one_warning_and_costs_only_that_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """AAP 0.1.2: an unrenderable traceback costs that file, one WARNING and nothing else."""
    caplog.set_level(logging.WARNING, logger="foundry")
    directory = _capture_streams(
        tmp_path, (("call", "stdout", "call line\n"),), longrepr=_Unrenderable()
    )
    assert not (directory / "traceback.txt").exists(), "an unrenderable traceback left a file"
    assert (directory / "stdout.log").read_text(encoding="utf-8") == "call line\n"
    assert (directory / "manifest.json").is_file(), "the fault skipped the manifest as well"
    warnings = [
        record.getMessage() for record in caplog.records if record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1, warnings
    assert "plumbing_failure" in warnings[0]
    assert "component=execution.coordinator" in warnings[0] and "error=RuntimeError" in warnings[0]
