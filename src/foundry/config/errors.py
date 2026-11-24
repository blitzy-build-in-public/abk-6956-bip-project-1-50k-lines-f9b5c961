"""The single construction site for every configuration error Foundry reports.

A configuration fault reaches a person as one message and, when a next step is worth
stating, one hint line indented beneath it (AAP 0.7.4). Both halves are composed here:
the layers under :mod:`foundry.config` call the builders below instead of assembling
text of their own, so the wording a reviewer greps for -- ``config file not found:
<path>``, ``configuration valid (profile=<name>)``, ``invalid value for http.timeout:
-1 (expected 0 < t <= 300)`` -- is stated once and cannot drift between the file,
environment and command-line layers (AAP 0.4.5, AAP 0.4.7).

No credential value can reach a message. Every builder takes an environment-variable
*name*, a dotted key, an option spelling or a non-secret scalar, and never a resolved
secret: the configuration stores the name of the variable that holds a credential, never
its value (implicit requirement I9). :func:`missing_secret_variable` is the call site
where a careless caller would leak one, so it accepts nothing but the variable's name
and the key that names it, reads nothing from the environment, and prints back only what
it was given.

Each builder returns a :class:`foundry.core.exceptions.ConfigError` rather than raising
it, and marks with ``key``, ``layer`` and ``path`` where the fault came from, so a
handler branches on the attributes instead of parsing prose. A configuration fault ends
the session with exit code 2 on both surfaces, under ``pytest`` and under
``foundry run`` (assumption A4), which is why nothing here builds a ``ValueError``, a
``KeyError`` or a ``click.UsageError``: the message shape and the exit code stay one
decision.

The module does no work at import time -- no I/O, no environment read, no logger
configuration, and only ``str`` constants as module state -- and imports
:mod:`foundry.core.exceptions` and :mod:`foundry.core.limits` alone.
:mod:`foundry.config.layers` imports this module, so a Foundry import in the other
direction would close that cycle on the startup path (AAP 0.4.8).
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


# The four resolution layers (AAP 0.4.5) plus the prefix that labels a profile table.
# A ``ConfigSource`` layer and a message layer are spelled by these same values, so the
# line ``foundry config show`` prints and the line an error prints name a layer alike.
LAYER_DEFAULT: Final[str] = "default"
LAYER_FILE: Final[str] = "file"
LAYER_ENV: Final[str] = "env"
LAYER_CLI: Final[str] = "cli"
PROFILE_LAYER_PREFIX: Final[str] = "profile:"

_PROFILE_KEY: Final[str] = "profile"


def env_detail(variable: str) -> str:
    """Return the detail string naming an environment variable.

    The environment layer hands each of these to a
    :class:`foundry.config.sources.ConfigSource` as its ``detail`` and to the messages
    below, so an operator reads the same spelling in ``foundry config show`` and in the
    error that rejected the value.

    Args:
        variable: The variable's spelling as the environment layer matched it, for
            example ``FOUNDRY_HTTP__TIMEOUT``.

    Returns:
        The spelling without surrounding whitespace.
    """
    return variable.strip()


def cli_detail(option: str) -> str:
    """Return the detail string naming a command-line option.

    Args:
        option: The option as an operator wrote it, for example
            ``--foundry-http.timeout``; a bare name is accepted and given its ``--``.

    Returns:
        The option spelling including its leading ``--``.
    """
    if option.startswith("--"):
        return option
    return f"--{option.lstrip('-')}"


def file_detail(path: Path) -> str:
    """Return the detail string naming a configuration file.

    Args:
        path: The configuration file the file layer read, or tried to read.

    Returns:
        The path as text, which is how a file layer's detail is reported everywhere.
    """
    return str(path)


def profile_layer(name: str) -> str:
    """Return the layer label naming the ``[profiles.<name>]`` table.

    Args:
        name: The profile whose table supplied the value, for example ``ci``.

    Returns:
        The layer label ``profile:<name>``, so a profile value is never attributed to
        the base file it sits beside.
    """
    return f"{PROFILE_LAYER_PREFIX}{name}"


# ----------------------------------------------------------------------------------
# Message templates. Each is the one wording of its fault, filled in by exactly one
# builder below; no builder composes a template of its own and no message ends with a
# trailing period, because these strings are read back by a reviewer and a CI log.
# ----------------------------------------------------------------------------------

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

AAP 0.4.5 encodes a key as the ``FOUNDRY_`` prefix followed by its segments, so such a name
leaves :func:`unknown_key` an empty key; the variable itself is named instead.
"""

