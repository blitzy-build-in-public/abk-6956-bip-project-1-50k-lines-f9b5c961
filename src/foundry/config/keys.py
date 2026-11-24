"""The immutable registry of Foundry's configuration keys.

Every key is described once -- dotted name, value shape, accepted values, option spelling and alias
(AAP 0.4.4, AAP 0.4.5) -- and ``plugin``, ``cli_layer`` and ``validation`` derive from it. A pure
table of standard library and :mod:`foundry.config.enums` only, with no default value here.
"""

from __future__ import annotations

import difflib
import types
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Final, Literal

from foundry.config.enums import (
    AuthStrategy,
    CassetteMode,
    LogLevel,
    ProfileName,
    QuarantineMode,
    RetryPredicate,
    accepted_values_text,
)

__all__ = [
    "ENV_PREFIX",
    "ENV_SETTABLE_KINDS",
    "GENERIC_OPTION_PREFIX",
    "KEY_REGISTRY",
    "KEY_SPECS",
    "KeyKind",
    "KeySpec",
    "SCALAR_KINDS",
    "cli_option",
    "closest_key",
    "constraint_for",
    "env_variable",
    "is_env_settable",
    "is_scalar",
    "key_alias_options",
    "key_from_env_variable",
    "leaf_keys",
    "option_dest",
    "option_to_key",
    "spec_for",
]

KeyKind = Literal["str", "int", "float", "bool", "path", "enum", "list", "mapping"]
"""The eight shapes a configuration value can take: six scalars, ``list`` and ``mapping``."""

SCALAR_KINDS: Final[frozenset[KeyKind]] = frozenset({"str", "int", "float", "bool", "path", "enum"})
"""Kinds a single ``--foundry-<dotted.key>`` option can carry (AAP 0.4.4); a list or map cannot."""

ENV_SETTABLE_KINDS: Final[frozenset[KeyKind]] = frozenset(
    {"str", "int", "float", "bool", "path", "enum", "list"}
)
"""Kinds :mod:`foundry.config.env` can read from a ``FOUNDRY_*`` variable.

:data:`SCALAR_KINDS` plus ``list``, comma separated (assumption A3); ``mapping`` is excluded
because a header table cannot be expressed as one variable.
"""

GENERIC_OPTION_PREFIX: Final[str] = "--foundry-"

ENV_PREFIX: Final[str] = "FOUNDRY_"

_OPTION_DEST_PREFIX: Final[str] = "foundry__"


@dataclass(frozen=True, slots=True)
class KeySpec:
    """The declared shape, accepted values and option spellings of one key.

    Frozen and slotted, set once at import; :data:`KEY_SPECS` holds one per key in the order
    AAP 0.4.5 documents them.

    Attributes:
        dotted: The dotted key, also the spelling the environment layer nests with ``__``.
        kind: The shape of this key's value.
        constraint: The accepted values or range, quoted by :mod:`foundry.config.validation`.
        aliases: Named option spellings besides the generic one.
        enum_type: The enumeration an ``enum`` key accepts, or the one a ``list`` key parses its
            elements with; ``None`` for every other key.
        nullable: Whether ``None`` is accepted -- ``http.base_url`` and the five credential names.
    """

    dotted: str
    kind: KeyKind
    constraint: str
    aliases: tuple[str, ...] = ()
    enum_type: type[Enum] | None = None
    nullable: bool = False


