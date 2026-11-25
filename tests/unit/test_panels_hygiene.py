"""The ``foundry.reporting.panels`` behaviours the findings on this surface fix: zero capture reads
and reports nothing, a fault warns once, the phase index is read-only, an unlisted tail is captured,
and no raw control character survives a panel message or a captured log line.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from foundry.obs.redaction import REDACTED
from foundry.reporting import panels
from foundry.reporting.record import FailureRecord, Outcome, ResultRecord

_OLDER_LINE = "the older captured line"
_DENIED_LINE = "x-api-key: " + "sentinel-value"
_STORED_LINE = f"x-api-key: {REDACTED}"
_LONG_LINE = "x" * (panels.MAX_LOG_LINE_CHARS + 1)
_CUT_LINE = _LONG_LINE[: panels.MAX_LOG_LINE_CHARS - 1] + panels._TRUNCATION_SUFFIX


def _failing(*messages: str) -> ResultRecord:
    return ResultRecord.model_validate(
        {
            "run_id": "20250102T030405-abc123",
            "started_at": datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC),
            "finished_at": datetime(2025, 1, 2, 3, 4, 6, tzinfo=UTC),
            "profile": "ci",
            "nodeid": "tests/test_probe.py::test_failing",
            "outcome": Outcome.FAILED,
            "attempts": 1,
            "duration_ms": 4.0,
            "failures": tuple(FailureRecord(phase="call", message=m) for m in messages),
        }
    )


def _panel_message(message: str) -> str:
    (panel,) = panels.build_panels([_failing(message)])
    return panel.message


def test_zero_limit_reads_nothing_and_reports_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    tail = tmp_path / "log_tail.log"
    tail.write_text(f"{_OLDER_LINE}\n{_DENIED_LINE}\n", encoding="utf-8", newline="\n")
    with caplog.at_level(logging.WARNING, logger="foundry"):
        assert panels.read_log_tail(tail, limit=0) == ((), False)
    assert caplog.records == []


def test_genuine_fault_still_warns_once(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    missing = tmp_path / "no.log"
    with caplog.at_level(logging.WARNING, logger="foundry"):
        assert panels.read_log_tail(missing, limit=5) == ((), False)
    assert len(caplog.records) == 1 and str(missing) in caplog.records[0].getMessage()


@pytest.mark.parametrize(
    ("stored", "limit", "expected"),
    [
        ((_OLDER_LINE, _DENIED_LINE), 1, (_DENIED_LINE,)),
        (("bell-\x07-end",), 5, ("bell-\\x07-end",)),
        ((_LONG_LINE,), 5, (_CUT_LINE,)),
        ((_STORED_LINE, _STORED_LINE), 2, (_STORED_LINE, _STORED_LINE)),
    ],
    ids=("newest-lines-only", "control-character-escape", "width-cut", "already-scrubbed"),
)
def test_a_positive_limit_renders_the_newest_lines_as_the_artifact_stored_them(
    tmp_path: Path, stored: tuple[str, ...], limit: int, expected: tuple[str, ...]
) -> None:
    # log_tail.log is the artifact writer's own output: the panel escapes and cuts it, never
    # scrubs it again, so a line the engine already masked comes back byte for byte (AAP 0.4.8).
    tail = tmp_path / "log_tail.log"
    tail.write_text("\n".join(stored) + "\n", encoding="utf-8", newline="\n")
    assert panels.read_log_tail(tail, limit=limit) == (expected, True)


def test_phase_order_index_is_read_only_and_complete() -> None:
    index = panels._PHASE_ORDER_INDEX
    assert not isinstance(index, dict)
    with pytest.raises(TypeError):
        index[panels.PHASE_ORDER[0]] = 0
    assert dict(index) == {member: position for position, member in enumerate(panels.PHASE_ORDER)}


@pytest.mark.parametrize("rooted", [True, False])
def test_an_unlisted_tail_reports_capture_only_with_a_root(tmp_path: Path, rooted: bool) -> None:
    root = tmp_path / "artifacts" if rooted else None
    (panel,) = panels.build_panels(
        [_failing("AssertionError: boom")], artifacts_root=root, report_dir=tmp_path, log_lines=5
    )
    assert (panel.log_available, panel.has_log, panel.log_lines) == (rooted, False, ())


@pytest.mark.parametrize(
    ("prepare", "message", "expected"),
    [
        (_panel_message, "nul-\x00-end", "nul-\\x00-end"),
        (_panel_message, "bel-\x07-end", "bel-\\x07-end"),
        (_panel_message, "esc-\x1b-end", "esc-\\x1b-end"),
        (_panel_message, "del-\x7f-end", "del-\\x7f-end"),
        (_panel_message, "assert 1 == 2\n\tassert 3 == 4", "assert 1 == 2\n\tassert 3 == 4"),
        (
            panels._prepare_line,
            "captured\x07line\twith\ttabs\x7f",
            "captured\\x07line\twith\ttabs\\x7f",
        ),
    ],
    ids=("nul", "bel", "esc", "del", "newline-and-tab", "captured-log-line"),
)
def test_every_control_character_is_spelled_out_and_line_structure_survives(
    prepare: Callable[[str], str], message: str, expected: str
) -> None:
    prepared = prepare(message)
    raw = [c for c in prepared if (ord(c) < 0x20 and c not in "\n\t") or 0x7F <= ord(c) <= 0x9F]
    assert (prepared, raw) == (expected, [])
