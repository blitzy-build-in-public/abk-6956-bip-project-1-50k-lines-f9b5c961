"""Unknown-key rejection, value coercion and error translation for the configuration layer.

:mod:`foundry.config` resolves one configuration through four layers -- defaults, the file,
the ``FOUNDRY_*`` environment and the ``--foundry-*`` command line -- and each layer hands
this module two jobs for its payload (AAP 0.4.5): reject a key the registry does not declare,
naming the closest valid spelling, and turn the layer's own text into the value its
:class:`~foundry.config.keys.KeySpec` declares, naming the variable or option when the text
cannot be read. :func:`translate_validation_error` closes the loop by turning the first
failure of a :class:`pydantic.ValidationError` into the one
:class:`~foundry.core.exceptions.ConfigError` a person meets, because pydantic's message, its
documentation URL and its echoed input are not the wording AAP 0.4.5 pins and can show a value
nobody should read.

**Ordering requirement.** Every layer calls :func:`validate_unknown_keys` -- or its payload
wrapper, :func:`validate_payload_shape` -- on its *flat* dotted payload **before**
:class:`~foundry.config.models.FoundryConfig` is constructed. ``extra="forbid"`` rejects an
unknown constructor key too, but only as a pydantic error carrying no closest-key suggestion,
and the environment and command-line layers arrive already flattened, so this registry check
is what makes AAP 0.4.5's rejection message possible at every layer.

**Every message belongs to :mod:`foundry.config.errors`.** Nothing here composes prose: it
chooses the builder and supplies the arguments, so the texts AAP 0.4.5 pins read the same from
the plugin, the command line and ``foundry doctor``. **No value that could be a credential
reaches a message**: the configuration stores the *name* of the variable holding a credential,
never its value (implicit requirement I9), and the one value rendered here is a scalar -- a
table or a sequence is described rather than printed, and URL userinfo is masked.

The module performs no I/O, reads no environment variable (the mapping is always passed in),
configures no logger and holds no mutable state. It imports :mod:`pydantic` at module scope,
which is safe because nothing on the plugin startup path imports *this* module: the
configuration modules are imported by it and never import it in return, which keeps
``import foundry.config.keys`` inside the 150 ms startup budget (AAP 0.14.2, I5).
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

Narrower than the environment form on purpose: AAP 0.4.4 names exactly ``true`` and
``false`` for the generic option, so ``--foundry-data.schema_strict 1`` is rejected while
``FOUNDRY_DATA__SCHEMA_STRICT=1`` is accepted.
"""

_ENV_BOOL_WORDS: Final[frozenset[str]] = BOOL_TRUE | BOOL_FALSE
"""Every word an environment variable may spell a boolean with."""

_UNATTRIBUTED_LAYER: Final[str] = "resolved"
"""Layer label used when a caller cannot say which layer supplied a rejected value.

``resolved`` says what is known -- the value came from the merged configuration -- rather
than inventing one of the four layers.
"""

_NO_KEY: Final[str] = "configuration"
"""Key named when a rejected value belongs to no single registered key."""

_DOCUMENTED_VALUE: Final[str] = "the documented value"
"""Accepted-value text used when a rejected key is not in the registry."""

_TABLE_VALUE: Final[str] = "(a table)"
"""What a message shows in place of a rejected table, whose entries may hold credentials."""

_SEQUENCE_VALUE: Final[str] = "(a list)"
"""What a message shows in place of a rejected sequence."""

