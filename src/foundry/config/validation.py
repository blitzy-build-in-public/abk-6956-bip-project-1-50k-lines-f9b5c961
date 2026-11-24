"""Unknown-key rejection, value coercion and error translation for the configuration layer.

Each layer rejects an unknown key on its *flat* dotted payload **before** the settings model is
built, because ``extra="forbid"`` alone reports it with no closest-key suggestion; every message
belongs to :mod:`foundry.config.errors`, and no credential reaches one (AAP 0.4.5, AAP 0.3.2).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from foundry.config.enums import accepted_values_text, parse_enum
from foundry.config.errors import (
    LAYER_CLI,
    bad_env_value,
    bad_option_value,
    cli_detail,
    env_key_unsupported,
    invalid_shape,
    missing_required_for,
    range_violation,
    unknown_key,
)
from foundry.config.errors import success_message as success_message
from foundry.config.keys import KeySpec, closest_key, is_env_settable, spec_for
from foundry.core.exceptions import ConfigError

__all__ = [
    "BOOL_FALSE",
    "BOOL_TRUE",
    "CLI_BOOL_WORDS",
    "accepted_range",
    "coerce_cli_value",
    "parse_env_value",
    "success_message",
    "translate_validation_error",
    "validate_file_values",
    "validate_payload_shape",
    "validate_unknown_keys",
]


# An operator exporting FOUNDRY_DATA__SCHEMA_STRICT reaches for any of these words, and 1/0
# is what a shell script computes, so the environment form accepts all four pairs.
BOOL_TRUE: Final[frozenset[str]] = frozenset({"true", "1", "yes", "on"})
"""The spellings an environment variable may use for ``True``."""

BOOL_FALSE: Final[frozenset[str]] = frozenset({"false", "0", "no", "off"})
"""The spellings an environment variable may use for ``False``."""

CLI_BOOL_WORDS: Final[frozenset[str]] = frozenset({"true", "false"})
"""The two words a ``--foundry-*`` option accepts for a boolean (AAP 0.4.4).

