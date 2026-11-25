"""Unit tests for ``src/foundry/config/``: the four-layer configuration engine of AAP 0.4.5.

The package resolves one frozen configuration whose every key carries the layer that supplied it,
and this module is the contract of the engine that produces it: the 28 published keys, the
five-step file discovery order and the sentence a missing explicit path reports, the
``FOUNDRY_``/``__`` environment encoding with its comma-separated lists (assumption A3), the
precedence ladder with its ``source_of`` attribution (implicit requirement I2), the profile merge,
the ``difflib`` closest-key suggestion, the pydantic-free import of ``foundry.config.keys``
(implicit requirement I5), and the ``resolved``/``sources`` documents ``foundry config show``
writes (AAP 0.7.3), and the fold that keeps an option's separately typed value out of pytest's
collection arguments before the option group is registered (AAP 0.2.1). The model's own surface is
proved in ``tests/unit/test_config_model.py`` and the file faults in
``tests/unit/test_config_files.py``.

Everything is driven through the package's own calls -- ``foundry.config.merge.resolve``,
``foundry.config.env.read_env`` and ``foundry.config.render.to_json_document`` -- because AAP 0.14.5
forbids proving a capability only in a unit test and this folder may not patch the click CLI. Each
resolution is one call over a directory under ``tmp_path`` with an explicit environment mapping, so
no case depends on the ambient environment.

Two points where the modules on disk differ from the brief, the modules being authoritative (AAP
0.4.3, AAP 0.4.9): the settings model raises pydantic's ``ValidationError``, which ``resolve``
translates into the wording AAP 0.4.5 pins; and five keys publish their range as the inclusive
interval ``0..10`` rather than as prose.

Determinism (AAP 0.14.3): every file lives under ``tmp_path``, no wall-clock value is read or
asserted, every collection compared is built in a fixed or sorted order, and the one subprocess runs
a two-line interpreter command that opens no socket.
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

from foundry.config import cli_layer, env, keys, layers, merge, render, validation
from foundry.config.enums import ProfileName, RetryPredicate
from foundry.config.models import FoundryConfig
from foundry.config.sources import ConfigSource
from foundry.core.exceptions import ConfigError
from foundry.obs import capture

# The two file names the third and fourth discovery steps look for (AAP 0.4.5) and a
# credential-shaped header value assembled at run time so no scanner reads this file as carrying a
# secret.
_FOUNDRY_TOML: Final[str] = "foundry.toml"
_PYPROJECT_TOML: Final[str] = "pyproject.toml"
_SECRET: Final[str] = "sentinel" + "-header-value"

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


def test_leaf_keys_are_the_twenty_eight_published_keys() -> None:
    """The registry publishes exactly the 28 keys of AAP 0.4.5, in table order and nothing else."""
    published = keys.leaf_keys()

    assert len(published) == 28
    assert ", ".join(published) == _EXPECTED_KEYS
    assert sorted(published) == sorted(set(published))


def test_an_unknown_key_is_refused_by_the_file_layer_and_by_the_model(
    tmp_path: Path, clean_foundry_env: None
) -> None:
    """``extra="forbid"`` rejects a misspelt key in a file and an unexpected constructor keyword."""
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


def test_discovery_walks_the_five_steps_in_the_documented_order(
    tmp_path: Path, clean_foundry_env: None
) -> None:
    """First match wins across the five discovery steps of AAP 0.4.5; the defaults come last."""
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
    """The registry imports without the settings stack, in a fresh interpreter (I5).

    It is imported on every pytest startup, and this process has already imported pydantic.
    """
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


def test_a_source_layer_is_a_config_layer_carrying_its_published_spelling() -> None:
    """A record's layer is a ``ConfigLayer``, the parameterised ``profile:<name>`` included."""
    profiled = ConfigSource.from_profile("ci")
    from_environment = ConfigSource.from_env("FOUNDRY_HTTP__TIMEOUT")

    assert isinstance(profiled.layer, layers.ConfigLayer)
    assert profiled.layer == "profile:ci"
    assert str(profiled.layer) == "profile:ci"
    assert profiled.label() == "profile:ci"
    assert from_environment.layer is layers.ConfigLayer.ENV
    assert from_environment.label() == "env:FOUNDRY_HTTP__TIMEOUT"

    members = [str(member) for member in layers.ConfigLayer]
    assert members == ["default", "file", "profile", "env", "cli"]


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--foundry-artifacts.dir", "/tmp/artifacts"], ["--foundry-artifacts.dir=/tmp/artifacts"]),
        (["--foundry-profile", "ci"], ["--foundry-profile=ci"]),
        (
            ["-k", "case", "--foundry-config", "foundry.toml"],
            ["-k", "case", "--foundry-config=foundry.toml"],
        ),
        (["--foundry-artifacts.dir=/tmp/artifacts"], None),
        (["--foundry-record"], None),
        (["--foundry-record", "tests"], None),
        (["--foundry-artifacts.dir"], None),
        (["--foundry-artifacts.dir", "--foundry-record"], None),
        (["tests", "-x"], None),
        ([], None),
    ],
)
def test_a_value_typed_as_its_own_token_is_folded_onto_its_option(
    argv: list[str], expected: list[str] | None
) -> None:
    """Only a Foundry value option and the token after it fold; a switch keeps its argument."""
    assert cli_layer.fold_option_values(argv) == expected


class _UnreadableConfig:
    """A ``pytest.Config`` stand-in whose pre-parse state cannot be read a second time."""

    rootpath = Path("/nowhere")
    inipath: Path | None = None
    _inicache: dict[str, object] = {}

    def getini(self, name: str) -> list[str]:
        """Answer the two ini reads the repair makes before it touches pytest's internals."""
        return []

    def _initini(self, args: list[str]) -> None:
        """Stand in for the private step a later pytest is free to have changed."""
        raise AttributeError("_initini")


def test_a_repair_that_cannot_run_warns_once_and_changes_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AAP 0.1.2: Foundry's own fault is one ``WARNING``, and nothing else about the run moves."""
    config = _UnreadableConfig()
    with caplog.at_level(logging.WARNING, logger="foundry"):
        repaired = cli_layer.restore_preparse_state(config, ["--foundry-artifacts.dir", "/tmp/x"])

    assert repaired is False
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "config.cli_layer" in warnings[0].getMessage()
