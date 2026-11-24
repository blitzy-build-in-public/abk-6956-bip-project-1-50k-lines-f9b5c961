"""Unknown-key rejection, value coercion and error translation for the configuration layer.

:mod:`foundry.config` resolves one configuration through four layers -- defaults, the file, the
``FOUNDRY_*`` environment and the ``--foundry-*`` command line -- and each layer hands this module
two jobs for its payload (AAP 0.4.5): reject a key the registry does not declare, naming the
closest valid spelling, and turn the layer's own text into the value its
:class:`~foundry.config.keys.KeySpec` declares. The file layer, whose values arrive typed rather
than as text, is also checked by :func:`validate_file_values`, so a value -- or a list member -- of
a Python type its key does not declare is never coerced. :func:`translate_validation_error` closes
the loop by turning the first failure of a :class:`pydantic.ValidationError` into the one
:class:`~foundry.core.exceptions.ConfigError` a person meets, never pydantic's own wording.

**Ordering requirement.** Every layer calls :func:`validate_unknown_keys` -- or its payload
wrapper, :func:`validate_payload_shape` -- on its *flat* dotted payload **before**
:class:`~foundry.config.models.FoundryConfig` is constructed: ``extra="forbid"`` rejects an
unknown constructor key only as a pydantic error carrying no closest-key suggestion, so this
registry check is what makes AAP 0.4.5's rejection message possible at every layer.

**Every message belongs to :mod:`foundry.config.errors`.** Nothing here composes prose: it chooses
the builder and supplies the arguments, so the texts AAP 0.4.5 pins read alike at every surface.
**No value that could be a credential reaches a message**: the configuration stores the *name* of
the variable holding a credential, never its value (I9), and every value rendered here -- a scalar,
or the one member a rejected list is faulted for -- passes through :mod:`foundry.obs.redaction`,
the single engine (AAP 0.3.2).

The module performs no I/O, reads no environment variable (the mapping is always passed in),
configures no logger and holds no mutable state. It imports :mod:`pydantic` at module scope, which
is safe because nothing on the plugin startup path imports it, so ``import foundry.config.keys``
stays inside the 150 ms startup budget (AAP 0.14.2, I5).
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

Narrower than the environment form on purpose: AAP 0.4.4 names exactly ``true`` and ``false``
for the generic option, so ``--foundry-data.schema_strict 1`` is rejected while
``FOUNDRY_DATA__SCHEMA_STRICT=1`` is accepted.
"""

_ENV_BOOL_WORDS: Final[frozenset[str]] = BOOL_TRUE | BOOL_FALSE
"""Every word an environment variable may spell a boolean with."""

_UNATTRIBUTED_LAYER: Final[str] = "resolved"
"""Layer label used when no caller can say which layer supplied a rejected value: ``resolved``
says what is known -- the value came from the merged configuration -- not a guessed layer."""

_NO_KEY: Final[str] = "configuration"
"""Key named when a rejected value belongs to no single registered key."""

_DOCUMENTED_VALUE: Final[str] = "the documented value"
"""Accepted-value text used when a rejected key is not in the registry."""

_STRING_VALUE: Final[str] = "a string value"
"""Accepted-value text used when a rejected key is an entry of a mapping-kind table."""

_TABLE_VALUE: Final[str] = "(a table)"
"""What a message shows in place of a rejected table, whose entries may hold credentials."""

_SEQUENCE_VALUE: Final[str] = "(a list)"
"""What a message shows in place of a rejected sequence."""

_EMPTY_VALUE: Final[str] = "(no value)"
"""What a message shows in place of a rejected ``None``."""

_EMPTY_PATH_VALUE: Final[str] = "(an empty path)"
"""What a message shows in place of a path value that is empty or only whitespace."""

_INT_FAULT_TYPES: Final[frozenset[str]] = frozenset({"int_from_float", "int_parsing"})
"""The pydantic error types reporting a value that is not a whole number for an ``int`` key."""

_UNRENDERABLE_VALUE: Final[str] = "(a value of an unsupported type)"
"""What a message shows in place of a value of any other type."""

_KEY_IN_MESSAGE_RE: Final[re.Pattern[str]] = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+")
"""Matches a dotted configuration key inside a model validator's own message text."""

_QUOTED_IN_MESSAGE_RE: Final[re.Pattern[str]] = re.compile(r"'([^']*)'")
"""Matches the single-quoted value a cross-field message states, such as ``'bearer'``."""

_BOUNDS_INTERVAL_RE: Final[re.Pattern[str]] = re.compile(r"^(\d+(?:\.\d+)?)\.\.(\d+(?:\.\d+)?)$")
"""Matches a constraint written as an inclusive interval, such as ``1..10``."""

