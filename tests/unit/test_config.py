"""Unit tests for ``src/foundry/config/``: the registry, the five-step discovery, the ``FOUNDRY_``
environment encoding, the four-layer ladder, ``config show`` and the file layer, with literals kept
on purpose. An unparsable candidate is a fault and never a silent return to the defaults, a
``pyproject.toml`` without ``[tool.foundry]`` is no configuration file, and a section header is
checked as a key of its own (AAP 0.4.5, AAP 0.4.7)."""

from __future__ import annotations

import logging
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Final

import pytest

from foundry.config import cli_layer, env, files, keys, layers, merge, render, validation
from foundry.config.enums import ProfileName
from foundry.config.models import FoundryConfig
from foundry.config.sources import ConfigSource
from foundry.core.exceptions import ConfigError

_FOUNDRY_TOML: Final[str] = "foundry.toml"
_PYPROJECT_TOML: Final[str] = "pyproject.toml"
_SECRET: Final[str] = "sentinel" + "-header-value"  # assembled so no scanner reads a secret
_SECRET_TOML: Final[str] = (
    'http.retries.backoff_max = 3.0\n[http.headers]\nAccept = "application/json"\n'
    f'Authorization = "Bearer {_SECRET}"\n'
)
_PROBE: Final[str] = (
    "import sys, foundry.config.keys; print([m for m in sys.modules if 'pydantic' in m])"
)
_EXPECTED_KEYS: Final[str] = (
    "profile, http.base_url, http.timeout, http.headers, http.retries.max_attempts, "
    "http.retries.backoff_base, http.retries.backoff_max, http.retries.retry_on, "
    "http.auth.strategy, http.auth.token_env, http.auth.user_env, http.auth.password_env, "
    "http.auth.header_name, http.auth.header_value_env, artifacts.dir, artifacts.keep_passed, "
    "reporting.html, reporting.json, data.dir, data.schema_strict, cassette.dir, cassette.mode, "
    "execution.retries, execution.timeout, execution.quarantine_mode, secrets.redact_keys, "
    "logging.level, logging.capture_lines"
)
_KEYLESS_TABLES: Final[tuple[tuple[str, str | None, str | None], ...]] = (
    ("[data.bogus]\n", "data.bogus", "file"),
    ("[data.bogus.deep]\n", "data.bogus.deep", "file"),
    ("[http.retries.bogus]\n", "http.retries.bogus", "file"),
    ("[artefacts]\n", "artefacts", "file"),
    ("[profiles.ci.data.bogus]\n", "data.bogus", "profile:ci"),
    ("[data.bogus]\nk = 1\n", "data.bogus.k", "file"),
    ("[data]\n[http]\n[http.auth]\n[http.retries]\n[http.headers]\n[profiles.ci]\n", None, None),
)


