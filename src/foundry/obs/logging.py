"""Logging for Foundry: one logger name, one ``NullHandler``, no host reconfiguration.

Every Foundry module logs through the single logger named :data:`LOGGER_NAME` (``"foundry"``) and
its children, and :func:`attach_handler` and :func:`detach_handler` are the only code that mutates
its handler list. :func:`install_redaction_filter` is the only code that mutates its filter list: it
masks the secrets a record carries *before* any handler -- Foundry's or the host's -- renders it, so
the terminal, pytest's captured-log block and every artifact carry the marker rather than the value
(AAP 0.2.1, AAP 0.14.3). A library does not reconfigure its host (AAP 0.4.8): no ``basicConfig``, no
root logger change, and ``propagate`` stays on, which pytest's captured-log blocks depend on.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Final

from foundry.obs.redaction import Redactor

__all__ = [
    "LOG_FORMAT",
    "LOGGER_NAME",
    "NULL_HANDLER",
    "RedactionFilter",
    "attach_handler",
    "configure",
    "detach_handler",
    "get_logger",
    "install_redaction_filter",
    "level_for",
]

LOGGER_NAME: Final[str] = "foundry"
"""Name of the one logger every Foundry module logs on."""

_CHILD_PREFIX: Final[str] = LOGGER_NAME + "."
"""What a name must begin with to count as a child of :data:`LOGGER_NAME`: a bare
``startswith(LOGGER_NAME)`` would accept ``"foundrydb"``, a logger outside the tree."""

LOG_FORMAT: Final[str] = "%(levelname)s %(name)s: %(message)s"
"""Format string for a Foundry record; it carries no timestamp, so captured output is stable."""

NULL_HANDLER: Final[logging.NullHandler] = logging.NullHandler()
"""The one handler a library may add to its own logger, installed once at import."""

_REDACTION_FAULT: Final[str] = (
    "could not mask the values of a log record, so the records after it keep the values they "
    "carry; the fault changes neither a test's outcome nor the run's exit code (AAP 0.1.2)"
)
"""Logged once per :class:`RedactionFilter` whose engine raised; never above ``WARNING``."""


def get_logger(name: str | None = None) -> logging.Logger:
    """Return the Foundry logger, or the ``foundry`` child logger that ``name`` designates.

    Returns:
        The logger for ``name``. The name is used unchanged only when it is :data:`LOGGER_NAME`
        itself or one of its dotted children, such as ``"foundry.obs.artifacts"`` -- so calling
        this with ``__name__`` from a Foundry module is idempotent; a name that merely begins with
        the same letters, such as ``"foundrydb"``, would land outside the tree and inherit neither
        the configured level nor the null handler.

    Nothing is configured here: no level is set and no handler is attached, so obtaining a
    logger is always free of side effects.
    """
    if name is None:
        return logging.getLogger(LOGGER_NAME)
    candidate = name.strip()
    if not candidate:
        return logging.getLogger(LOGGER_NAME)
    if candidate == LOGGER_NAME or candidate.startswith(_CHILD_PREFIX):
        return logging.getLogger(candidate)
    return logging.getLogger(f"{LOGGER_NAME}.{candidate}")


def level_for(name: str | int) -> int:
    """Return the numeric logging level named by ``name``, defaulting to ``INFO``.

    An integer passes through, a name is mapped through :func:`logging.getLevelNamesMapping`, and
    anything unreadable yields ``INFO``, never raising; a ``LogLevel`` enum resolves by its value.
    """
    # bool is an int subclass, and True would otherwise pass for level 1.
    if isinstance(name, int) and not isinstance(name, bool):
        return name
    if isinstance(name, str):
        return logging.getLevelNamesMapping().get(name.strip().upper(), logging.INFO)
    value = getattr(name, "value", None)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        return logging.getLevelNamesMapping().get(value.strip().upper(), logging.INFO)
    return logging.INFO


def configure(level: str | int) -> None:
    """Apply ``level`` to the ``foundry`` logger, and to nothing else.

    Setting the level is idempotent and child loggers inherit it.
    """
    logging.getLogger(LOGGER_NAME).setLevel(level_for(level))


def attach_handler(handler: logging.Handler) -> None:
    """Attach ``handler`` to the ``foundry`` logger, once.

    An already attached handler changes nothing and a non-handler value is ignored, since a capture
    failure must not change a test's outcome.
    """
    if not isinstance(handler, logging.Handler):
        return
    logger = logging.getLogger(LOGGER_NAME)
    if handler in logger.handlers:
        return
    logger.addHandler(handler)


def detach_handler(handler: logging.Handler) -> None:
    """Detach ``handler`` from the ``foundry`` logger, if it is attached.

    Detaching an unattached handler, or a non-handler value, is a no-op, and a removal failure is
    suppressed on purpose: a ring that cannot be removed changes neither outcome nor exit code.
    """
    if not isinstance(handler, logging.Handler):
        return
    logger = logging.getLogger(LOGGER_NAME)
    with contextlib.suppress(ValueError):
        logger.removeHandler(handler)


class RedactionFilter(logging.Filter):
    """Mask the values a ``foundry`` log record carries before any handler renders it.

    A project logging through the ``foundry`` logger is how a credential reaches a sink no writer of
    Foundry's can intercept: pytest renders a failing test's captured records verbatim, and so does
    every handler the host attaches, which is the residual channel AAP 0.2.1 names. The filter
    therefore scrubs the record's rendered message through the run's
    :class:`~foundry.obs.redaction.Redactor` in place, ahead of the handler chain, so the terminal,
    a captured-log block and an artifact all carry :data:`~foundry.obs.redaction.REDACTED`.

    ``record.msg`` and ``record.args`` are replaced together, and only when the scrub changes the
    message: a record carrying no secret keeps the format string, the argument shapes and the
    argument values the caller logged, and a record the engine does rewrite is rendered from the
    interpolated form, so no format string is ever re-parsed and no argument has to be a ``str``.
    Every record is kept -- a redaction fault may change neither an outcome nor an exit code
    (AAP 0.1.2) -- and the fault costs one ``WARNING`` instead.
    """

    def __init__(self, redactor: Redactor) -> None:
        """Bind the filter to ``redactor``, the engine the resolved configuration built."""
        super().__init__()
        self._redactor = redactor
        self._faulted = False

    @property
    def redactor(self) -> Redactor:
        """Return the engine this filter masks with, so a reinstall can keep one filter."""
        return self._redactor

    def filter(self, record: logging.LogRecord) -> bool:
        """Mask ``record`` in place and keep it, whatever the engine does."""
        if self._faulted:
            return True
        try:
            self._mask(record)
        except Exception:
            # Failure-isolation rule (AAP 0.1.2): the fault is reported once, at WARNING, and the
            # record is kept as it stands, so it can change neither the outcome nor the exit code.
            self._faulted = True
            with contextlib.suppress(Exception):
                logging.getLogger(LOGGER_NAME).warning(_REDACTION_FAULT)
        return True

    def _mask(self, record: logging.LogRecord) -> None:
        """Replace the record's rendered message with its redacted form when the two differ."""
        try:
            message = record.getMessage()
        except (AttributeError, TypeError, ValueError):
            # A record that cannot be interpolated is rendered by the capture ring's placeholder, so
            # there is no text here for the engine to read and nothing to rewrite.
            return
        scrubbed = self._redactor.text(message)
        if scrubbed == message:
            return
        record.msg = scrubbed
        record.args = None