# AAP 0.4.5 table order: profile, http.*, artifacts.*, reporting.*, data.*, cassette.*,
# execution.*, secrets.redact_keys, logging.*. Adding, renaming or removing an entry breaks a
# published surface (AAP 0.4.9); --foundry-json-report and --foundry-config are not keys here.
KEY_SPECS: tuple[KeySpec, ...] = (
    KeySpec(
        dotted="profile",
        kind="enum",
        constraint=accepted_values_text(ProfileName),
        enum_type=ProfileName,
        aliases=("--foundry-profile",),
    ),
    KeySpec(
        dotted="http.base_url",
        kind="str",
        constraint="an absolute http or https URL",
        nullable=True,
    ),
    KeySpec(dotted="http.timeout", kind="float", constraint="0 < t <= 300"),
    KeySpec(
        dotted="http.headers",
        kind="mapping",
        constraint="a table of header names to values",
    ),
    KeySpec(dotted="http.retries.max_attempts", kind="int", constraint="1..10"),
    KeySpec(dotted="http.retries.backoff_base", kind="float", constraint="0..10"),
    KeySpec(dotted="http.retries.backoff_max", kind="float", constraint="0..60"),
    KeySpec(
        dotted="http.retries.retry_on",
        kind="list",
        constraint=f"a comma-separated list of {accepted_values_text(RetryPredicate)}",
        enum_type=RetryPredicate,
    ),
    KeySpec(
        dotted="http.auth.strategy",
        kind="enum",
        constraint=accepted_values_text(AuthStrategy),
        enum_type=AuthStrategy,
    ),
    KeySpec(
        dotted="http.auth.token_env",
        kind="str",
        constraint="a variable name",
        nullable=True,
    ),
    KeySpec(
        dotted="http.auth.user_env",
        kind="str",
        constraint="a variable name",
        nullable=True,
    ),
    KeySpec(
        dotted="http.auth.password_env",
        kind="str",
        constraint="a variable name",
        nullable=True,
    ),
    KeySpec(
        dotted="http.auth.header_name",
        kind="str",
        constraint="a header name",
        nullable=True,
    ),
    KeySpec(
        dotted="http.auth.header_value_env",
        kind="str",
        constraint="a variable name",
        nullable=True,
    ),
    KeySpec(dotted="artifacts.dir", kind="path", constraint="a writable directory path"),
    KeySpec(dotted="artifacts.keep_passed", kind="bool", constraint="true or false"),
    KeySpec(dotted="reporting.html", kind="bool", constraint="true or false"),
    KeySpec(dotted="reporting.json", kind="bool", constraint="true or false"),
    KeySpec(dotted="data.dir", kind="path", constraint="a directory path"),
    KeySpec(dotted="data.schema_strict", kind="bool", constraint="true or false"),
    KeySpec(dotted="cassette.dir", kind="path", constraint="a directory path"),
    KeySpec(
        dotted="cassette.mode",
        kind="enum",
        constraint=accepted_values_text(CassetteMode),
        enum_type=CassetteMode,
    ),
    KeySpec(
        dotted="execution.retries",
        kind="int",
        constraint="0..10",
        aliases=("--foundry-retries",),
    ),
    KeySpec(
        dotted="execution.timeout",
        kind="float",
        constraint="0 < t <= 3600",
        aliases=("--foundry-timeout",),
    ),
    KeySpec(
        dotted="execution.quarantine_mode",
        kind="enum",
        constraint=accepted_values_text(QuarantineMode),
        enum_type=QuarantineMode,
    ),
    KeySpec(
        dotted="secrets.redact_keys",
        kind="list",
        constraint="a comma-separated list of header or JSON key names",
    ),
    KeySpec(
        dotted="logging.level",
        kind="enum",
        constraint=accepted_values_text(LogLevel),
        enum_type=LogLevel,
    ),
    KeySpec(dotted="logging.capture_lines", kind="int", constraint="0..10000"),
)


KEY_REGISTRY: Mapping[str, KeySpec] = types.MappingProxyType(
    {spec.dotted: spec for spec in KEY_SPECS}
)
"""Every key by its dotted spelling, in a read-only view of the table.

The proxy keeps :data:`KEY_SPECS` and this index mutually consistent without exposing a dict a
caller could mutate.
"""

_ENV_TO_KEY: Mapping[str, str] = types.MappingProxyType(
    {(ENV_PREFIX + spec.dotted.replace(".", "__")).upper(): spec.dotted for spec in KEY_SPECS}
)
"""Canonical environment-variable spelling back to its dotted key."""


def leaf_keys() -> tuple[str, ...]:
    """Return every configuration key's dotted spelling, in AAP 0.4.5's order."""
    return tuple(spec.dotted for spec in KEY_SPECS)


def spec_for(dotted: str) -> KeySpec | None:
    """Return the spec declared for ``dotted``, or ``None`` when no key has that spelling.

    Nothing is raised: the caller decides whether an unknown key is an error and builds the message.
    """
    return KEY_REGISTRY.get(dotted)


