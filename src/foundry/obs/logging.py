"""Logging for Foundry: one logger name, one ``NullHandler``, no host reconfiguration.

Every Foundry module logs through the single logger named :data:`LOGGER_NAME` (``"foundry"``) and
its children, and :func:`attach_handler` and :func:`detach_handler` are the only code that mutates
its handler list. A library does not reconfigure its host (AAP 0.4.8): no ``basicConfig``, no root
logger change, and ``propagate`` stays on, which pytest's captured-log blocks depend on.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Final

__all__ = [
    "LOG_FORMAT",
    "LOGGER_NAME",
    "NULL_HANDLER",
    "attach_handler",
    "configure",
    "detach_handler",
    "get_logger",
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


def _install_null_handler() -> None:
    """Attach :data:`NULL_HANDLER` unless a handler of that type is already attached."""
    logger = logging.getLogger(LOGGER_NAME)
    if any(isinstance(attached, logging.NullHandler) for attached in logger.handlers):
        return
    logger.addHandler(NULL_HANDLER)


_install_null_handler()
