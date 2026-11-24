"""Unit tests for the ``foundry.obs`` foundation package.

Six modules, each pinned to the contract it owns (AAP 0.2.1, 0.3.2, 0.4.6, 0.4.7, 0.4.8, 0.13.1,
0.14.3):

* ``logging`` owns the ``foundry`` logger and nothing else: ``configure`` moves that logger's level
  and leaves the root logger's handlers, its level and the manager-wide ``disable`` flag untouched,
  because a library that reconfigures its host breaks every other library in the process.
* ``capture`` keeps a bounded ring of the newest records and returns the *last* ``n`` in order,
  ``tail(0)`` being the empty tuple rather than everything (implicit requirement I7).
* ``redaction`` is the one engine every sink calls before it writes, so the assertions inspect the
  **serialised** form -- ``json.dumps`` of a redacted mapping and the bytes of a written artifact --
  because a test that only walks the object graph passes while the writer leaks.
* ``manifest`` publishes the field set, the size and the digest of every artifact file, and
  ``artifacts`` writes the per-test layout, where a directory that cannot be created disables
  capture with one ``WARNING`` and no exception: the AAP 0.4.7 state row, proven at unit level.
* ``events`` emits nothing above ``WARNING`` -- the single ``INFO`` line of a run, the ``DEBUG``
  detail lines and the two ``WARNING`` failure-isolation lines -- with free text scrubbed.

Three expectations differ from the brief this file was written against, the modules on disk being
authoritative: ``manifest.render`` serialises in the published field order rather than with
``sort_keys=True``, its stability coming from the frozen dataclass and the ordered ``artifacts``
tuple; ``write_manifest`` returns ``None`` when the run directory itself cannot be created, so
``artifacts_disabled: true`` is read from the second scenario's manifest, a write that fails *after*
the directory exists; and ``LogRing.records`` holds records while ``tail(n)`` formats them.

Determinism and isolation (AAP 0.14.3): inputs live in ``tmp_path``, the loggers this module changes
are restored by the ``restore_foundry_logger`` fixture, nothing sleeps and no wall-clock value is
asserted, and no pytest machinery, subprocess, network call or CLI is driven here -- the pytester,
subprocess and ``CliRunner`` surfaces of AAP 0.14.5 are the business of ``tests/plugin`` and
``tests/e2e``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from foundry.core.types import ARTIFACT_KINDS
from foundry.obs import artifacts, capture, events, manifest, redaction
from foundry.obs import logging as obs_logging

# A run id of the shape AAP 0.4.3 publishes, a profile from AAP 0.4.5's four, a realistic node id
# carrying "/", "::" and "[", and a fixed `created_at` so no assertion reads the wall clock.
_RUN_ID = "20250102T030405-abc123"
_PROFILE = "ci"
_NODEID = "tests/test_api.py::test_cart[1]"
_CREATED_AT = "2025-01-02T03:04:05.678+00:00"

# Values no serialised form may contain: distinctive enough that a match is a real leak.
_SECRET_SENTINELS = ("hunter2secret", "tok_live_9f3")

# One spelling per entry of the AAP 0.3.2 header deny-list, mixing case on purpose.
_DENY_LISTED_HEADERS: tuple[tuple[str, str], ...] = (
    ("authorization", "Bearer bearer-credential"),
    ("Proxy-Authorization", "Basic proxy-credential"),
    ("COOKIE", "sid=cookie-credential"),
    ("Set-Cookie", "sid=cookie-credential; HttpOnly"),
    ("X-API-Key", "api-key-credential"),
)


# --- Helpers and fixtures ---------------------------------------------------


def _record(
    level: int = logging.WARNING, message: str = "cart empty", *args: object
) -> logging.LogRecord:
    """Build a log record without a logger, so formatting can be asserted on its own."""
    return logging.LogRecord("foundry.obs", level, __file__, 1, message, args or None, None)


def _writer(root: Path, *, nodeid: str | None = _NODEID) -> artifacts.ArtifactWriter:
    """Return an artifact writer rooted at ``root`` and bound to one test as ``nodeid`` names it."""
    return artifacts.ArtifactWriter(
        root,
        run_id=_RUN_ID,
        profile=_PROFILE,
        redactor=redaction.default_redactor(),
        nodeid=nodeid,
    )


def _document(entries: tuple[manifest.ManifestEntry, ...] = ()) -> manifest.ManifestDocument:
    """Return a manifest document for this module's fixed run, profile and creation time."""
    return manifest.build_document(
        run_id=_RUN_ID,
        nodeid=_NODEID,
        profile=_PROFILE,
        created_at=_CREATED_AT,
        artifacts_disabled=False,
        artifacts=entries,
    )


