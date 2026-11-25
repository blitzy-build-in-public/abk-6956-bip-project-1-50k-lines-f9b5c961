"""Unit tests for the ``foundry.obs`` foundation package.

Each of the four modules tested here is pinned to the contract it owns (AAP 0.2.1, 0.3.2, 0.4.6,
0.4.8, 0.13.1, 0.14.3): ``logging`` moves only the ``foundry`` logger's level; ``capture`` keeps a
bounded ring and returns the last ``n`` records (I7); ``redaction`` is the one engine every sink
calls, asserted on the serialised form; ``events`` emits nothing above ``WARNING``. The package's
other two modules — ``manifest`` and ``artifacts``, which publish the per-test layout and disable
capture with one ``WARNING`` — are ``tests/unit/test_obs_artifacts.py``'s, and that module imports
this one's run id, node id and secret sentinels rather than restating them, so the suite still
describes one run.

Isolation (AAP 0.14.3): inputs live in ``tmp_path``, the loggers this module changes are restored
by ``restore_foundry_logger``, and nothing sleeps or reads the wall clock.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Iterator

import pytest

from foundry.obs import capture, events, redaction
from foundry.obs import logging as obs_logging

# Fixed run id, profile and node id, so no assertion reads the wall clock.
_RUN_ID = "20250102T030405-abc123"
_PROFILE = "ci"
_NODEID = "tests/test_api.py::test_cart[1]"

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


def _record(
    level: int = logging.WARNING, message: str = "cart empty", *args: object
) -> logging.LogRecord:
    """Build a log record without a logger, so formatting can be asserted on its own."""
    return logging.LogRecord("foundry.obs", level, __file__, 1, message, args or None, None)


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

    The suite shares one process, so a test that configured the level would otherwise decide what
    every later test observes (AAP 0.4.8: one owner per piece of state).
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
    # A lifecycle line already begins with the logger name, so the prefix is rendered once.
    assert capture.format_record(_record(message="foundry: x")) == "WARNING foundry: x"


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
