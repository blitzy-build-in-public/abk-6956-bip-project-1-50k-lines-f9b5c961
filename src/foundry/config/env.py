"""The ``FOUNDRY_*`` environment layer of Foundry's layered configuration.

AAP 0.4.5 fixes how a configuration key is spelled in the environment: the prefix
``FOUNDRY_``, ``__`` between the segments of a dotted key, names read case-insensitively,
and a list written comma separated (assumption A3). This module is the one place that
encoding is read. :func:`read_env` turns a mapping of variables into an
:class:`EnvPayload`: one coerced value per dotted key, plus the exact spelling of the
variable that supplied it, which is what a source record renders as ``env:<VARIABLE>`` in
``foundry config show`` (implicit requirement I2). :mod:`foundry.config.merge` owns the
precedence order and the attribution record; this module contributes one layer's values,
their spellings and nothing else.

**This is one of exactly two modules permitted to read ``os.environ``** (AAP 0.4.8); the
other is :mod:`foundry.http.auth`, which resolves the credential variables a configuration
names. Every other module under :mod:`foundry.config` -- :mod:`foundry.config.merge`,
:mod:`foundry.config.files`, :mod:`foundry.config.cli_layer`,
:mod:`foundry.config.render`, :mod:`foundry.config.models` and
:mod:`foundry.config.validation` -- receives the mapping as an argument, so a test supplies
its own environment instead of mutating the process's. :func:`effective_environ` is that
read, and it happens only when a caller passes ``None``; :func:`current_environ` is the
explicit spelling for a caller that wants the process environment by name.

Two readers share one set of rules. :func:`read_env` is the merge path: it carries the
variable spelling per key and every message AAP 0.4.5 pins, because the wording lives in
:mod:`foundry.config.errors` and the coercion in :mod:`foundry.config.validation`. A table
such as ``http.headers`` is refused, an unrecognised ``FOUNDRY_*`` name is reported as an
unknown key with its nearest registered spelling, and ``FOUNDRY_CONFIG`` is skipped
because it selects a configuration file rather than setting a key.
:func:`payload_from_source` is the cross-check: it runs the
:mod:`pydantic_settings` source machinery over the same mapping -- which is what proves
the prefix, the ``__`` nesting and the case folding come from the library rather than from
a hand-rolled parse -- and coerces what comes back with the same
:func:`foundry.config.validation.parse_env_value`, so both readers agree for every case in
the AAP 0.4.5 table.

Nothing here composes a message and nothing here describes a key: the key table, the
nearest-spelling suggestion and the error texts belong to :mod:`foundry.config.keys`,
:mod:`foundry.config.errors` and :mod:`foundry.config.validation`. No value is logged,
printed or stored beyond the payload a caller asked for, and the variables a
``http.auth.*_env`` key *names* are never followed: this module records the name of a
credential variable, never its value (implicit requirement I9).

Importing this module does no I/O, reads no environment variable, configures no logger and
holds no mutable module state. :mod:`foundry.plugin` imports it lazily inside
``pytest_configure`` and never on the ``pytest_addoption`` path, which is why
:mod:`pydantic_settings` at module scope is acceptable -- and why
:mod:`foundry.config.models` is deliberately not imported at all: the settings class the
cross-check runs against is derived from the registry, so no key table and no model is
duplicated here.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from pydantic import create_model
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, EnvSettingsSource

from foundry.config import errors, keys, layers, validation
from foundry.core.exceptions import ConfigError

__all__: Final[tuple[str, ...]] = (
    "CASE_SENSITIVE",
    "ENV_CONFIG_VAR",
    "ENV_NESTED_DELIMITER",
    "ENV_PREFIX",
    "EnvPayload",
    "FoundryEnvSource",
    "current_environ",
    "dotted_for",
    "effective_environ",
    "payload_from_source",
    "read_env",
    "variable_for",
)


ENV_PREFIX: Final[str] = keys.ENV_PREFIX
"""Prefix of every variable that sets a key (AAP 0.4.5).

Taken from the registry rather than respelled, so the function that derives a variable
name from a key and the one that recognises a variable name cannot drift apart.
"""

ENV_NESTED_DELIMITER: Final[str] = "__"
"""Separator between the segments of a dotted key inside a variable name (AAP 0.4.5).

