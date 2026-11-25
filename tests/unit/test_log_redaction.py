"""The record-level seam: a ``foundry`` log record is masked before any handler renders it.

AAP 0.2.1 names pytest's captured-log block as a residual leak channel and AAP 0.14.3 forbids a
secret reaching the terminal, so :class:`foundry.obs.logging.RedactionFilter` rewrites a record in
place at the logger and at the handler Foundry owns at the tree root, ahead of every handler --
Foundry's own ring and the host's alike -- so a record a ``foundry`` child logger originates is
masked too. The engine's
rules are ``tests/unit/test_obs.py``'s and the six-sink absence proof is
``tests/integration/test_redaction_end_to_end.py``'s; this module holds the seam's own contract: the
argument shapes a caller may log, the record it must leave exactly as written, idempotency across
repeated bootstraps in one process, the loggers it must not touch and the isolation AAP 0.1.2
requires of a redaction fault.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Final

import pytest

from foundry.obs import events
from foundry.obs import logging as obs_logging
from foundry.obs.redaction import REDACTED, Redactor

_LOGGER: Final[str] = obs_logging.LOGGER_NAME
"""The one logger the filter is installed on, so the records here are the ones it governs."""

_SECRET: Final[str] = "probe-credential-not-a-real-value"
"""A bare value under a deny-listed header name; the name, not the shape, is what masks it."""

_TRACE: Final[str] = "trace-not-a-secret-1"
"""The non-secret value beside it, so a mask that swallowed the whole line would be caught."""

_CHILD: Final[str] = f"{_LOGGER}.obs.child"
"""A child of the ``foundry`` logger, the tree a module of Foundry logs from."""


def _record(message: str, args: object = None) -> logging.LogRecord:
    """Return an unemitted ``INFO`` record whose ``args`` are exactly what ``args`` holds."""
    record = logging.LogRecord(_LOGGER, logging.INFO, __file__, 1, message, None, None)
    record.args = args  # type: ignore[assignment] # a single object is a shape logging accepts
    return record


@pytest.fixture
def foundry_filters() -> Iterator[list[logging.Filter]]:
    """Restore the ``foundry`` logger's filter list, so no case outlives the one that ran it."""
    logger = logging.getLogger(_LOGGER)
    saved = list(logger.filters)
    try:
        yield saved
    finally:
        logger.filters[:] = saved


@pytest.fixture
def handler_filters() -> Iterator[list[logging.Filter]]:
    """Restore the ``foundry`` handler's filter list, so no case outlives the one that ran it."""
    handler = obs_logging.NULL_HANDLER
    saved = list(handler.filters)
    try:
        yield saved
    finally:
        handler.filters[:] = saved


@pytest.mark.parametrize(
    ("message", "args", "kept"),
    [
        ("caller sent x-trace-id=%s authorization=%s", (_TRACE, _SECRET), f"x-trace-id={_TRACE}"),
        ("authorization=%(credential)s", {"credential": _SECRET}, "authorization="),
        ("authorization=%s", _SECRET, "authorization="),
        ("authorization=%s items=%d", (_SECRET, 3), "items=3"),
    ],
    ids=["tuple", "mapping", "single-object", "non-string-beside"],
)
def test_the_secret_is_masked_whatever_shape_the_arguments_take(
    message: str, args: object, kept: str
) -> None:
    """A tuple, a mapping, a single object and a mixed tuple lose the value, keep the rest."""
    record = _record(message, args)

    assert obs_logging.RedactionFilter(Redactor()).filter(record) is True

    rendered = record.getMessage()
    assert _SECRET not in rendered
    assert REDACTED in rendered
    assert kept in rendered, f"the mask swallowed text that is not a secret: {rendered!r}"


@pytest.mark.parametrize(
    ("message", "args"),
    [
        ("cart %s empty", ("basket",)),
        ("cart %d items", (3,)),
        ("%(count)s items", {"count": 3}),
        ("no arguments at all", None),
    ],
    ids=["tuple", "int", "mapping", "none"],
)
def test_a_record_carrying_no_secret_keeps_what_the_caller_logged(
    message: str, args: object
) -> None:
    """An innocent record is untouched: its format string and the caller's argument object stay."""
    record = _record(message, args)
    rendered = record.getMessage()

    assert obs_logging.RedactionFilter(Redactor()).filter(record) is True

    assert record.getMessage() == rendered
    assert record.msg == message
    assert record.args is args


def test_a_record_that_cannot_be_interpolated_is_kept_as_it_stands() -> None:
    """A format string the engine cannot render is no reason to raise, or to alter the record."""
    record = _record("authorization=%s %s", (_SECRET,))

    assert obs_logging.RedactionFilter(Redactor()).filter(record) is True

    assert record.args == (_SECRET,)
    with pytest.raises(TypeError):
        record.getMessage()


def test_installing_twice_keeps_one_filter_carrying_the_newest_engine(
    foundry_filters: list[logging.Filter], handler_filters: list[logging.Filter]
) -> None:
    """Repeated bootstraps keep exactly one such filter, and it carries the newest engine."""
    first = obs_logging.install_redaction_filter(Redactor(["x-api-key"]))
    assert obs_logging.install_redaction_filter(first.redactor) is first

    newest = Redactor(["cookie"])
    installed = obs_logging.install_redaction_filter(newest)

    ours = [
        attached
        for attached in logging.getLogger(_LOGGER).filters
        if isinstance(attached, obs_logging.RedactionFilter)
    ]
    assert len(ours) == 1, f"a second bootstrap added a filter instead of replacing one: {ours}"
    assert ours[0] is installed
    assert installed.redactor is newest


