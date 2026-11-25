"""Pins the six ``foundry.obs`` modules: the ``foundry`` logger's seams, the bounded capture ring,
the redaction values every sink reads, the ``***`` marker on a written byte, the WARNING-capped
lifecycle events, and the artifact writer with its per-test manifest, which writes the layout AAP
0.4.6 publishes and disables capture with one ``WARNING`` and no exception (AAP 0.4.7). Redaction is
asserted on the bytes written, because a writer that redacts nothing passes the object graph and
leaks the file (AAP 0.3.2, 0.14.3, I4). Every input lives in ``tmp_path`` and log capture reads
pytest's ``caplog``.
"""

from __future__ import annotations

import hashlib
import json
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
# The fixed creation time the manifest cases stamp their document with, so nothing reads the clock.
_CREATED_AT = "2025-01-02T03:04:05.678+00:00"


def _record(
    level: int = logging.WARNING, message: str = "cart empty", *args: object
) -> logging.LogRecord:
    return logging.LogRecord("foundry.obs", level, __file__, 1, message, args or None, None)


def _writer(root: Path, *, nodeid: str | None = _NODEID) -> artifacts.ArtifactWriter:
    return artifacts.ArtifactWriter(
        root, run_id=_RUN_ID, profile=_PROFILE, redactor=redaction.default_redactor(), nodeid=nodeid
    )


def _document(entries: tuple[manifest.ManifestEntry, ...] = ()) -> manifest.ManifestDocument:
    # A manifest document for this module's fixed run, profile and creation time.
    return manifest.build_document(
        run_id=_RUN_ID,
        nodeid=_NODEID,
        profile=_PROFILE,
        created_at=_CREATED_AT,
        artifacts_disabled=False,
        artifacts=entries,
    )


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    # The foundry tree's own WARNING lines: a host logger's warning is not Foundry's fault line.
    root = obs_logging.LOGGER_NAME
    return [
        r for r in caplog.records if r.levelno == logging.WARNING and r.name.split(".")[0] == root
    ]


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
    assert written.count(redaction.REDACTED) == 2
    assert json.loads(written)["method"] == "POST"
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


def test_the_manifest_publishes_its_fields_digests_and_refuses_an_escape() -> None:
    document = _document()
    assert (manifest.MANIFEST_SCHEMA_VERSION, manifest.MANIFEST_FILENAME) == (1, "manifest.json")
    assert tuple(document.as_dict()) == (
        "schema_version",
        "run_id",
        "nodeid",
        "profile",
        "created_at",
        "artifacts_disabled",
        "artifacts",
    )
    assert document.as_dict()["nodeid"] == _NODEID
    assert document.as_dict()["artifacts_disabled"] is False
    # One entry per artifact file: its size, and the lowercase digest of the bytes on disk.
    payload = b'{"cart": []}\n'
    entry = manifest.entry_for("request", "node/request.json", payload, redacted=True)
    assert (entry.kind, entry.file) == ("request", "node/request.json")
    assert (entry.bytes, entry.redacted) == (len(payload), True)
    assert entry.sha256 == hashlib.sha256(payload).hexdigest() == entry.sha256.lower()
    assert tuple(entry.as_dict()) == ("kind", "file", "bytes", "sha256", "redacted")
    # Entries are ordered by kind, and the rendered document is stable from one call to the next.
    stdout = manifest.entry_for("stdout", "node/stdout.log", b"out\n", redacted=False)
    rendered = manifest.render(_document((stdout, entry)))
    assert manifest.render(_document((stdout, entry))) == rendered
    assert rendered.endswith("\n") and not rendered.endswith("\n\n")
    assert [row["kind"] for row in json.loads(rendered)["artifacts"]] == ["request", "stdout"]
    escaping = manifest.ManifestEntry(
        kind="request", file="../elsewhere/request.json", bytes=0, sha256="0" * 64, redacted=False
    )
    with pytest.raises(manifest.ArtifactError):
        _document((escaping,))


def test_every_artifact_kind_has_one_published_file_name() -> None:
    assert len(ARTIFACT_KINDS) == 7
    assert set(artifacts.ARTIFACT_FILENAMES) == set(ARTIFACT_KINDS)
    assert artifacts.ARTIFACT_FILENAMES["log_tail"] == "log_tail.log"
    assert len(set(artifacts.ARTIFACT_FILENAMES.values())) == len(ARTIFACT_KINDS)
    assert frozenset({manifest.MANIFEST_FILENAME}) == artifacts.KEPT_WHEN_PASSED


