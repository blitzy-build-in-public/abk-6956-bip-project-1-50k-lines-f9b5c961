"""The settings model of ``src/foundry/config/models.py``: its keys, bounds and provenance.

AAP 0.4.5 publishes 28 leaf keys, each with a type, a default and an accepted range, and AAP 0.4.9
freezes that surface together with the ``http.auth`` cross-field rules and the read-only
provenance ``foundry config show`` reads. This module is the contract of the model itself: every
published default, both ends of every numeric range and one value beyond each end, each credential
strategy with and without the keys naming its secret, and a ``sources`` field that dumps as a JSON
object and cannot be rewritten.

Its siblings carry the rest: ``tests/unit/test_config.py`` holds the engine around the model -- the
registry, file discovery, the environment encoding, the four-layer ladder, the profile merge and
the two ``config show`` documents -- ``tests/unit/test_config_files.py`` the faults the file layer
raises on its own, and ``tests/unit/test_config_constants.py`` the tie between the registry's
numbers and their single source. Everything here is driven through
:func:`foundry.config.merge.resolve` over a directory under ``tmp_path`` with an explicit
environment mapping, so no case reads the ambient environment or another case's files (AAP 0.14.3).
"""

from __future__ import annotations

import json
import warnings
from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from typing import Final

import pytest

from foundry.config import cli_layer, merge
from foundry.config.models import FoundryConfig
from foundry.config.sources import ConfigSource
from foundry.core.exceptions import ConfigError

# The file the range and auth cases write, the constraint ``http.timeout`` quotes (AAP 0.4.5), and
# the four variable names the auth cases bind, whose values config never holds (requirement I9).
_FOUNDRY_TOML: Final[str] = "foundry.toml"
_TIMEOUT_CONSTRAINT: Final[str] = "0 < t <= 300"
_TOKEN_VARIABLE: Final[str] = "TEST_API_TOKEN"
_USER_VARIABLE: Final[str] = "TEST_API_USER"
_PASSWORD_VARIABLE: Final[str] = "TEST_API_PASSWORD"
_HEADER_VARIABLE: Final[str] = "TEST_API_HEADER_VALUE"

_DEFAULT_CASES: Final[tuple[tuple[str, object], ...]] = (
    ("profile", "default"),
    ("http.base_url", None),
    ("http.timeout", 10.0),
    ("http.headers", {}),
    ("http.retries.max_attempts", 3),
    ("http.retries.backoff_base", 0.2),
    ("http.retries.backoff_max", 2.0),
    ("http.retries.retry_on", ["timeout", "connect", "status_5xx"]),
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
    ("secrets.redact_keys", ["authorization", "set-cookie", "cookie", "x-api-key"]),
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

    A path renders POSIX, an enumeration member as its value, and a list or a tuple element by
    element, so a comparison names a value rather than a type.
    """
    value = config.leaf_values()[dotted]
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, list):
        return [str(item.value) if isinstance(item, Enum) else str(item) for item in value]
    if isinstance(value, tuple):
        return tuple(str(item.value) if isinstance(item, Enum) else str(item) for item in value)
    return value


def _source_label(config: FoundryConfig, dotted: str) -> str:
    """Return the Source cell ``foundry config show`` prints for ``dotted`` (AAP 0.7.4)."""
    return config.source_of(dotted).label()


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
    """A value beyond a range is refused in AAP 0.4.5's wording, with nothing of pydantic in it."""
    _write(tmp_path, _FOUNDRY_TOML, f"{dotted} = {literal}\n")

    with pytest.raises(ConfigError) as caught:
        _resolve(tmp_path)

    error = caught.value
    assert error.message == f"invalid value for {dotted}: {literal} (expected {constraint})"
    assert error.key == dotted
    assert error.layer == "file"
    assert "ConfigLayer" not in repr(error)
    assert "https://" not in error.message
    assert "pydantic" not in error.message.lower()
    assert "input_value" not in error.message
    assert error.hint is not None
    assert str((tmp_path / _FOUNDRY_TOML).resolve()) in error.hint


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


def test_the_provenance_is_read_only_and_still_dumps_as_a_json_object(
    tmp_path: Path, clean_foundry_env: None
) -> None:
    """``sources`` keeps the published JSON object and cannot be rewritten (AAP 0.7.2)."""
    config = _resolve(tmp_path)

    with pytest.raises(TypeError):
        config.sources["http.timeout"] = ConfigSource.from_env("FOUNDRY_HTTP__TIMEOUT")

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        document = config.model_dump(mode="json")
    assert isinstance(document["sources"], dict)
    assert document["sources"]["http.timeout"] == {"layer": "default", "detail": ""}
    assert json.dumps(document)