def test_installing_touches_no_logger_but_the_foundry_one(
    foundry_filters: list[logging.Filter], handler_filters: list[logging.Filter]
) -> None:
    """AAP 0.4.8: the root logger and another package's logger keep their filters and handlers."""
    root = logging.getLogger()
    foreign = logging.getLogger("acme.probe")
    ours = logging.getLogger(_LOGGER)
    before = {
        "root": (list(root.filters), list(root.handlers), root.level),
        "foreign": (list(foreign.filters), list(foreign.handlers), foreign.level),
        "foundry-handlers": list(ours.handlers),
        "foundry-propagate": ours.propagate,
    }

    obs_logging.install_redaction_filter(Redactor())

    assert (list(root.filters), list(root.handlers), root.level) == before["root"]
    assert (list(foreign.filters), list(foreign.handlers), foreign.level) == before["foreign"]
    assert list(ours.handlers) == before["foundry-handlers"]
    assert ours.propagate == before["foundry-propagate"]


def test_a_raising_engine_costs_one_warning_and_keeps_the_record(
    caplog: pytest.LogCaptureFixture, foundry_filters: list[logging.Filter]
) -> None:
    """AAP 0.1.2: a redaction fault is one ``WARNING`` and changes no record's fate."""

    class _RaisingRedactor(Redactor):
        """A redactor whose free-text scan raises, the fault the isolation rule covers."""

        def text(self, text: str, *, limit: int | None = None) -> str:
            raise RuntimeError("injected redaction failure")

    caplog.set_level(logging.WARNING, logger=_LOGGER)
    filter_ = obs_logging.RedactionFilter(_RaisingRedactor())
    first = _record("authorization=%s", (_SECRET,))
    second = _record("authorization=%s", (_SECRET,))

    assert filter_.filter(first) is True
    assert filter_.filter(second) is True

    assert first.getMessage() == f"authorization={_SECRET}", "a kept record keeps what it logged"
    warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert len(warnings) == 1, [record.getMessage() for record in warnings]
    assert "could not mask" in warnings[0].getMessage()


def test_a_record_logged_through_the_logger_reaches_a_host_handler_masked(
    caplog: pytest.LogCaptureFixture,
    foundry_filters: list[logging.Filter],
    handler_filters: list[logging.Filter],
) -> None:
    """The guarantee the finding asks for: a host handler renders the mask, not the value."""
    obs_logging.install_redaction_filter(Redactor())

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        logging.getLogger(_LOGGER).info(
            "caller sent x-trace-id=%s authorization=%s", _TRACE, _SECRET
        )

    assert _SECRET not in caplog.text
    assert f"x-trace-id={_TRACE}" in caplog.text
    assert REDACTED in caplog.text


def test_the_resolution_line_keeps_the_key_it_names(
    caplog: pytest.LogCaptureFixture,
    foundry_filters: list[logging.Filter],
    handler_filters: list[logging.Filter],
) -> None:
    """AAP 0.1.2: the ``DEBUG`` resolution line carries the layer and the key, mask included."""
    obs_logging.install_redaction_filter(Redactor())
    caplog.set_level(logging.DEBUG, logger=_LOGGER)

    events.emit_resolution("http.timeout", "env", "FOUNDRY_HTTP__TIMEOUT")

    lines = [record.getMessage() for record in caplog.records if record.name == _LOGGER]
    assert len(lines) == 1
    assert "option=http.timeout" in lines[0]
    assert "layer=env" in lines[0]
    assert REDACTED not in lines[0], f"the mask ate a diagnostic that names no secret: {lines[0]!r}"


def test_a_record_logged_through_a_child_logger_reaches_a_host_handler_masked(
    caplog: pytest.LogCaptureFixture,
    foundry_filters: list[logging.Filter],
    handler_filters: list[logging.Filter],
) -> None:
    """A ``foundry`` child's record is masked before a host handler renders it.

    A logger's own filters never see a record its children originate, so the handler Foundry owns at
    the tree root is what masks it: the host handler receives the marker, and the rest of the line.
    """
    obs_logging.install_redaction_filter(Redactor())

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        logging.getLogger(_CHILD).info("caller sent x-trace-id=%s token=%s", _TRACE, _SECRET)

    assert _SECRET not in caplog.text, caplog.text
    assert f"x-trace-id={_TRACE}" in caplog.text
    assert REDACTED in caplog.text


def test_a_repeated_install_keeps_one_filter_on_the_logger_and_one_on_the_handler(
    foundry_filters: list[logging.Filter], handler_filters: list[logging.Filter]
) -> None:
    """Repeated bootstraps leave one filter on each, and both carry the newest engine."""
    obs_logging.install_redaction_filter(Redactor(["x-api-key"]))
    newest = Redactor(["cookie"])

    installed = obs_logging.install_redaction_filter(newest)
    assert obs_logging.install_redaction_filter(newest) is installed

    on_logger = [
        attached
        for attached in logging.getLogger(_LOGGER).filters
        if isinstance(attached, obs_logging.RedactionFilter)
    ]
    on_handler = [
        attached
        for attached in obs_logging.NULL_HANDLER.filters
        if isinstance(attached, obs_logging.RedactionFilter)
    ]
    assert len(on_logger) == 1, f"the logger holds more than one filter: {on_logger}"
    assert len(on_handler) == 1, f"the handler holds more than one filter: {on_handler}"
    assert on_logger[0] is installed
    assert on_handler[0] is installed
    assert installed.redactor is newest
