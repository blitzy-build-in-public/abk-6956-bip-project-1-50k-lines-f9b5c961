"""The four-layer merge: one resolved configuration, with the origin of every value.

:func:`resolve` applies the layers of AAP 0.4.5 -- field defaults, the configuration file with its
selected ``[profiles.<name>]`` table, the ``FOUNDRY_*`` environment and the ``--foundry-*`` command
line -- in :data:`foundry.config.layers.LAYER_ORDER` and returns the one immutable
:class:`foundry.config.models.FoundryConfig` a run reads, with the provenance recorded here because
``pydantic-settings`` resolves values but names no source. The only fault is
:class:`foundry.core.exceptions.ConfigError`, each ending the session at exit code 2 before anything
is collected; a run with no configuration file is not a fault and logs at ``DEBUG``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Final

from pydantic import ValidationError

from foundry.config import cli_layer, env, errors, files, keys, layers, profiles, validation
from foundry.config.enums import AuthStrategy, ProfileName
from foundry.config.layers import ConfigLayer, LayerPayload
from foundry.config.models import FoundryConfig, HttpAuthConfig
from foundry.config.sources import ConfigSource, ConfigSources
from foundry.core.exceptions import ConfigError
from foundry.obs import events

__all__: Final[tuple[str, ...]] = (
    "check_secret_variables",
    "resolve",
)

_COMMAND_LINE_DETAIL: Final[str] = "command line"
"""What a rejection of the whole command-line payload names as its detail.

