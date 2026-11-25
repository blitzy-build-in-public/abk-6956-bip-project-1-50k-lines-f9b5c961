"""The settings model of ``src/foundry/config/models.py``: its 28 published defaults, the ends of
every numeric range with one value beyond each, the ``http.auth`` cross-field rules, the URL and
header faults the HTTP layer would otherwise refuse at request time, and the read-only ``sources``
provenance. The numbers stay literals: the model must match them."""

from __future__ import annotations

import json
import tomllib
import warnings
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import pytest

from foundry.config import layers, merge, render
from foundry.config.models import FoundryConfig
from foundry.config.sources import ConfigSource
from foundry.core.exceptions import ConfigError

_FOUNDRY_TOML: Final[str] = "foundry.toml"
_USER, _PASSWORD, _HEADER, _TOKEN = "T_USER", "T_PASSWORD", "T_HEADER", "T_TOKEN"
_URL_USER: Final[str] = "QAURLUSER_2b7d"
_URL_PASSWORD: Final[str] = "QAURLPASS_9c4e"
_HEADER_SECRET: Final[str] = "QAHEADER_3e8a7f1b6c2d94a0"
_BASE_URL_CONSTRAINT: Final[str] = "an absolute http or https URL with no userinfo"
_HEADERS_CONSTRAINT: Final[str] = "a table of header names to values without control characters"
_HEADER_NAME_CONSTRAINT: Final[str] = "a header name"
# The URL is assembled from parts so no literal scheme://user:password@host is committed.
_USERINFO_URL: Final[str] = f"http://{_URL_USER}:{_URL_PASSWORD}@127.0.0.1:1"
_USERINFO_TOML: Final[str] = f'[http]\nbase_url = "{_USERINFO_URL}"\n'
_EXPECTED_DEFAULTS: Final[Mapping[str, object]] = json.loads(
    '{"profile": "default", "http.base_url": null, "http.timeout": 10.0, "http.headers": {}, '
    '"http.retries.max_attempts": 3, "http.retries.backoff_base": 0.2, '
    '"http.retries.backoff_max": 2.0, "http.retries.retry_on": ["timeout", "connect", '
    '"status_5xx"], "http.auth.strategy": "none", "http.auth.token_env": null, '
    '"http.auth.user_env": null, "http.auth.password_env": null, "http.auth.header_name": null, '
    '"http.auth.header_value_env": null, "artifacts.dir": "artifacts", '
    '"artifacts.keep_passed": false, "reporting.html": true, "reporting.json": true, '
    '"data.dir": "testdata", "data.schema_strict": true, "cassette.dir": "cassettes", '
    '"cassette.mode": "off", "execution.retries": 0, "execution.timeout": 60.0, '
    '"execution.quarantine_mode": "report", "secrets.redact_keys": ["authorization", '
    '"set-cookie", "cookie", "x-api-key"], "logging.level": "INFO", '
    '"logging.capture_lines": 200}'
)
_RANGES: Final[tuple[tuple[str, str, str], ...]] = (
    ("http.timeout", "0.001", "300.0"),
    ("http.retries.max_attempts", "1", "10"),
    ("http.retries.backoff_base", "0.0", "10.0"),
    ("http.retries.backoff_max", "0.0", "60.0"),
    ("execution.retries", "0", "10"),
    ("execution.timeout", "0.001", "3600.0"),
    ("logging.capture_lines", "0", "10000"),
)
_RANGE_REJECTS: Final[tuple[tuple[str, str, str], ...]] = (
    ("http.timeout", "0", "0 < t <= 300"),
    ("http.timeout", "-1", "0 < t <= 300"),
    ("http.timeout", "300.001", "0 < t <= 300"),
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
_AUTH_CASES: Final[tuple[tuple[str, Mapping[str, str], str | None], ...]] = (
    ("bearer", {}, "http.auth.token_env"),
    ("bearer", {"token_env": _TOKEN}, None),
    ("basic", {"user_env": _USER}, "http.auth.password_env"),
    ("basic", {"user_env": _USER, "password_env": _PASSWORD}, None),
    ("basic", {"password_env": _PASSWORD}, "http.auth.user_env"),
    ("header", {"header_name": "X-Api-Key"}, "http.auth.header_value_env"),
    ("header", {"header_name": "X-Api-Key", "header_value_env": _HEADER}, None),
    ("header", {"header_value_env": _HEADER}, "http.auth.header_name"),
)
# One row per fault the HTTP layer would otherwise refuse at request time: the file text, the key
# the message names, the exact message, and the credentials that must not appear beside it.
_HTTP_VALUE_FAULTS: Final[tuple[tuple[str, str, str, tuple[str, ...]], ...]] = (
    (
        _USERINFO_TOML,
        "http.base_url",
        f"invalid value for http.base_url: http://127.0.0.1:1 (expected {_BASE_URL_CONSTRAINT})",
        (_URL_USER, _URL_PASSWORD),
    ),
    (
        '[http]\nbase_url = "http://127.0.0.1:1/x\\r\\nInjected: yes"\n',
        "http.base_url",
        "invalid value for http.base_url: http://127.0.0.1:1/x\\x0d\\x0aInjected: yes "
        f"(expected {_BASE_URL_CONSTRAINT})",
        (),
    ),
    (
        '[http]\nbase_url = "http://127.0.0.1:1"\n\n'
        f'[http.headers]\n"X-Inject" = "{_HEADER_SECRET}\\r\\nSet-Cookie: pwned=1"\n',
        "http.headers",
        f"invalid shape for http.headers: expected {_HEADERS_CONSTRAINT}",
        (_HEADER_SECRET,),
    ),
    (
        '[http.headers]\n"X-Bad\\nName" = "value"\n',
        "http.headers",
        f"invalid shape for http.headers: expected {_HEADERS_CONSTRAINT}",
        (),
    ),
    (
        '[http.auth]\nstrategy = "header"\nheader_name = "X-Bad\\nName"\n'
        f'header_value_env = "{_HEADER}"\n',
        "http.auth.header_name",
        f"invalid value for http.auth.header_name: X-Bad\\x0aName "
        f"(expected {_HEADER_NAME_CONSTRAINT})",
        (),
    ),
)
_ACCEPTED_TOML: Final[str] = (
    '[http]\nbase_url = "http://127.0.0.1:1/v1"\n\n'
    '[http.headers]\n"X-Plain" = "a\\tb"\nAccept = "application/json"\n'
)


def _write(directory: Path, text: str) -> Path:
    path = directory / _FOUNDRY_TOML
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _resolve(directory: Path, environ=None) -> FoundryConfig:
    return merge.resolve(cli_overrides=None, environ=environ or {}, cwd=directory)


def _resolved(config: FoundryConfig) -> Mapping[str, object]:
    return layers.flatten(render.to_json_document(config)["resolved"])


def _auth_table(strategy: str, companions: Mapping[str, str]) -> str:
    rows = "".join(f'{name} = "{value}"\n' for name, value in companions.items())
    return f'[http.auth]\nstrategy = "{strategy}"\n{rows}'


def test_every_published_key_carries_its_documented_default(
    tmp_path: Path, clean_foundry_env: None
) -> None:
    config = _resolve(tmp_path)
    assert dict(_resolved(config)) == dict(_EXPECTED_DEFAULTS)
    assert {source.label() for source in config.sources.values()} == {"default"}


@pytest.mark.parametrize(
    ("dotted", "literal"), [(dotted, end) for dotted, *ends in _RANGES for end in ends]
)
def test_both_ends_of_every_numeric_range_are_accepted(
    dotted: str, literal: str, tmp_path: Path, clean_foundry_env: None
) -> None:
    _write(tmp_path, f"{dotted} = {literal}\n")
    config = _resolve(tmp_path)
    assert _resolved(config)[dotted] == tomllib.loads(f"value = {literal}")["value"]
    assert config.source_of(dotted).label() == f"file:{(tmp_path / _FOUNDRY_TOML).resolve()}"


@pytest.mark.parametrize(("dotted", "literal", "constraint"), _RANGE_REJECTS)
def test_a_value_outside_its_range_names_the_key_the_value_and_the_range(
    dotted: str, literal: str, constraint: str, tmp_path: Path, clean_foundry_env: None
) -> None:
    _write(tmp_path, f"{dotted} = {literal}\n")
    with pytest.raises(ConfigError) as caught:
        _resolve(tmp_path)
    error = caught.value
    assert error.message == f"invalid value for {dotted}: {literal} (expected {constraint})"
    assert (error.key, error.layer) == (dotted, "file")
    assert "ConfigLayer" not in repr(error) and "input_value" not in error.message
    assert "pydantic" not in error.message.lower() and "https://" not in error.message
    assert error.hint is not None and str((tmp_path / _FOUNDRY_TOML).resolve()) in error.hint


@pytest.mark.parametrize(("foundry_toml", "dotted", "message", "credentials"), _HTTP_VALUE_FAULTS)
def test_a_url_or_header_value_the_http_layer_refuses_is_a_configuration_fault(
    foundry_toml: str,
    dotted: str,
    message: str,
    credentials: tuple[str, ...],
    tmp_path: Path,
    clean_foundry_env: None,
) -> None:
    _write(tmp_path, foundry_toml)
    with pytest.raises(ConfigError) as caught:
        _resolve(tmp_path)
    error = caught.value
    assert (error.key, error.layer) == (dotted, "file")
    assert error.message == message
    assert dotted in error.message
    rendered = f"{error.message}{error.hint or ''}{error!r}"
    assert not [value for value in credentials if value in rendered], rendered
    assert "\r" not in rendered and "\n" not in rendered, "a control character may not reach stderr"


def test_a_url_without_userinfo_and_an_ordinary_header_table_still_resolve(
    tmp_path: Path, clean_foundry_env: None
) -> None:
    _write(tmp_path, _ACCEPTED_TOML)
    config = _resolve(tmp_path)
    assert config.http.base_url == "http://127.0.0.1:1/v1"
    assert config.http.headers == {"X-Plain": "a\tb", "Accept": "application/json"}


@pytest.mark.parametrize(("strategy", "companions", "missing"), _AUTH_CASES)
def test_a_strategy_needs_every_companion_key_and_holds_no_secret(
    strategy: str,
    companions: Mapping[str, str],
    missing: str | None,
    tmp_path: Path,
    clean_foundry_env: None,
) -> None:
    _write(tmp_path, _auth_table(strategy, companions))
    if missing is not None:
        with pytest.raises(ConfigError) as caught:
            _resolve(tmp_path)
        error = caught.value
        assert error.key == missing and missing in error.message
        assert "http.auth.strategy is" in error.message and "sentinel" not in error.message
        assert error.hint is not None and missing in error.hint
        return
    environment = {v: f"secret-{n}" for n, v in companions.items() if n.endswith("_env")}
    config = _resolve(tmp_path, environ=environment)
    assert config.http.auth.strategy.value == strategy
    rendered = f"{config.leaf_values()!r}{config!r}"
    assert not any(value in rendered for value in environment.values())


def test_the_provenance_is_read_only_and_still_dumps_as_a_json_object(
    tmp_path: Path, clean_foundry_env: None
) -> None:
    config = _resolve(tmp_path)
    with pytest.raises(TypeError):
        config.sources["http.timeout"] = ConfigSource.from_env("FOUNDRY_HTTP__TIMEOUT")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        document = config.model_dump(mode="json")
    assert document["sources"]["http.timeout"] == {"layer": "default", "detail": ""}
    assert json.dumps(document)