Narrower than the environment form on purpose: ``1`` is accepted there and rejected here.
"""

_ENV_BOOL_WORDS: Final[frozenset[str]] = BOOL_TRUE | BOOL_FALSE

_UNATTRIBUTED_LAYER: Final[str] = "resolved"

_NO_KEY: Final[str] = "configuration"

_DOCUMENTED_VALUE: Final[str] = "the documented value"

_STRING_VALUE: Final[str] = "a string value"

_TABLE_VALUE: Final[str] = "(a table)"

_SEQUENCE_VALUE: Final[str] = "(a list)"

_EMPTY_VALUE: Final[str] = "(no value)"

_EMPTY_PATH_VALUE: Final[str] = "(an empty path)"

_INT_FAULT_TYPES: Final[frozenset[str]] = frozenset({"int_from_float", "int_parsing"})

_UNRENDERABLE_VALUE: Final[str] = "(a value of an unsupported type)"

_KEY_IN_MESSAGE_RE: Final[re.Pattern[str]] = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+")

_QUOTED_IN_MESSAGE_RE: Final[re.Pattern[str]] = re.compile(r"'([^']*)'")

_BOUNDS_INTERVAL_RE: Final[re.Pattern[str]] = re.compile(r"^(\d+(?:\.\d+)?)\.\.(\d+(?:\.\d+)?)$")

_BOUNDS_LIMIT_RE: Final[re.Pattern[str]] = re.compile(r"^(\d+(?:\.\d+)?) < t <= (\d+(?:\.\d+)?)$")


def accepted_range(spec: KeySpec) -> str:
    """Return the accepted values or range of ``spec`` as a message quotes it (AAP 0.4.5)."""
    return spec.constraint


def validate_unknown_keys(values: Mapping[str, object], *, layer: str, detail: str) -> None:
    """Reject the first key of a flat payload that the configuration registry lacks.

    The key reported is the mapping's first undeclared entry, so a payload built from sorted input
    is refused deterministically; an entry nested under a mapping-kind key is accepted, being an
    operator's header name and not a configuration key.

    Raises:
        ConfigError: For that key, with the closest valid spelling as its hint (AAP 0.4.5).
    """
    for key in values:
        if spec_for(key) is not None:
            continue
        if _nested_under_mapping_key(key):
            continue
        raise unknown_key(key, closest=closest_key(key), layer=layer, detail=detail)


def validate_payload_shape(payload: Mapping[str, object], *, layer: str) -> None:
    """Reject an unknown key in an already-flat payload, with no layer detail to name.

    Raises:
        ConfigError: For the first key the registry does not declare.
    """
    validate_unknown_keys(payload, layer=layer, detail="")


def validate_file_values(values: Mapping[str, object], *, layer: str, detail: str) -> None:
    """Reject the first file-layer value whose Python type its key does not declare.

    TOML types a boolean, an integer and a float distinctly while the settings model runs in
    pydantic's lax mode, so this check keeps a stray ``true`` from becoming a one-second timeout
    (AAP 0.4.5); a whole-number float or numeric text for an ``int`` key still reaches the model.

    Raises:
        ConfigError: For the first such value, in sorted key order, worded like every value fault.
    """
    for key in sorted(values):
        spec = spec_for(key)
        if spec is None or _nested_under_mapping_key(key):
            continue
        value = values[key]
        bool_for_number = spec.kind in {"int", "float"} and isinstance(value, bool)
        number_for_bool = spec.kind == "bool" and not isinstance(value, bool)
        empty_path = spec.kind == "path" and isinstance(value, str) and not value.strip()
        if bool_for_number or number_for_bool or empty_path:
            rendered = _EMPTY_PATH_VALUE if empty_path else _render_value(value)
            raise range_violation(key, rendered, spec.constraint, layer=layer, detail=detail)
        if spec.kind == "list" and spec.enum_type is not None and isinstance(value, (list, tuple)):
            for index, element in enumerate(value, start=1):
                if parse_enum(spec.enum_type, str(element)) is None:
                    note = f"element {index} is {_render_value(element)!r}"
                    raise range_violation(key, note, spec.constraint, layer=layer, detail=detail)


def parse_env_value(spec: KeySpec, raw: str, *, variable: str) -> object:
    """Turn one ``FOUNDRY_*`` variable's text into the value its key declares.

    A list's elements are comma separated and an empty value parses to an empty tuple; a
    mapping-kind key has no single-variable form (AAP 0.4.5, assumption A3).

    Raises:
        ConfigError: When the key is not settable from one variable, or its text does not parse.
    """
    if not is_env_settable(spec):
        raise env_key_unsupported(variable, spec.dotted)

    def error_for(expected: str) -> ConfigError:
        return bad_env_value(variable, expected, key=spec.dotted)

    if spec.kind == "list":
        return _parse_list_elements(spec, raw.split(","), error_for=error_for)
    return _parse_scalar(spec, raw, bool_words=_ENV_BOOL_WORDS, error_for=error_for)


def coerce_cli_value(spec: KeySpec, raw: object, *, option: str) -> object:
    """Turn one ``--foundry-*`` option's value into the value its key declares.

    A value already of the target type passes through after a range probe and text is parsed by
    the environment rules, except that a boolean accepts only ``true`` and ``false`` (AAP 0.4.4);
    ``None``, an option not supplied, comes back as no override at all.

    Raises:
        ConfigError: When the value cannot be parsed or lies outside the accepted range.
    """
    spelling = cli_detail(option)

    def error_for(expected: str) -> ConfigError:
        return bad_option_value(spelling, expected, key=spec.dotted)

    if raw is None:
        return None
    if spec.kind == "list":
        return _coerce_list_value(spec, raw, error_for=error_for)
    if isinstance(raw, str):
        parsed = _parse_scalar(spec, raw, bool_words=CLI_BOOL_WORDS, error_for=error_for)
        if isinstance(parsed, int | float) and not isinstance(parsed, bool):
            _probe_range(spec, float(parsed), option=spelling)
        return parsed
    if spec.kind == "bool" and isinstance(raw, bool):
        return raw
    if spec.kind == "int" and isinstance(raw, int) and not isinstance(raw, bool):
        _probe_range(spec, float(raw), option=spelling)
        return raw
    if spec.kind == "float" and isinstance(raw, (int, float)) and not isinstance(raw, bool):
        number = float(raw)
        _probe_range(spec, number, option=spelling)
        return number
    if spec.kind == "path" and isinstance(raw, Path):
        return raw
    if spec.kind == "enum" and spec.enum_type is not None and isinstance(raw, spec.enum_type):
        return raw
    raise error_for(spec.constraint)


def _parse_scalar(
    spec: KeySpec,
    raw: str,
    *,
    bool_words: frozenset[str],
    error_for: Callable[[str], ConfigError],
) -> object:
    """Parse one atomic value by the rules the environment and the command line share.

    ``error_for`` builds the calling layer's error, so both surfaces parse identically while each
    message names the spelling its reader can change; an empty path is refused, not built.
    """
    if spec.kind == "str":
        return _without_trailing_newline(raw)
    if spec.kind == "path":
        text = _without_trailing_newline(raw)
        if not text.strip():
            raise error_for(spec.constraint)
        return Path(text)
    if spec.kind == "int":
        try:
            return int(raw)
        except ValueError:
            raise error_for("an integer") from None
    if spec.kind == "float":
        try:
            return float(raw)
        except ValueError:
            raise error_for("a number") from None
    if spec.kind == "bool":
        parsed = _bool_value(raw, words=bool_words)
        if parsed is None:
            raise error_for("true or false")
        return parsed
    if spec.kind == "enum":
        expected = (
            accepted_values_text(spec.enum_type) if spec.enum_type is not None else spec.constraint
        )
        if spec.enum_type is not None:
            member = parse_enum(spec.enum_type, raw)
            if member is not None:
                return member
        raise error_for(expected)
    raise error_for(spec.constraint)


def _coerce_list_value(
    spec: KeySpec, raw: object, *, error_for: Callable[[str], ConfigError]
) -> tuple[object, ...]:
    """Parse a list-kind option given as comma-separated text or as a split sequence.

    Raises:
        ConfigError: When an element is not an accepted value of the key's enumeration.
    """
    if isinstance(raw, str):
        return _parse_list_elements(spec, raw.split(","), error_for=error_for)
    if isinstance(raw, (list, tuple)):
        return _parse_list_elements(spec, (str(item) for item in raw), error_for=error_for)
    raise error_for(spec.constraint)


def _parse_list_elements(
    spec: KeySpec, elements: Iterable[str], *, error_for: Callable[[str], ConfigError]
) -> tuple[object, ...]:
    """Parse the elements of a list-kind value, dropping the empty ones, in the order given.

    ``a, b ,`` yields ``("a", "b")``, and an empty or all-empty value yields ``()``, the spelling
    that clears a list the file layer supplied.
    """
    kept = [element.strip() for element in elements]
    kept = [element for element in kept if element]
    if not kept:
        return ()
    if spec.enum_type is None:
        return tuple(kept)
    expected = accepted_values_text(spec.enum_type)
    members: list[object] = []
    for index, element in enumerate(kept, start=1):
        member = parse_enum(spec.enum_type, element)
        if member is None:
            raise error_for(f"{expected} (element {index} is {_render_value(element)!r})")
        members.append(member)
    return tuple(members)


def _bool_value(raw: str, *, words: frozenset[str]) -> bool | None:
    """Return the boolean ``raw`` spells with ``words``, or ``None`` when it spells none.

    The caller builds the error for ``None``, which keeps the layer's wording out of the parser.
    """
    lowered = raw.strip().lower()
    if lowered not in words:
        return None
    return lowered in BOOL_TRUE


def _without_trailing_newline(raw: str) -> str:
    r"""Return ``raw`` with a single trailing newline removed and nothing else changed.

    ``export FOUNDRY_DATA__DIR=$(pwd)`` captures the newline its command printed, so exactly one is
    tolerated and every other character, spaces included, is left alone.
    """
    if raw.endswith("\r\n"):
        return raw[:-2]
    if raw.endswith(("\n", "\r")):
        return raw[:-1]
    return raw


def _bounds(spec: KeySpec) -> tuple[float, bool, float, bool] | None:
    """Return the numeric bounds the key's constraint text states, if it states any.

    Reading the same constraint text the message quotes (AAP 0.4.9) keeps the two in agreement.
    """
    text = spec.constraint.strip()
    interval = _BOUNDS_INTERVAL_RE.match(text)
    if interval is not None:
        return (float(interval.group(1)), True, float(interval.group(2)), True)
    limit = _BOUNDS_LIMIT_RE.match(text)
    if limit is not None:
        return (float(limit.group(1)), False, float(limit.group(2)), True)
    return None


def _probe_range(spec: KeySpec, value: float, *, option: str) -> None:
    """Raise when ``value`` lies outside the bounds the key's constraint states.

    Raises:
        ConfigError: For a value outside the range, worded as AAP 0.4.5 works it through.
    """
    bounds = _bounds(spec)
    if bounds is None:
        return
    low, low_inclusive, high, high_inclusive = bounds
    below = value < low if low_inclusive else value <= low
    above = value > high if high_inclusive else value >= high
    if below or above:
        raise range_violation(
            spec.dotted,
            _render_value(value),
            spec.constraint,
            layer=LAYER_CLI,
            detail=option,
        )


def translate_validation_error(
    exc: ValidationError, *, layer: str | None = None, detail: str = ""
) -> ConfigError:
    """Return the :class:`ConfigError` describing the first failure in ``exc``.

    The settings model reports one entry per rejected field in declaration order, so the first is
    the first key a person should fix, in the wording AAP 0.4.5 documents rather than pydantic's.
    """
    label = layer if layer is not None else _UNATTRIBUTED_LAYER
    reported = exc.errors()
    if not reported:
        return range_violation(_NO_KEY, _EMPTY_VALUE, _DOCUMENTED_VALUE, layer=label, detail=detail)
    first = reported[0]
    message = _clean_message(first["msg"])
    dotted = _dotted_from_loc(first["loc"])
    key = dotted if dotted else _NO_KEY
    cross_field = _cross_field_error(message)
    if cross_field is not None:
        return cross_field
    spec = spec_for(key)
    if spec is not None and spec.kind == "mapping":
        return invalid_shape(key, spec.constraint, layer=label)
    if spec is None:
        constraint = _STRING_VALUE if _nested_under_mapping_key(key) else _DOCUMENTED_VALUE
    else:
        constraint = accepted_range(spec)
        if spec.kind == "int" and first["type"] in _INT_FAULT_TYPES:
            constraint = f"an integer in {constraint}"
    return range_violation(
        key, _render_value(first["input"]), constraint, layer=label, detail=detail
    )


def _dotted_from_loc(loc: tuple[int | str, ...]) -> str:
    """Return the dotted key an error's location names, ignoring list indices.

    A location naming a nested model only, such as ``("http", "auth")``, yields a path that is not a
    registered key, which is how the caller knows it is not a leaf.
    """
    return ".".join(str(part) for part in loc if isinstance(part, str))


def _clean_message(message: str) -> str:
    """Return a validator's message without the prefix pydantic adds to it.

    The text the rule itself wrote is what the keys it names can be read back out of.
    """
    for prefix in ("Value error, ", "Assertion failed, "):
        if message.startswith(prefix):
            return message[len(prefix) :]
    return message


def _cross_field_error(message: str) -> ConfigError | None:
    """Return the cross-field error ``message`` states, or ``None`` when it states none.

    A model validator's rule text is kept verbatim, so the keys and the quoted strategy value are
    read back out of it and the sentence is rebuilt by the builder that owns its wording.
    """
    mentioned = [key for key in _KEY_IN_MESSAGE_RE.findall(message) if spec_for(key) is not None]
    strategy_keys = [key for key in mentioned if _is_strategy_key(key)]
    companions = [key for key in mentioned if key not in strategy_keys]
    if not strategy_keys or not companions:
        return None
    quoted = _QUOTED_IN_MESSAGE_RE.search(message)
    strategy = quoted.group(1) if quoted is not None else "the selected strategy"
    return missing_required_for(companions[0], strategy_keys[0], strategy=strategy)


def _is_strategy_key(key: str) -> bool:
    """Return whether the leaf of ``key`` is the one whose value is an auth strategy."""
    return key.rsplit(".", 1)[-1] == "strategy"


def _render_value(value: object) -> str:
    """Render a rejected value for a message without ever printing a table or a list.

    Only a non-secret scalar is rendered: a table or a list is described rather than printed, so
    no entry of it reaches a terminal, a CI log or a report (implicit requirement I9).
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    if isinstance(value, str):
        return _redacted_text(value)
    if isinstance(value, Mapping):
        return _TABLE_VALUE
    if isinstance(value, (list, tuple, set, frozenset)):
        return _SEQUENCE_VALUE
    if value is None:
        return _EMPTY_VALUE
    return _UNRENDERABLE_VALUE


def _redacted_text(value: str) -> str:
    """Return ``value`` as the single redaction engine renders it.

    A rejected value reaches stderr at exit 2 and therefore a CI log, so it goes through
    :mod:`foundry.obs.redaction`, the only engine (AAP 0.3.2), whose ``Redactor.text`` is total;
    the import stays inside the function to keep the engine off the plugin's startup path.
    """
    from foundry.obs.redaction import default_redactor

    return default_redactor().text(value)


def _nested_under_mapping_key(dotted: str) -> bool:
    """Return whether ``dotted`` is an entry of a table a mapping-kind key declares.

    A flattened ``[http.headers]`` table produces ``http.headers.Accept``, an operator's own
    header name rather than a configuration key.
    """
    parts = dotted.split(".")
    for end in range(1, len(parts)):
        ancestor = spec_for(".".join(parts[:end]))
        if ancestor is not None:
            return ancestor.kind == "mapping"
    return False