RANGE_TEMPLATE: Final[str] = RANGE_FAULT_TEMPLATE
"""AAP 0.4.5 worked case: ``http.timeout = -1`` reports the key and its range.

The template is defined once, in :data:`foundry.core.limits.RANGE_FAULT_TEMPLATE`, and filled in
by :func:`foundry.core.limits.range_fault_message`, so this layer and the capability packages that
validate a policy of their own cannot spell one fault two different ways.
"""

BAD_ENV_TEMPLATE: Final[str] = (
    "invalid value for environment variable {variable}: expected {expected}"
)
"""AAP 0.4.5: an unparseable variable names the variable and the expected type."""

BAD_OPTION_TEMPLATE: Final[str] = "invalid value for option {option}: expected {expected}"
"""AAP 0.4.4: an unreadable ``--foundry-*`` value names the option spelling it was given with.

The environment wording above cannot serve here: an operator who mistyped
``--foundry-data.schema_strict`` has a command line to correct, not a variable, and a message
calling an option an environment variable sends them to the wrong place.
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

PROFILE_INVALID_TEMPLATE: Final[str] = "unknown profile: {value} (expected one of {accepted})"
"""A ``profile`` value outside the four names AAP 0.4.5 accepts."""

PROFILE_OVERRIDE_TEMPLATE: Final[str] = "a profile table may not set profile: [profiles.{name}]"
"""A ``[profiles.<name>]`` table that tries to change the active profile itself."""

RECORD_AND_REPLAY_TEMPLATE: Final[str] = "--foundry-record and --foundry-replay cannot be combined"
"""AAP 0.4.4: supplying both cassette switches is a usage error, exit 2."""

VALIDATION_OK_TEMPLATE: Final[str] = "configuration valid (profile={profile})"
"""AAP 0.4.4/0.4.5: the line ``foundry config validate`` prints on success."""


def _layer_note(layer: str, detail: str) -> str:
    """Return the hint fragment naming the layer a value came from.

    A fault is only actionable once an operator knows which of the four layers supplied
    the offending value, so every builder that was told the layer and its detail states
    them here rather than in the message, keeping the message greppable.

    Args:
        layer: The layer label, for example ``file`` or the ``profile:<name>`` form.
        detail: The file path, variable spelling or option spelling that layer used. An
            empty detail is left out of the fragment.

    Returns:
        A sentence naming the layer and, when there is one, its detail.
    """
    if not detail:
        return f"The {layer} layer supplied it"
    return f"The {layer} layer supplied it ({detail})"


def _is_strategy_key(key: str) -> bool:
    """Return whether a dotted key is the one whose value is an auth strategy.

    Args:
        key: A dotted configuration key, for example ``http.auth.strategy``.

    Returns:
        ``True`` when the key's leaf is ``strategy``.
    """
    return key.rsplit(".", 1)[-1] == "strategy"


def missing_file(path: Path) -> ConfigError:
    """Build the error for an explicitly named configuration file that is absent.

    Implements the AAP 0.4.7 row "Explicit config path missing": the message is exactly
    ``config file not found: <path>``, nothing is collected and no report is written.
    There is no hint, because the path in the message is the whole of the next step.

    Args:
        path: The configuration file named by ``--foundry-config`` or ``FOUNDRY_CONFIG``
            that does not exist.

    Returns:
        A :class:`ConfigError` carrying the path and the ``file`` layer.
    """
    return ConfigError(
        MISSING_FILE_TEMPLATE.format(path=path),
        layer=LAYER_FILE,
        path=path,
    )


def unreadable_file(path: Path, cause: str) -> ConfigError:
    """Build the error for a configuration file that exists but could not be read.

    Args:
        path: The configuration file the file layer opened.
        cause: The operating system's own description of the fault, such as
            ``Permission denied``. It describes the file, never a configuration value.

    Returns:
        A :class:`ConfigError` carrying the path, the ``file`` layer and a hint naming
        what to check.
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

    AAP 0.4.5 requires a located error for a parse fault, so the line -- and the column,
    when the TOML parser reported one -- is appended to the message rather than buried in
    the hint. A column without a line locates nothing and is therefore left out.

    Args:
        path: The configuration file that failed to parse.
        detail: The parser's own description of the fault.
        line: The 1-based line the fault starts on, or ``None`` when unknown.
        column: The 1-based column the fault starts at, or ``None`` when unknown.

    Returns:
        A :class:`ConfigError` carrying the path, the ``file`` layer and a hint stating
        that the file contributed nothing.
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

    Rejection applies to every layer (AAP 0.4.5), so the layer label and its detail are
    arguments rather than an assumption that a TOML file supplied the key. The closest
    valid spelling comes from :func:`foundry.config.keys.closest_key` and travels in the
    hint, which is where AAP 0.4.5 puts the suggestion.

    Args:
        key: The unknown key as the layer spelled it.
        closest: The registered key nearest to ``key``, or ``None`` when nothing is close
            enough to be worth suggesting.
        layer: The layer label that supplied the key.
        detail: The file path, variable spelling or option spelling of that layer.

    Returns:
        A :class:`ConfigError` carrying the key, the layer and a hint that names the
        suggestion and the layer.
    """
    hint_parts: list[str] = []
    if closest is not None:
        hint_parts.append(UNKNOWN_KEY_HINT_TEMPLATE.format(closest=closest))
    hint_parts.append(_layer_note(layer, detail))
    return ConfigError(
        UNKNOWN_KEY_TEMPLATE.format(key=key),
        key=key,
        layer=layer,
        hint=" ".join(hint_parts),
    )


def unknown_variable(variable: str) -> ConfigError:
    """Build the error for an environment variable whose name spells no key at all.

    AAP 0.4.5 encodes a key as the ``FOUNDRY_`` prefix followed by its ``__``-joined
    segments, so the bare ``FOUNDRY_`` leaves :func:`unknown_key` an empty key to report and
    a message ending after its colon. This builder names the variable the environment held,
    and its hint states the encoding, because an empty key-part gives ``difflib`` nothing to
    match. Only the name is taken, so no credential can reach this message (requirement I9).

    Args:
        variable: The variable's name as the environment held it, for example ``FOUNDRY_``.

    Returns:
        A :class:`ConfigError` carrying the ``env`` layer and no key, because none was
        spelled.
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

    AAP 0.4.5's worked case is ``http.timeout = -1``, which must report ``http.timeout``
    and ``0 < t <= 300``; both appear in the message, which keeps them greppable.

    Args:
        key: The dotted key at fault, for example ``http.timeout``.
        value: The offending value, already rendered by the caller because the layer
            holds the raw text. Only a non-secret scalar is ever rendered this way: a
            credential reaches Foundry as a variable name, so there is no value here to
            print.
        constraint: The accepted range as :func:`foundry.config.keys.constraint_for`
            spells it, for example ``0 < t <= 300``.
        layer: The layer label that supplied the value.
        detail: The file path, variable spelling or option spelling of that layer.

    Returns:
        A :class:`ConfigError` carrying the key, the layer and the layer note as its hint.
    """
    return ConfigError(
        range_fault_message(key, value, constraint),
        key=key,
        layer=layer,
        hint=_layer_note(layer, detail),
    )


def bad_env_value(variable: str, expected: str, *, key: str) -> ConfigError:
    """Build the error for an environment variable the environment layer cannot parse.

    AAP 0.4.5 requires the variable's spelling and the expected type in the message, so
    an operator sees which ``FOUNDRY_*`` line to correct rather than which field of the
    settings model failed.

    Args:
        variable: The variable's spelling, for example ``FOUNDRY_HTTP__TIMEOUT``.
        expected: The type the variable had to parse as, such as ``a float``.
        key: The dotted key the variable sets.

    Returns:
        A :class:`ConfigError` carrying the key and the ``env`` layer.
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

    AAP 0.4.4 gives every scalar key a generic ``--foundry-<dotted.key>`` option, so a value
    arrives as text the command-line layer has to coerce, and AAP 0.4.5 requires the rejected
    value to be reported against the spelling the operator can fix. That spelling is the
    option -- ``--foundry-http.timeout``, or the named alias ``--foundry-retries`` -- and
    never an environment variable, which is why the wording is its own template.

    Args:
        option: The option's spelling, already carrying its leading ``--`` as
            :func:`cli_detail` renders it.
        expected: The type or accepted values the option had to carry, such as ``an
            integer`` or ``0 < t <= 300``.
        key: The dotted key the option sets.

    Returns:
        A :class:`ConfigError` carrying the key and the ``cli`` layer.
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

    A table such as ``http.headers`` has no faithful single-variable form, so
    :mod:`foundry.config.env` rejects the attempt and points the operator at the file
    layer, which can express it.

    Args:
        variable: The variable's spelling that named the key.
        key: The dotted key the variable tried to set.

    Returns:
        A :class:`ConfigError` carrying the key and the ``env`` layer.
    """
    return ConfigError(
        ENV_NOT_SUPPORTED_TEMPLATE.format(variable=variable, key=key),
        key=key,
        layer=LAYER_ENV,
        hint=f"Set {key} in the configuration file instead",
    )


def option_not_settable(option: str, key: str) -> ConfigError:
    """Build the error for a list- or table-valued key named on the command line.

    AAP 0.4.4 registers ``--foundry-<dotted.key>`` for the scalar keys only, so the three
    non-scalar keys of AAP 0.4.5 arrive as a dotted key or an unregistered spelling, and AAP
    0.4.5's value-format text would invite the very form this layer rejects. The message
    therefore states the refusal and names the layer that can carry the key: the file.

    Args:
        option: The option's spelling, already carrying its leading ``--`` as
            :func:`cli_detail` renders it.
        key: The dotted key the option would set.

    Returns:
        A :class:`ConfigError` carrying the key, the ``cli`` layer and the option as its
        layer note.
    """
    return ConfigError(
        OPTION_NOT_SETTABLE_TEMPLATE.format(option=option, key=key),
        key=key,
        layer=LAYER_CLI,
        hint=_layer_note(LAYER_CLI, option),
    )


def missing_secret_variable(variable: str, key: str) -> ConfigError:
    """Build the error for an absent environment variable that a key names.

    No credential value can reach this message. The builder is given the variable's name
    and the key that names it -- never a resolved value -- so this module is incapable of
    contributing a secret to stderr, a CI log or a report, and nothing here reads the
    environment or stores what it was given (implicit requirement I9): the message names
    the variable alone, and the key that pointed at it travels in the hint.

    Args:
        variable: The variable's spelling the configuration named, for example
            ``FOUNDRY_API_TOKEN``.
        key: The dotted key that names the variable, for example
            ``http.auth.token_env``.

    Returns:
        A :class:`ConfigError` carrying the key and the ``env`` layer.
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

    The cross-field rules are ``bearer`` requiring ``http.auth.token_env``, ``basic``
    requiring ``http.auth.user_env`` and ``http.auth.password_env``, and ``header``
    requiring ``http.auth.header_name`` and ``http.auth.header_value_env``. Both keys are
    named, in a fixed order: whichever argument carries the strategy is quoted with the
    strategy's value, and the other is the key that must be added, because the pair is
    declared in either order by its callers and the message has to read the same way.

    Args:
        key: One of the two keys -- usually the missing companion.
        required_key: The other of the two keys -- usually the one carrying the strategy.
        strategy: The strategy value that makes the companion mandatory, for example
            ``bearer``. It is an enum value from the configuration, never a credential.

    Returns:
        A :class:`ConfigError` carrying the missing key and a hint naming where to set it.
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