def _write(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _resolve(directory: Path, environ=None, cli=None) -> FoundryConfig:
    overrides = None if cli is None else cli_layer.from_mapping(cli)
    return merge.resolve(cli_overrides=overrides, environ=environ or {}, cwd=directory)


def test_the_28_keys_are_published_shown_and_unknown_ones_refused(
    tmp_path: Path, clean_foundry_env: None
) -> None:
    assert (len(published := keys.leaf_keys()), ", ".join(published)) == (28, _EXPECTED_KEYS)
    assert [str(m) for m in layers.ConfigLayer] == ["default", "file", "profile", "env", "cli"]
    profiled, sourced = ConfigSource.from_profile("ci"), ConfigSource.from_env("FOUNDRY_X")
    assert profiled.label() == str(profiled.layer) == "profile:ci"
    assert sourced.layer is layers.ConfigLayer.ENV and sourced.label() == "env:FOUNDRY_X"
    project = tmp_path / "project"
    path = _write(project, _FOUNDRY_TOML, _SECRET_TOML)
    config = _resolve(project, environ={"FOUNDRY_HTTP__TIMEOUT": "3.5"})
    document = render.to_json_document(config)
    assert sorted(layers.flatten(document["resolved"])) == sorted(published)
    detail = {"layer": "env", "detail": "FOUNDRY_HTTP__TIMEOUT"}
    assert document["sources"]["http.timeout"] == detail
    assert config.source_of("http.retries.backoff_max").label() == f"file:{path.resolve()}"
    rows = {row.key: row for row in render.rows(config)}
    assert len(rows) == 28 and rows["http.timeout"].source == "env:FOUNDRY_HTTP__TIMEOUT"
    assert rows["logging.capture_lines"].source == "default"
    headers = rows["http.headers"].value
    assert "Authorization=***" in headers and "Accept=application/json" in headers
    rendered = [render.to_json_text(config), render.to_toml_text(config)]
    rendered.extend(row.value for row in rows.values())
    assert not any(_SECRET in produced for produced in rendered)
    with pytest.raises(ConfigError) as from_env:
        _resolve(tmp_path, environ={"FOUNDRY_HTTP__TIMOUT": "1.0"})
    assert from_env.value.message == "unknown configuration key: http.timout"
    assert (from_env.value.key, from_env.value.layer) == ("http.timout", "env")
    assert from_env.value.hint is not None and "FOUNDRY_HTTP__TIMOUT" in from_env.value.hint
    _write(tmp_path, _FOUNDRY_TOML, "http.timout = 1.0\n")
    with pytest.raises(ConfigError) as caught:
        _resolve(tmp_path)
    assert caught.value.message == "unknown configuration key: http.timout"
    assert (caught.value.key, caught.value.layer) == ("http.timout", "file")
    assert caught.value.hint is not None and "did you mean 'http.timeout'?" in caught.value.hint
    assert str((tmp_path / _FOUNDRY_TOML).resolve()) in caught.value.hint
    assert keys.closest_key("http.timout") == "http.timeout"
    assert keys.closest_key("zzzzzzzzzz") is None
    with pytest.raises(ValueError) as rejected:
        FoundryConfig.model_validate({"http": {"timeout": 1.0}, "nope": 1})
    translated = validation.translate_validation_error(rejected.value)
    assert translated.key == "nope" and "input_value" not in translated.message
    assert "https://" not in translated.message


def test_discovery_walks_the_five_steps_and_a_configuration_free_run_uses_defaults(
    tmp_path: Path, clean_foundry_env: None, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="foundry")
    default = _resolve(tmp_path)
    assert default.profile is ProfileName.DEFAULT and default.http.timeout == 10.0
    line = next(r for r in caplog.records if r.getMessage() == "foundry: no config file found")
    assert (line.name, line.levelno) == ("foundry", logging.DEBUG)
    caplog.clear()
    project = tmp_path / "project"
    _write(project, _FOUNDRY_TOML, "http.timeout = 2.0\n")
    _write(project, _PYPROJECT_TOML, "[tool.foundry]\nhttp.timeout = 5.0\n")
    pyproject = _write(tmp_path / "only", _PYPROJECT_TOML, "[tool.foundry]\nhttp.timeout = 5.0\n")
    named = _write(tmp_path / "elsewhere", "from-env.toml", "http.timeout = 3.0\n")
    explicit = _write(tmp_path / "elsewhere", "explicit.toml", "http.timeout = 1.0\n")
    from_pyproject, from_local = _resolve(pyproject.parent), _resolve(project)
    assert from_pyproject.http.timeout == 5.0 and from_local.http.timeout == 2.0
    assert from_pyproject.source_of("http.timeout").label() == f"file:{pyproject.resolve()}"
    environ = {"FOUNDRY_CONFIG": str(named)}
    assert _resolve(project, environ=environ).http.timeout == 3.0
    from_cli = _resolve(project, environ=environ, cli={"--foundry-config": str(explicit)})
    assert from_cli.http.timeout == 1.0
    assert from_cli.source_of("http.timeout").detail == str(explicit.resolve())
    missing = (tmp_path / "absent.toml").resolve()
    with pytest.raises(ConfigError) as caught:
        _resolve(tmp_path, cli={"--foundry-config": str(missing)})
    assert caught.value.message == f"config file not found: {missing}"
    assert (caught.value.path, caught.value.layer, caught.value.hint) == (missing, "file", None)
    _resolve(project)
    assert "foundry: no config file found" not in [r.getMessage() for r in caplog.records]


def test_the_environment_layer_names_every_fault_and_the_ladder_wins_in_order(
    tmp_path: Path, clean_foundry_env: None
) -> None:
    for variable in ("FOUNDRY_HTTP__TIMEOUT", "foundry_http__timeout"):
        payload = env.read_env({variable: "3.5"})
        assert payload.values == {"http.timeout": 3.5}
        assert payload.variables == {"http.timeout": variable}
        spelling = _resolve(tmp_path, environ={variable: "3.5"})
        assert spelling.http.timeout == 3.5
        assert spelling.source_of("http.timeout").as_dict() == {"layer": "env", "detail": variable}
        assert spelling.source_of("http.timeout").label() == f"env:{variable}"
    retrying = _resolve(tmp_path, environ={"FOUNDRY_HTTP__RETRIES__RETRY_ON": "timeout,status_429"})
    assert [m.value for m in retrying.http.retries.retry_on] == ["timeout", "status_429"]
    with pytest.raises(ConfigError) as as_json:
        _resolve(tmp_path, environ={"FOUNDRY_HTTP__RETRIES__RETRY_ON": '["timeout"]'})
    assert "FOUNDRY_HTTP__RETRIES__RETRY_ON" in as_json.value.message
    with pytest.raises(ConfigError) as bad_value:
        _resolve(tmp_path, environ={"FOUNDRY_HTTP__TIMEOUT": "abc"})
    expected = "invalid value for environment variable FOUNDRY_HTTP__TIMEOUT: expected a number"
    assert bad_value.value.message == expected
    assert (bad_value.value.key, bad_value.value.layer) == ("http.timeout", "env")
    assert "FOUNDRY_CONFIG" not in keys.leaf_keys()
    assert keys.key_from_env_variable("FOUNDRY_CONFIG") is None
    assert env.read_env({"FOUNDRY_CONFIG": str(tmp_path / _FOUNDRY_TOML)}).values == {}
    assert _resolve(tmp_path, environ={"FOUNDRY_CONFIG": ""}).http.timeout == 10.0
    (bare := tmp_path / "bare").mkdir()
    path = _write(project := tmp_path / "project", _FOUNDRY_TOML, "http.timeout = 2.0\n")
    environment, cli = {"FOUNDRY_HTTP__TIMEOUT": "3.5"}, {"http.timeout": "1.0"}
    for rung, timeout, layer, detail in (
        (_resolve(bare), 10.0, "default", ""),
        (_resolve(project), 2.0, "file", str(path.resolve())),
        (_resolve(project, environ=environment), 3.5, "env", "FOUNDRY_HTTP__TIMEOUT"),
        (_resolve(project, environ=environment, cli=cli), 1.0, "cli", "--foundry-http.timeout"),
    ):
        assert rung.http.timeout == timeout
        assert rung.source_of("http.timeout").as_dict() == {"layer": layer, "detail": detail}
    _write(tmp_path, _FOUNDRY_TOML, "http.timeout = 2.0\n\n[profiles.ci]\nhttp.timeout = 7.0\n")
    profile = _resolve(tmp_path, environ={"FOUNDRY_PROFILE": "ci"})
    assert (profile.profile, profile.http.timeout) == (ProfileName.CI, 7.0)
    assert profile.sources["http.timeout"].label() == "profile:ci"
    assert profile.source_of("profile").label() == "env:FOUNDRY_PROFILE"
    override = {"FOUNDRY_PROFILE": "ci", "FOUNDRY_HTTP__TIMEOUT": "9.0"}
    assert _resolve(tmp_path, environ=override).http.timeout == 9.0
    cli_profile = _resolve(tmp_path, cli={"profile": "ci"}).source_of("profile")
    assert cli_profile.label() == "cli:--foundry-profile"
    _write(tmp_path, _FOUNDRY_TOML, 'profile = "nightly"\n')
    with pytest.raises(ConfigError) as caught:
        _resolve(tmp_path)
    described = "unknown profile: nightly (expected one of default, local, ci, staging)"
    assert (caught.value.key, caught.value.message) == ("profile", described)


def test_keys_imports_without_pydantic_or_pydantic_settings(
    repo_root: Path, subprocess_env: Callable[..., dict[str, str]]
) -> None:
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=str(repo_root),
        env=subprocess_env({"PYTHONPATH": str(repo_root / "src")}),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert (completed.returncode, completed.stdout.strip()) == (0, "[]")


class _UnreadableConfig:
    rootpath = Path("/nowhere")
    inipath: Path | None = None
    _inicache: dict[str, object] = {}

    def getini(self, name: str) -> list[str]:
        return []

    def _initini(self, args: list[str]) -> None:
        raise AttributeError("_initini")


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--foundry-artifacts.dir", "/tmp/artifacts"], ["--foundry-artifacts.dir=/tmp/artifacts"]),
        (["--foundry-profile", "ci"], ["--foundry-profile=ci"]),
        (["-k", "case", "--foundry-config", "f.toml"], ["-k", "case", "--foundry-config=f.toml"]),
        (["--foundry-artifacts.dir=/tmp/artifacts"], None),
        (["--foundry-record"], None),
        (["--foundry-record", "tests"], None),
        (["--foundry-artifacts.dir"], None),
        (["--foundry-artifacts.dir", "--foundry-record"], None),
    ],
)
def test_the_preparse_folding_and_a_repair_that_cannot_run(
    argv: list[str], expected: list[str] | None, caplog: pytest.LogCaptureFixture
) -> None:
    assert cli_layer.fold_option_values(argv) == expected
    with caplog.at_level(logging.WARNING, logger="foundry"):
        repaired = cli_layer.restore_preparse_state(
            _UnreadableConfig(), ["--foundry-artifacts.dir", "/tmp/x"]
        )
    assert repaired is False
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1 and "config.cli_layer" in warnings[0].getMessage()


