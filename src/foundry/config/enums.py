"""Enumerations naming the accepted values of Foundry's enum-typed settings.

Six enums cover every enum-typed configuration key: ``profile``,
``http.auth.strategy``, ``http.retries.retry_on``, ``cassette.mode``,
``execution.quarantine_mode`` and ``logging.level``. Each is a
:class:`enum.StrEnum`, so a member is a ``str``: it serialises as its plain
value in ``results.json``, ``config show --format json`` and TOML output,
compares equal to that string, and formats as the value rather than as
``ProfileName.DEFAULT``. ``StrEnum`` is the form the ``ruff`` rule set accepts
for the supported 3.11+ floor.

Member values are part of Foundry's published compatibility promise: a later
release may add a member, but may never rename or remove one. The
accepted-value tuples below are derived from the enums for the same reason, so
a member and the text that names it can never drift apart.

The module imports the standard library only, performs no I/O at import time
and holds no mutable module state: it sits on the plugin startup path through
:mod:`foundry.config.keys`, which must stay free of third-party imports.
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

    A profile's ``[profiles.<name>]`` table is merged over the base
    configuration file and below the environment and CLI layers, so it can
    override the file's values but never an operator's.
    """

    DEFAULT = "default"
    LOCAL = "local"
    CI = "ci"
    STAGING = "staging"


class AuthStrategy(StrEnum):
    """Credential strategies accepted by the ``http.auth.strategy`` key.

    The strategy decides how :mod:`foundry.http.auth` attaches credentials to
    requests issued by the ``foundry_api`` fixture. No secret appears here: each
    strategy reads its credential from the environment variable named by the
    companion ``http.auth.*_env`` key at session start.
    """

    NONE = "none"
    BEARER = "bearer"
    BASIC = "basic"
    HEADER = "header"


class RetryPredicate(StrEnum):
    """Predicates accepted as elements of the ``http.retries.retry_on`` list.

    The key's environment form is comma separated, for example
    ``FOUNDRY_HTTP__RETRIES__RETRY_ON=timeout,status_429``, so
    :func:`parse_enum` validates each element on its own.
    """

    TIMEOUT = "timeout"
    CONNECT = "connect"
    STATUS_5XX = "status_5xx"
    STATUS_429 = "status_429"


class CassetteMode(StrEnum):
    """Record/replay modes accepted by the ``cassette.mode`` key.

    ``RECORD`` writes one cassette per test, ``REPLAY`` serves recorded
    responses and opens no socket, ``PASSTHROUGH`` replays a match and
    otherwise falls through to the live transport, and ``OFF`` bypasses
    cassettes entirely. The behaviour behind each mode belongs to
    :mod:`foundry.cassette.modes`.
    """

    OFF = "off"
    RECORD = "record"
    REPLAY = "replay"
    PASSTHROUGH = "passthrough"


class QuarantineMode(StrEnum):
    """Modes accepted by the ``execution.quarantine_mode`` key.

    ``REPORT`` records a quarantined failure without failing the run;
    ``STRICT`` lets that failure count against it. Only the modes are named
    here — the exit-code decision belongs to :mod:`foundry.execution.quarantine`.
    """

    REPORT = "report"
    STRICT = "strict"


class LogLevel(StrEnum):
    """Levels accepted by the ``logging.level`` key.

    Values are upper case because they are :mod:`logging` level names, and
    ``INFO`` is the default.
    """

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


def _values(enum_type: type[Enum]) -> tuple[str, ...]:
    """Return the member values of ``enum_type`` as strings in declaration order."""
    return tuple(str(member.value) for member in enum_type)


# Accepted-value tuples for the enum-typed keys. Each is derived from its enum,
# holds no mutable state, and preserves declaration order because every error
# message, CLI help text and rendered document iterates it in that order.
PROFILE_NAMES: tuple[str, ...] = _values(ProfileName)
AUTH_STRATEGIES: tuple[str, ...] = _values(AuthStrategy)
RETRY_PREDICATES: tuple[RetryPredicate, ...] = tuple(RetryPredicate)
CASSETTE_MODES: tuple[str, ...] = _values(CassetteMode)
QUARANTINE_MODES: tuple[str, ...] = _values(QuarantineMode)
LOG_LEVELS: tuple[str, ...] = _values(LogLevel)


def accepted_values_text(enum_type: type[Enum]) -> str:
    """Render ``enum_type``'s accepted values for an error or help message.

    Args:
        enum_type: Enumeration whose member values are being named.

    Returns:
        The member values in declaration order, joined by ``", "`` — for
        example ``"default, local, ci, staging"`` for :class:`ProfileName`.
        The text is purely declarative: it parses nothing and suggests no
        nearest match, which is :func:`foundry.config.keys.closest_key`'s job.
    """
    return ", ".join(str(member.value) for member in enum_type)


def parse_enum(enum_type: type[_EnumT], raw: str) -> _EnumT | None:
    """Match ``raw`` against ``enum_type``'s member values, ignoring case.

    Environment variables arrive in whatever case an operator wrote them, so
    ``FOUNDRY_PROFILE=CI`` has to resolve exactly as ``ci`` does.

    Args:
        enum_type: Enumeration to match against.
        raw: Candidate text, typically an environment-variable or CLI value.
            Surrounding whitespace is ignored.

    Returns:
        The matching member, typed as ``enum_type``'s own member type, or
        ``None`` when no member's value matches. Nothing is raised here: the
        caller builds the ``ConfigError`` naming the offending key, which is
        what keeps this module free of any ``foundry.core`` import.
    """
    if not isinstance(raw, str):
        return None
    candidate = raw.strip().lower()
    for member in enum_type:
        if str(member.value).lower() == candidate:
            return member
    return None