def _foundry_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Return the records the ``foundry`` logger tree emitted during the current test."""
    prefix = obs_logging.LOGGER_NAME + "."
    return [
        record
        for record in caplog.records
        if record.name == obs_logging.LOGGER_NAME or record.name.startswith(prefix)
    ]


@pytest.fixture
def restore_foundry_logger() -> Iterator[None]:
    """Restore the ``foundry`` logger's level, handlers and propagation after the test.

    The suite shares one process, so a test that configures the level or attaches a ring would
    otherwise decide what every later test observes (AAP 0.4.8: one owner per piece of state).
    """
    logger = logging.getLogger(obs_logging.LOGGER_NAME)
    level = logger.level
    handlers = list(logger.handlers)
    propagate = logger.propagate
    try:
        yield
    finally:
        logger.setLevel(level)
        logger.handlers[:] = handlers
        logger.propagate = propagate


# --- Logger ownership (AAP 0.2.1, AAP 0.4.8) --------------------------------


def test_configure_moves_the_foundry_level_and_leaves_root_untouched(
    restore_foundry_logger: None,
) -> None:
    root = logging.getLogger()
    handlers_before = tuple(root.handlers)
    level_before = root.level
    disable_before = logging.Logger.manager.disable

    obs_logging.configure("DEBUG")

    assert tuple(root.handlers) == handlers_before
    assert root.level == level_before
    assert logging.Logger.manager.disable == disable_before
    added = [handler for handler in root.handlers if handler not in handlers_before]
    assert not [handler for handler in added if isinstance(handler, logging.StreamHandler)]
    assert logging.getLogger(obs_logging.LOGGER_NAME).level == logging.DEBUG
    assert obs_logging.level_for("DEBUG") == logging.DEBUG
    # A child logger holds no level of its own and inherits the one just configured.
    child = obs_logging.get_logger("foundry.obs.artifacts")
    assert child.level == logging.NOTSET
    assert child.getEffectiveLevel() == logging.DEBUG


def test_get_logger_names_the_foundry_tree_and_its_one_null_handler() -> None:
    logger = logging.getLogger(obs_logging.LOGGER_NAME)
    null_handlers = tuple(
        handler for handler in logger.handlers if isinstance(handler, logging.NullHandler)
    )

    assert obs_logging.LOGGER_NAME == "foundry"
    assert obs_logging.get_logger().name == "foundry"
    assert obs_logging.get_logger("").name == "foundry"
    assert obs_logging.get_logger("x").name == "foundry.x"
    assert obs_logging.get_logger("foundry.obs.capture").name == "foundry.obs.capture"
    # A name that merely begins with the same letters must not land outside the tree.
    assert obs_logging.get_logger("foundrydb").name == "foundry.foundrydb"
    # Import attached exactly one NullHandler, and propagation stays on for pytest's capture.
    assert null_handlers == (obs_logging.NULL_HANDLER,)
    assert logger.propagate is True


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        ("DEBUG", logging.DEBUG),
        ("warning", logging.WARNING),
        (" Error ", logging.ERROR),
        (logging.CRITICAL, logging.CRITICAL),
        ("not-a-level", logging.INFO),
        (True, logging.INFO),
        (None, logging.INFO),
    ],
)
def test_level_for_resolves_names_and_passes_integers_through(
    declared: object, expected: int
) -> None:
    assert obs_logging.level_for(declared) == expected  # type: ignore[arg-type]


def test_attach_and_detach_handler_change_the_handler_list_once(
    restore_foundry_logger: None,
) -> None:
    ring = capture.LogRing(capacity=2)
    logger = logging.getLogger(obs_logging.LOGGER_NAME)

    obs_logging.attach_handler(ring)
    obs_logging.attach_handler(ring)
    assert logger.handlers.count(ring) == 1

    obs_logging.detach_handler(ring)
    obs_logging.detach_handler(ring)
    assert ring not in logger.handlers

    assert obs_logging.attach_handler("not a handler") is None  # type: ignore[arg-type]
    assert obs_logging.detach_handler("not a handler") is None  # type: ignore[arg-type]


# --- Log ring (implicit requirement I7, AAP 0.4.6) --------------------------


def _filled_ring(capacity: int, messages: tuple[str, ...]) -> capture.LogRing:
    """Return a ring that captured one ``WARNING`` record per message, oldest first."""
    ring = capture.LogRing(capacity=capacity)
    for message in messages:
        ring.emit(_record(message=message))
    return ring


def test_ring_retains_the_newest_records_and_tail_returns_the_last_ones_in_order() -> None:
    ring = _filled_ring(3, ("step 0", "step 1", "step 2", "step 3", "step 4"))

    assert ring.capacity == 3
    assert len(ring.records) == 3
    assert ring.tail(2) == ("WARNING foundry.obs: step 3", "WARNING foundry.obs: step 4")
    assert ring.tail(3) == (
        "WARNING foundry.obs: step 2",
        "WARNING foundry.obs: step 3",
        "WARNING foundry.obs: step 4",
    )
    # A count past the retained total yields every record, exactly as ``None`` does.
    assert ring.tail(None) == ring.tail(10**6) == ring.tail(3)
    # The shared helper the ``user_properties`` ferry uses renders the same last n.
    assert capture.tail_lines(ring.records, 2) == ring.tail(2)
    assert capture.tail_lines(ring.records, 0) == ()
    ring.clear()
    assert ring.records == ()
    assert len(ring) == 0
    assert ring.tail(5) == ()


def test_tail_of_zero_and_of_a_negative_count_is_the_empty_tuple_not_everything() -> None:
    ring = _filled_ring(3, ("only message",))

    assert ring.tail(0) == ()
    assert isinstance(ring.tail(0), tuple)
    assert ring.tail(-1) == ()


def test_the_default_capacity_is_the_published_capture_lines_default() -> None:
    assert capture.DEFAULT_CAPTURE_LINES == 200
    assert (capture.MIN_CAPTURE_LINES, capture.MAX_CAPTURE_LINES) == (0, 10_000)
    assert capture.LogRing().capacity == capture.DEFAULT_CAPTURE_LINES


@pytest.mark.parametrize(
    ("requested", "expected"),
    [(-5, 0), (0, 0), (3, 3), (10_000, 10_000), (10_001, 10_000), (True, 200)],
)
def test_capacity_is_clamped_to_the_published_bounds(requested: int, expected: int) -> None:
    ring = capture.LogRing(capacity=requested)

    ring.emit(_record())

    assert ring.capacity == expected
    if expected == 0:
        # Capacity 0 disables capture: nothing is retained and nothing is rendered.
        assert ring.records == ()
        assert ring.tail(1) == ()


def test_a_record_is_formatted_without_a_timestamp_and_identically_every_time() -> None:
    earlier = _record(logging.WARNING, "cart %s", "empty")
    later = _record(logging.WARNING, "cart %s", "empty")
    earlier.created, earlier.msecs = 0.0, 0.0
    later.created, later.msecs = 1_760_000_000.0, 500.0

    assert capture.format_record(earlier) == "WARNING foundry.obs: cart empty"
    assert capture.format_record(later) == capture.format_record(earlier)
    collapsed = capture.format_record(_record(logging.INFO, "one\ntwo\r\nthree"))
    assert "\n" not in collapsed
    assert "\r" not in collapsed
    assert collapsed == "INFO foundry.obs: one | two | three"


def test_an_attached_ring_captures_what_the_foundry_logger_emits(
    restore_foundry_logger: None,
) -> None:
    obs_logging.configure("DEBUG")
    ring = capture.LogRing(capacity=2)

    with ring:
        obs_logging.get_logger().debug("first")
        obs_logging.get_logger().warning("second")
        obs_logging.get_logger().debug("third")

    assert ring not in logging.getLogger(obs_logging.LOGGER_NAME).handlers
    assert ring.tail(2) == ("WARNING foundry: second", "DEBUG foundry: third")


# --- Redaction (AAP 0.3.2, AAP 0.14.3, implicit requirement I4) -------------


def test_the_published_header_deny_list_holds_the_secret_header_set() -> None:
    published = frozenset(
        {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key"}
    )
    redactor = redaction.Redactor()

    assert redaction.REDACTED == "***"
    assert published == redaction.DEFAULT_HEADER_DENY_LIST
    assert redaction.MAX_EXCERPT_BYTES == 200
    assert all(redactor.redacts(name) for name in published)
    # The engine is immutable, so no sink can narrow the deny-list it was handed.
    with pytest.raises(dataclasses.FrozenInstanceError):
        redactor._keys = frozenset()


def test_every_deny_listed_header_is_redacted_case_insensitively() -> None:
    supplied = dict(_DENY_LISTED_HEADERS)
    supplied["Content-Type"] = "application/json"
    supplied["X-Request-Id"] = "req-1"

    headers = redaction.Redactor().headers(supplied)

    for name, _value in _DENY_LISTED_HEADERS:
        assert headers[name] == redaction.REDACTED
    assert headers["Content-Type"] == "application/json"
    assert headers["X-Request-Id"] == "req-1"
    serialised = json.dumps(headers)
    for _name, value in _DENY_LISTED_HEADERS:
        assert value not in serialised
    assert serialised.count(redaction.REDACTED) == len(_DENY_LISTED_HEADERS)


def test_configured_keys_widen_the_deny_list_at_any_depth() -> None:
    redactor = redaction.Redactor(["tenant-token", "x-custom-secret"])
    payload = {
        "token": "tok-abc",
        "nested": {
            "X-Custom-Secret": "custom-value",
            "note": "authorization is required",
            "items": [{"tenant-token": "tenant-1"}],
        },
        "items": [{"Authorization": "Bearer bearer-credential", "id": 7}],
    }

    redacted = redactor.mapping(payload)
    serialised = json.dumps(redacted)

    for sentinel in ("tok-abc", "custom-value", "tenant-1", "bearer-credential"):
        assert sentinel not in serialised
    assert serialised.count(redaction.REDACTED) == 4
    assert redacted["nested"] == {
        "X-Custom-Secret": "***",
        "note": "authorization is required",
        "items": [{"tenant-token": "***"}],
    }
    assert redacted["items"] == [{"Authorization": "***", "id": 7}]


def test_url_redaction_drops_userinfo_and_secret_query_parameters() -> None:
    redactor = redaction.Redactor()

    redacted = redactor.url(
        "https://cart-user:hunter2secret@shop.example/p/checkout"
        "?access_token=tok_live_9f3&page=2&api_key=api-key-credential"
    )

    assert redacted.startswith("https://shop.example/p/checkout?")
    assert "cart-user" not in redacted
    for sentinel in (*_SECRET_SENTINELS, "api-key-credential"):
        assert sentinel not in redacted
    assert "page=2" in redacted
    assert redacted.count(redaction.REDACTED) == 2
    # A reference carrying no scheme is scrubbed by the free-text pass, not the URL parser.
    assert redactor.url("/p/checkout?token=tok_live_9f3&page=2") == "/p/checkout?token=***&page=2"
    params = redaction.Redactor(["session-id"]).query(
        {"token": "tok_live_9f3", "session-id": "session-1", "page": "2"}
    )
    assert params == {"page": "2", "session-id": "***", "token": "***"}


def test_text_and_body_excerpt_truncate_at_the_published_byte_budget() -> None:
    redactor = redaction.Redactor()
    payload = "cart line " * 100

    excerpt = redactor.body_excerpt(payload)

    assert excerpt.endswith(redaction.TRUNCATION_MARKER)
    clipped = excerpt[: -len(redaction.TRUNCATION_MARKER)]
    assert clipped == payload[: redaction.MAX_EXCERPT_BYTES]
    assert payload.startswith(clipped)
    assert redactor.text(payload) == payload
    assert redactor.text(payload, limit=redaction.MAX_EXCERPT_BYTES) == excerpt
    assert redactor.body_excerpt(payload.encode("utf-8")) == excerpt
    assert redactor.body_excerpt("short body") == "short body"


def test_redaction_precedes_serialisation_of_the_written_artifact(tmp_path: Path) -> None:
    writer = _writer(tmp_path)

    reference = writer.write_json(
        "request",
        {
            "method": "POST",
            "url": "https://cart-user:hunter2secret@shop.example/p?token=tok_live_9f3",
            "headers": {"Authorization": "Bearer bearer-credential"},
        },
    )

    assert reference is not None
    assert reference.redacted is True
    written = (writer.run_dir / reference.path).read_text(encoding="utf-8")
    for sentinel in (*_SECRET_SENTINELS, "bearer-credential", "cart-user"):
        assert sentinel not in written
    assert written.count(redaction.REDACTED) == 2
    assert json.loads(written)["method"] == "POST"


# --- Manifest (AAP 0.4.6) ---------------------------------------------------


def test_the_manifest_document_carries_the_published_field_set() -> None:
    document = _document()

    assert manifest.MANIFEST_SCHEMA_VERSION == 1
    assert manifest.MANIFEST_FILENAME == "manifest.json"
    assert document.schema_version == manifest.MANIFEST_SCHEMA_VERSION
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
    assert entry.bytes == len(payload)
    assert entry.sha256 == hashlib.sha256(payload).hexdigest() == entry.sha256.lower()
    assert entry.redacted is True
    assert tuple(entry.as_dict()) == ("kind", "file", "bytes", "sha256", "redacted")


def test_every_artifact_kind_has_one_published_file_name() -> None:
    assert len(ARTIFACT_KINDS) == 7
    assert set(artifacts.ARTIFACT_FILENAMES) == set(ARTIFACT_KINDS)
    assert artifacts.ARTIFACT_FILENAMES["log_tail"] == "log_tail.log"
    assert len(set(artifacts.ARTIFACT_FILENAMES.values())) == len(ARTIFACT_KINDS)
    assert frozenset({manifest.MANIFEST_FILENAME}) == artifacts.KEPT_WHEN_PASSED


def test_a_rendered_manifest_is_valid_json_ordered_and_stable() -> None:
    entries = (
        manifest.entry_for("stdout", "node/stdout.log", b"out\n", redacted=False),
        manifest.entry_for("request", "node/request.json", b"{}\n", redacted=False),
    )
    document = _document(entries)

    first = manifest.render(document)

    assert manifest.render(document) == first
    assert first.endswith("\n")
    assert not first.endswith("\n\n")
    payload = json.loads(first)
    assert [entry["kind"] for entry in payload["artifacts"]] == ["request", "stdout"]


def test_a_manifest_path_outside_the_run_directory_is_rejected() -> None:
    escaping = manifest.ManifestEntry(
        kind="request", file="../elsewhere/request.json", bytes=0, sha256="0" * 64, redacted=False
    )

    with pytest.raises(manifest.ArtifactError):
        _document((escaping,))


# --- Artifact writer (AAP 0.4.6, AAP 0.4.7) ---------------------------------


def test_dir_for_sanitises_the_node_id_into_one_path_component(tmp_path: Path) -> None:
    writer = _writer(tmp_path)

    directory = writer.dir_for(_NODEID)

    assert directory.is_dir()
    assert directory.is_relative_to(tmp_path / _RUN_ID)
    assert directory == artifacts.node_dir(writer.run_dir, _NODEID)
    assert "%2F" in directory.name
    assert "%3A" in directory.name
    for separator in ("/", "\\", "::", "[", "]"):
        assert separator not in directory.name


def test_write_records_a_reference_to_the_bytes_on_disk(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    payload = '{"cart": []}\n'

    reference = writer.write("request", None, payload)

    assert reference is not None
    assert reference.kind == "request"
    assert reference.redacted is False
    assert reference.bytes == len(payload.encode("utf-8"))
    assert reference.sha256 == hashlib.sha256(payload.encode("utf-8")).hexdigest()
    assert (writer.run_dir / reference.path).read_bytes() == payload.encode("utf-8")
    assert not Path(reference.path).is_absolute()
    assert ".." not in Path(reference.path).parts
    # The log tail is one line per captured record, and an empty tail writes nothing at all.
    tail = writer.write_log_tail(["WARNING foundry: one", "INFO foundry: two"])
    assert tail is not None
    assert Path(tail.path).name == "log_tail.log"
    written = (writer.run_dir / tail.path).read_text(encoding="utf-8")
    assert written.splitlines() == ["WARNING foundry: one", "INFO foundry: two"]
    assert writer.write_log_tail([]) is None


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
def test_write_uses_the_published_file_name_of_each_kind(
    tmp_path: Path, kind: str, expected: str
) -> None:
    writer = _writer(tmp_path)

    reference = writer.write_text(kind, "payload\n")  # type: ignore[arg-type]

    assert reference is not None
    assert Path(reference.path).name == expected
    assert (writer.run_dir / reference.path).read_text(encoding="utf-8") == "payload\n"


def test_the_manifest_lists_one_entry_per_written_file(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    request = writer.write("request", None, '{"cart": []}')
    stdout = writer.write_text("stdout", "picked 2 items\n")
    assert request is not None
    assert stdout is not None
    assert {reference.path for reference in writer.manifest} == {request.path, stdout.path}

    document = writer.write_manifest()

    assert document is not None
    assert Path(document.path).name == manifest.MANIFEST_FILENAME
    payload = json.loads((writer.run_dir / document.path).read_text(encoding="utf-8"))
    assert payload["schema_version"] == manifest.MANIFEST_SCHEMA_VERSION
    assert payload["artifacts_disabled"] is False
    assert [entry["file"] for entry in payload["artifacts"]] == sorted((request.path, stdout.path))
    for entry in payload["artifacts"]:
        written = writer.run_dir / entry["file"]
        assert entry["bytes"] == written.stat().st_size
        assert entry["sha256"] == hashlib.sha256(written.read_bytes()).hexdigest()
    # Finishing a passing test prunes every artifact but the manifest it just wrote.
    kept = writer.finalize(passed=True)
    assert [Path(reference.path).name for reference in kept] == [manifest.MANIFEST_FILENAME]
    assert sorted(path.name for path in writer.dir_for(_NODEID).iterdir()) == [
        manifest.MANIFEST_FILENAME
    ]


def test_an_uncreatable_run_directory_disables_capture_with_one_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "artifacts"
    root.write_text("a regular file where the artifacts root should be\n", encoding="utf-8")
    writer = _writer(root)
    caplog.set_level(logging.WARNING, logger=obs_logging.LOGGER_NAME)

    directory = writer.dir_for(_NODEID)

    assert not directory.exists()
    assert writer.disabled is True
    assert writer.manifest == ()
    # A writer derived for one test shares the run's fault state, which is the plugin's path.
    assert writer.for_node(_NODEID).disabled is True
    assert writer.write("request", None, '{"cart": []}') is None
    assert writer.write_json("response", {"status": 200}) is None
    assert writer.write_text("stdout", "out\n") is None
    assert writer.write_log_tail(["INFO foundry: x"]) is None
    assert writer.write_manifest() is None
    warnings = [record for record in _foundry_records(caplog) if record.levelno == logging.WARNING]
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
    warnings = [record for record in _foundry_records(caplog) if record.levelno == logging.WARNING]
    assert len(warnings) == 1


# --- Lifecycle events (AAP 0.13.1) -----------------------------------------


def test_every_emitter_stays_at_or_below_warning(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger=obs_logging.LOGGER_NAME)

    events.emit_run_start(_RUN_ID, _PROFILE, "artifacts")
    events.emit_resolution("http.timeout", "env", "FOUNDRY_HTTP__TIMEOUT")
    events.emit_cassette_mode("replay", _NODEID)
    events.emit_sharding_decision("gw0", 4)
    events.emit_attempt(_NODEID, 2, "passed")
    events.emit_artifact_written(_NODEID, "request", "request.json", 8, "ab" * 32)
    events.emit_report_written("html", "report.html")
    events.emit_plumbing_failure("artifacts", "cannot write the artifact directory")
    events.emit_html_fallback("the report template failed")

    records = _foundry_records(caplog)
    assert len(records) == 9
    assert max(record.levelno for record in records) == events.MAX_EVENT_LEVEL == logging.WARNING
    levels = [record.levelno for record in records]
    # One INFO line for the run, two WARNING lines for the isolation paths, the rest DEBUG.
    assert (levels.count(logging.INFO), levels.count(logging.WARNING)) == (1, 2)
    assert records[0].getMessage().startswith("foundry: run_start")
    assert [record for record in records if record.levelno >= logging.ERROR] == []

    # A level declared above the band is clamped, so nothing Foundry emits is an ERROR.
    caplog.clear()
    declared = events.LifecycleEvent(
        name="plumbing_failure", level=logging.ERROR, fields=(("component", "artifacts"),)
    )
    events.log_event(declared)

    assert events.clamp_level(logging.CRITICAL) == events.MAX_EVENT_LEVEL
    assert [record.levelno for record in _foundry_records(caplog)] == [logging.WARNING]


def test_a_lifecycle_event_renders_its_fields_sorted() -> None:
    first = events.LifecycleEvent(
        name="attempt",
        level=logging.DEBUG,
        fields=(("outcome", "passed"), ("attempt", "2"), ("nodeid", _NODEID)),
    )
    second = events.LifecycleEvent(
        name="attempt",
        level=logging.DEBUG,
        fields=(("nodeid", _NODEID), ("attempt", "2"), ("outcome", "passed")),
    )

    assert first.as_line() == second.as_line()
    assert first.as_line() == f"foundry: attempt attempt=2 nodeid={_NODEID} outcome=passed"
    assert events.event_line("run_start", {"b": 2, "a": 1}) == "foundry: run_start a=1 b=2"


def test_free_text_a_warning_emitter_carries_is_redacted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=obs_logging.LOGGER_NAME)
    message = "cannot write https://cart-user:hunter2secret@shop.example/p?token=tok_live_9f3"

    events.emit_plumbing_failure("artifacts", message, exc=OSError("not a directory"))
    events.emit_html_fallback(message)

    lines = [record.getMessage() for record in _foundry_records(caplog)]
    assert len(lines) == 2
    for line in lines:
        for sentinel in _SECRET_SENTINELS:
            assert sentinel not in line
        assert "cart-user" not in line
        assert redaction.REDACTED in line
        assert "shop.example/p" in line
    assert "error=OSError" in lines[0]
