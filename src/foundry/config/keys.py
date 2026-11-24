"""The immutable registry of Foundry's configuration keys.

Every configuration key Foundry resolves is described here exactly once: its
dotted name, the shape of its value, the text naming the values it accepts, the
``--foundry-<dotted.key>`` spelling derived from it, and the named option
aliases bound to it (AAP 0.4.4, AAP 0.4.5). Three consumers share this one
table:

- :mod:`foundry.plugin` derives the whole ``--foundry-*`` option group from
  :data:`KEY_SPECS`, registering a generic option for every key whose
  :func:`is_scalar` is true and reading each value back through
  :func:`option_dest`;
- :mod:`foundry.config.cli_layer` maps those values onto keys and labels an
  override with :func:`option_to_key`;
- :mod:`foundry.config.validation` builds its messages from
  :func:`constraint_for` and suggests a valid spelling with :func:`closest_key`.

The module is a pure table. It imports the standard library and
:mod:`foundry.config.enums` only, performs no I/O, reads no environment
variable, configures no logger and raises nothing. That discipline is what
keeps it importable from ``pytest_addoption`` on every pytest startup, inside
the 150 ms plugin budget (AAP 0.14.2, implicit requirement I5): it must never
grow an import of :mod:`pydantic`, :mod:`foundry.config.models` or any other
module on the settings path, not even inside a function, because a later
refactor that called it at import time would silently break the budget.

:data:`KEY_SPECS` and :data:`KEY_REGISTRY` are immutable constants rather than
the module-level mutable state the layering rules prohibit (AAP 0.4.8). The
tuple is built once at import, the mapping is a
:class:`types.MappingProxyType` over a fresh dict, and neither can be mutated
afterwards, so two imports, two workers and two runs observe the same table in
the same order. Only :mod:`foundry.data.registry` and
:mod:`foundry.reporting.renderers` populate mutable registration tables.

No default value appears here: the value a key falls back to is declared in
:mod:`foundry.config.models`, which is the single place a default lives and the
single place a ``default``-layer :class:`foundry.config.sources.ConfigSource`
is reported from.
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
"""The eight shapes a configuration value can take.

``str``, ``int``, ``float``, ``bool`` and ``path`` are atomic scalars, ``enum``
is one member of the :class:`enum.Enum` named by the spec's ``enum_type``,
``list`` is an ordered sequence (comma separated in its environment form per
assumption A3) and ``mapping`` is a table such as ``http.headers``.
"""

SCALAR_KINDS: Final[frozenset[KeyKind]] = frozenset({"str", "int", "float", "bool", "path", "enum"})
"""Kinds a single ``--foundry-<dotted.key>`` option can carry (AAP 0.4.4).

The two container kinds are excluded: a list or a header map has no faithful
single-token CLI form, so they are reachable only through the file and
environment layers.
"""

ENV_SETTABLE_KINDS: Final[frozenset[KeyKind]] = frozenset(
    {"str", "int", "float", "bool", "path", "enum", "list"}
)
"""Kinds :mod:`foundry.config.env` can read from a ``FOUNDRY_*`` variable.