_EMPTY_VALUE: Final[str] = "(no value)"
"""What a message shows in place of a rejected ``None``."""

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

    The single accessor of :attr:`~foundry.config.keys.KeySpec.constraint`, so no caller
    re-derives a range and a message can never disagree with the registry.

    Args:
        spec: The key definition to read.

    Returns:
        The constraint text, for example ``0 < t <= 300`` for ``http.timeout``.
    """
    return spec.constraint


def validate_unknown_keys(values: Mapping[str, object], *, layer: str, detail: str) -> None:
    """Reject the first key of a flat payload that the configuration registry lacks.

    Called by every layer before the settings model is built (see the module docstring).
    Iteration follows the mapping's insertion order, so the key reported is the first offender
    of a payload built from sorted TOML keys, a sorted variable list or a fixed option order:
    deterministic across runs and across xdist workers.

    A key nested beneath a mapping-kind key is accepted rather than rejected: a flattened
    ``[http.headers]`` table yields ``http.headers.Accept``, and those entries are the
    operator's header names, not configuration keys.

    Args:
        values: The layer's flattened ``dotted key -> value`` payload.
        layer: The layer label that supplied the payload, for example ``file`` or
            ``profile:ci``.
        detail: The file path, variable spelling or option spelling of that layer.

    Raises:
        ConfigError: For the first key the registry does not declare. The message names the
            key and the hint carries the closest valid spelling and the layer that supplied
            it (AAP 0.4.5); a registry lookup and a ``difflib`` comparison raise nothing, so
            no other exception type can leave this function.
    """
    for key in values:
        if spec_for(key) is not None:
            continue
        if _nested_under_mapping_key(key):
            continue
        raise unknown_key(key, closest=closest_key(key), layer=layer, detail=detail)


def validate_payload_shape(payload: Mapping[str, object], *, layer: str) -> None:
    """Reject an unknown key in an already-flat payload, with no layer detail to name.

    The shortcut a :mod:`foundry.config` layer takes when it knows nothing more specific than
    the layer itself; one that knows the file path, the variable or the option it read calls
    :func:`validate_unknown_keys` instead, so the hint can name it.

    Args:
        payload: The layer's flattened ``dotted key -> value`` payload.
        layer: The layer label that supplied the payload.

    Raises:
        ConfigError: For the first key the registry does not declare.
    """
    validate_unknown_keys(payload, layer=layer, detail="")


def parse_env_value(spec: KeySpec, raw: str, *, variable: str) -> object:
    """Turn one ``FOUNDRY_*`` variable's text into the value its key declares.

    Every environment form AAP 0.4.5 declares is accepted, including a list whose elements are
    comma separated (assumption A3): ``timeout,status_429`` parses to the two retry predicates
    and an empty value to an empty tuple. A mapping-kind key has no single-variable form and is
    refused before any parsing happens.

    Args:
        spec: The key the variable sets, as the registry declares it.
        raw: The variable's value, exactly as the environment holds it.
        variable: The variable's spelling as the environment layer matched it, for example
            ``FOUNDRY_HTTP__TIMEOUT``. It is quoted in every error raised here and is the only
            evidence of the layer that reaches the message (implicit requirement I2).

    Returns:
        The parsed value: ``str``, :class:`pathlib.Path`, ``int``, ``float``, ``bool``, an enum
        member, or a ``tuple`` for a list-kind key.

    Raises:
        ConfigError: When the key cannot be set from one variable, or when the text does not
            parse. The message names the variable and the expected type and never echoes a
            value belonging to another key.
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
    ``--foundry-retries`` -- or the raw text of the generic ``--foundry-<dotted.key>`` form, so
    both arrive: a value already of the target type is passed through after a range probe, and
    text is parsed by the rules the environment layer applies, except that a boolean accepts
    only ``true`` and ``false`` (AAP 0.4.4). The probe reads the same constraint text the
    message quotes (:func:`_bounds`), so a rejected override and its message cannot disagree;
    a constraint stating no numeric bounds is left to the model, which owns that rule.

    Args:
        spec: The key the option sets, as the registry declares it.
        raw: The value the option parser produced, or ``None`` when the option was not supplied.
        option: The option's spelling as the operator wrote it, for example
            ``--foundry-http.timeout``. It is quoted in every error raised here.

    Returns:
        The coerced value, or ``None`` when ``raw`` is ``None`` -- an option nobody passed
        carries ``None``, which the command-line layer records as no override at all.

    Raises:
        ConfigError: When the value cannot be parsed or lies outside the key's accepted range.
            The message names the option spelling rather than an environment variable, because
            its reader has a command line to fix.
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

    ``error_for`` builds the error for the calling layer -- naming the variable or the option
    -- so both surfaces parse identically while each message names the spelling its reader can
    change.

    Args:
        spec: The key whose declared kind selects the parsing rule.
        raw: The text to parse.
        bool_words: The words a boolean may be spelled with in this layer.
        error_for: Factory building the layer's error for an unparseable value, given the
            expected type or accepted values.

    Returns:
        The parsed scalar.

    Raises:
        ConfigError: When ``raw`` does not parse as the declared kind.
    """
    if spec.kind == "str":
        return _without_trailing_newline(raw)
    if spec.kind == "path":
        return Path(_without_trailing_newline(raw))
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
        spec: The list-kind key, whose ``enum_type`` decides whether an element is a member of
            an enumeration or free text.
        raw: Either the option's text or a sequence click already split.
        error_for: Factory building the layer's error for a bad element.

    Returns:
        The parsed elements in the order given, with the empty ones dropped.

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

    ``timeout,status_429`` yields the two retry predicates, ``a, b ,`` yields ``("a", "b")``
    for the redaction keys, and an empty or all-empty value yields ``()`` -- the spelling that
    clears a list the file layer supplied.

    Args:
        spec: The list-kind key, whose ``enum_type`` decides how an element is parsed.
        elements: The elements to parse, already split and possibly padded with blanks.
        error_for: Factory building the layer's error for a bad element.

    Returns:
        The parsed elements in the order given.

    Raises:
        ConfigError: When an element is not an accepted value of the key's enumeration. The
            message names the variable or option and the accepted values.
    """
    kept = [element.strip() for element in elements]
    kept = [element for element in kept if element]
    if not kept:
        return ()
    if spec.enum_type is None:
        return tuple(kept)
    expected = accepted_values_text(spec.enum_type)
    members: list[object] = []
    for element in kept:
        member = parse_enum(spec.enum_type, element)
        if member is None:
            raise error_for(expected)
        members.append(member)
    return tuple(members)


