"""The single construction site for every configuration error Foundry reports.

A fault reaches a person as one message and, when a next step is worth stating, one hint line
beneath it. The layers call these builders rather than compose text, so each wording is stated
once, and no builder ever receives a resolved credential.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from foundry.core.exceptions import ConfigError
from foundry.core.limits import RANGE_FAULT_TEMPLATE, range_fault_message

__all__: Final[tuple[str, ...]] = (
    "BAD_ENV_TEMPLATE",
    "BAD_OPTION_TEMPLATE",
    "ENV_NOT_SUPPORTED_TEMPLATE",
    "HEADER_CONTROL_CHARACTER_HINT",
    "HEADER_NAME_REQUIRED_HINT",
    "INVALID_SHAPE_HINT",
    "INVALID_SHAPE_TEMPLATE",
    "LAYER_CLI",
    "LAYER_DEFAULT",
    "LAYER_ENV",
    "LAYER_FILE",
    "MALFORMED_TOML_TEMPLATE",
    "MISSING_FILE_TEMPLATE",
    "MISSING_REQUIRED_TEMPLATE",
    "MISSING_SECRET_TEMPLATE",
    "OPTION_NOT_SETTABLE_TEMPLATE",
    "PROFILE_INVALID_TEMPLATE",
    "PROFILE_LAYER_PREFIX",
    "PROFILE_OVERRIDE_TEMPLATE",
    "RANGE_TEMPLATE",
    "RECORD_AND_REPLAY_TEMPLATE",
    "UNKNOWN_KEY_HINT_TEMPLATE",
    "UNKNOWN_KEY_TEMPLATE",
    "UNKNOWN_VARIABLE_TEMPLATE",
    "UNREADABLE_FILE_TEMPLATE",
    "VALIDATION_OK_TEMPLATE",
    "bad_env_value",
    "bad_option_value",
    "both_record_and_replay",
    "cli_detail",
    "env_detail",
    "env_key_unsupported",
    "file_detail",
    "invalid_shape",
    "malformed_toml",
    "missing_file",
    "missing_required_for",
    "missing_secret_variable",
    "option_not_settable",
    "profile_invalid",
    "profile_layer",
    "profile_override_attempt",
    "range_violation",
    "success_message",
    "unknown_key",
    "unknown_variable",
    "unreadable_file",
)


# The four resolution layers plus the prefix that labels a profile table. A ``ConfigSource`` layer
# and a message layer are spelled by these same values, so a row and an error name a layer alike.
LAYER_DEFAULT: Final[str] = "default"
LAYER_FILE: Final[str] = "file"
LAYER_ENV: Final[str] = "env"
LAYER_CLI: Final[str] = "cli"
PROFILE_LAYER_PREFIX: Final[str] = "profile:"

_PROFILE_KEY: Final[str] = "profile"


def env_detail(variable: str) -> str:
    """Return the detail string naming an environment variable, without surrounding whitespace."""
    return variable.strip()


def cli_detail(option: str) -> str:
    """Return the detail string naming a command-line option, including its leading ``--``."""
    if option.startswith("--"):
        return option
    return f"--{option.lstrip('-')}"


def file_detail(path: Path) -> str:
    """Return the detail string naming a configuration file: the path as text."""
    return str(path)


def profile_layer(name: str) -> str:
    """Return the layer label ``profile:<name>`` for a ``[profiles.<name>]`` table."""
    return f"{PROFILE_LAYER_PREFIX}{name}"


# Message templates. Each is the one wording of its fault, filled in by exactly one builder
# below; no builder composes a template of its own and no message ends with a full stop.

MISSING_FILE_TEMPLATE: Final[str] = "config file not found: {path}"
"""AAP 0.4.7 "Explicit config path missing": the message, byte for byte."""

UNREADABLE_FILE_TEMPLATE: Final[str] = "config file could not be read: {path} ({cause})"
"""An explicitly named file exists but could not be read: permissions, a directory."""

MALFORMED_TOML_TEMPLATE: Final[str] = "invalid TOML in {path}: {detail}{location}"
"""A TOML parse fault, with `` (line N, column M)`` appended when the parser knew it."""

UNKNOWN_KEY_TEMPLATE: Final[str] = "unknown configuration key: {key}"
"""AAP 0.4.5: every layer rejects a key that is not in the registry."""

UNKNOWN_KEY_HINT_TEMPLATE: Final[str] = "did you mean '{closest}'?"
"""The suggestion half of an unknown-key hint, carrying ``difflib``'s nearest key."""