def invalid_shape(key: str, expected: str, *, layer: str) -> ConfigError:
    """Build the error for a value whose shape is wrong for its key.

    It covers a TOML section that is not a table, a key given a table where a scalar is
    expected, and two dotted keys that cannot both be flattened into one mapping. The
    layer is an argument because the fault can come from the file layer or the CLI.

    Args:
        key: The dotted key whose value has the wrong shape.
        expected: The shape the key accepts, for example a table of header names mapped
            to values.
        layer: The layer label that supplied the value.

    Returns:
        A :class:`ConfigError` carrying the key and the layer.
    """
    return ConfigError(
        INVALID_SHAPE_TEMPLATE.format(key=key, expected=expected),
        key=key,
        layer=layer,
        hint="Check that a section is a table of keys and that no key is defined twice",
    )


def profile_invalid(value: str, accepted: str) -> ConfigError:
    """Build the error for a profile that is not one of the four accepted names.

    Args:
        value: The profile name as the layers spelled it.
        accepted: The accepted names, as
            :func:`foundry.config.enums.accepted_values_text` renders them.

    Returns:
        A :class:`ConfigError` carrying the ``profile`` key and a hint naming where
        profile tables live.
    """
    return ConfigError(
        PROFILE_INVALID_TEMPLATE.format(value=value, accepted=accepted),
        key=_PROFILE_KEY,
        hint="Profiles are declared under [profiles.<name>] in foundry.toml",
    )


