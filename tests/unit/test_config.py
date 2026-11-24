"""Unit tests for ``src/foundry/config/``: the four-layer configuration engine of AAP 0.4.5.

The package resolves one frozen configuration whose every key carries the layer that supplied it,
and this module is the contract of that surface: the 28 published keys with their defaults, every
numeric range boundary, ``extra="forbid"``, the cross-field ``http.auth`` rules, the five-step file
discovery order, the ``FOUNDRY_``/``__`` environment encoding with its comma-separated lists
(assumption A3), the four-layer precedence ladder with its ``source_of`` attribution (implicit
requirement I2), the profile merge, the ``difflib`` closest-key suggestion, the pydantic-free import
of ``foundry.config.keys`` (implicit requirement I5), and the ``resolved``/``sources`` documents
``foundry config show`` writes (AAP 0.7.3).

Everything is driven through the package's own calls -- :func:`foundry.config.merge.resolve`,
:func:`foundry.config.env.read_env`, :func:`foundry.config.files.discover_file` and
:func:`foundry.config.render.to_json_document` -- because AAP 0.14.5 forbids proving a capability
only in a unit test and this folder may not patch the click CLI or the plugin. Each resolution is
one call over a directory under ``tmp_path`` with an explicit environment mapping, so no case
depends on the ambient environment or on another case's files.

Two points where the modules on disk differ from the brief this file was written against, the
modules being authoritative (AAP 0.4.3, AAP 0.4.9): the settings model raises pydantic's
``ValidationError``, which ``resolve`` translates into the wording AAP 0.4.5 pins, so range faults
are asserted through ``resolve``; and five keys publish their range as the inclusive interval
``0..10`` rather than as prose, because the registry's constraint text is what a message quotes.

Determinism (AAP 0.14.3): every file lives under ``tmp_path``, no wall-clock value is read or
asserted, no sleep is introduced, every collection compared in an assertion is built in a fixed or
sorted order, and the one subprocess runs a two-line interpreter command that opens no socket.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import tomllib
from collections.abc import Callable, Mapping
from enum import Enum
from pathlib import Path
from typing import Final

import pytest

from foundry.config import cli_layer, env, files, keys, layers, merge, render, validation
from foundry.config.enums import ProfileName, RetryPredicate
from foundry.config.models import FoundryConfig
from foundry.core.exceptions import ConfigError
from foundry.obs import capture

# The two file names the third and fourth discovery steps look for (AAP 0.4.5), the constraint
# ``http.timeout`` quotes, the four variable names the auth cases bind, and a credential-shaped
# header value assembled at run time so no scanner reads this file as carrying a secret.
_FOUNDRY_TOML: Final[str] = "foundry.toml"
_PYPROJECT_TOML: Final[str] = "pyproject.toml"
_TIMEOUT_CONSTRAINT: Final[str] = "0 < t <= 300"
_SECRET: Final[str] = "sentinel" + "-header-value"
_TOKEN_VARIABLE: Final[str] = "TEST_API_TOKEN"
_USER_VARIABLE: Final[str] = "TEST_API_USER"
_PASSWORD_VARIABLE: Final[str] = "TEST_API_PASSWORD"
_HEADER_VARIABLE: Final[str] = "TEST_API_HEADER_VALUE"

_GENERIC_OPTION: Final[str] = "--foundry-http.timeout"
"""The generic option spelling of ``http.timeout`` (AAP 0.4.4)."""

_EXPECTED_KEYS: Final[str] = (
    "profile, http.base_url, http.timeout, http.headers, http.retries.max_attempts, "
    "http.retries.backoff_base, http.retries.backoff_max, http.retries.retry_on, "
    "http.auth.strategy, http.auth.token_env, http.auth.user_env, http.auth.password_env, "
    "http.auth.header_name, http.auth.header_value_env, artifacts.dir, artifacts.keep_passed, "
    "reporting.html, reporting.json, data.dir, data.schema_strict, cassette.dir, cassette.mode, "
    "execution.retries, execution.timeout, execution.quarantine_mode, secrets.redact_keys, "
    "logging.level, logging.capture_lines"
)
"""The 28 published keys as one comma-separated text, in AAP 0.4.5's table order.

