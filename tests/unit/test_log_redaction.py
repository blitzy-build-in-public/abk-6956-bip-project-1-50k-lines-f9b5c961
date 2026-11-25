"""Masks a ``foundry`` log record at the logger, ahead of every handler, so the captured log and
the terminal cannot carry a secret; a redaction fault costs one WARNING and no record changes fate.
"""

import logging
from collections.abc import Iterator
from typing import Final

import pytest

from foundry.obs import events
from foundry.obs import logging as obs_logging
from foundry.obs.redaction import REDACTED, Redactor

_LOGGER: Final[str] = obs_logging.LOGGER_NAME
_SECRET, _TRACE = "probe-credential-not-a-real-value", "trace-not-a-secret-1"
_CHILD = f"{_LOGGER}.obs.child"


def _record(message: str, args: object = None) -> logging.LogRecord:
    record = logging.LogRecord(_LOGGER, logging.INFO, __file__, 1, message, None, None)
    record.args = args  # type: ignore[assignment] # a single object is a shape logging accepts
    return record


@pytest.fixture
def foundry_filters() -> Iterator[None]:
    saved = list(logging.getLogger(_LOGGER).filters)
    try:
        yield
    finally:
        logging.getLogger(_LOGGER).filters[:] = saved


@pytest.fixture
def handler_filters() -> Iterator[None]:
    saved = list(obs_logging.NULL_HANDLER.filters)
    try:
        yield
    finally:
        obs_logging.NULL_HANDLER.filters[:] = saved


@pytest.mark.parametrize(
    ("message", "args", "kept", "masked"),
    [
        ("x-trace-id=%s authorization=%s", (_TRACE, _SECRET), f"x-trace-id={_TRACE}", True),
        ("authorization=%(credential)s", {"credential": _SECRET}, "authorization=", True),
        ("authorization=%s", _SECRET, "authorization=", True),
        ("authorization=%s items=%d", (_SECRET, 3), "items=3", True),
        ("cart %s empty", ("basket",), "cart basket empty", False),
    ],
)
def test_the_filter_masks_a_secret_and_leaves_every_other_record_alone(
    message: str, args: object, kept: str, masked: bool
) -> None:
    record = _record(message, args)
    assert obs_logging.RedactionFilter(Redactor()).filter(record) is True
    if masked:
        rendered = record.getMessage()
        assert _SECRET not in rendered and kept in rendered and REDACTED in rendered
    else:
        assert (record.getMessage(), record.msg, record.args) == (kept, message, args)


def test_installing_twice_keeps_one_filter_on_the_logger_and_one_on_the_handler(
    foundry_filters: None, handler_filters: None
) -> None:
    ours, handler = logging.getLogger(_LOGGER), obs_logging.NULL_HANDLER
    others = (logging.getLogger(), logging.getLogger("acme.probe"))
    first = obs_logging.install_redaction_filter(Redactor(["x-api-key"]))
    assert obs_logging.install_redaction_filter(first.redactor) is first
    installed = obs_logging.install_redaction_filter(newest := Redactor(["cookie"]))
    filters = [f for f in ours.filters if isinstance(f, obs_logging.RedactionFilter)]
    attached = [f for f in handler.filters if isinstance(f, obs_logging.RedactionFilter)]
    assert (len(filters), filters[0] is installed, installed.redactor is newest) == (1, True, True)
    assert (len(attached), attached[0] is installed) == (1, True)
    assert not any(isinstance(f, obs_logging.RedactionFilter) for o in others for f in o.filters)


def test_a_raising_engine_costs_one_warning_and_keeps_the_record(
    caplog: pytest.LogCaptureFixture, foundry_filters: None
) -> None:
    class _RaisingRedactor(Redactor):
        def text(self, text: str, *, limit: int | None = None) -> str:
            raise RuntimeError("injected redaction failure")

    caplog.set_level(logging.WARNING, logger=_LOGGER)
    filter_ = obs_logging.RedactionFilter(_RaisingRedactor())
    first, second = _record("authorization=%s", (_SECRET,)), _record("authorization=%s", (_SECRET,))
    assert (filter_.filter(first), filter_.filter(second)) == (True, True)
    assert first.getMessage() == f"authorization={_SECRET}", "a kept record keeps what it logged"
    assert len(caplog.messages) == 1 and "could not mask" in caplog.messages[0]


@pytest.mark.parametrize("name", [_LOGGER, _CHILD])
def test_a_record_reaches_a_host_handler_masked_from_the_logger_or_a_child(
    caplog: pytest.LogCaptureFixture, foundry_filters: None, handler_filters: None, name: str
) -> None:
    obs_logging.install_redaction_filter(Redactor())
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        logging.getLogger(name).info("x-trace-id=%s authorization=%s", _TRACE, _SECRET)
        events.emit_resolution("http.timeout", "env", "FOUNDRY_HTTP__TIMEOUT")
    assert _SECRET not in caplog.text
    assert f"x-trace-id={_TRACE}" in caplog.text and REDACTED in caplog.text
    assert "layer=env option=http.timeout" in caplog.text