_BOUNDS_LIMIT_RE: Final[re.Pattern[str]] = re.compile(r"^(\d+(?:\.\d+)?) < t <= (\d+(?:\.\d+)?)$")
"""Matches a constraint written as limits, such as ``0 < t <= 300``."""


def accepted_range(spec: KeySpec) -> str:
    """Return the accepted values or range of ``spec``, phrased for a message.

    The single accessor of :attr:`~foundry.config.keys.KeySpec.constraint`: no caller re-derives
    a range, and a message can never disagree with the registry.

    Args:
        spec: The key definition to read.

    Returns:
        The constraint text, for example ``0 < t <= 300`` for ``http.timeout``.
    """
    return spec.constraint


def validate_unknown_keys(values: Mapping[str, object], *, layer: str, detail: str) -> None:
    """Reject the first key of a flat payload that the configuration registry lacks.

    Every layer calls this before the settings model is built (see the module docstring). Iteration
    follows the mapping's insertion order, so the key reported is the first offender of a payload
    built from sorted TOML keys, a sorted variable list or a fixed option order -- deterministic
    across runs and across xdist workers. A key nested beneath a mapping-kind key is accepted,
    because those entries are the operator's header names, not configuration keys.

    Args:
        values: The layer's flattened ``dotted key -> value`` payload.
        layer: The layer label that supplied it, for example ``file`` or ``profile:ci``.
        detail: The file path, variable spelling or option spelling of that layer.

    Raises:
        ConfigError: For the first key the registry does not declare, naming the key with the
            closest valid spelling as its hint (AAP 0.4.5). A registry lookup and a ``difflib``
            comparison raise nothing, so no other exception type can leave this function.
    """
    for key in values:
        if spec_for(key) is not None:
            continue
        if _nested_under_mapping_key(key):
            continue
        raise unknown_key(key, closest=closest_key(key), layer=layer, detail=detail)


def validate_payload_shape(payload: Mapping[str, object], *, layer: str) -> None:
    """Reject an unknown key in an already-flat payload, with no layer detail to name.

    The shortcut for a layer that knows nothing more specific than itself; one that knows the file
    path, the variable or the option calls :func:`validate_unknown_keys`, whose hint names it.

    Args:
        payload: The layer's flattened ``dotted key -> value`` payload.
        layer: The layer label that supplied the payload.

    Raises:
        ConfigError: For the first key the registry does not declare.
    """
    validate_unknown_keys(payload, layer=layer, detail="")


def validate_file_values(values: Mapping[str, object], *, layer: str, detail: str) -> None:
    """Reject the first file-layer value whose Python type its key does not declare.

    TOML parses a boolean, an integer and a float into three distinct types, while the settings
    model runs in pydantic's lax mode -- the mode that lets the environment layer's text coerce --
    so without this check a stray ``true`` in a numeric slot becomes a one-second timeout, though
    AAP 0.4.5 declares one type per key. Refused: a boolean for an ``int`` or ``float`` key, a
    non-boolean for a ``bool`` key, a ``path`` that is empty or only whitespace, and a member the
    enumeration of a list-kind key does not name, reported with its 1-based position. A whole-number
    float or numeric text for an ``int`` key still reaches the model; an undeclared key, or an entry
    nested under a mapping-kind key, is left to :func:`validate_unknown_keys`.

    Args:
        values: The layer's flattened ``dotted key -> value`` payload.
        layer: The layer label that supplied the payload, for example ``file`` or ``profile:ci``.
        detail: The file path that layer read.

    Raises:
        ConfigError: For the first value, in sorted key order, of a type its key does not declare,
            worded like every value AAP 0.4.5 rejects: ``invalid value for http.timeout:
            true (expected 0 < t <= 300)``.
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

    Every environment form AAP 0.4.5 declares is accepted, a list's elements comma separated
    (assumption A3): ``timeout,status_429`` parses to the two retry predicates and an empty value
    to an empty tuple. A mapping-kind key has no single-variable form and is refused first.

    Args:
        spec: The key the variable sets, as the registry declares it.
        raw: The variable's value, exactly as the environment holds it.
        variable: The variable's spelling, for example ``FOUNDRY_HTTP__TIMEOUT``; it is quoted in
            every error raised here and is the only evidence of the layer that reaches the
            message (implicit requirement I2).

    Returns:
        The parsed value: ``str``, :class:`pathlib.Path`, ``int``, ``float``, ``bool``, an enum
        member, or a ``tuple`` for a list-kind key.

    Raises:
        ConfigError: When the key cannot be set from one variable, or when the text does not parse,
            naming the variable and the expected type and never echoing another key's value.
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

    click may hand through the type its parser declared for a named alias -- an ``int`` for
    ``--foundry-retries`` -- or the raw text of the generic ``--foundry-<dotted.key>`` form, so a
    value already of the target type passes through after a range probe and text is parsed by the
    environment rules, except that a boolean accepts only ``true`` and ``false`` (AAP 0.4.4).
    The probe reads the constraint text the message quotes, so the two cannot disagree.

    Args:
        spec: The key the option sets, as the registry declares it.
        raw: The value the option parser produced, or ``None`` when the option was not supplied.
        option: The option's spelling, for example ``--foundry-http.timeout``, quoted in each error.

    Returns:
        The coerced value, or ``None`` when ``raw`` is ``None``, which the command-line layer
        records as no override at all.

    Raises:
        ConfigError: When the value cannot be parsed or lies outside the key's accepted range,
            naming the option spelling rather than an environment variable.
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

    ``error_for`` builds the error for the calling layer, so both surfaces parse identically while
    each message names the spelling its reader can change. An empty or whitespace-only path is
    refused here rather than built, because ``Path("")`` is ``Path(".")``.

    Args:
        spec: The key whose declared kind selects the parsing rule.
        raw: The text to parse.
        bool_words: The words a boolean may be spelled with in this layer.
        error_for: Factory building the layer's error for an unparseable value.

    Raises:
        ConfigError: When ``raw`` does not parse as the declared kind.
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

    Args:
        spec: The list-kind key, whose ``enum_type`` decides how an element is parsed.
        raw: Either the option's text or a sequence click already split.
        error_for: Factory building the layer's error for a bad element.

    Raises:
        ConfigError: When an element is not an accepted value of the key's enumeration, or when
            ``raw`` is neither text nor a sequence.
    """
    if isinstance(raw, str):
        return _parse_list_elements(spec, raw.split(","), error_for=error_for)
    if isinstance(raw, (list, tuple)):
        return _parse_list_elements(spec, (str(item) for item in raw), error_for=error_for)
    raise error_for(spec.constraint)