UNKNOWN_VARIABLE_TEMPLATE: Final[str] = "unknown configuration variable: {variable}"
"""A ``FOUNDRY_*`` name that spells no key at all, such as the bare ``FOUNDRY_``.

AAP 0.4.5 encodes a key as the prefix followed by its segments, so such a name leaves
:func:`unknown_key` an empty key and the variable itself is named instead.
"""

RANGE_TEMPLATE: Final[str] = RANGE_FAULT_TEMPLATE
"""AAP 0.4.5 worked case: ``http.timeout = -1`` reports the key and its range (``core.limits``)."""

BAD_ENV_TEMPLATE: Final[str] = (
    "invalid value for environment variable {variable}: expected {expected}"
)
"""AAP 0.4.5: an unparseable variable names the variable and the expected type."""

BAD_OPTION_TEMPLATE: Final[str] = "invalid value for option {option}: expected {expected}"
"""An unreadable ``--foundry-*`` value names the option spelling it was given with.

The environment wording cannot serve here: the operator has a command line to correct.
"""

ENV_NOT_SUPPORTED_TEMPLATE: Final[str] = (
    "environment variable {variable} cannot set {key}: a table has no single-variable form"
)
"""A mapping-kind key such as ``http.headers`` attempted through the environment."""

OPTION_NOT_SETTABLE_TEMPLATE: Final[str] = (
    "invalid value for option {option}: {key} is not settable from the command line; "
    "set it in foundry.toml"
)
"""A list- or table-valued key named on the command line; the file layer carries all three."""

MISSING_SECRET_TEMPLATE: Final[str] = "required environment variable is not set: {variable}"
"""AAP 0.4.5: an absent secret variable is named alone, never with a value."""

MISSING_REQUIRED_TEMPLATE: Final[str] = "{key} is required when {strategy_key} is '{strategy}'"
"""A cross-field rule: a strategy was selected without its companion key."""

INVALID_SHAPE_TEMPLATE: Final[str] = "invalid shape for {key}: expected {expected}"
"""A TOML section that is not a table, or two keys that cannot both be flattened."""

INVALID_SHAPE_HINT: Final[str] = (
    "Check that a section is a table of keys and that no key is defined twice"
)
"""The structural remedy: the section's layout is what the operator changes."""

HEADER_CONTROL_CHARACTER_HINT: Final[str] = (
    "Remove the control characters from the header name or value; a tab stays legal in a value"
)
"""The remedy for a control character in a header entry, which the hint never quotes."""

HEADER_NAME_REQUIRED_HINT: Final[str] = (
    "Give the header a name; an empty header name cannot be sent"
)
"""The remedy for an empty header name: the entry needs a name, not a different layout."""

PROFILE_INVALID_TEMPLATE: Final[str] = "unknown profile: {value} (expected one of {accepted})"
"""A ``profile`` value outside the four names AAP 0.4.5 accepts."""

PROFILE_OVERRIDE_TEMPLATE: Final[str] = "a profile table may not set profile: [profiles.{name}]"
"""A ``[profiles.<name>]`` table that tries to change the active profile itself."""

RECORD_AND_REPLAY_TEMPLATE: Final[str] = "--foundry-record and --foundry-replay cannot be combined"
"""AAP 0.4.4: supplying both cassette switches is a usage error, exit 2."""

VALIDATION_OK_TEMPLATE: Final[str] = "configuration valid (profile={profile})"
"""AAP 0.4.4/0.4.5: the line ``foundry config validate`` prints on success."""


def _layer_note(layer: str, detail: str) -> str:
    """Return the hint fragment naming the layer a value came from; an empty detail is left out."""
    if not detail:
        return f"The {layer} layer supplied it"
    return f"The {layer} layer supplied it ({detail})"


def _is_strategy_key(key: str) -> bool:
    """Return whether a dotted key is the one whose value is an auth strategy."""
    return key.rsplit(".", 1)[-1] == "strategy"


def missing_file(path: Path) -> ConfigError:
    """Build the error for an explicitly named configuration file that is absent.

    The message is exactly ``config file not found: <path>`` and there is no hint.
    """
    return ConfigError(MISSING_FILE_TEMPLATE.format(path=path), layer=LAYER_FILE, path=path)