def profile_override_attempt(name: str) -> ConfigError:
    """Build the error for a profile table that tries to change the profile itself.

    A ``[profiles.<name>]`` table is merged over the base file, so a ``profile`` key
    inside it would select a profile while the table of another one is being applied.
    The fault is attributed to the ``profile:<name>`` layer rather than to the base file.

    Args:
        name: The profile whose table carries the offending key.

    Returns:
        A :class:`ConfigError` carrying the ``profile`` key and the profile layer label.
    """
    return ConfigError(
        PROFILE_OVERRIDE_TEMPLATE.format(name=name),
        key=_PROFILE_KEY,
        layer=profile_layer(name),
        hint="Remove the profile key from that table; the active profile comes from the "
        "configuration file, the environment or the command line",
    )


def both_record_and_replay() -> ConfigError:
    """Build the error for supplying ``--foundry-record`` and ``--foundry-replay``.

    AAP 0.4.4 makes the combination a usage error, which the command line maps to exit 2
    and the plugin reports through ``pytest.exit(returncode=2)`` (assumption A4). The
    fault comes from the command-line layer, so it carries that label and no key.

    Returns:
        A :class:`ConfigError` carrying the ``cli`` layer and a hint naming the fix.
    """
    return ConfigError(
        RECORD_AND_REPLAY_TEMPLATE,
        layer=LAYER_CLI,
        hint="Pass exactly one of --foundry-record or --foundry-replay",
    )


def success_message(profile: str) -> str:
    """Return the line ``foundry config validate`` prints for a valid configuration.

    AAP 0.4.4 and AAP 0.4.5 pin the text to ``configuration valid (profile=<name>)``, so
    the line is built from one template rather than assembled by the subcommand.

    Args:
        profile: The active profile name, for example ``ci``.

    Returns:
        The success line, without a trailing newline.
    """
    return VALIDATION_OK_TEMPLATE.format(profile=profile)