def _parse_list_elements(
    spec: KeySpec, elements: Iterable[str], *, error_for: Callable[[str], ConfigError]
) -> tuple[object, ...]:
    """Parse the elements of a list-kind value, dropping the empty ones.

    ``timeout,status_429`` yields the two retry predicates, ``a, b ,`` yields ``("a", "b")`` for
    the redaction keys, and an empty or all-empty value yields ``()`` -- the spelling that clears
    a list the file layer supplied. The elements come back in the order given.

    Args:
        spec: The list-kind key, whose ``enum_type`` decides how an element is parsed.
        elements: The elements to parse, already split and possibly padded with blanks.
        error_for: Factory building the layer's error for a bad element.

    Raises:
        ConfigError: When an element is not an accepted value of the key's enumeration, naming the
            variable or option, the accepted values, and the element with its 1-based position.
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

    ``True`` comes back for a word in :data:`BOOL_TRUE`, ``False`` for one in :data:`BOOL_FALSE`;
    the caller builds the error for ``None``, which keeps the layer's wording out of the parser.

    Args:
        raw: The text to read, surrounding whitespace ignored and case folded.
        words: The words this layer accepts.
    """
    lowered = raw.strip().lower()
    if lowered not in words:
        return None
    return lowered in BOOL_TRUE


def _without_trailing_newline(raw: str) -> str:
    r"""Return ``raw`` with a single trailing newline removed and nothing else changed.

    ``export FOUNDRY_DATA__DIR=$(pwd)`` captures the newline its command printed, so the text
    form tolerates exactly one and leaves every other character -- spaces included -- alone.

    Args:
        raw: The text to trim.

    Returns:
        The text without one trailing ``\r\n``, ``\n`` or ``\r``.
    """
    if raw.endswith("\r\n"):
        return raw[:-2]
    if raw.endswith(("\n", "\r")):
        return raw[:-1]
    return raw


