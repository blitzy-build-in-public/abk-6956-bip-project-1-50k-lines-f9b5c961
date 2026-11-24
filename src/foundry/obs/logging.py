"""Logging for Foundry: one logger name, one ``NullHandler``, no host reconfiguration.

Every Foundry module logs through the single logger named :data:`LOGGER_NAME` (``"foundry"``)
and its children, and never through a logger it does not own. Library code obtains a logger
with :func:`get_logger`, the session applies the configured level with :func:`configure`, and
the bounded per-test log ring is installed and removed with :func:`attach_handler` and
:func:`detach_handler`. Those two functions are the only code in the package that mutates the
handler list of the ``foundry`` logger.

Import-time behaviour
    This module adds exactly one :class:`logging.NullHandler` to the ``foundry`` logger at
    import, guarded by a membership check so that a repeated import, or an
    :func:`importlib.reload`, never duplicates it. That is the only import-time side effect
    permitted anywhere in :mod:`foundry.obs`: importing a Foundry module performs no I/O, no
    environment read, no argument parsing and no logger configuration.

Why a ``NullHandler``
    With no handler attached, ``logging.lastResort`` prints every record of ``WARNING`` and
    above to ``stderr``, so a library that logs would pollute the output of the application
    that embedded it. A single ``NullHandler`` at import is the documented remedy, and it is
    also why this module installs no terminal handler of its own.

What this module must never do
    A library does not reconfigure the logging of the application hosting it. Therefore this
    module, and everything it exposes, never:

    * calls :func:`logging.basicConfig`;
    * changes the root logger's level or handlers (``logging.getLogger()``);
    * calls :func:`logging.disable` or :func:`logging.captureWarnings`;
    * sets ``propagate`` to ``False`` -- pytest's logging plugin attaches its handlers to the
      root logger and captures ``foundry`` records through propagation, so disabling it would
      blind ``caplog``, the captured-log block on a failing test, and the host project's own
      logging;
    * adds a :class:`logging.StreamHandler` or :class:`logging.FileHandler` -- pytest owns the
      terminal, and a second handler would duplicate lines and pollute the captured-stdout
      artifact;
    * touches another package's logger. A project that wants httpx's INFO request-URL lines
      suppressed sets that on the ``httpx`` logger itself, as ``README.md`` documents.

Level policy followed by the rest of the package
    ``DEBUG`` for configuration resolution (the layer and the key, never a secret value),
    cassette-mode selection, sharding decisions and per-attempt detail; exactly one ``INFO``
    line per run, carrying the run id, the profile and the artifacts root; ``WARNING`` only
    for the failure-isolation cases and the documented HTML fallback; and never above
    ``WARNING`` from library code.
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

LOG_FORMAT: Final[str] = "%(levelname)s %(name)s: %(message)s"
"""Format string for a Foundry record, for ``capture`` and for a host-attached handler.

It carries no timestamp, so captured log output is byte-identical across runs, which the
determinism guarantee depends on.
"""

NULL_HANDLER: Final[logging.NullHandler] = logging.NullHandler()
"""The one handler a library may add to its own logger, installed once at import."""


def get_logger(name: str | None = None) -> logging.Logger:
    """Return the Foundry logger, or the ``foundry`` child logger that ``name`` designates.

    Args:
        name: A module name such as ``"foundry.obs.artifacts"``, or a bare suffix such as
            ``"obs.artifacts"``. ``None``, an empty name or a whitespace-only name returns
            the ``foundry`` logger itself.

    Returns:
        The logger for ``name``. A name that already begins with :data:`LOGGER_NAME` is used
        unchanged, so calling this with ``__name__`` from a Foundry module is idempotent;
        any other name is prefixed with ``"foundry."``.

    Nothing is configured here: no level is set and no handler is attached, so obtaining a
    logger is always free of side effects.
    """
    if name is None:
        return logging.getLogger(LOGGER_NAME)
    candidate = name.strip()
    if not candidate:
        return logging.getLogger(LOGGER_NAME)
    if candidate.startswith(LOGGER_NAME):
        return logging.getLogger(candidate)
    return logging.getLogger(f"{LOGGER_NAME}.{candidate}")


def level_for(name: str | int) -> int:
    """Return the numeric logging level named by ``name``, defaulting to ``INFO``.

    Args:
        name: A level name such as ``"DEBUG"``, ``"INFO"``, ``"WARNING"``, ``"ERROR"`` or
            ``"CRITICAL"``, in any capitalisation, or an already numeric level.

    Returns:
        The numeric level. An integer is returned unchanged, a recognised name is mapped
        through :func:`logging.getLevelNamesMapping`, and anything unrecognised -- an unknown
        name, an empty name, a value of another type -- yields :data:`logging.INFO`.

    A level-valued enum member is resolved by its value, so the configuration's ``LogLevel``
    reaches this function whether or not it subclasses :class:`str`. A value that cannot be
    understood never raises, because a bad level name must never fail a run: Foundry reports
    the fault and carries on with the default instead.
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

    Args:
        level: A level name or a numeric level, resolved through :func:`level_for`. A value
            that cannot be understood is reported as ``INFO`` rather than raising.

    The ``foundry`` logger is the only logger this function touches, and setting its level is
    idempotent, so calling ``configure`` once per run or once per test has the same effect.
    Child loggers keep the ``NOTSET`` level and therefore inherit whatever is set here.

    It deliberately does not call :func:`logging.basicConfig`, does not touch the root
    logger's level or handlers, does not call :func:`logging.disable` or
    :func:`logging.captureWarnings`, does not change ``propagate``, does not install a
    console or file handler, and does not configure another package's logger -- each of those
    would reconfigure the application hosting Foundry. See the module docstring for why.
    """
    logging.getLogger(LOGGER_NAME).setLevel(level_for(level))


def attach_handler(handler: logging.Handler) -> None:
    """Attach ``handler`` to the ``foundry`` logger, once.

    Args:
        handler: The handler to attach, typically the bounded per-test log ring that
            :class:`foundry.obs.capture.LogRing` builds. Attaching an already attached
            handler changes nothing, and a value that is not a handler is ignored rather than
            raising -- a capture failure must not change a test's outcome.

    Only the ``foundry`` logger's handler list is modified; no other logger is touched.
    """
    if not isinstance(handler, logging.Handler):
        return
    logger = logging.getLogger(LOGGER_NAME)
    if handler in logger.handlers:
        return
    logger.addHandler(handler)


def detach_handler(handler: logging.Handler) -> None:
    """Detach ``handler`` from the ``foundry`` logger, if it is attached.

    Args:
        handler: The handler to detach. Detaching a handler that is not attached, or a value
            that is not a handler at all, is a no-op rather than an error.

    Removal failures are suppressed on purpose: a log ring that cannot be removed must never
    change a test's outcome or an exit code.
    """
    if not isinstance(handler, logging.Handler):
        return
    logger = logging.getLogger(LOGGER_NAME)
    with contextlib.suppress(ValueError):
        logger.removeHandler(handler)


def _install_null_handler() -> None:
    """Attach :data:`NULL_HANDLER` to the ``foundry`` logger unless a null handler is there.

    The check is by type rather than by identity so that a repeated import, or an
    :func:`importlib.reload`, leaves exactly one null handler on the logger instead of adding
    a second one.
    """
    logger = logging.getLogger(LOGGER_NAME)
    if any(isinstance(attached, logging.NullHandler) for attached in logger.handlers):
        return
    logger.addHandler(NULL_HANDLER)


_install_null_handler()