def install_redaction_filter(redactor: Redactor) -> RedactionFilter:
    """Attach one :class:`RedactionFilter`, bound to ``redactor``, to the ``foundry`` logger.

    The session bootstrap calls this where the resolved configuration first yields the run's
    redaction engine, so every record the suite logs through the ``foundry`` logger is masked before
    any handler renders it (AAP 0.2.1, AAP 0.3.2, AAP 0.14.3). It is idempotent: however many
    bootstraps share one process -- a ``pytester`` child run, a replay run -- the logger holds
    exactly one such filter, always carrying the newest engine. No other logger is touched, and
    ``propagate`` is left on, so the host's own handlers still see the masked record (AAP 0.4.8).

    Returns:
        The filter attached to the ``foundry`` logger.
    """
    logger = logging.getLogger(LOGGER_NAME)
    installed = [attached for attached in logger.filters if isinstance(attached, RedactionFilter)]
    if len(installed) == 1 and installed[0].redactor is redactor:
        return installed[0]
    for stale in installed:
        logger.removeFilter(stale)
    filter_ = RedactionFilter(redactor)
    logger.addFilter(filter_)
    return filter_


def _install_null_handler() -> None:
    """Attach :data:`NULL_HANDLER` unless a handler of that type is already attached."""
    logger = logging.getLogger(LOGGER_NAME)
    if any(isinstance(attached, logging.NullHandler) for attached in logger.handlers):
        return
    logger.addHandler(NULL_HANDLER)


_install_null_handler()