``FOUNDRY_HTTP__RETRIES__RETRY_ON`` therefore sets ``http.retries.retry_on``: two
underscores, because a single one may occur inside a segment and the prefix already ends
in one.
"""

ENV_CONFIG_VAR: Final[str] = "FOUNDRY_CONFIG"
"""The variable that selects the configuration file instead of setting a key.

:mod:`foundry.config.files` imports this constant for the discovery step AAP 0.4.5 places
second, after ``--foundry-config``. It is not a configuration key:
:func:`foundry.config.keys.key_from_env_variable` maps it to ``None`` and both readers here
skip it, so no layer can mistake it for one.
"""

CASE_SENSITIVE: Final[bool] = False
"""Whether variable names are matched case-sensitively (AAP 0.4.5: they are not).

``FOUNDRY_HTTP__TIMEOUT`` and ``foundry_http__timeout`` set the same key. On POSIX both
spellings can be present at once, so :func:`read_env` documents which one wins.
"""

_SETTINGS_CLASS_NAME: Final[str] = "_FoundryEnvSettings"
"""Name of the throwaway settings class the cross-check runs the source machinery against."""


def effective_environ(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    """Return the mapping to read variables from, reading the process only for ``None``.

    This is the one environment read AAP 0.4.8 permits in the configuration package, and it
    happens only when the caller supplies no mapping. A supplied mapping is returned as
    given -- not copied and not consulted against the process environment -- so a test's
    environment is the whole environment for the call.

    Args:
        environ: The mapping to read, or ``None`` to read the process environment.

    Returns:
        ``environ`` itself when it is not ``None``, otherwise ``os.environ``.
    """
    if environ is None:
        return os.environ
    return environ


def current_environ() -> Mapping[str, str]:
    """Return the process environment, named explicitly at the call site.

    The same value :func:`effective_environ` returns for ``None``, offered so that a caller
    who does want the process environment says so in the code rather than by passing
    ``None``, which reads like an omitted argument.

    Returns:
        The process environment, which the caller must not mutate.
    """
    return os.environ


@dataclass(frozen=True, slots=True)
class EnvPayload:
    """What the environment layer contributes: values by dotted key, and their spellings.

    Frozen and slotted: a payload is built once, by :func:`read_env` or
    :func:`payload_from_source`, and cannot be edited afterwards, so a layer cannot be
    surprised by a later change and a reader cannot add an attribute a second reader would
    have to account for. A changed payload is a new payload.

    Attributes:
        values: One coerced value per dotted key, for example
            ``{"http.timeout": 3.5}``. Coercion follows the key's declared kind, so a
            ``float`` field holds a ``float``, a list field a tuple and an enum field a
            member of the registered enumeration.
        variables: One entry per key of :attr:`values`, holding the variable's spelling
            exactly as the environment held it -- ``FOUNDRY_HTTP__TIMEOUT`` or
            ``foundry_http__timeout`` -- because ``foundry config show`` reports the
            spelling the operator can search for rather than one Foundry folded to.
    """

    values: Mapping[str, object]
    variables: Mapping[str, str]

    def is_empty(self) -> bool:
        """Report whether the environment supplied no configuration value at all.

        An empty payload is the normal state of a run with no ``FOUNDRY_*`` variable set,
        so a caller skips the layer rather than logging or reporting an empty one.

        Returns:
            ``True`` when :attr:`values` holds no dotted key.
        """
        return len(self.values) == 0

    def dotted_keys(self) -> tuple[str, ...]:
        """Return the supplied keys in sorted order.

        Sorted rather than insertion-ordered because the order reaches a log line, the
        source table and the ``foundry config show`` document, and two runs that set the
        same variables in a different order must produce the same text (AAP 0.13.1).

        Returns:
            One dotted key per value, sorted.
        """
        return tuple(sorted(self.values))


def variable_for(dotted: str) -> str:
    """Return the canonical variable that sets ``dotted``.

    Args:
        dotted: The dotted configuration key, for example ``http.retries.retry_on``.

    Returns:
        The upper-cased name with ``__`` in place of each ``.``, for example
        ``FOUNDRY_HTTP__RETRIES__RETRY_ON`` -- the spelling a message quotes when it tells
        an operator which variable to unset.
    """
    return keys.env_variable(dotted)


def dotted_for(variable: str) -> str | None:
    """Return the key a ``FOUNDRY_*`` variable sets, or ``None`` when it sets none.

    Args:
        variable: A variable name as the environment holds it, in any case.

    Returns:
        The dotted key, for example ``http.timeout``, or ``None`` for a name that maps to
        no registered key -- ``FOUNDRY_CONFIG`` among them. Nothing is raised: the caller
        decides whether an unmapped name is an error and builds the message from
        :mod:`foundry.config.errors`.
    """
    return keys.key_from_env_variable(variable)


def read_env(environ: Mapping[str, str] | None = None) -> EnvPayload:
    """Read one layer's worth of configuration values from the environment.

    Every ``FOUNDRY_*`` entry of the mapping is matched case-insensitively against the
    registry, refused when it names no key, refused when it names a table-valued key, and
    otherwise coerced to the value its key declares -- a comma separated list to a tuple
    (assumption A3), a number to an ``int`` or a ``float``, a boolean word to a ``bool``, an
    accepted name to its enum member, a path to :class:`pathlib.Path`. The keys are then
    visited in sorted order, so the first fault reported and the payload produced are
    identical on every run and on every xdist worker.

    **Tie-break.** Two variables can name the same key when both cases are present in a
    POSIX environment. The canonical upper-case spelling wins -- ``FOUNDRY_HTTP__TIMEOUT``
    over ``foundry_http__timeout`` -- because that is the spelling
    :func:`variable_for` derives and the one a message quotes; when neither spelling is
    canonical, the entry that sorts last wins, which is again independent of the order the
    mapping happened to hold.

    Args:
        environ: The mapping to read, or ``None`` to read the process environment (see
            :func:`effective_environ`).

    Returns:
        The layer's values by dotted key, with the spelling that supplied each one.

    Raises:
        ConfigError: For the first variable, in sorted order, that names no registered key
            (message naming the intended key, hint naming the closest valid spelling and
            the variable), that names a table-valued key (a table has no single-variable
            form), or whose text does not parse as the declared kind (message naming the
            variable and the expected type). The builders live in
            :mod:`foundry.config.errors`; no wording is composed here.
    """
    mapping = effective_environ(environ)
    entries = _resolve_variables(mapping)
    values: dict[str, object] = {}
    variables: dict[str, str] = {}
    for dotted in sorted(entries):
        spec, variable = entries[dotted]
        values[dotted] = validation.parse_env_value(spec, mapping[variable], variable=variable)
        variables[dotted] = variable
    return EnvPayload(values=values, variables=variables)


class FoundryEnvSource(EnvSettingsSource):
    """The ``FOUNDRY_*`` settings source :mod:`pydantic_settings` provides, mapping-backed.

    AAP 0.2.1 names this source as the one that performs the prefixing and nesting, and
    :func:`payload_from_source` is what puts it to work. It differs from the stock source in
    three ways, each of which keeps the environment read inside this module:

    - it reads the mapping it was constructed with instead of ``os.environ``, by overriding
      the ``_load_env_vars`` hook the library documents for a source with its own storage
      (``DotEnvSettingsSource`` uses the same hook);
    - it records, in :attr:`matched_variables`, the operator's spelling of the variable the
      parent source found for a field, because the library exposes no public API naming it
      (AAP 0.2.1), which is why attribution is Foundry's own record (implicit requirement
      I2);
    - :meth:`__call__` pins the returned shape to plain builtins, so the caller hands
      :func:`foundry.config.layers.flatten` a ``Mapping[str, object]`` and no ``Any`` leaks
      upward from the library's own signature.

    Construct it with a settings class and, optionally, the mapping to read; with no mapping
    it reads the process environment, which is the behaviour :func:`effective_environ` gives
    every other reader here.
    """

    matched_variables: dict[str, str]
    """Field name to the spelling of the variable the parent source read for it.

    Only the settings model's own top-level fields pass through ``get_field_value``: a
    table's leaves are exploded by the library without a per-field callback, so the complete
    dotted-key record of spellings is :attr:`EnvPayload.variables`, which the scan every
    reader shares produces. This mapping exists for a caller that inspects a single field.
    """

    _environ: Mapping[str, str]
    """The mapping this source reads, held as given -- never mutated, never copied."""

    _spellings: Mapping[str, str]
    """Folded variable name to the spelling the environment held, for :attr:`matched_variables`."""

    def __init__(
        self,
        settings_cls: type[BaseSettings],
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        """Bind the source to a settings class and to the mapping it should read.

        The mapping and the spelling index are stored before the parent constructor runs,
        because the parent resolves its variables inside that constructor through the
        ``_load_env_vars`` hook overridden below.

        Args:
            settings_cls: The settings class whose fields the source resolves. It supplies
                the field names and annotations the nesting is derived from.
            environ: The mapping to read, or ``None`` to read the process environment.
        """
        self._environ = effective_environ(environ)
        self._spellings = _spelling_index(self._environ)
        self.matched_variables = {}
        super().__init__(
            settings_cls,
            case_sensitive=CASE_SENSITIVE,
            env_prefix=ENV_PREFIX,
            env_nested_delimiter=ENV_NESTED_DELIMITER,
        )

    def _load_env_vars(self) -> Mapping[str, str | None]:
        """Return the bound mapping, keyed the way the library looks names up.

        Names are lower-cased because :data:`CASE_SENSITIVE` is ``False``: the parent
        compares each field's own variable name against these keys, so folding both sides
        the same way is what makes ``foundry_http__timeout`` match the field the way
        ``FOUNDRY_HTTP__TIMEOUT`` does (AAP 0.4.5).

        Returns:
            One entry per bound variable, keyed by its lower-cased name.
        """
        return {name.lower(): value for name, value in self._environ.items()}

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        """Return the parent's answer for a field and record which variable supplied it.

        The parent returns the value, the field key the payload is keyed by and whether the
        value is complex; AAP 0.2.1 records that it names no variable, so the spelling is
        recovered here from the bound mapping and stored in :attr:`matched_variables`. The
        signature and the return type are the parent's -- ``Any`` included -- because this
        is an override of a library hook rather than part of Foundry's own public API.

        Args:
            field: The field the parent is resolving.
            field_name: The field's name, which is the head of its variable's name.

        Returns:
            The parent's ``(value, field key, value is complex)`` triple, unchanged.
        """
        value, field_key, value_is_complex = super().get_field_value(field, field_name)
        if value is not None:
            variable = self._spellings.get(f"{ENV_PREFIX}{field_name}".upper())
            if variable is not None:
                self.matched_variables[field_name] = variable
        return value, field_key, value_is_complex

    def __call__(self) -> dict[str, object]:
        """Return the nested payload the source resolves for its bound mapping.

        The tables come back as ordinary dictionaries built by the library's own nesting
        pass, which is what :func:`foundry.config.layers.flatten` then turns into dotted
        keys. The copy pins the value type to ``object``, so the library's ``Any`` stops
        here rather than spreading through every caller.

        Returns:
            The nested values, for example
            ``{"http": {"timeout": "3.5", "retries": {"retry_on": "timeout,status_429"}}}``.
            Leaves are the variables' text: coercion is the layer's job, done by
            :func:`foundry.config.validation.parse_env_value` for both readers.
        """
        nested: dict[str, Any] = super().__call__()
        return dict(nested)


def payload_from_source(environ: Mapping[str, str]) -> EnvPayload:
    """Build the layer's payload through :mod:`pydantic_settings` rather than by hand.

    The cross-check of :func:`read_env`. It resolves the same names -- so an unknown
    variable, a table-valued one and an unparseable value are refused with the same errors
    and in the same order -- then hands those variables to :class:`FoundryEnvSource`, which
    is what establishes that the prefix, the ``__`` nesting and the case folding really do
    turn variable names into these dotted keys, and coerces each recovered leaf with the
    same :func:`foundry.config.validation.parse_env_value` call the merge path makes.
    Agreement is therefore a property of the construction, which a unit test asserts for
    every case in the AAP 0.4.5 table.

    The source is given the variable each key resolved to rather than the whole mapping.
    That is the one case the library cannot express: it folds every name before it matches,
    so two spellings of one key collide inside it, and which of them supplies the key is
    Foundry's tie-break (:func:`read_env`), not the library's. Selecting first keeps both
    readers in agreement without taking the prefix and nesting work away from the library.

    Only the keys both readers resolved enter the payload. A key the scan resolved that the
    source did not report is therefore left out rather than filled from the mapping, so a
    regression in the library's prefix, delimiter or case handling shows up as a
    disagreement with :func:`read_env` instead of being papered over.

    The settings class it runs against is derived from the registry at call time rather than
    imported from :mod:`foundry.config.models`: this module describes no key and imports no
    model, so the cross-check cannot disagree with the contract of the published surface.

    Args:
        environ: The mapping to read. There is no ``None`` default, because a caller of the
            cross-check always names the environment it wants verified.

    Returns:
        The layer's values by dotted key, with the spelling that supplied each one, exactly
        as :func:`read_env` reports them.

    Raises:
        ConfigError: For the same faults, in the same order, that :func:`read_env` refuses.
    """
    mapping = effective_environ(environ)
    entries = _resolve_variables(mapping)
    selected: dict[str, str] = {}
    for _, variable in entries.values():
        selected[variable] = mapping[variable]
    flat = layers.flatten(FoundryEnvSource(_settings_class(), environ=selected)())
    values: dict[str, object] = {}
    variables: dict[str, str] = {}
    for dotted in sorted(entries.keys() & flat.keys()):
        spec, variable = entries[dotted]
        raw = _raw_text(flat[dotted], fallback=mapping[variable])
        values[dotted] = validation.parse_env_value(spec, raw, variable=variable)
        variables[dotted] = variable
    return EnvPayload(values=values, variables=variables)


def _candidates(mapping: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    """Return the mapping's configuration variables as folded name and given spelling.

    A candidate is an entry whose upper-cased name starts with :data:`ENV_PREFIX`, except
    :data:`ENV_CONFIG_VAR`, which selects a configuration file rather than a key. The list
    is sorted by folded name and then by the spelling as given, so both the first fault
    reported and the tie-break outcome are independent of the order the mapping happened to
    hold -- which is what makes two runs and two workers agree (AAP 0.13.1). The pass is a
    string prefix test and one ``upper()`` per name: no regular expression and no copy of
    the whole environment, because :func:`read_env` sits inside the 50 ms configuration
    budget (AAP 0.14.2).

    Args:
        mapping: The environment to scan, as given.

    Returns:
        One ``(folded name, spelling)`` pair per configuration variable, sorted.
    """
    found: list[tuple[str, str]] = []
    for name in mapping:
        folded = name.upper()
        if not folded.startswith(ENV_PREFIX) or folded == ENV_CONFIG_VAR:
            continue
        found.append((folded, name))
    found.sort()
    return tuple(found)


def _spelling_index(mapping: Mapping[str, str]) -> Mapping[str, str]:
    """Return each variable's folded name mapped to the spelling the environment held.

    The index is the reverse lookup :class:`FoundryEnvSource` needs in order to name the
    variable the library matched, which the library itself does not report (implicit
    requirement I2). Both cases of one name collapse onto the canonical upper-case spelling,
    under the same rule :func:`read_env` documents.

    Args:
        mapping: The environment to index.

    Returns:
        One entry per configuration variable, keyed by its folded name.
    """
    index: dict[str, str] = {}
    for folded, spelling in _candidates(mapping):
        recorded = index.get(folded)
        if recorded is None or recorded != folded:
            index[folded] = spelling
    return index


def _intended_key(folded_name: str) -> str:
    """Return the dotted key a variable name was spelling before it was rejected.

    Args:
        folded_name: The variable's upper-cased name, which is known to start with
            :data:`ENV_PREFIX`.

    Returns:
        The dotted form the name describes, for example ``http.timeout`` for
        ``FOUNDRY_HTTP__TIMEOUT``. It is what the unknown-key message names and what the
        nearest-spelling suggestion is computed from, so a misspelt variable is answered
        with the key its author meant.
    """
    suffix = folded_name[len(ENV_PREFIX) :]
    return suffix.lower().replace(ENV_NESTED_DELIMITER, ".")


def _unknown_variable(spelling: str, folded_name: str) -> ConfigError:
    """Build the unknown-key error for a variable that sets no registered key.

    AAP 0.4.5's unknown-key rule applies to this layer too, so the error is the one
    :func:`foundry.config.errors.unknown_key` words: the message names the key the variable
    was spelling, the hint carries the closest registered key and the variable itself, and
    nothing about the value is quoted.

    Args:
        spelling: The variable's name as the environment held it.
        folded_name: The same name upper-cased, from which the intended key is derived.

    Returns:
        The :class:`foundry.core.exceptions.ConfigError` the caller raises.
    """
    intended = _intended_key(folded_name)
    return errors.unknown_key(
        intended,
        closest=keys.closest_key(intended),
        layer=errors.LAYER_ENV,
        detail=spelling,
    )


def _resolve_variables(mapping: Mapping[str, str]) -> dict[str, tuple[keys.KeySpec, str]]:
    """Match every configuration variable to its key, refusing the ones that fit none.

    The single pass both readers make, so :func:`read_env` and :func:`payload_from_source`
    cannot refuse different names or report them in a different order. Candidates are
    visited in the order :func:`_candidates` fixes, which makes the first fault of a mapping
    with several deterministic.

    The mapping of a key that two spellings name is decided by the rule :func:`read_env`
    documents: the canonical upper-case spelling wins, and among non-canonical spellings the
    one that sorts last wins.

    Args:
        mapping: The environment to resolve.

    Returns:
        One entry per dotted key, holding the key's spec and the spelling that supplied it.

    Raises:
        ConfigError: For a name that maps to no registered key, or for one that names a
            table-valued key, whose table has no single-variable form.
    """
    entries: dict[str, tuple[keys.KeySpec, str]] = {}
    for folded, spelling in _candidates(mapping):
        dotted = keys.key_from_env_variable(spelling)
        spec = keys.spec_for(dotted) if dotted is not None else None
        if spec is None:
            raise _unknown_variable(spelling, folded)
        if not keys.is_env_settable(spec):
            raise errors.env_key_unsupported(spelling, spec.dotted)
        recorded = entries.get(spec.dotted)
        if recorded is None or recorded[1] != keys.env_variable(spec.dotted):
            entries[spec.dotted] = (spec, spelling)
    return entries


def _settings_class() -> type[BaseSettings]:
    """Derive the settings class :class:`FoundryEnvSource` resolves its fields from.

    Built from :data:`foundry.config.keys.KEY_SPECS` on every call rather than cached,
    because a cache would be the module-level mutable state AAP 0.4.8 prohibits; the
    cross-check is not the merge path, so the construction cost is irrelevant where the
    budget applies.

    The shape is the registry's: a key that names no table is a top-level string field, and
    every segment that opens a dotted key becomes a table typed ``dict[str, object]``. The
    library then does the work the cross-check is checking -- applying the prefix, splitting
    on the delimiter, folding case and assembling the nested payload -- while the leaves
    stay the variables' text for both readers to coerce identically. Typing a table as a
    dictionary rather than as a further model keeps a key such as ``reporting.json`` from
    shadowing an attribute of :class:`pydantic.BaseModel`, which the library's own
    introspection warns about.

    Returns:
        A settings class declaring exactly the keys the environment layer can set.
    """
    fields: dict[str, Any] = {}
    for spec in keys.KEY_SPECS:
        if not keys.is_env_settable(spec):
            continue
        if "." in spec.dotted:
            fields[spec.dotted.split(".", 1)[0]] = (dict[str, object], ...)
        else:
            fields[spec.dotted] = (str, ...)
    return create_model(_SETTINGS_CLASS_NAME, __base__=BaseSettings, **fields)


def _raw_text(value: object, *, fallback: str) -> str:
    """Return the text a resolved leaf carries.

    The leaves the derived settings class produces are the variables' text, which is what
    both readers parse. The fallback covers a leaf the library chose to coerce instead: no
    annotation in the derived class asks for that, so reaching it would mean the library
    changed, and the variable's own text keeps the value the environment holds rather than a
    half-coerced one.

    Args:
        value: The leaf the source reported for a key both readers resolved.
        fallback: The text of the variable that set the key, read from the mapping.

    Returns:
        ``value`` when it is text, otherwise ``fallback``.
    """
    if isinstance(value, str):
        return value
    return fallback
