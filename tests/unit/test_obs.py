"""Pins the six ``foundry.obs`` modules: the ``foundry`` logger's seams, the bounded capture ring,
the redaction values every sink reads, the ``***`` marker on a written byte, and the WARNING-capped
lifecycle events. The artifact layout, the manifest fields and the capture-fault isolation are
asserted in ``tests/unit/test_obs_artifacts.py``, which imports this module's run id, profile, node
id and sentinels rather than restating them.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from foundry.core.types import ARTIFACT_KINDS
from foundry.obs import artifacts, capture, events, manifest, redaction
from foundry.obs import logging as obs_logging

_RUN_ID = "20250102T030405-abc123"
_PROFILE = "ci"
_NODEID = "tests/test_api.py::test_cart[1]"
_SECRET_SENTINELS = ("hunter2secret", "tok_live_9f3")


def _record(
    level: int = logging.WARNING, message: str = "cart empty", *args: object
) -> logging.LogRecord:
    return logging.LogRecord("foundry.obs", level, __file__, 1, message, args or None, None)


def _writer(root: Path, *, nodeid: str | None = _NODEID) -> artifacts.ArtifactWriter:
    return artifacts.ArtifactWriter(
        root, run_id=_RUN_ID, profile=_PROFILE, redactor=redaction.default_redactor(), nodeid=nodeid
    )


def test_the_logger_seams_touch_only_the_foundry_tree() -> None:
    root, tree = logging.getLogger(), logging.getLogger(obs_logging.LOGGER_NAME)
    # One process is shared, so a configured level must not decide what every later test observes.
    saved = (tree.level, list(tree.handlers), tree.propagate)
    before = (tuple(root.handlers), root.level, logging.Logger.manager.disable)
    try:
        obs_logging.configure("DEBUG")
        assert (tuple(root.handlers), root.level, logging.Logger.manager.disable) == before
        # A child logger holds no level of its own and inherits the one just configured.
        child = obs_logging.get_logger("foundry.obs.artifacts")
        assert (tree.level, child.getEffectiveLevel()) == (logging.DEBUG, logging.DEBUG)
        assert child.level == logging.NOTSET
        # A name that merely begins with the same letters must not land outside the tree.
        assert obs_logging.get_logger("foundrydb").name == "foundry.foundrydb"
        nulls = tuple(h for h in tree.handlers if isinstance(h, logging.NullHandler))
        assert (nulls, tree.propagate) == ((obs_logging.NULL_HANDLER,), True)
        ring = capture.LogRing(capacity=2)
        obs_logging.attach_handler(ring)
        obs_logging.attach_handler(ring)
        assert tree.handlers.count(ring) == 1
        obs_logging.detach_handler(ring)
        obs_logging.detach_handler(ring)
        assert (obs_logging.attach_handler("x"), obs_logging.detach_handler("x")) == (None, None)
        with ring:
            tree.warning("second")
            tree.debug("third")
        assert ring not in tree.handlers
        assert ring.tail(2) == ("WARNING foundry: second", "DEBUG foundry: third")
    finally:
        tree.setLevel(saved[0])
        tree.handlers[:] = saved[1]
        tree.propagate = saved[2]


def test_the_log_ring_keeps_the_newest_records_and_renders_them_without_a_timestamp() -> None:
    ring = capture.LogRing(capacity=3)
    for number in range(5):
        ring.emit(_record(message=f"step {number}"))
    assert (ring.capacity, len(ring.records)) == (3, 3)
    assert ring.tail(2) == ("WARNING foundry.obs: step 3", "WARNING foundry.obs: step 4")
    # A count past the retained total, the helper the ferry uses, and no count at all agree.
    assert ring.tail(3) == tuple(f"WARNING foundry.obs: step {n}" for n in (2, 3, 4))
    assert ring.tail(None) == ring.tail(3) == capture.tail_lines(ring.records, 3)
    ring.clear()
    assert (ring.records, len(ring), ring.tail(0), ring.tail(-1)) == ((), 0, (), ())
    # A captured line is one line, and a lifecycle line carries its prefix once.
    assert capture.format_record(_record(logging.INFO, "one\ntwo")) == "INFO foundry.obs: one | two"
    assert capture.format_record(_record(message="foundry: x")) == "WARNING foundry: x"


@pytest.mark.parametrize(
    ("requested", "expected"),
    [(-5, 0), (0, 0), (3, 3), (10_000, 10_000), (10_001, 10_000), (True, 200)],
)
def test_capacity_is_clamped_to_the_published_bounds(requested: int, expected: int) -> None:
    ring = capture.LogRing(capacity=requested)
    ring.emit(_record())
    assert (ring.capacity, capture.DEFAULT_CAPTURE_LINES) == (expected, 200)
    if expected == 0:  # capacity 0 disables capture: nothing is retained and nothing is rendered
        assert (ring.records, ring.tail(1)) == ((), ())


def test_the_published_capture_and_redaction_values() -> None:
    assert capture.LogRing().capacity == capture.DEFAULT_CAPTURE_LINES == 200
    assert (capture.MIN_CAPTURE_LINES, capture.MAX_CAPTURE_LINES) == (0, 10_000)
    assert (redaction.REDACTED, redaction.MAX_EXCERPT_BYTES) == ("***", 200)
    deny_list = {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key"}
    assert frozenset(deny_list) == redaction.DEFAULT_HEADER_DENY_LIST
    assert len(ARTIFACT_KINDS) == len(set(artifacts.ARTIFACT_FILENAMES.values())) == 7
    assert set(artifacts.ARTIFACT_FILENAMES) == set(ARTIFACT_KINDS)
    assert frozenset({manifest.MANIFEST_FILENAME}) == artifacts.KEPT_WHEN_PASSED


def test_redaction_precedes_serialisation_of_the_written_artifact(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    document = {
        "method": "POST",
        "url": "https://cart-user:hunter2secret@shop.example/p?token=tok_live_9f3",
        "headers": {"Authorization": "Bearer bearer-credential"},
    }
    reference = writer.write_json("request", document)
    assert reference is not None and reference.redacted is True
    written = (writer.run_dir / reference.path).read_text(encoding="utf-8")
    assert [s for s in (*_SECRET_SENTINELS, "bearer-credential", "cart-user") if s in written] == []
    # A foundry-labelled line keeps its label; a host logger's leaves only the masked value.
    secret = _SECRET_SENTINELS[0]
    lines = [f"INFO foundry.qa.secrets: token={secret}", f"INFO mytool.auth: token={secret}"]
    tail = writer.write_log_tail(lines)
    assert tail is not None
    logged = (writer.run_dir / tail.path).read_text(encoding="utf-8")
    assert logged.splitlines() == ["INFO foundry.qa.secrets: token=***", "INFO mytool.auth: ***"]


@pytest.mark.parametrize("capturing", [True, False])
def test_the_session_writer_answers_for_the_test_in_flight(tmp_path: Path, capturing: bool) -> None:
    session = _writer(tmp_path, nodeid=None)
    item = session.for_node(_NODEID)
    artifacts.set_active_writer(item if capturing else None)
    try:
        if not capturing:
            with pytest.raises(artifacts.ArtifactError):
                session.write("stdout", None, "published write")
            with pytest.raises(artifacts.ArtifactError):
                _ = session.manifest
            return
        reference = session.write("stdout", None, "published write")
        assert reference is not None
        written = session.run_dir / reference.path
        assert written.parent == item.dir_for(_NODEID)
        assert written.read_text(encoding="utf-8") == "published write"
        assert session.manifest == item.manifest == (reference,)
    finally:
        artifacts.set_active_writer(None)


def test_every_emitter_stays_at_or_below_warning_and_sorts_its_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=obs_logging.LOGGER_NAME)
    events.emit_run_start(_RUN_ID, _PROFILE, "artifacts")
    events.emit_cassette_mode("replay", _NODEID)
    events.emit_sharding_decision("gw0", 4)
    events.emit_attempt(_NODEID, 2, "passed")
    events.emit_artifact_written(_NODEID, "request", "request.json", 8, "ab" * 32)
    events.emit_report_written("html", "report.html")
    events.emit_plumbing_failure("artifacts", "cannot write the artifact directory")
    events.emit_html_fallback("the report template failed")
    tree = obs_logging.LOGGER_NAME
    levels = [record.levelno for record in caplog.records if record.name == tree]
    # One INFO line for the run, two WARNING lines for the isolation paths, the rest DEBUG.
    assert (len(levels), levels.count(logging.INFO), levels.count(logging.WARNING)) == (8, 1, 2)
    assert max(levels) == events.MAX_EVENT_LEVEL == logging.WARNING
    assert events.event_line("run_start", {"b": 2, "a": 1}) == "foundry: run_start a=1 b=2"
    # A level declared above the band is clamped, so nothing Foundry emits is an ERROR.
    caplog.clear()
    events.log_event(events.LifecycleEvent("plumbing_failure", logging.ERROR, (("c", "a"),)))
    assert events.clamp_level(logging.CRITICAL) == events.MAX_EVENT_LEVEL
    kept = [(r.levelno, r.name) for r in caplog.records]
    assert kept == [(logging.WARNING, obs_logging.LOGGER_NAME)]