def _bool_value(raw: str, *, words: frozenset[str]) -> bool | None:
    """Return the boolean ``raw`` spells with ``words``, or ``None`` when it spells none.

    Args:
        raw: The text to read, surrounding whitespace ignored and case folded.
        words: The words this layer accepts.

    Returns:
        ``True`` for a word in :data:`BOOL_TRUE`, ``False`` for one in :data:`BOOL_FALSE`, and
        ``None`` otherwise -- the caller builds the error, which keeps this layer's wording out
        of the parser.
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

    The registry keeps a key's accepted range as the text a message quotes, because that text
    is a published surface (AAP 0.4.9). Reading the same text here rather than declaring the
    numbers a second time is what makes the probe and its message incapable of disagreeing.

    Args:
        spec: The key definition to read.

    Returns:
        ``(low, low_inclusive, high, high_inclusive)`` for a constraint written as ``1..10``
        or ``0 < t <= 300``, and ``None`` for any other constraint -- ``true or false``, an
        accepted-values list, free text -- whose rules belong to the settings model.
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
        ConfigError: For a value outside the range, naming the key, the value and the range as
            AAP 0.4.5's worked case reads it: ``invalid value for http.timeout: -1 (expected 0
            < t <= 300)``.
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
    first entry is the first key a person should fix and the report is deterministic. It is
    turned into the wording AAP 0.4.5 documents, never into pydantic's own: the message, the
    documentation URL and the echoed input all stay out of the text -- the input of a
    model-level rule is the whole configuration, which may carry a header value.

    Args:
        exc: The validation failure the settings model raised.
        layer: The layer that supplied the offending value, when the caller knows it, or
            ``None`` once the layers are merged.
        detail: The file path, variable spelling or option spelling of that layer.

    Returns:
        The error for the first failure, built by the builder that owns its wording: a value
        outside its accepted range or values by ``errors.range_violation``, a rejected table by
        ``errors.invalid_shape``, and a cross-field rule -- ``bearer`` without
        ``http.auth.token_env`` -- by ``errors.missing_required_for``.
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
    constraint = accepted_range(spec) if spec is not None else _DOCUMENTED_VALUE
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
        registered key, which is how the caller recognises it is not a leaf.
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

    A model validator writes the text of its own rule, for example ``http.auth.token_env is
    required when http.auth.strategy is 'bearer'``, and pydantic keeps it verbatim, so both
    keys and the strategy's value can be read back out of it and the sentence rebuilt through
    the builder that owns the wording (AAP 0.4.5).

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
        value: The input pydantic reported for the rejected key.

    Returns:
        The value as text for a boolean, a number or a string -- a URL's userinfo masked -- and
        a placeholder describing the shape for anything else.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    if isinstance(value, str):
        return _mask_userinfo(value)
    if isinstance(value, Mapping):
        return _TABLE_VALUE
    if isinstance(value, (list, tuple, set, frozenset)):
        return _SEQUENCE_VALUE
    if value is None:
        return _EMPTY_VALUE
    return _UNRENDERABLE_VALUE


def _mask_userinfo(text: str) -> str:
    """Return ``text`` with the userinfo of a URL-like value replaced by ``***``.

    ``http.base_url`` may be written with credentials in it, and URL userinfo is a documented
    leak channel of the tooling around HTTP (AAP 0.2.1), so it is masked wherever a value is
    rendered rather than left to every caller to remember.

    Args:
        text: The rejected string.

    Returns:
        The string unchanged when it is not a URL carrying userinfo, otherwise the same URL with
        ``***`` in place of the username and password.
    """
    scheme, separator, rest = text.partition("://")
    if not separator:
        return text
    authority, slash, remainder = rest.partition("/")
    if "@" not in authority:
        return text
    host = authority.rpartition("@")[2]
    return f"{scheme}://***@{host}{slash}{remainder}"


def _nested_under_mapping_key(dotted: str) -> bool:
    """Return whether ``dotted`` is an entry of a table a mapping-kind key declares.

    A flattened configuration file produces ``http.headers.Accept`` for the ``[http.headers]``
    table, and those entries are the operator's header names rather than configuration keys, so
    rejecting them would refuse a valid configuration.

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