def _bounds(spec: KeySpec) -> tuple[float, bool, float, bool] | None:
    """Return the numeric bounds the key's constraint text states, if it states any.

    The registry keeps a key's accepted range as the text a message quotes, because that text is
    a published surface (AAP 0.4.9). Reading the same text here rather than declaring the numbers
    a second time is what makes the probe and its message incapable of disagreeing.

    Args:
        spec: The key definition to read.

    Returns:
        ``(low, low_inclusive, high, high_inclusive)`` for a constraint written as ``1..10`` or
        ``0 < t <= 300``, and ``None`` for any other, whose rules belong to the settings model.
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

    Args:
        spec: The key whose accepted range is being checked.
        value: The numeric value an option already carried in its target type.
        option: The option's spelling, recorded as the layer's detail in the message.

    Raises:
        ConfigError: For a value outside the range, in AAP 0.4.5's worked wording: ``invalid
            value for http.timeout: -1 (expected 0 < t <= 300)``.
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

    The settings model reports one entry per rejected field in field-declaration order, so the
    first entry is the first key a person should fix. It is turned into the wording AAP 0.4.5
    documents, never into pydantic's own: the message, the documentation URL and the echoed input
    all stay out, because the input of a model-level rule is the whole configuration.

    Args:
        exc: The validation failure the settings model raised.
        layer: The layer that supplied the offending value, when the caller knows it, or
            ``None`` once the layers are merged.
        detail: The file path, variable spelling or option spelling of that layer.

    Returns:
        The error for the first failure, built by the builder that owns its wording: a range
        violation by ``errors.range_violation``, a rejected table by ``errors.invalid_shape``, a
        cross-field rule by ``errors.missing_required_for``. An ``int`` key given a value that is
        not a whole number names the type too, and an entry of a mapping-kind table -- a key the
        registry does not hold -- states the string its value has to be.
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

    Args:
        loc: The location pydantic reported, for example ``("http", "timeout")``.

    Returns:
        The dotted key with the integer parts dropped, for example ``http.timeout``. A location
        naming a nested model only -- ``("http", "auth")`` -- yields a path that is not a
        registered key, which is how the caller knows it is not a leaf.
    """
    return ".".join(str(part) for part in loc if isinstance(part, str))


def _clean_message(message: str) -> str:
    """Return a validator's message without the prefix pydantic adds to it.

    Args:
        message: The message pydantic reported, which a ``model_validator`` failure prefixes
            with ``Value error, `` or ``Assertion failed, ``.

    Returns:
        The text the rule itself wrote, so the keys it names can be read back out of it.
    """
    for prefix in ("Value error, ", "Assertion failed, "):
        if message.startswith(prefix):
            return message[len(prefix) :]
    return message


def _cross_field_error(message: str) -> ConfigError | None:
    """Return the cross-field error ``message`` states, or ``None`` when it states none.

    A model validator writes its rule's text, for example ``http.auth.token_env is required when
    http.auth.strategy is 'bearer'``, and pydantic keeps it verbatim, so both keys and the strategy
    value can be read back out of it and the sentence rebuilt by the builder that owns its wording.

    Args:
        message: The cleaned validator message.

    Returns:
        The error naming the missing companion key, or ``None`` when the message names no
        registered strategy key beside another registered key.
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
    """Return whether the leaf of a dotted key is the one whose value is an auth strategy.

    Args:
        key: A dotted configuration key, for example ``http.auth.strategy``.

    Returns:
        ``True`` when the key's last segment is ``strategy``.
    """
    return key.rsplit(".", 1)[-1] == "strategy"


def _render_value(value: object) -> str:
    """Render a rejected value for a message without ever printing a table or a list.

    Only a non-secret scalar is rendered: the configuration holds the *names* of the variables
    that carry credentials (implicit requirement I9), and a table or a list is described rather
    than printed, so no entry of it can reach a terminal, a CI log or a report.

    Args:
        value: The input pydantic reported for the rejected key, or the file value being checked.

    Returns:
        The value as text for a boolean, a number or a string, the string through
        :func:`_redacted_text`, and a placeholder describing the shape for anything else.
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

    A rejected value reaches stderr at exit 2, therefore a CI log, so the string goes through
    :mod:`foundry.obs.redaction`, the only engine (AAP 0.3.2): it strips the documented leak
    channels -- URL userinfo and deny-listed query values (AAP 0.2.1) -- and its ``Redactor.text``
    is total, so no fault path is handled here. The import stays inside the function because at
    module scope it would put the engine on the plugin's startup path (AAP 0.14.2).

    Args:
        value: The rejected string, a scalar one layer of the configuration supplied.

    Returns:
        The value with its secrets replaced, or its credential-free form for a URL.
    """
    from foundry.obs.redaction import default_redactor

    return default_redactor().text(value)


def _nested_under_mapping_key(dotted: str) -> bool:
    """Return whether ``dotted`` is an entry of a table a mapping-kind key declares.

    A flattened file produces ``http.headers.Accept`` for the ``[http.headers]`` table, and those
    entries are the operator's own header names rather than configuration keys.

    Args:
        dotted: The key to inspect.

    Returns:
        ``True`` when the nearest registered ancestor of ``dotted`` is of the mapping kind.
    """
    parts = dotted.split(".")
    for end in range(1, len(parts)):
        ancestor = spec_for(".".join(parts[:end]))
        if ancestor is not None:
            return ancestor.kind == "mapping"
    return False
