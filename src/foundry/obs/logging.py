"""Logging for Foundry: one logger name, one ``NullHandler``, no host reconfiguration.

Every Foundry module logs through the single logger named :data:`LOGGER_NAME` (``"foundry"``) and
its children; :func:`attach_handler` and :func:`detach_handler` are the only code that mutates its
handler list. :func:`install_redaction_filter` is the only code that mutates its filter list, and
it puts one filter on that logger and on :data:`NULL_HANDLER`, the handler Foundry owns at the tree
root, so a record's secrets are masked *before* any handler renders it: a logger's own filters
never see a record its children originate, the tree-root handler's do (AAP 0.2.1, AAP 0.14.3). A
library does not reconfigure its host (AAP 0.4.8): no ``basicConfig``, no root logger change, and
``propagate`` stays on, which pytest's captured-log blocks depend on.
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


class _FilteringNullHandler(logging.NullHandler):
    """A ``NullHandler`` that runs the filters it carries, then drops the record.

    ``logging.NullHandler.handle`` is a stub, so a filter attached to a plain one is never reached;
    running the filter chain without emitting keeps the null behaviour and lets the handler mask a
    ``foundry`` child's record in place at the tree root. No lock is taken.
    """

    def handle(self, record: logging.LogRecord) -> bool:
        """Mask ``record`` through the handler's filters, then drop it as a ``NullHandler`` does."""
        return bool(self.filter(record))


NULL_HANDLER: Final[logging.NullHandler] = _FilteringNullHandler()
"""The one handler a library may add to its own logger, installed once at import. It is the handler
:func:`install_redaction_filter` rides to mask the records ``foundry`` children originate, which no
logger's own filter list would see."""

_REDACTION_FAULT: Final[str] = (
    "could not mask the values of a log record, so the records after it keep the values they "
    "carry; the fault changes neither a test's outcome nor the run's exit code (AAP 0.1.2)"
)
"""Logged once per :class:`RedactionFilter` whose engine raised; never above ``WARNING``."""


def get_logger(name: str | None = None) -> logging.Logger:
    """Return the Foundry logger, or the ``foundry`` child logger that ``name`` designates.

    Returns:
        The logger for ``name``, used unchanged only when it is :data:`LOGGER_NAME` or one of its
        dotted children such as ``"foundry.obs.artifacts"``, so calling this with ``__name__`` from
        a Foundry module is idempotent; a name merely beginning with the same letters, such as
        ``"foundrydb"``, lands outside the tree and inherits neither level nor null handler.
    Nothing is configured here: no level is set, no handler is attached, no side effects.
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
    a captured-log block and an artifact all carry :data:`~foundry.obs.redaction.REDACTED`. One
    instance rides both the ``foundry`` logger and :data:`NULL_HANDLER`: the logger's copy masks the
    records it originates, the handler's copy those its children originate, which never reach the
    ancestor logger's filter list. ``record.msg`` and ``record.args`` are replaced together, and
    only when the scrub changes the message: a record carrying no secret keeps its format string,
    argument shapes and values, and a rewritten one is rendered from the interpolated form, so no
    format string is re-parsed. Every record is kept -- a redaction fault may change neither outcome
    nor exit code (AAP 0.1.2) -- and the fault costs one ``WARNING`` instead.
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
    """Install one :class:`RedactionFilter`, bound to ``redactor``, wherever a record can be seen.

    The filter goes on the ``foundry`` logger and on :data:`NULL_HANDLER`, the handler Foundry owns
    at the tree root: a record a ``foundry.*`` child originates never passes the ancestor logger's
    filter list, and a handler's filters run for every record routed to it, so :data:`NULL_HANDLER`
    masks the child's record in place before the chain reaches the root logger's handlers (AAP
    0.2.1, AAP 0.3.2, AAP 0.14.3). It is idempotent: however many bootstraps share one process, the
    logger and the handler each hold exactly one such filter, always carrying the newest engine. No
    other logger and no other handler is touched, and ``propagate`` is left on, so the host's own
    handlers still see the masked record (AAP 0.4.8).

    Returns:
        The filter, which is now the logger's and the handler's only one of its kind.
    """
    logger = logging.getLogger(LOGGER_NAME)
    installed = [attached for attached in logger.filters if isinstance(attached, RedactionFilter)]
    if len(installed) == 1 and installed[0].redactor is redactor:
        filter_ = installed[0]
    else:
        for stale in installed:
            logger.removeFilter(stale)
        filter_ = RedactionFilter(redactor)
        logger.addFilter(filter_)
    _install_handler_filter(filter_)
    return filter_


def _install_handler_filter(filter_: RedactionFilter) -> None:
    """Make ``filter_`` the only :class:`RedactionFilter` on :data:`NULL_HANDLER`.

    The handler's copy is the one that masks a ``foundry.*`` child's records, and a repeated
    bootstrap replaces it rather than adding to it, so the newest engine is the only one in effect.
    """
    ours = [attached for attached in NULL_HANDLER.filters if isinstance(attached, RedactionFilter)]
    for stale in ours:
        if stale is not filter_:
            NULL_HANDLER.removeFilter(stale)
    if filter_ not in ours:
        NULL_HANDLER.addFilter(filter_)


def _install_null_handler() -> None:
    """Attach :data:`NULL_HANDLER` unless a handler of that type is already attached."""
    logger = logging.getLogger(LOGGER_NAME)
    if any(isinstance(attached, logging.NullHandler) for attached in logger.handlers):
        return
    logger.addHandler(NULL_HANDLER)


_install_null_handler()