def unreadable_file(path: Path, cause: str) -> ConfigError:
    """Build the error for a configuration file that exists but could not be read.

    ``cause`` is the operating system's description, such as ``Permission denied``.
    """
    return ConfigError(
        UNREADABLE_FILE_TEMPLATE.format(path=path, cause=cause),
        layer=LAYER_FILE,
        path=path,
        hint="Check that the path is a readable regular file owned by the user running Foundry",
    )


def malformed_toml(
    path: Path,
    detail: str,
    *,
    line: int | None = None,
    column: int | None = None,
) -> ConfigError:
    """Build the error for a configuration file that is not valid TOML.

    A located error is required, so the 1-based line and column are appended to the message.
    """
    location = ""
    if line is not None:
        location = f" (line {line}"
        if column is not None:
            location += f", column {column}"
        location += ")"
    return ConfigError(
        MALFORMED_TOML_TEMPLATE.format(path=path, detail=detail, location=location),
        layer=LAYER_FILE,
        path=path,
        hint="No key from this file was applied; fix the reported location and re-run",
    )


def unknown_key(key: str, *, closest: str | None, layer: str, detail: str) -> ConfigError:
    """Build the error for a key that is not in the configuration registry.

    Rejection applies to every layer, and the closest valid spelling is the hint.
    """
    hint_parts: list[str] = []
    if closest is not None:
        hint_parts.append(UNKNOWN_KEY_HINT_TEMPLATE.format(closest=closest))
    hint_parts.append(_layer_note(layer, detail))
    return ConfigError(
        UNKNOWN_KEY_TEMPLATE.format(key=key), key=key, layer=layer, hint=" ".join(hint_parts)
    )


def unknown_variable(variable: str) -> ConfigError:
    """Build the error for an environment variable whose name spells no key at all.

    The bare ``FOUNDRY_`` leaves :func:`unknown_key` an empty key to report, so this builder names
    the variable the environment held and states the encoding in its hint, an empty key-part giving
    ``difflib`` nothing to match. Only the name is taken, so no credential can reach this message.

    Args:
        variable: The variable's name as the environment held it, for example ``FOUNDRY_``.

    Returns:
        A :class:`ConfigError` carrying the ``env`` layer and no key, because none was spelled.
    """
    return ConfigError(
        UNKNOWN_VARIABLE_TEMPLATE.format(variable=variable),
        layer=LAYER_ENV,
        hint="A configuration variable names a key after the FOUNDRY_ prefix, for example "
        "FOUNDRY_HTTP__TIMEOUT",
    )


def range_violation(
    key: str,
    value: str,
    constraint: str,
    *,
    layer: str,
    detail: str,
) -> ConfigError:
    """Build the error for a value outside the range its key accepts.

    AAP 0.4.5's worked case is ``http.timeout = -1``, which reports the key and ``0 < t <= 300``.
    """
    return ConfigError(
        range_fault_message(key, value, constraint),
        key=key,
        layer=layer,
        hint=_layer_note(layer, detail),
    )


def bad_env_value(variable: str, expected: str, *, key: str) -> ConfigError:
    """Build the error for an environment variable the environment layer cannot parse.

    The message carries the variable's spelling and the expected type.
    """
    return ConfigError(
        BAD_ENV_TEMPLATE.format(variable=variable, expected=expected),
        key=key,
        layer=LAYER_ENV,
        hint=f"Set {variable} to a value of the expected type, or unset it to keep the "
        "value the lower layers supply",
    )


def bad_option_value(option: str, expected: str, *, key: str) -> ConfigError:
    """Build the error for a ``--foundry-*`` override whose value cannot be read.

    The value is reported against the option spelling, never an environment variable.
    """
    return ConfigError(
        BAD_OPTION_TEMPLATE.format(option=option, expected=expected),
        key=key,
        layer=LAYER_CLI,
        hint=f"Pass {option} a value of the expected type, or omit it to keep the value the "
        "lower layers supply",
    )


def env_key_unsupported(variable: str, key: str) -> ConfigError:
    """Build the error for a mapping-kind key attempted through the environment.

    A table such as ``http.headers`` has no single-variable form, so the hint points at the file.
    """
    return ConfigError(
        ENV_NOT_SUPPORTED_TEMPLATE.format(variable=variable, key=key),
        key=key,
        layer=LAYER_ENV,
        hint=f"Set {key} in the configuration file instead",
    )