def test_a_pyproject_candidate_is_read_the_same_way_with_or_without_the_table(
    tmp_path: Path, clean_foundry_env: None, write_project: Callable[..., Path]
) -> None:
    write_project(tmp_path, files={_PYPROJECT_TOML: "[tool.foundry\n"})
    path = (tmp_path / _PYPROJECT_TOML).resolve()
    with pytest.raises(ConfigError) as caught:
        _resolve(tmp_path)
    assert caught.value.message.startswith("invalid TOML in ") and str(path) in caught.value.message
    assert (caught.value.path, caught.value.layer) == (path, "file")
    other = tmp_path / "other"
    write_project(other, files={_PYPROJECT_TOML: '[project]\nname = "something-else"\n'})
    assert files.discover_file(environ={}, cwd=other) is None
    config = _resolve(other)
    assert config.http.timeout == 10.0 and config.source_of("http.timeout").layer == "default"


@pytest.mark.parametrize(("toml_text", "key", "layer"), _KEYLESS_TABLES)
def test_a_section_header_is_checked_as_a_key_of_its_own(
    toml_text: str,
    key: str | None,
    layer: str | None,
    tmp_path: Path,
    clean_foundry_env: None,
    write_project: Callable[..., Path],
) -> None:
    write_project(tmp_path, foundry_toml=toml_text)
    path = tmp_path / _FOUNDRY_TOML
    if key is None:
        config = _resolve(tmp_path)
        assert config.http.timeout == 10.0
        assert config.source_of("http.timeout").layer == "default"
        return
    with pytest.raises(ConfigError) as caught:
        _resolve(tmp_path)
    error = caught.value
    assert error.key == key and error.message == f"unknown configuration key: {key}"
    assert error.layer == layer and error.hint is not None and str(path.resolve()) in error.hint