This is :data:`SCALAR_KINDS` plus ``list``, whose elements a variable spells
comma separated (assumption A3). ``mapping`` is excluded because a header table
cannot be expressed as one variable: ``env.py`` rejects the attempt with a
:class:`foundry.core.exceptions.ConfigError` naming the variable.
"""

GENERIC_OPTION_PREFIX: Final[str] = "--foundry-"
"""Prefix of the option generated for every scalar key (AAP 0.4.4)."""

ENV_PREFIX: Final[str] = "FOUNDRY_"
"""Prefix of every environment variable that sets a key (AAP 0.4.5)."""

_OPTION_DEST_PREFIX: Final[str] = "foundry__"
"""Prefix of the pytest ``dest`` a generic option stores its value under."""


@dataclass(frozen=True, slots=True)
class KeySpec:
    """The declared shape, accepted values and option spellings of one key.

    A spec is frozen and slotted: every attribute is set once at import, and an
    instance cannot acquire attributes a later reader would have to account
    for. :data:`KEY_SPECS` holds one instance per configuration key in the
    order AAP 0.4.5 documents them.

    Attributes:
        dotted: The dotted configuration key, for example ``http.timeout``.
            It is also the spelling the environment layer nests with ``__``
            and the CLI layer appends to :data:`GENERIC_OPTION_PREFIX`.
        kind: The shape of this key's value.
        constraint: The accepted values or range, phrased for an error message
            and quoted verbatim by :mod:`foundry.config.validation`, for
            example ``0 < t <= 300`` or ``default, local, ci, staging``.
        aliases: Named option spellings bound to this key besides the generic
            one, for example ``("--foundry-retries",)`` for
            ``execution.retries``. Empty for every other key.
        enum_type: The enumeration a ``kind="enum"`` key accepts, or — for the
            ``kind="list"`` key ``http.retries.retry_on`` — the enumeration its
            elements are parsed with. ``None`` for every other key.
        nullable: Whether ``None`` is an accepted value, which is true for
            ``http.base_url`` and the five credential-name keys that store a
            variable or header *name* rather than a value (implicit
            requirement I9).
    """

    dotted: str
    kind: KeyKind
    constraint: str
    aliases: tuple[str, ...] = ()
    enum_type: type[Enum] | None = None
    nullable: bool = False


# AAP 0.4.5 table order: profile, http.*, artifacts.*, reporting.*, data.*,
# cassette.*, execution.*, secrets.redact_keys, logging.*. Adding, renaming or
# removing an entry is a breaking change to a published surface (AAP 0.4.9);
# in particular --foundry-json-report relocates report files and
# --foundry-config selects the configuration file, so neither is a key here.
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

The proxy is what makes :data:`KEY_SPECS` and this index mutually consistent
without exposing a dict a caller could mutate.
"""

_ENV_TO_KEY: Mapping[str, str] = types.MappingProxyType(
    {(ENV_PREFIX + spec.dotted.replace(".", "__")).upper(): spec.dotted for spec in KEY_SPECS}
)
"""Canonical environment-variable spelling back to its dotted key."""


def leaf_keys() -> tuple[str, ...]:
    """Return every configuration key's dotted spelling in registry order.

    Returns:
        The 28 dotted keys, in the order AAP 0.4.5 documents them. The order is
        fixed at import, so two runs and two workers iterate it identically and
        ``foundry config show`` renders the same table every time.
    """
    return tuple(spec.dotted for spec in KEY_SPECS)


def spec_for(dotted: str) -> KeySpec | None:
    """Return the spec declared for ``dotted``.

    Args:
        dotted: The dotted key to look up, for example ``http.timeout``.

    Returns:
        The matching :class:`KeySpec`, or ``None`` when no key has that
        spelling. Nothing is raised here: the caller decides whether an unknown
        key is an error and builds the message.
    """
    return KEY_REGISTRY.get(dotted)


def closest_key(dotted: str) -> str | None:
    """Return the registered key closest to a misspelt one.

    This is the single implementation of the "closest valid key" suggestion
    AAP 0.4.5 requires an unknown-key error to carry.

    Args:
        dotted: The spelling a caller supplied, which may be misspelt or
            entirely unrelated.

    Returns:
        The single nearest registered key when its similarity score reaches
        ``0.6``, otherwise ``None``. The comparison is a similarity score rather
        than a case-insensitive lookup, so ``Http.Timeout`` is suggested for
        ``http.timeout`` while ``HTTP.TIMEOUT`` scores too low to be one; the
        case-insensitive reading of a variable name is
        :func:`key_from_env_variable`'s job.
    """
    matches = difflib.get_close_matches(dotted, leaf_keys(), n=1, cutoff=0.6)
    return matches[0] if matches else None


def cli_option(dotted: str) -> str:
    """Return the generic command-line spelling of ``dotted``.

    Args:
        dotted: The dotted key, for example ``http.timeout``.

    Returns:
        The option pytest registers for that key, for example
        ``--foundry-http.timeout``. The spelling is derived for every key, not
        only the registered ones, so a caller can quote the option an operator
        would have had to write.
    """
    return GENERIC_OPTION_PREFIX + dotted


def option_dest(dotted: str) -> str:
    """Return the pytest ``dest`` the generic option for ``dotted`` stores to.

    This is a cross-folder contract: :mod:`foundry.plugin` registers each
    generic option with ``dest=option_dest(dotted)`` and
    :mod:`foundry.config.cli_layer` reads the value back with
    ``config.getoption(option_dest(dotted), default=None)``. Both sides call
    this function rather than repeating the spelling, so the two cannot
    disagree.

    Args:
        dotted: The dotted key, for example ``http.timeout``.

    Returns:
        The namespace attribute pytest uses, for example
        ``foundry__http__timeout``. The ``foundry__`` prefix keeps Foundry's
        destinations clear of pytest's own, and ``__`` replaces ``.`` because a
        destination has to be a valid argparse attribute name.
    """
    return _OPTION_DEST_PREFIX + dotted.replace(".", "__")


