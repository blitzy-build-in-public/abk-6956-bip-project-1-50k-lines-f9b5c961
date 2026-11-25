"""Enumerations naming the accepted values of Foundry's six enum-typed settings.

``profile``, ``http.auth.strategy``, ``http.retries.retry_on``, ``cassette.mode``,
``execution.quarantine_mode`` and ``logging.level`` each accept one of these ``StrEnum`` members,
which serialise as their plain value. Values are a compatibility promise.
"""

from __future__ import annotations

from enum import Enum, StrEnum
from typing import TypeVar

_EnumT = TypeVar("_EnumT", bound=Enum)

__all__ = [
    "AUTH_STRATEGIES",
    "AuthStrategy",
    "CASSETTE_MODES",
    "CassetteMode",
    "LOG_LEVELS",
    "LogLevel",
    "PROFILE_NAMES",
    "ProfileName",
    "QUARANTINE_MODES",
    "QuarantineMode",
    "RETRY_PREDICATES",
    "RetryPredicate",
    "accepted_values_text",
    "parse_enum",
]


class ProfileName(StrEnum):
    """Names accepted by the ``profile`` key, selecting the active profile.

    A profile's table is merged over the base file and stays below the environment and CLI layers.
    """

    DEFAULT = "default"
    LOCAL = "local"
    CI = "ci"
    STAGING = "staging"


class AuthStrategy(StrEnum):
    """Credential strategies accepted by the ``http.auth.strategy`` key.

    Each reads its credential from the variable a companion key names at session start, so no
    secret value is ever held here.
    """

    NONE = "none"
    BEARER = "bearer"
    BASIC = "basic"
    HEADER = "header"


class RetryPredicate(StrEnum):
    """Predicates accepted as elements of the ``http.retries.retry_on`` list."""

    TIMEOUT = "timeout"
    CONNECT = "connect"
    STATUS_5XX = "status_5xx"
    STATUS_429 = "status_429"


class CassetteMode(StrEnum):
    """Record/replay modes accepted by the ``cassette.mode`` key.

    ``RECORD`` writes one cassette per test, ``REPLAY`` serves them and opens no socket,
    ``PASSTHROUGH`` replays a match and otherwise goes live, and ``OFF`` bypasses the store.
    """

    OFF = "off"
    RECORD = "record"
    REPLAY = "replay"
    PASSTHROUGH = "passthrough"


class QuarantineMode(StrEnum):
    """Modes accepted by the ``execution.quarantine_mode`` key.

    ``REPORT`` records a quarantined failure without failing the run; ``STRICT`` lets it count
    against the run.
    """

    REPORT = "report"
    STRICT = "strict"


class LogLevel(StrEnum):
    """Levels accepted by the ``logging.level`` key; the values are :mod:`logging` level names."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


def _values(enum_type: type[Enum]) -> tuple[str, ...]:
    """Return the member values of ``enum_type`` as strings in declaration order."""
    return tuple(str(member.value) for member in enum_type)


# Accepted-value tuples for the enum-typed keys, each derived from its enum in declaration order.
PROFILE_NAMES: tuple[str, ...] = _values(ProfileName)
AUTH_STRATEGIES: tuple[str, ...] = _values(AuthStrategy)
RETRY_PREDICATES: tuple[RetryPredicate, ...] = tuple(RetryPredicate)
CASSETTE_MODES: tuple[str, ...] = _values(CassetteMode)
QUARANTINE_MODES: tuple[str, ...] = _values(QuarantineMode)
LOG_LEVELS: tuple[str, ...] = _values(LogLevel)


def accepted_values_text(enum_type: type[Enum]) -> str:
    """Render ``enum_type``'s member values for an error or help message, joined by ``", "``."""
    return ", ".join(str(member.value) for member in enum_type)


def parse_enum(enum_type: type[_EnumT], raw: str) -> _EnumT | None:
    """Match ``raw`` against ``enum_type``'s member values, ignoring case.

    Returns the member, or ``None`` when none matches: the caller builds the ``ConfigError``, which
    keeps this module free of a ``foundry.core`` import.
    """
    if not isinstance(raw, str):
        return None
    candidate = raw.strip().lower()
    for member in enum_type:
        if str(member.value).lower() == candidate:
            return member
    return None