The option spelling of a rejected *value* travels with it, but a payload rejected before any value
is applied has nothing finer to name than the layer itself.
"""

_TOKEN_ENV_KEY: Final[str] = "http.auth.token_env"
"""The key naming the variable a ``bearer`` session reads its token from."""

_USER_ENV_KEY: Final[str] = "http.auth.user_env"
"""The key naming the variable a ``basic`` session reads its user name from."""

_PASSWORD_ENV_KEY: Final[str] = "http.auth.password_env"
"""The key naming the variable a ``basic`` session reads its password from."""

_HEADER_VALUE_ENV_KEY: Final[str] = "http.auth.header_value_env"
"""The key naming the variable a ``header`` session reads its header value from."""

_LAYER_SEPARATOR: Final[str] = ":"
"""What separates a layer label from its detail, as :func:`foundry.config.layers.layer_label` joins
them."""


@dataclass(frozen=True, slots=True)
class _Layer:
    """One layer ready to fold: the payload it contributes, and how each of its keys is named.

    Most layers have one detail for all their values -- the file layer names its path, the profile
    layer its name. Two do not: every ``FOUNDRY_*`` variable and every ``--foundry-*`` option names
    one key, so those layers carry a per-key map in :attr:`details` and fall back to the payload's
    own detail.
    """

    payload: LayerPayload
    details: Mapping[str, str] = field(default_factory=dict)

    def detail_for(self, dotted: str) -> str:
        """Return the detail that names what supplied ``dotted`` inside this layer.

        Args:
            dotted: The dotted configuration key, for example ``http.timeout``.

        Returns:
            The per-key detail -- the variable's spelling or the option as it was typed -- when the
            layer knows one, otherwise the detail the payload carries for the layer as a whole.
        """
        return self.details.get(dotted, self.payload.detail)


def resolve(
    cli_overrides: cli_layer.CliInput | None = None,
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> FoundryConfig:
    """Resolve the configuration a run uses, from the four layers of AAP 0.4.5.

    The single entry point of the configuration package: a pytest session calls it once at boot and
    the ``run``, ``config`` and ``doctor`` commands once per invocation, so its arguments are the
    whole of a resolution's input and nothing is read from the process the caller cannot see.

    The layers are applied in the order :data:`foundry.config.layers.LAYER_ORDER` states, so the
    precedence rule holds by construction, and which layer won each key is reported by the returned
    configuration's :meth:`foundry.config.models.FoundryConfig.source_of`.

    Args:
        cli_overrides: What the command line supplied, already coerced by
            :mod:`foundry.config.cli_layer`, or ``None`` when the caller has no command line -- the
            zero-configuration state.
        environ: The environment to read ``FOUNDRY_*`` variables from, or ``None`` to read the
            process environment. A supplied mapping *is* the environment for this resolution, so a
            test never depends on the ambient one.
        cwd: The invocation directory that file discovery and a relative ``--foundry-config``
            resolve against, or ``None`` for the process working directory.

    Returns:
        The resolved, frozen configuration carrying one
        :class:`foundry.config.sources.ConfigSource` per published key, so a key no layer supplied
        reports as ``default`` rather than as an absence.

    Raises:
        foundry.core.exceptions.ConfigError: For the first fatal fault, each naming the key and the
            layer that supplied it: a named file that is absent or unreadable, an unknown key in the
            file, the profile table, the environment or the command line, a file value of a type its
            key does not declare, an unparseable environment value, a value outside its accepted
            range, a profile outside the four names, and a configured auth variable the environment
            does not set. A resolution either returns a configuration or raises this one type, which
            a caller maps to exit code 2.
    """
    cli = cli_layer.EMPTY_CLI_INPUT if cli_overrides is None else cli_overrides
    environment = env.effective_environ(environ)
    directory = Path(cwd) if cwd is not None else Path.cwd()

    discovered = files.discover_file(cli_path=cli.config_path, environ=environment, cwd=directory)
    file_payload = files.load_file(discovered)
    if discovered is None:
        _log_no_config_file()
    # TOML delivers typed values, so the file layer's value *types* are checked here, as its key
    # names already are in files.load_file: pydantic's lax mode would otherwise turn a boolean in
    # a numeric slot into a number (AAP 0.4.5).
    validation.validate_file_values(
        file_payload.base, layer=errors.LAYER_FILE, detail=file_payload.label
    )
    for name in file_payload.profile_names():
        validation.validate_file_values(
            file_payload.profiles[name],
            layer=errors.profile_layer(name),
            detail=file_payload.label,
        )

    env_payload = env.read_env(environment)
    selected = profiles.select_profile(cli=cli, env=env_payload, base=file_payload.base)
    application = profiles.apply_profile(file_payload.base, file_payload.profiles, selected)
    validation.validate_unknown_keys(
        cli.overrides, layer=errors.LAYER_CLI, detail=_COMMAND_LINE_DETAIL
    )

    values, recorded = _fold(
        _contributions(
            cli=cli,
            environment=env_payload,
            application=application,
            file_payload=file_payload,
            selected=selected,
        )
    )
    config = _build(values, recorded)
    check_secret_variables(config, environment)
    resolved = config.with_sources(_complete_sources(config, recorded))
    _emit_sources(resolved)
    return resolved


def check_secret_variables(config: FoundryConfig, environ: Mapping[str, str]) -> None:
    """Verify that every variable the selected auth strategy needs is present, by name alone.

    The check is a membership test: for each variable ``http.auth.*_env`` names while its strategy
    is selected, the name is looked up in ``environ`` and the value never read, stored, compared or
    logged, so the message can name the variable an operator has to set, never the credential.

    Checking the name here while the value is read at session start keeps two rules together: an
    absent configured secret variable is a configuration-time fault at exit code 2, while
    :mod:`foundry.http.auth` resolves the values at session start, so no configuration object,
    ``results.json`` or report holds a credential. A variable that disappears between those moments
    raises the same ``ConfigError`` from there.

    Raises:
        foundry.core.exceptions.ConfigError: For the first required variable, in the fixed order of
            the strategy's keys, that ``environ`` does not hold. The message names the variable, and
            the error's ``key`` attribute names the ``http.auth`` key that pointed at it.
    """
    for dotted, variable in _required_secrets(config.http.auth):
        if variable not in environ:
            raise errors.missing_secret_variable(variable, dotted)


def _required_secrets(auth: HttpAuthConfig) -> tuple[tuple[str, str], ...]:
    """Return the key-and-variable pairs the selected strategy needs the environment to hold.

    The pairs are ordered as AAP 0.4.5 lists the keys, so a configuration missing both halves of a
    ``basic`` credential always reports the same one and two runs of a failing resolution print the
    same message (AAP 0.13.1).

    Args:
        auth: The resolved ``http.auth`` table.

    Returns:
        One ``(dotted key, variable name)`` pair per environment variable the strategy requires;
        empty for ``none`` and for a name the strategy does not read.
    """
    strategy = auth.strategy
    pairs: list[tuple[str, str]] = []
    if strategy is AuthStrategy.BEARER and auth.token_env is not None:
        pairs.append((_TOKEN_ENV_KEY, auth.token_env))
    if strategy is AuthStrategy.BASIC:
        if auth.user_env is not None:
            pairs.append((_USER_ENV_KEY, auth.user_env))
        if auth.password_env is not None:
            pairs.append((_PASSWORD_ENV_KEY, auth.password_env))
    if strategy is AuthStrategy.HEADER and auth.header_value_env is not None:
        pairs.append((_HEADER_VALUE_ENV_KEY, auth.header_value_env))
    return tuple(pairs)


def _contributions(
    *,
    cli: cli_layer.CliInput,
    environment: env.EnvPayload,
    application: profiles.ProfileApplication,
    file_payload: files.FilePayload,
    selected: ProfileName,
) -> Mapping[ConfigLayer, _Layer]:
    """Assemble the five layers as the payloads the fold applies.

    The file layer carries the values the *file* supplied and the profile layer the values the
    selected ``[profiles.<name>]`` table supplied. Splitting them makes the per-key attribution
    exact: a value the table overrode reports ``profile:<name>`` rather than ``file:<path>``, and a
    value it left alone keeps the file's path although the two arrived in one document. The
    ``default`` layer contributes no values, which is why a key with no entry in the source map
    reports as ``default`` rather than as absent.

    Args:
        cli: The command line's coerced overrides, with the option spelling behind each one.
        environment: The environment layer's values, with the variable spelling behind each one.
        application: The base file's values with the selected profile table applied over them, and
            the set of keys that table supplied.
        file_payload: The file the values were read from, whose label the file layer records.
        selected: The profile :func:`foundry.config.profiles.select_profile` chose.

    Returns:
        One entry per layer, keyed by :class:`foundry.config.layers.ConfigLayer`; reading them in
        :data:`~foundry.config.layers.LAYER_ORDER` is the precedence rule.
    """
    base_values, profile_values = _split_profile(application)
    return MappingProxyType(
        {
            ConfigLayer.DEFAULT: _Layer(payload=LayerPayload(layer=ConfigLayer.DEFAULT, values={})),
            ConfigLayer.FILE: _Layer(
                payload=LayerPayload(
                    layer=ConfigLayer.FILE, values=base_values, detail=file_payload.label
                )
            ),
            ConfigLayer.PROFILE: _Layer(
                payload=LayerPayload(
                    layer=ConfigLayer.PROFILE, values=profile_values, detail=selected.value
                )
            ),
            ConfigLayer.ENV: _Layer(
                payload=LayerPayload(layer=ConfigLayer.ENV, values=environment.values),
                details=environment.variables,
            ),
            ConfigLayer.CLI: _Layer(
                payload=LayerPayload(layer=ConfigLayer.CLI, values=cli.overrides),
                details=cli.spellings,
            ),
        }
    )


def _split_profile(
    application: profiles.ProfileApplication,
) -> tuple[dict[str, object], dict[str, object]]:
    """Divide a profile application into the values the file kept and the values the table supplied.

    One pass over the application's sorted keys, so both halves iterate identically on two runs and
    a key can never land in both: the table's keys are exactly
    :attr:`~foundry.config.profiles.ProfileApplication.contributed`, and every other key came from
    the base file (or from neither, when a caller hands in an empty payload).

    Args:
        application: The result of :func:`foundry.config.profiles.apply_profile`.

    Returns:
        The ``(file values, profile values)`` pair, each keyed by dotted key in sorted order.
    """
    base: dict[str, object] = {}
    profile: dict[str, object] = {}
    for dotted in application.dotted_keys():
        target = profile if dotted in application.contributed else base
        target[dotted] = application.values[dotted]
    return base, profile


def _fold(
    contributions: Mapping[ConfigLayer, _Layer],
) -> tuple[dict[str, object], dict[str, ConfigSource]]:
    """Apply the layers in precedence order, recording the value and the origin of every key.

    The loop is :data:`foundry.config.layers.LAYER_ORDER` itself rather than a list written out
    here, so the precedence rule has one encoding in the package. A later layer overwrites an
    earlier one's value and its source together, which keeps a value and its attributed origin
    impossible to disagree.

    Args:
        contributions: One assembled layer per :class:`foundry.config.layers.ConfigLayer`.

    Returns:
        The merged flat values and, for every key some layer supplied, the
        :class:`foundry.config.sources.ConfigSource` describing the layer that won it. Keys no layer
        supplied are absent from both: they are the field defaults, which the caller completes.
    """
    values: dict[str, object] = {}
    recorded: dict[str, ConfigSource] = {}
    for name in layers.LAYER_ORDER:
        contribution = contributions[name]
        for dotted in contribution.payload.dotted_keys():
            values[dotted] = contribution.payload.values[dotted]
            recorded[dotted] = _source(name, contribution.detail_for(dotted))
    return values, recorded


def _source(layer: ConfigLayer, detail: str) -> ConfigSource:
    """Build the provenance record of a value ``layer`` supplied, named by ``detail``.

    :mod:`foundry.config.sources` owns the five published spellings, so this function only chooses
    among its constructors and composes no label of its own, which keeps every renderer agreeing on
    what ``env:FOUNDRY_HTTP__TIMEOUT`` means.

    Args:
        layer: The layer that supplied the value.
        detail: What inside that layer named it: the file path, the profile name, the variable's
            spelling or the option as it was typed.

    Returns:
        The record for that layer; the ``default`` record for the layer with nothing to name.
    """
    if layer is ConfigLayer.FILE:
        return ConfigSource.from_file(detail)
    if layer is ConfigLayer.PROFILE:
        return ConfigSource.from_profile(detail)
    if layer is ConfigLayer.ENV:
        return ConfigSource.from_env(detail)
    if layer is ConfigLayer.CLI:
        return ConfigSource.from_cli(detail)
    return ConfigSource.default()


def _build(values: Mapping[str, object], recorded: Mapping[str, ConfigSource]) -> FoundryConfig:
    """Construct the settings model, attributing a rejected value to the layer that supplied it.

    Args:
        values: The merged flat values, as :func:`_fold` produced them.
        recorded: The provenance recorded while folding them.

    Returns:
        The validated configuration, without its source map -- the caller attaches that.

    The nesting is handed to ``model_validate`` rather than splatted into ``FoundryConfig(...)``:
    the model's generated ``__init__`` declares one keyword per field, which a ``dict[str, object]``
    cannot satisfy under ``mypy --strict``, while validating the mapping runs the same validators
    and -- because the class keeps the init source alone -- consults no settings source either way.

    Raises:
        foundry.core.exceptions.ConfigError: The first value the model rejects, described by
            :func:`_attributed_failure`.
    """
    merged = layers.unflatten(values)
    try:
        return FoundryConfig.model_validate(merged)
    except ValidationError as exc:
        raise _attributed_failure(exc, recorded) from None


def _attributed_failure(exc: ValidationError, recorded: Mapping[str, ConfigSource]) -> ConfigError:
    """Return the error for the first rejected value, naming the layer that supplied it.

    :func:`foundry.config.validation.translate_validation_error` owns both the wording and the
    reading of pydantic's report, so it is asked once without a layer to learn which key failed, and
    the recorder -- which knows that key's origin -- supplies the layer for the second call.
    Pydantic's location format is therefore never parsed here.

    A fault the recorder cannot place is returned as the first call built it, which is the
    cross-field rule case: ``bearer`` without ``http.auth.token_env`` names a companion key that no
    layer may have set, so the message it already carries is the complete one.

    Args:
        exc: The failure the settings model raised.
        recorded: The provenance recorded while merging the layers.

    Returns:
        The :class:`foundry.core.exceptions.ConfigError` the caller raises, with ``from None`` so
        the pydantic error -- which echoes the offending input -- is not chained into a traceback.
    """
    unattributed = validation.translate_validation_error(exc)
    if unattributed.key is None:
        return unattributed
    source = recorded.get(unattributed.key)
    if source is None:
        return unattributed
    return validation.translate_validation_error(
        exc, layer=str(source.layer), detail=_note_detail(source)
    )


def _note_detail(source: ConfigSource) -> str:
    """Return the detail worth stating in a message about ``source``, empty when it adds nothing.

    Every builder in :mod:`foundry.config.errors` phrases the layer note as "The <layer> layer
    supplied it (<detail>)", so a profile-sourced record -- whose ``layer`` already carries the name
    as ``profile:<name>`` and whose ``detail`` repeats it -- would print the name twice. The suffix
    of the label is the test for that, which keeps the rule about labels in one place instead of
    asking each caller to know how profiles are spelled.

    Args:
        source: The provenance record of the value being reported.

    Returns:
        The record's detail, or ``""`` when it merely repeats what its layer label already says.
    """
    label_detail = source.layer.partition(_LAYER_SEPARATOR)[2]
    return "" if source.detail == label_detail else source.detail


def _refuse_unregistered_keys(recorded: Mapping[str, ConfigSource]) -> None:
    """Refuse a recorded key the configuration registry does not declare.

    The record is built from payloads each layer already validated, so this guard exists for the
    caller that bypassed validation by building a
    :class:`foundry.config.layers.LayerPayload` itself: without it such a key would reach
    ``sources``, and ``foundry config show`` would print a row no model field backs.

    Args:
        recorded: The provenance recorded while merging the layers.

    Returns:
        Nothing, when every recorded key is registered.

    Raises:
        foundry.core.exceptions.ConfigError: For the first unregistered key in sorted order,
            carrying the closest valid spelling and the layer the record attributes it to.
    """
    for dotted in sorted(recorded):
        if keys.spec_for(dotted) is not None:
            continue
        source = recorded[dotted]
        raise errors.unknown_key(
            dotted,
            closest=keys.closest_key(dotted),
            layer=str(source.layer),
            detail=_note_detail(source),
        )


def _complete_sources(config: FoundryConfig, recorded: Mapping[str, ConfigSource]) -> ConfigSources:
    """Return a provenance record for every key the resolved configuration publishes.

    Total, so :meth:`foundry.config.models.FoundryConfig.source_of` has an answer for any published
    key and ``foundry config show`` prints one row per key rather than one per key some layer set.
    The key set comes from the configuration itself -- the keys a reader can ask about -- and is
    ordered by sorted key, so two resolutions of the same inputs hand over mappings that compare and
    iterate identically (AAP 0.4.5, AAP 0.13.1).

    Args:
        config: The resolved configuration, whose published leaf keys the map must cover.
        recorded: The provenance recorded while merging the layers, holding only the keys some layer
            supplied.

    Returns:
        One :class:`foundry.config.sources.ConfigSource` per published key: the recorded origin for
        a key a layer supplied, and the ``default`` record for every other key, which is the field
        default it kept.

    Raises:
        foundry.core.exceptions.ConfigError: For a recorded key the registry does not declare, as
            :func:`_refuse_unregistered_keys` reports it.
    """
    _refuse_unregistered_keys(recorded)
    return {
        dotted: recorded.get(dotted, ConfigSource.default())
        for dotted in sorted(config.leaf_values())
    }


def _emit_sources(config: FoundryConfig) -> None:
    """Record one ``DEBUG`` line per published key, naming its layer and never its value.

    AAP 0.1.2's logging rule for configuration resolution -- the layer and the key, never the value
    of a secret -- is met by passing :meth:`foundry.config.sources.ConfigSource`'s two strings and
    nothing else to :func:`foundry.obs.events.emit_resolution`, so a key beside a credential
    (``http.headers``, ``secrets.redact_keys``) cannot carry one into a captured-log block.

    Args:
        config: The resolved configuration whose provenance is announced.
    """
    for dotted in sorted(config.sources):
        source = config.sources[dotted]
        events.emit_resolution(dotted, source.layer, source.detail)


def _log_no_config_file() -> None:
    """Log the ``DEBUG`` line a run with no configuration file produces, and nothing else.

    AAP 0.4.7 makes this the whole trace of the zero-configuration state: the run proceeds from the
    field defaults with the profile ``default`` and exit code 0, so the line is informational and
    belongs at ``DEBUG`` on the package's own logger. It is emitted through
    :func:`foundry.obs.events.emit_no_config_file`, the one place a Foundry lifecycle record is
    written, so the level clamp and the single-line format every other lifecycle line gets apply
    here too rather than a logger being called from this module.
    """
    events.emit_no_config_file()