A literal rather than a read of :data:`foundry.config.keys.KEY_SPECS`, because a test that read the
registry could not notice a key being added, renamed or removed (AAP 0.4.9).
"""

_DEFAULT_CASES: Final[tuple[tuple[str, object], ...]] = (
    ("profile", "default"),
    ("http.base_url", None),
    ("http.timeout", 10.0),
    ("http.headers", {}),
    ("http.retries.max_attempts", 3),
    ("http.retries.backoff_base", 0.2),
    ("http.retries.backoff_max", 2.0),
    ("http.retries.retry_on", ("timeout", "connect", "status_5xx")),
    ("http.auth.strategy", "none"),
    ("http.auth.token_env", None),
    ("http.auth.user_env", None),
    ("http.auth.password_env", None),
    ("http.auth.header_name", None),
    ("http.auth.header_value_env", None),
    ("artifacts.dir", "artifacts"),
    ("artifacts.keep_passed", False),
    ("reporting.html", True),
    ("reporting.json", True),
    ("data.dir", "testdata"),
    ("data.schema_strict", True),
    ("cassette.dir", "cassettes"),
    ("cassette.mode", "off"),
    ("execution.retries", 0),
    ("execution.timeout", 60.0),
    ("execution.quarantine_mode", "report"),
    ("secrets.redact_keys", ("authorization", "set-cookie", "cookie", "x-api-key")),
    ("logging.level", "INFO"),
    ("logging.capture_lines", 200),
)
"""Every published default, as AAP 0.4.5 states it, normalised to a plain Python value."""

_RANGE_ACCEPT_CASES: Final[tuple[tuple[str, str, object], ...]] = (
    ("http.timeout", "0.001", 0.001),
    ("http.timeout", "300.0", 300.0),
    ("http.retries.max_attempts", "1", 1),
    ("http.retries.max_attempts", "10", 10),
    ("http.retries.backoff_base", "0.0", 0.0),
    ("http.retries.backoff_base", "10.0", 10.0),
    ("http.retries.backoff_max", "0.0", 0.0),
    ("http.retries.backoff_max", "60.0", 60.0),
    ("execution.retries", "0", 0),
    ("execution.retries", "10", 10),
    ("execution.timeout", "0.001", 0.001),
    ("execution.timeout", "3600.0", 3600.0),
    ("logging.capture_lines", "0", 0),
    ("logging.capture_lines", "10000", 10000),
)
"""Both ends of every numeric range, each written as it appears in a file, with its value."""

_RANGE_REJECT_CASES: Final[tuple[tuple[str, str, str], ...]] = (
    ("http.timeout", "0", _TIMEOUT_CONSTRAINT),
    ("http.timeout", "-1", _TIMEOUT_CONSTRAINT),
    ("http.timeout", "300.001", _TIMEOUT_CONSTRAINT),
    ("http.retries.max_attempts", "0", "1..10"),
    ("http.retries.max_attempts", "11", "1..10"),
    ("http.retries.backoff_base", "-0.5", "0..10"),
    ("http.retries.backoff_base", "10.5", "0..10"),
    ("http.retries.backoff_max", "-0.5", "0..60"),
    ("http.retries.backoff_max", "60.5", "0..60"),
    ("execution.retries", "-1", "0..10"),
    ("execution.retries", "11", "0..10"),
    ("execution.timeout", "0", "0 < t <= 3600"),
    ("execution.timeout", "3600.001", "0 < t <= 3600"),
    ("logging.capture_lines", "-1", "0..10000"),
    ("logging.capture_lines", "10001", "0..10000"),
)
"""One value beyond each end of every range, with the constraint text its message quotes."""

_AUTH_MISSING_CASES: Final[tuple[tuple[str, str], ...]] = (
    ('[http.auth]\nstrategy = "bearer"\n', "http.auth.token_env"),
    (
        f'[http.auth]\nstrategy = "basic"\nuser_env = "{_USER_VARIABLE}"\n',
        "http.auth.password_env",
    ),
    (
        f'[http.auth]\nstrategy = "basic"\npassword_env = "{_PASSWORD_VARIABLE}"\n',
        "http.auth.user_env",
    ),
    (
        '[http.auth]\nstrategy = "header"\nheader_name = "X-Api-Key"\n',
        "http.auth.header_value_env",
    ),
    (
        f'[http.auth]\nstrategy = "header"\nheader_value_env = "{_HEADER_VARIABLE}"\n',
        "http.auth.header_name",
    ),
)
"""A selected strategy missing one companion key, with the key its message must name (AAP 0.7.2)."""

_AUTH_COMPLETE_CASES: Final[tuple[tuple[str, str, dict[str, str]], ...]] = (
    (
        f'[http.auth]\nstrategy = "bearer"\ntoken_env = "{_TOKEN_VARIABLE}"\n',
        "bearer",
        {_TOKEN_VARIABLE: "sentinel-token"},
    ),
    (
        f'[http.auth]\nstrategy = "basic"\nuser_env = "{_USER_VARIABLE}"'
        f'\npassword_env = "{_PASSWORD_VARIABLE}"\n',
        "basic",
        {_USER_VARIABLE: "sentinel-user", _PASSWORD_VARIABLE: "sentinel-password"},
    ),
    (
        f'[http.auth]\nstrategy = "header"\nheader_name = "X-Api-Key"'
        f'\nheader_value_env = "{_HEADER_VARIABLE}"\n',
        "header",
        {_HEADER_VARIABLE: "sentinel-header"},
    ),
)
"""Each strategy with its complete companion set, beside the environment its names resolve in."""


def _write(directory: Path, name: str, text: str) -> Path:
    """Write ``text`` to ``directory/name``, creating parents, and return the path as spelled."""
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _resolve(
    directory: Path,
    *,
    environ: Mapping[str, str] | None = None,
    cli: Mapping[str, object] | None = None,
) -> FoundryConfig:
    """Resolve one configuration over ``directory``, reading only the environment given.

    Args:
        directory: The invocation directory discovery and relative paths resolve against.
        environ: The ``FOUNDRY_*`` environment for this resolution, or ``None`` for none at all.
        cli: The ``--foundry-*`` overrides, keyed by option spelling or dotted key, or ``None``.
    """
    overrides = None if cli is None else cli_layer.from_mapping(cli)
    return merge.resolve(
        cli_overrides=overrides,
        environ={} if environ is None else environ,
        cwd=directory,
    )


def _leaf_value(config: FoundryConfig, dotted: str) -> object:
    """Return ``dotted``'s value as a plain Python value a literal can be compared with.

    A path renders POSIX, an enumeration member as its value, and a tuple element by element, so a
    comparison names a value rather than a type.
    """
    value = config.leaf_values()[dotted]
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, tuple):
        return tuple(str(item.value) if isinstance(item, Enum) else str(item) for item in value)
    return value


def _source_label(config: FoundryConfig, dotted: str) -> str:
    """Return the Source cell ``foundry config show`` prints for ``dotted`` (AAP 0.7.4)."""
    return config.source_of(dotted).label()


def test_leaf_keys_are_the_twenty_eight_published_keys() -> None:
    """The registry publishes exactly the 28 keys of AAP 0.4.5, in table order and nothing else."""
    published = keys.leaf_keys()

    assert len(published) == 28
    assert ", ".join(published) == _EXPECTED_KEYS
    assert sorted(published) == sorted(set(published))


@pytest.mark.parametrize(("dotted", "expected"), _DEFAULT_CASES)
def test_every_published_key_carries_its_documented_default(
    dotted: str, expected: object, tmp_path: Path, clean_foundry_env: None
) -> None:
    """A run with no configuration file resolves every default AAP 0.4.5 states."""
    config = _resolve(tmp_path)

    assert _leaf_value(config, dotted) == expected
    assert config.source_of(dotted).is_default() is True
    assert _source_label(config, dotted) == "default"


@pytest.mark.parametrize(("dotted", "literal", "expected"), _RANGE_ACCEPT_CASES)
def test_both_ends_of_every_numeric_range_are_accepted(
    dotted: str, literal: str, expected: object, tmp_path: Path, clean_foundry_env: None
) -> None:
    """The inclusive bounds a key publishes are accepted values, not off-by-one rejections."""
    _write(tmp_path, _FOUNDRY_TOML, f"{dotted} = {literal}\n")

    config = _resolve(tmp_path)

    assert _leaf_value(config, dotted) == expected
    assert _source_label(config, dotted) == f"file:{(tmp_path / _FOUNDRY_TOML).resolve()}"


@pytest.mark.parametrize(("dotted", "literal", "constraint"), _RANGE_REJECT_CASES)
def test_a_value_outside_its_range_names_the_key_the_value_and_the_range(
    dotted: str, literal: str, constraint: str, tmp_path: Path, clean_foundry_env: None
) -> None:
    """A value beyond a range is refused in AAP 0.4.5's wording, with nothing of pydantic in it.

    The worked case is ``http.timeout = -1``; the same three facts are asserted for every numeric
    key, and pydantic's own message, documentation URL and echoed input stay out of the text.
    """
    _write(tmp_path, _FOUNDRY_TOML, f"{dotted} = {literal}\n")

    with pytest.raises(ConfigError) as caught:
        _resolve(tmp_path)

    error = caught.value
    assert error.message == f"invalid value for {dotted}: {literal} (expected {constraint})"
    assert error.key == dotted
    assert error.layer == "file"
    assert "https://" not in error.message
    assert "pydantic" not in error.message.lower()
    assert "input_value" not in error.message
    assert error.hint is not None
    assert str((tmp_path / _FOUNDRY_TOML).resolve()) in error.hint


def test_an_unknown_key_is_refused_by_the_file_layer_and_by_the_model(
    tmp_path: Path, clean_foundry_env: None
) -> None:
    """``extra="forbid"`` rejects a misspelt key in a file and an unexpected constructor keyword.

    The file layer names the closest valid spelling through ``difflib`` (AAP 0.4.5); the settings
    model rejects the extra field itself, and the translator turns that failure into the same
    ``ConfigError`` wording without pydantic's documentation URL or echoed input.
    """
    _write(tmp_path, _FOUNDRY_TOML, "http.timout = 1.0\n")

    with pytest.raises(ConfigError) as caught:
        _resolve(tmp_path)

    error = caught.value
    assert error.message == "unknown configuration key: http.timout"
    assert error.key == "http.timout"
    assert error.layer == "file"
    assert error.hint is not None
    assert "did you mean 'http.timeout'?" in error.hint
    assert str((tmp_path / _FOUNDRY_TOML).resolve()) in error.hint
    assert keys.closest_key("http.timout") == "http.timeout"
    assert keys.closest_key("zzzzzzzzzz") is None

    with pytest.raises(ValueError) as rejected:
        FoundryConfig.model_validate({"http": {"timeout": 1.0}, "nope": 1})

    translated = validation.translate_validation_error(rejected.value)
    assert translated.key == "nope"
    assert translated.message.startswith("invalid value for nope")
    assert "https://" not in translated.message
    assert "input_value" not in translated.message


@pytest.mark.parametrize(("toml_text", "missing"), _AUTH_MISSING_CASES)
def test_a_strategy_without_its_companion_key_is_refused(
    toml_text: str, missing: str, tmp_path: Path, clean_foundry_env: None
) -> None:
    """A strategy missing the key naming its credential names that key and never a value."""
    _write(tmp_path, _FOUNDRY_TOML, toml_text)

    with pytest.raises(ConfigError) as caught:
        _resolve(tmp_path)

    error = caught.value
    assert error.key == missing
    assert missing in error.message
    assert "http.auth.strategy is" in error.message
    assert "sentinel" not in error.message
    assert error.hint is not None
    assert missing in error.hint


@pytest.mark.parametrize(("toml_text", "strategy", "environment"), _AUTH_COMPLETE_CASES)
def test_a_strategy_with_its_companion_keys_resolves_and_holds_no_secret(
    toml_text: str,
    strategy: str,
    environment: dict[str, str],
    tmp_path: Path,
    clean_foundry_env: None,
) -> None:
    """Each complete strategy resolves, holding variable names and no values (requirement I9)."""
    _write(tmp_path, _FOUNDRY_TOML, toml_text)

    config = _resolve(tmp_path, environ=environment)

    assert config.http.auth.strategy.value == strategy
    rendered = f"{config.leaf_values()!r}{config!r}"
    assert not any(value in rendered for value in environment.values())


def test_discovery_walks_the_five_steps_in_the_documented_order(
    tmp_path: Path, clean_foundry_env: None
) -> None:
    """First match wins across the five discovery steps of AAP 0.4.5; the defaults come last.

    The steps are walked in one test because they describe one order: defaults, then a local
    ``pyproject.toml``'s ``[tool.foundry]``, then a local ``foundry.toml``, then ``FOUNDRY_CONFIG``,
    then ``--foundry-config``.
    """
    bare = tmp_path / "bare"
    bare.mkdir()
    project = tmp_path / "project"
    local = _write(project, _FOUNDRY_TOML, "http.timeout = 2.0\n")
    _write(project, _PYPROJECT_TOML, "[tool.foundry]\nhttp.timeout = 5.0\n")
    only_pyproject = tmp_path / "only-pyproject"
    pyproject = _write(only_pyproject, _PYPROJECT_TOML, "[tool.foundry]\nhttp.timeout = 5.0\n")
    from_environment = _write(tmp_path / "elsewhere", "from-env.toml", "http.timeout = 3.0\n")
    explicit = _write(tmp_path / "elsewhere", "explicit.toml", "http.timeout = 1.0\n")

    default = _resolve(bare)
    assert default.http.timeout == 10.0
    assert default.source_of("http.timeout").layer == "default"

    from_pyproject = _resolve(only_pyproject)
    assert from_pyproject.http.timeout == 5.0
    assert _source_label(from_pyproject, "http.timeout") == f"file:{pyproject.resolve()}"

    from_local = _resolve(project)
    assert from_local.http.timeout == 2.0
    assert _source_label(from_local, "http.timeout") == f"file:{local.resolve()}"

    named = {"FOUNDRY_CONFIG": str(from_environment)}
    assert _resolve(project, environ=named).http.timeout == 3.0

    from_cli = _resolve(project, environ=named, cli={"--foundry-config": str(explicit)})
    assert from_cli.http.timeout == 1.0
    assert from_cli.source_of("http.timeout").detail == str(explicit.resolve())


def test_no_config_file_resolves_defaults_and_logs_the_debug_line(
    tmp_path: Path, clean_foundry_env: None, caplog: pytest.LogCaptureFixture
) -> None:
    """A configuration-free run proceeds from the defaults and logs one DEBUG line (AAP 0.4.7)."""
    caplog.set_level(logging.DEBUG, logger="foundry")

    config = _resolve(tmp_path)

    assert config.profile is ProfileName.DEFAULT
    assert config.http.timeout == 10.0
    assert "foundry: no config file found" in [r.getMessage() for r in caplog.records]
    # AAP 0.4.7 pins the logger, the level and the text: the ``foundry`` logger itself, at DEBUG.
    line = next(r for r in caplog.records if r.getMessage() == "foundry: no config file found")
    assert (line.name, line.levelno) == ("foundry", logging.DEBUG)
    assert capture.format_record(line) == "DEBUG foundry: no config file found"

    caplog.clear()
    _write(tmp_path, _FOUNDRY_TOML, "http.timeout = 2.0\n")
    _resolve(tmp_path)
    assert "foundry: no config file found" not in [r.getMessage() for r in caplog.records]


def test_a_missing_explicit_file_reports_its_path(tmp_path: Path, clean_foundry_env: None) -> None:
    """An explicitly named file that is absent is the exact sentence AAP 0.4.7 pins."""
    missing = (tmp_path / "absent.toml").resolve()

    with pytest.raises(ConfigError) as caught:
        _resolve(tmp_path, cli={"--foundry-config": str(missing)})

    error = caught.value
    assert error.message == f"config file not found: {missing}"
    assert error.path == missing
    assert error.layer == "file"
    assert error.hint is None


def test_an_unparseable_pyproject_is_not_a_configuration_file(
    tmp_path: Path, clean_foundry_env: None
) -> None:
    """A ``pyproject.toml`` nobody named that does not parse contributes nothing."""
    _write(tmp_path, _PYPROJECT_TOML, "this is not toml [[[\n")

    assert files.discover_file(environ={}, cwd=tmp_path) is None
    assert _resolve(tmp_path).http.timeout == 10.0


@pytest.mark.parametrize("variable", ("FOUNDRY_HTTP__TIMEOUT", "foundry_http__timeout"))
def test_a_variable_sets_its_key_in_either_case(
    variable: str, tmp_path: Path, clean_foundry_env: None
) -> None:
    """``FOUNDRY_HTTP__TIMEOUT`` and ``foundry_http__timeout`` set the same key (AAP 0.4.5)."""
    payload = env.read_env({variable: "3.5"})

    assert payload.values == {"http.timeout": 3.5}
    assert payload.variables == {"http.timeout": variable}

    config = _resolve(tmp_path, environ={variable: "3.5"})
    assert config.http.timeout == 3.5
    assert config.source_of("http.timeout").as_dict() == {"layer": "env", "detail": variable}
    assert _source_label(config, "http.timeout") == f"env:{variable}"


def test_a_list_variable_is_comma_separated_rather_than_json(
    tmp_path: Path, clean_foundry_env: None
) -> None:
    """A list-valued variable splits on commas (assumption A3) and refuses a JSON array."""
    environment = {"FOUNDRY_HTTP__RETRIES__RETRY_ON": "timeout,status_429"}

    payload = env.read_env(environment)
    assert payload.values["http.retries.retry_on"] == (
        RetryPredicate.TIMEOUT,
        RetryPredicate.STATUS_429,
    )

    config = _resolve(tmp_path, environ=environment)
    assert [member.value for member in config.http.retries.retry_on] == ["timeout", "status_429"]

    with pytest.raises(ConfigError) as caught:
        _resolve(tmp_path, environ={"FOUNDRY_HTTP__RETRIES__RETRY_ON": '["timeout"]'})

    assert "FOUNDRY_HTTP__RETRIES__RETRY_ON" in caught.value.message
    assert "expected" in caught.value.message


def test_the_environment_layer_names_the_variable_in_every_fault(
    tmp_path: Path, clean_foundry_env: None
) -> None:
    """An unparseable value and an unrecognised name are reported against the variable; so is
    ``FOUNDRY_CONFIG``, which selects a file and sets no key."""
    with pytest.raises(ConfigError) as bad_value:
        _resolve(tmp_path, environ={"FOUNDRY_HTTP__TIMEOUT": "abc"})

    assert bad_value.value.message == (
        "invalid value for environment variable FOUNDRY_HTTP__TIMEOUT: expected a number"
    )
    assert bad_value.value.key == "http.timeout"
    assert bad_value.value.layer == "env"

    with pytest.raises(ConfigError) as unknown:
        _resolve(tmp_path, environ={"FOUNDRY_HTTP__TIMOUT": "1.0"})

    assert unknown.value.message == "unknown configuration key: http.timout"
    assert unknown.value.layer == "env"
    assert unknown.value.hint is not None
    assert "did you mean 'http.timeout'?" in unknown.value.hint
    assert "FOUNDRY_HTTP__TIMOUT" in unknown.value.hint

    assert "FOUNDRY_CONFIG" not in keys.leaf_keys()
    assert keys.key_from_env_variable("FOUNDRY_CONFIG") is None
    assert env.read_env({"FOUNDRY_CONFIG": str(tmp_path / _FOUNDRY_TOML)}).values == {}
    assert _resolve(tmp_path, environ={"FOUNDRY_CONFIG": ""}).http.timeout == 10.0


def test_the_four_layer_ladder_resolves_and_attributes_each_rung(
    tmp_path: Path, clean_foundry_env: None
) -> None:
    """Defaults, file, environment, CLI: 10.0, 2.0, 3.5, 1.0, each attributed (AAP 0.1.2, I2)."""
    bare = tmp_path / "bare"
    bare.mkdir()
    project = tmp_path / "project"
    path = _write(project, _FOUNDRY_TOML, "http.timeout = 2.0\n")
    environment = {"FOUNDRY_HTTP__TIMEOUT": "3.5"}

    default = _resolve(bare)
    assert default.http.timeout == 10.0
    assert default.source_of("http.timeout").as_dict() == {"layer": "default", "detail": ""}

    from_file = _resolve(project)
    assert from_file.http.timeout == 2.0
    assert from_file.source_of("http.timeout").as_dict() == {
        "layer": "file",
        "detail": str(path.resolve()),
    }

    from_env = _resolve(project, environ=environment)
    assert from_env.http.timeout == 3.5
    assert from_env.source_of("http.timeout").as_dict() == {
        "layer": "env",
        "detail": "FOUNDRY_HTTP__TIMEOUT",
    }

    from_cli = _resolve(project, environ=environment, cli={"http.timeout": "1.0"})
    assert from_cli.http.timeout == 1.0
    assert from_cli.source_of("http.timeout").as_dict() == {
        "layer": "cli",
        "detail": _GENERIC_OPTION,
    }
    assert from_cli.sources["http.timeout"].label() == f"cli:{_GENERIC_OPTION}"

    for config in (default, from_file, from_env, from_cli):
        assert tuple(sorted(config.sources)) == tuple(sorted(keys.leaf_keys()))


def test_a_profile_overrides_the_file_and_the_environment_overrides_the_profile(
    tmp_path: Path, clean_foundry_env: None
) -> None:
    """A profile table beats the file, the environment beats the profile, unknown names fail."""
    _write(tmp_path, _FOUNDRY_TOML, "http.timeout = 2.0\n\n[profiles.ci]\nhttp.timeout = 7.0\n")
    selected = {"FOUNDRY_PROFILE": "ci"}

    assert _resolve(tmp_path).http.timeout == 2.0
    profile = _resolve(tmp_path, environ=selected)
    assert profile.profile is ProfileName.CI
    assert profile.http.timeout == 7.0
    assert profile.sources["http.timeout"].label() == "profile:ci"
    assert _source_label(profile, "profile") == "env:FOUNDRY_PROFILE"
    overridden = _resolve(tmp_path, environ={**selected, "FOUNDRY_HTTP__TIMEOUT": "9.0"})
    assert overridden.http.timeout == 9.0
    from_cli = _resolve(tmp_path, cli={"profile": "ci"})
    assert from_cli.http.timeout == 7.0
    assert _source_label(from_cli, "profile") == "cli:--foundry-profile"

    _write(tmp_path, _FOUNDRY_TOML, 'profile = "nightly"\n')
    with pytest.raises(ConfigError) as caught:
        _resolve(tmp_path)
    assert caught.value.message == (
        "unknown profile: nightly (expected one of default, local, ci, staging)"
    )
    assert caught.value.key == "profile"


def test_keys_imports_without_pydantic_or_pydantic_settings(
    repo_root: Path, subprocess_env: Callable[..., dict[str, str]]
) -> None:
    """The registry imports without the settings stack, in a fresh interpreter (I5), because it is
    imported on every pytest startup and this process has already imported pydantic."""
    command = [
        sys.executable,
        "-c",
        "import sys, foundry.config.keys; "
        "print(sorted(m for m in ('pydantic', 'pydantic_settings') if m in sys.modules))",
    ]

    completed = subprocess.run(
        command,
        cwd=str(repo_root),
        env=subprocess_env({"PYTHONPATH": str(repo_root / "src")}),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "[]"


def test_the_documents_hold_the_resolved_values_and_their_sources(
    tmp_path: Path, clean_foundry_env: None
) -> None:
    """``--format json`` and ``--format toml`` document the same 28 keys and sources (AAP 0.4.5)."""
    project = tmp_path / "project"
    path = _write(project, _FOUNDRY_TOML, "http.retries.max_attempts = 4\n")
    config = _resolve(project, environ={"FOUNDRY_HTTP__TIMEOUT": "3.5"})

    document = render.to_json_document(config)
    resolved = document["resolved"]
    sources = document["sources"]
    assert isinstance(resolved, dict)
    assert isinstance(sources, dict)
    flat = layers.flatten(resolved)
    assert sorted(document) == ["resolved", "sources"]
    assert sorted(flat) == sorted(keys.leaf_keys())
    assert flat["http.timeout"] == 3.5
    assert flat["http.base_url"] is None
    assert sorted(sources) == sorted(keys.leaf_keys())
    assert sources["http.timeout"] == {"layer": "env", "detail": "FOUNDRY_HTTP__TIMEOUT"}
    assert sources["http.retries.max_attempts"] == {"layer": "file", "detail": str(path.resolve())}

    text = render.to_json_text(config)
    assert json.loads(text) == document
    assert text.endswith("}\n")
    assert not text.endswith("\n\n")

    table = tomllib.loads(render.to_toml_text(config))
    assert table["http"]["timeout"] == 3.5
    assert "base_url" not in table["http"]
    assert sorted(table["sources"]) == sorted(keys.leaf_keys())
    assert table["sources"]["http.timeout"] == {
        "layer": "env",
        "detail": "FOUNDRY_HTTP__TIMEOUT",
    }


def test_the_table_prints_the_five_source_spellings_and_redacts_a_header(
    tmp_path: Path, clean_foundry_env: None
) -> None:
    """The Source column reads the five layer spellings of AAP 0.7.4 and redacts a secret header."""
    project = tmp_path / "project"
    path = _write(
        project,
        _FOUNDRY_TOML,
        "http.retries.backoff_max = 3.0\n\n[http.headers]\n"
        f'Authorization = "Bearer {_SECRET}"\nAccept = "application/json"\n'
        "\n[profiles.ci]\nhttp.retries.max_attempts = 4\n",
    )
    config = _resolve(
        project,
        environ={"FOUNDRY_PROFILE": "ci", "FOUNDRY_HTTP__TIMEOUT": "3.5"},
        cli={"execution.retries": 2},
    )

    rows = render.rows(config)
    sources = {row.key: row.source for row in rows}
    assert len(rows) == 28
    assert [row.key for row in rows] == sorted(sources)
    assert sources["http.retries.backoff_max"] == f"file:{path.resolve()}"
    assert sources["http.retries.max_attempts"] == "profile:ci"
    assert sources["http.timeout"] == "env:FOUNDRY_HTTP__TIMEOUT"
    assert sources["execution.retries"] == "cli:--foundry-execution.retries"
    assert sources["logging.capture_lines"] == "default"

    headers = {row.key: row.value for row in rows}["http.headers"]
    assert "Authorization=***" in headers
    assert "Accept=application/json" in headers
    rendered = [render.to_json_text(config), render.to_toml_text(config)]
    rendered.extend(row.value for row in rows)
    assert not any(_SECRET in text for text in rendered)