@pytest.mark.parametrize(
    "nodeid",
    [
        "tests/test_api.py::test_cart[1]",
        "test_secrets.py::test_alpha",
        "tests/test_api_key_flow.py::test_login",
    ],
)
def test_dir_for_sanitises_a_node_id_into_one_unredacted_component(
    tmp_path: Path, nodeid: str
) -> None:
    """Percent-encoded as pytest reports it, and never redacted into another test's directory."""
    writer = _writer(tmp_path)
    directory = writer.dir_for(nodeid)
    assert directory.is_dir() and directory.is_relative_to(tmp_path / _RUN_ID)
    assert directory == artifacts.node_dir(writer.run_dir, nodeid)
    assert directory.name.count("%2F") == nodeid.count("/")
    assert "%3A" in directory.name and "%2A" not in directory.name
    for separator in ("/", "\\", "::", "[", "]"):
        assert separator not in directory.name
    assert writer.dir_for(nodeid.replace("::test_", "::test_second_")).name != directory.name


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("request", "request.json"),
        ("response", "response.json"),
        ("stdout", "stdout.log"),
        ("stderr", "stderr.log"),
        ("traceback", "traceback.txt"),
    ],
)
def test_write_records_a_relative_reference_to_the_published_file_name(
    tmp_path: Path, kind: str, expected: str
) -> None:
    writer = _writer(tmp_path)
    payload = '{"cart": []}\n'
    reference = writer.write_text(kind, payload)  # type: ignore[arg-type]
    assert reference is not None and reference.redacted is False
    assert (reference.kind, Path(reference.path).name) == (kind, expected)
    assert (reference.bytes, reference.sha256) == (
        len(payload.encode("utf-8")),
        hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    )
    assert not Path(reference.path).is_absolute() and ".." not in Path(reference.path).parts
    assert (writer.run_dir / reference.path).read_text(encoding="utf-8") == payload


def test_the_log_tail_keeps_a_foundry_records_label(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    secret = _SECRET_SENTINELS[0]
    lines = [f"INFO foundry.qa.secrets: token={secret}", f"INFO mytool.auth: token={secret}"]
    tail = writer.write_log_tail(lines)
    assert tail is not None and Path(tail.path).name == "log_tail.log"
    written = (writer.run_dir / tail.path).read_text(encoding="utf-8")
    assert written.splitlines() == ["INFO foundry.qa.secrets: token=***", "INFO mytool.auth: ***"]
    # One line per captured record, and an empty tail writes no file at all.
    assert writer.write_log_tail([]) is None


def test_the_manifest_lists_one_entry_per_written_file(tmp_path: Path) -> None:
    # A secret-shaped node id names its own directory and is published unsanitised.
    writer = _writer(tmp_path, nodeid="test_secrets.py::test_alpha")
    request = writer.write("request", None, '{"cart": []}')
    stdout = writer.write_text("stdout", "picked 2 items\n")
    assert request is not None and stdout is not None
    assert {reference.path for reference in writer.manifest} == {request.path, stdout.path}
    document = writer.write_manifest()
    assert document is not None and Path(document.path).name == manifest.MANIFEST_FILENAME
    payload = json.loads((writer.run_dir / document.path).read_text(encoding="utf-8"))
    assert (payload["schema_version"], payload["nodeid"], payload["artifacts_disabled"]) == (
        manifest.MANIFEST_SCHEMA_VERSION,
        "test_secrets.py::test_alpha",
        False,
    )
    assert [entry["file"] for entry in payload["artifacts"]] == sorted((request.path, stdout.path))
    for entry in payload["artifacts"]:
        written = writer.run_dir / entry["file"]
        assert (entry["bytes"], entry["sha256"]) == (
            written.stat().st_size,
            hashlib.sha256(written.read_bytes()).hexdigest(),
        )
    # Finishing a passing test prunes every artifact but the manifest it just wrote.
    kept = writer.finalize(passed=True)
    assert [Path(reference.path).name for reference in kept] == [manifest.MANIFEST_FILENAME]
    assert [path.name for path in writer.dir_for("test_secrets.py::test_alpha").iterdir()] == [
        manifest.MANIFEST_FILENAME
    ]


def test_an_uncreatable_run_directory_disables_capture_with_one_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "artifacts"
    root.write_text("a regular file where the artifacts root should be\n", encoding="utf-8")
    writer = _writer(root)
    caplog.set_level(logging.WARNING, logger=obs_logging.LOGGER_NAME)
    assert not writer.dir_for(_NODEID).exists()
    assert writer.disabled is True and writer.manifest == ()
    # A writer derived for one test shares the run's fault state, which is the plugin's path.
    assert writer.for_node(_NODEID).disabled is True
    assert writer.write("request", None, '{"cart": []}') is None
    assert writer.write_json("response", {"status": 200}) is None
    assert writer.write_text("stdout", "out\n") is None
    assert writer.write_log_tail(["INFO foundry: x"]) is None
    assert writer.write_manifest() is None
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert "cannot create artifact directory" in warnings[0].getMessage()


def test_a_failed_write_disables_capture_and_the_manifest_records_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    writer = _writer(tmp_path)
    request = writer.write("request", None, '{"cart": []}')
    assert request is not None
    blocker = writer.run_dir / Path(request.path).parent / artifacts.ARTIFACT_FILENAMES["response"]
    blocker.mkdir()
    caplog.set_level(logging.WARNING, logger=obs_logging.LOGGER_NAME)
    assert writer.write_json("response", {"status": 500}) is None
    assert writer.disabled is True
    document = writer.write_manifest()
    assert document is not None
    payload = json.loads((writer.run_dir / document.path).read_text(encoding="utf-8"))
    assert payload["artifacts_disabled"] is True
    assert [entry["kind"] for entry in payload["artifacts"]] == ["request"]
    assert writer.write_text("stdout", "after the fault\n") is None
    assert len(_warnings(caplog)) == 1