def option_to_key(option: str) -> str | None:
    """Return the key an option spelling configures.

    The inverse of :func:`cli_option` for registered keys, used by
    :mod:`foundry.config.cli_layer` to label an override. A named alias such as
    ``--foundry-retries`` resolves to the key it is bound to, and an option that
    configures no key — ``--foundry-config``, ``--foundry-json-report``,
    ``--foundry-record``, ``--foundry-replay`` — resolves to ``None``.

    Args:
        option: An option spelling as an operator wrote it on the command line,
            optionally carrying its value after ``=``, for example
            ``--foundry-http.timeout`` or ``--foundry-http.timeout=1.0``.

    Returns:
        The dotted key, for example ``http.timeout``, or ``None`` when the
        spelling belongs to no registered key.
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
    """Return the named option spellings bound to ``dotted``.

    Args:
        dotted: The dotted key to inspect.

    Returns:
        The alias spellings in declaration order — for example
        ``("--foundry-retries",)`` for ``execution.retries`` — or an empty tuple
        for a key with no alias and for a key that is not registered.

    Note:
        ``profile``'s alias is the generic spelling itself, because the key has
        no dotted name to lengthen it. A registrar that hands both the generic
        option and the aliases to one ``parser.addoption`` call must therefore
        drop the duplicate, or argparse rejects the repeated option string.
    """
    spec = KEY_REGISTRY.get(dotted)
    return spec.aliases if spec is not None else ()


def constraint_for(dotted: str) -> str | None:
    """Return the accepted values or range of ``dotted``, phrased for a message.

    Args:
        dotted: The dotted key to inspect.

    Returns:
        The constraint text :class:`KeySpec` records, for example
        ``0 < t <= 300`` for ``http.timeout``, or ``None`` for a key that is not
        registered.
    """
    spec = KEY_REGISTRY.get(dotted)
    return spec.constraint if spec is not None else None


def env_variable(dotted: str) -> str:
    """Return the canonical environment variable that sets ``dotted``.

    Args:
        dotted: The dotted key, for example ``http.retries.retry_on``.

    Returns:
        The upper-cased variable name with ``__`` in place of each ``.``, for
        example ``FOUNDRY_HTTP__RETRIES__RETRY_ON``. Names are read
        case-insensitively (AAP 0.4.5); this is the spelling the registry itself
        indexes by.
    """
    return (ENV_PREFIX + dotted.replace(".", "__")).upper()


def key_from_env_variable(variable: str) -> str | None:
    """Return the key a ``FOUNDRY_*`` variable sets.

    Args:
        variable: A variable name as it appears in the environment, in any case,
            for example ``foundry_http__timeout``.

    Returns:
        The dotted key it configures, for example ``http.timeout``, or ``None``
        when the name maps to no registered key — ``FOUNDRY_CONFIG``, which only
        selects the configuration file, among them. Returning ``None`` rather
        than raising lets :mod:`foundry.config.validation` build an unknown-key
        error that names the variable.
    """
    return _ENV_TO_KEY.get(variable.strip().upper())


def is_scalar(spec: KeySpec) -> bool:
    """Report whether ``spec`` can be carried by one ``--foundry-*`` option.

    :mod:`foundry.plugin` registers the generic option for exactly the keys this
    predicate accepts (AAP 0.4.4).

    Args:
        spec: The key definition to inspect.

    Returns:
        ``True`` when the key's value is a single token — a string, number,
        boolean, path or enum member — and ``False`` for the list and mapping
        kinds.
    """
    return spec.kind in SCALAR_KINDS


def is_env_settable(spec: KeySpec) -> bool:
    """Report whether ``spec`` can be read from a ``FOUNDRY_*`` variable.

    Args:
        spec: The key definition to inspect.

    Returns:
        ``True`` for every kind except ``mapping``, whose header table has no
        single-variable form; :mod:`foundry.config.env` raises
        :class:`foundry.core.exceptions.ConfigError` for the excluded one.
    """
    return spec.kind in ENV_SETTABLE_KINDS