def option_not_settable(option: str, key: str) -> ConfigError:
    """Build the error for a list- or table-valued key named on the command line.

    The generic option exists for the scalar keys only, so a table or list arrives as a dotted key
    or an unregistered spelling, and value-format advice would invite the very form this layer
    rejects. The message instead states the refusal and names the file, which can carry the key.

    Args:
        option: The option's spelling, already carrying its leading ``--``.
        key: The dotted key the option would set.

    Returns:
        A :class:`ConfigError` carrying the key, the ``cli`` layer and the option as layer note.
    """
    return ConfigError(
        OPTION_NOT_SETTABLE_TEMPLATE.format(option=option, key=key),
        key=key,
        layer=LAYER_CLI,
        hint=_layer_note(LAYER_CLI, option),
    )


def missing_secret_variable(variable: str, key: str) -> ConfigError:
    """Build the error for an absent environment variable that a key names.

    The builder takes the variable's name, never a value, so no credential reaches the message.
    """
    return ConfigError(
        MISSING_SECRET_TEMPLATE.format(variable=variable),
        key=key,
        layer=LAYER_ENV,
        hint=f"Set {variable} in the environment Foundry runs in, or change {key} to "
        "name a variable that is set",
    )


def missing_required_for(key: str, required_key: str, *, strategy: str) -> ConfigError:
    """Build the error for a selected strategy whose companion key is absent.

    Whichever argument carries the strategy is quoted with its value, the other is the key to add.
    """
    strategy_key, missing_key = (
        (key, required_key) if _is_strategy_key(key) else (required_key, key)
    )
    return ConfigError(
        MISSING_REQUIRED_TEMPLATE.format(
            key=missing_key, strategy_key=strategy_key, strategy=strategy
        ),
        key=missing_key,
        hint=f"Set {missing_key} in the same layer that selects {strategy_key}",
    )


def invalid_shape(key: str, expected: str, *, layer: str, hint: str | None = None) -> ConfigError:
    """Build the error for a value whose shape is wrong for its key.

    Covers a section that is not a table, a table where a scalar belongs and two keys that cannot
    both be flattened. AAP 0.4.5 sets the message. ``hint`` states the remedy, or the structural
    one when it is ``None``.
    """
    return ConfigError(
        INVALID_SHAPE_TEMPLATE.format(key=key, expected=expected),
        key=key,
        layer=layer,
        hint=INVALID_SHAPE_HINT if hint is None else hint,
    )


def profile_invalid(value: str, accepted: str) -> ConfigError:
    """Build the error for a profile that is not one of the four accepted names.

    ``accepted`` is rendered by :func:`foundry.config.enums.accepted_values_text`.
    """
    return ConfigError(
        PROFILE_INVALID_TEMPLATE.format(value=value, accepted=accepted),
        key=_PROFILE_KEY,
        hint="Profiles are declared under [profiles.<name>] in foundry.toml",
    )


def profile_override_attempt(name: str) -> ConfigError:
    """Build the error for a profile table that tries to change the profile itself.

    A ``[profiles.<name>]`` table is merged over the base file, so a ``profile`` key inside it would
    select a profile while another one is applied.
    """
    return ConfigError(
        PROFILE_OVERRIDE_TEMPLATE.format(name=name),
        key=_PROFILE_KEY,
        layer=profile_layer(name),
        hint="Remove the profile key from that table; the active profile comes from the "
        "configuration file, the environment or the command line",
    )


def both_record_and_replay() -> ConfigError:
    """Build the error for supplying ``--foundry-record`` and ``--foundry-replay`` together.

    The combination is a usage error, mapped to exit 2 by the command line and reported through
    ``pytest.exit(returncode=2)`` by the plugin.
    """
    return ConfigError(
        RECORD_AND_REPLAY_TEMPLATE,
        layer=LAYER_CLI,
        hint="Pass exactly one of --foundry-record or --foundry-replay",
    )


def success_message(profile: str) -> str:
    """Return the line ``foundry config validate`` prints for a valid configuration.

    The text is pinned to ``configuration valid (profile=<name>)``, with no newline.
    """
    return VALIDATION_OK_TEMPLATE.format(profile=profile)
