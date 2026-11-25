"""The ``FOUNDRY_*`` environment layer of Foundry's layered configuration.

AAP 0.4.5 fixes the encoding read here: the ``FOUNDRY_`` prefix, ``__`` between the segments of a
dotted key, case-insensitive names and a comma-separated list. This is one of the two modules that
may read ``os.environ``, and a variable naming a credential is recorded by name, never followed.
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
"""Prefix of every variable that sets a key, taken from the registry rather than respelled."""

ENV_NESTED_DELIMITER: Final[str] = "__"
"""Separator between the segments of a dotted key inside a variable name."""

ENV_CONFIG_VAR: Final[str] = "FOUNDRY_CONFIG"
"""The variable that selects the configuration file instead of setting a key.

:mod:`foundry.config.files` imports it for the discovery step, and both readers here skip it.
"""

CASE_SENSITIVE: Final[bool] = False
"""Whether variable names are matched case-sensitively, which they are not.

Both cases of one name can be present at once, so :func:`read_env` states which one wins.
"""

_SETTINGS_CLASS_NAME: Final[str] = "_FoundryEnvSettings"
"""Name of the throwaway settings class the cross-check runs the source machinery against."""


def effective_environ(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    """Return the mapping to read variables from, reading the process only for ``None``.

    A supplied mapping is returned as given, so a test's environment is the whole environment.
    """
    if environ is None:
        return os.environ
    return environ


def current_environ() -> Mapping[str, str]:
    """Return the process environment, named explicitly at the call site.

    Returns:
        The process environment, which the caller must not mutate.
    """
    return os.environ


@dataclass(frozen=True, slots=True)
class EnvPayload:
    """What the environment layer contributes: values by dotted key, and their spellings.

    Frozen and slotted, so a payload built once cannot be edited afterwards.

    Attributes:
        values: One coerced value per dotted key, following the key's declared kind.
        variables: The spelling the environment held for each key, which is what a source report
            shows rather than one Foundry folded to.
    """

    values: Mapping[str, object]
    variables: Mapping[str, str]

    def is_empty(self) -> bool:
        """Report whether the environment supplied no configuration value at all.

        Returns:
            ``True`` when :attr:`values` holds no dotted key.
        """
        return len(self.values) == 0

    def dotted_keys(self) -> tuple[str, ...]:
        """Return the supplied keys in sorted order, the order every report prints them in.

        Returns:
            One dotted key per value, sorted.
        """
        return tuple(sorted(self.values))


def variable_for(dotted: str) -> str:
    """Return the canonical variable that sets ``dotted``, the spelling a message quotes."""
    return keys.env_variable(dotted)


def dotted_for(variable: str) -> str | None:
    """Return the key a ``FOUNDRY_*`` variable sets, or ``None`` when it sets none.

    Nothing is raised: the caller decides whether an unmapped name is an error.
    """
    return keys.key_from_env_variable(variable)


def read_env(environ: Mapping[str, str] | None = None) -> EnvPayload:
    """Read one layer's worth of configuration values from the environment.

    Every ``FOUNDRY_*`` entry is matched case-insensitively against the registry, refused when it
    names no key or a table-valued one, and otherwise coerced to the value its key declares, a
    comma-separated list becoming a tuple. Keys are visited in sorted order, so the first fault and
    the payload are identical on every run and every xdist worker.

    **Tie-break.** Two variables can name one key when both cases are present in a POSIX
    environment: the canonical upper-case spelling wins, being what :func:`variable_for` derives
    and what a message quotes, and among non-canonical spellings the one that sorts last.

    Raises:
        ConfigError: For the first variable, in sorted order, naming no registered key, naming a
            table-valued key, or whose text does not parse as the declared kind.
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

    It reads the mapping it was constructed with instead of ``os.environ``, records in
    :attr:`matched_variables` the operator's spelling the parent found -- which the library exposes
    nowhere -- and pins :meth:`__call__` to plain builtins, so no ``Any`` leaks upward from the
    library's signature.
    """

    matched_variables: dict[str, str]
    """Field name to the spelling of the variable the parent source read for it.

    Only the settings model's own top-level fields pass through ``get_field_value``: a table's
    leaves are exploded by the library without a per-field callback, so the complete record of
    spellings is :attr:`EnvPayload.variables`. This mapping serves a caller inspecting one field.
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

        The mapping and its spelling index are stored before the parent constructor runs, because
        the parent resolves its variables inside that constructor.
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

        Names are lower-cased because :data:`CASE_SENSITIVE` is ``False``, which is what makes
        ``foundry_http__timeout`` match as ``FOUNDRY_HTTP__TIMEOUT`` does.
        """
        return {name.lower(): value for name, value in self._environ.items()}

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        """Return the parent's answer for a field and record which variable supplied it.

        The parent names no variable, so the spelling is recovered here from the bound mapping. The
        ``Any`` in the signature is the parent's, this being an override of a library hook.
        """
        value, field_key, value_is_complex = super().get_field_value(field, field_name)
        if value is not None:
            variable = self._spellings.get(f"{ENV_PREFIX}{field_name}".upper())
            if variable is not None:
                self.matched_variables[field_name] = variable
        return value, field_key, value_is_complex

    def __call__(self) -> dict[str, object]:
        """Return the nested payload the source resolves for its bound mapping.

        The tables come back as ordinary dictionaries built by the library's own nesting pass, which
        :func:`foundry.config.layers.flatten` turns into dotted keys.
        """
        nested: dict[str, Any] = super().__call__()
        return dict(nested)


def payload_from_source(environ: Mapping[str, str]) -> EnvPayload:
    """Build the layer's payload through :mod:`pydantic_settings` rather than by hand.

    The cross-check of :func:`read_env`: it resolves the same names with the same errors and order
    and coerces each leaf through the same :func:`foundry.config.validation.parse_env_value` call,
    so agreement is a property of the construction rather than of two hand-kept copies.

    The source is given only the variable each key resolved to rather than the whole mapping,
    because the library folds every name before matching: two spellings of one key collide inside
    it, and which supplies the key is Foundry's tie-break. Only keys both readers resolved enter the
    payload, so a regression in the library's prefix, delimiter or case handling shows up as a
    disagreement instead of being papered over.

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

    An entry qualifies when its upper-cased name starts with :data:`ENV_PREFIX`, except
    :data:`ENV_CONFIG_VAR`. The list is sorted by folded name and then by the spelling as given, so
    the first fault and the tie-break outcome are independent of the mapping's own order; the pass
    is a prefix test and one ``upper()`` per name, because :func:`read_env` sits inside the 50 ms
    configuration budget.
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

    The reverse lookup :class:`FoundryEnvSource` needs to name the variable the library matched,
    which the library does not report; both cases of one name collapse onto the canonical spelling.
    """
    index: dict[str, str] = {}
    for folded, spelling in _candidates(mapping):
        recorded = index.get(folded)
        if recorded is None or recorded != folded:
            index[folded] = spelling
    return index


def _intended_key(folded_name: str) -> str:
    """Return the dotted key a variable name was spelling before it was rejected.

    ``FOUNDRY_HTTP__TIMEOUT`` describes ``http.timeout``, which the unknown-key message names and
    the nearest-spelling suggestion is computed from, so a misspelt variable is answered with the
    key its author meant.
    """
    suffix = folded_name[len(ENV_PREFIX) :]
    return suffix.lower().replace(ENV_NESTED_DELIMITER, ".")


def _unknown_variable(spelling: str, folded_name: str) -> ConfigError:
    """Build the unknown-key error for a variable that sets no registered key.

    The unknown-key rule applies to this layer too, so the message names the key the variable was
    spelling, the hint carries the closest registered key, and no value is quoted. A bare
    ``FOUNDRY_`` spells no key at all and is answered by
    :func:`foundry.config.errors.unknown_variable` instead.

    Returns:
        The :class:`ConfigError` the caller raises.
    """
    intended = _intended_key(folded_name)
    if not intended:
        return errors.unknown_variable(spelling)
    return errors.unknown_key(
        intended,
        closest=keys.closest_key(intended),
        layer=errors.LAYER_ENV,
        detail=spelling,
    )


def _resolve_variables(mapping: Mapping[str, str]) -> dict[str, tuple[keys.KeySpec, str]]:
    """Match every configuration variable to its key, refusing the ones that fit none.

    The single pass both readers make, so they cannot refuse different names or report them in a
    different order.

    Returns:
        One entry per dotted key, holding the key's spec and the spelling that supplied it.

    Raises:
        ConfigError: For a name that maps to no registered key, or names a table-valued key.
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

    Built on every call rather than cached, a cache being the module-level mutable state the plan
    prohibits. A key naming no table is a top-level string field and every segment opening a dotted
    key becomes a ``dict[str, object]`` table, which keeps a key such as ``reporting.json`` from
    shadowing an attribute of :class:`pydantic.BaseModel`.
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

    The derived class produces the variables' text, which both readers parse; the fallback covers a
    leaf the library chose to coerce.
    """
    if isinstance(value, str):
        return value
    return fallback