def closest_key(dotted: str) -> str | None:
    """Return the registered key closest to a misspelt one, or ``None`` below a similarity of 0.6.

    The single implementation of the suggestion AAP 0.4.5 requires an unknown-key error to carry.
    ``Http.Timeout`` is suggested for ``http.timeout`` while ``HTTP.TIMEOUT`` scores too low: the
    case-insensitive reading of a variable name is :func:`key_from_env_variable`'s job.
    """
    matches = difflib.get_close_matches(dotted, leaf_keys(), n=1, cutoff=0.6)
    return matches[0] if matches else None


def cli_option(dotted: str) -> str:
    """Return the generic command-line spelling of ``dotted``, such as ``--foundry-http.timeout``.

    Derived for every key, not only the registered ones, so a caller can quote the option an
    operator would have had to write.
    """
    return GENERIC_OPTION_PREFIX + dotted


def option_dest(dotted: str) -> str:
    """Return the pytest ``dest`` the generic option for ``dotted`` stores to.

    A cross-folder contract: :mod:`foundry.plugin` registers each option with
    ``dest=option_dest(dotted)`` and :mod:`foundry.config.cli_layer` reads it back with
    ``config.getoption(option_dest(dotted), default=None)``, so the two cannot disagree.
    """
    return _OPTION_DEST_PREFIX + dotted.replace(".", "__")


def option_to_key(option: str) -> str | None:
    """Return the key an option spelling configures.

    The inverse of :func:`cli_option` for registered keys, used by :mod:`foundry.config.cli_layer`
    to label an override; a named alias such as ``--foundry-retries`` resolves to its key.
    """
    spelling = option.split("=", 1)[0]
    if not spelling.startswith(GENERIC_OPTION_PREFIX):
        return None
    candidate = spelling[len(GENERIC_OPTION_PREFIX) :]
    if candidate in KEY_REGISTRY:
        return candidate
    for spec in KEY_SPECS:
        if spelling in spec.aliases:
            return spec.dotted
    return None


def key_alias_options(dotted: str) -> tuple[str, ...]:
    """Return the named option spellings bound to ``dotted``, in declaration order.

    Empty for a key with no alias and for an unregistered key.

    Note:
        ``profile``'s alias is the generic spelling itself, because the key has no dotted name to
        lengthen it. A registrar that hands both the generic option and the aliases to one
        ``parser.addoption`` call must therefore drop the duplicate.
    """
    spec = KEY_REGISTRY.get(dotted)
    return spec.aliases if spec is not None else ()


def constraint_for(dotted: str) -> str | None:
    """Return the accepted values or range of ``dotted``, phrased for a message."""
    spec = KEY_REGISTRY.get(dotted)
    return spec.constraint if spec is not None else None


def env_variable(dotted: str) -> str:
    """Return the canonical environment variable that sets ``dotted``.

    The upper-cased name with ``__`` in place of each ``.``, for example
    ``FOUNDRY_HTTP__RETRIES__RETRY_ON``; names are read case-insensitively (AAP 0.4.5).
    """
    return (ENV_PREFIX + dotted.replace(".", "__")).upper()


def key_from_env_variable(variable: str) -> str | None:
    """Return the key a ``FOUNDRY_*`` variable sets, whichever case it arrives in.

    ``None`` when the name maps to no registered key, ``FOUNDRY_CONFIG`` among them; returning
    ``None`` rather than raising lets :mod:`foundry.config.validation` name the variable in its
    unknown-key error.
    """
    return _ENV_TO_KEY.get(variable.strip().upper())


def is_scalar(spec: KeySpec) -> bool:
    """Report whether ``spec`` can be carried by one ``--foundry-*`` option (AAP 0.4.4).

    ``False`` for the list and mapping kinds.
    """
    return spec.kind in SCALAR_KINDS


def is_env_settable(spec: KeySpec) -> bool:
    """Report whether ``spec`` can be read from a ``FOUNDRY_*`` variable.

    ``False`` only for ``mapping``, whose header table has no single-variable form.
    """
    return spec.kind in ENV_SETTABLE_KINDS
