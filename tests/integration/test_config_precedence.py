"""The four-layer configuration ladder with per-key provenance (AAP 0.14.1 criterion 4): the value
resolves 1.0 (cli), 3.5 (env), 2.0 (file) and 10.0 (default), each naming its layer."""

import json
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TypeAlias

import pytest
from click.testing import CliRunner

from foundry.cli.main import build_group
from foundry.config import FoundryConfig, ProfileName, cli_layer, resolve
from foundry.core.exceptions import ConfigError

TIMEOUT_KEY: Final[str] = "http.timeout"
TIMEOUT_OPTION: Final[str] = "--foundry-http.timeout"
CONFIG_OPTION: Final[str] = "--foundry-config"
TIMEOUT_VARIABLE: Final[str] = "FOUNDRY_HTTP__TIMEOUT"
CONFIG_FILE_NAME: Final[str] = "foundry.toml"
PYPROJECT_FILE_NAME: Final[str] = "pyproject.toml"

DEFAULT_TIMEOUT: Final[float] = 10.0
FILE_TIMEOUT: Final[float] = 2.0
ENVIRONMENT_TIMEOUT: Final[float] = 3.5
OPTION_TIMEOUT: Final[float] = 1.0
PROFILE_TIMEOUT: Final[float] = 4.0
PYPROJECT_PROFILE_TIMEOUT: Final[float] = 4.5
FILE_MAX_ATTEMPTS: Final[int] = 5
ENVIRONMENT_OVERRIDE: Final[Mapping[str, str]] = {TIMEOUT_VARIABLE: str(ENVIRONMENT_TIMEOUT)}
OPTION_OVERRIDE: Final[Mapping[str, str]] = {TIMEOUT_OPTION: str(OPTION_TIMEOUT)}

FILE_DOCUMENT: Final[str] = "[http]\ntimeout = 2.0\n[http.retries]\nmax_attempts = 5\n"

PROFILE_DOCUMENT: Final[str] = (
    f'profile = "ci"\n\n{FILE_DOCUMENT}\n[profiles.ci.http]\ntimeout = {PROFILE_TIMEOUT}\n'
)

PYPROJECT_PROFILE_DOCUMENT: Final[str] = (
    f'[tool.foundry]\nprofile = "ci"\n\n[tool.foundry.http]\ntimeout = {FILE_TIMEOUT}\n'
    f"\n[tool.foundry.profiles.ci.http]\ntimeout = {PYPROJECT_PROFILE_TIMEOUT}\n"
)


def doc(timeout: float, *, table: str = "[http]") -> str:
    return f"{table}\ntimeout = {timeout}\n"


@dataclass(frozen=True, slots=True)
class Candidate:
    file_name: str
    document: str
    timeout: float


# Each candidate declares a distinct value, so the winner of a pair cannot be mistaken.
DISCOVERY_CANDIDATES: Final[Mapping[str, Candidate]] = {
    "cli": Candidate("from-option.toml", doc(7.5), 7.5),
    "env": Candidate("from-variable.toml", doc(6.5), 6.5),
    "toml": Candidate(CONFIG_FILE_NAME, doc(5.5), 5.5),
    "pyproject": Candidate(PYPROJECT_FILE_NAME, doc(4.25, table="[tool.foundry.http]"), 4.25),
}


def write_file(directory: Path, name: str, document: str) -> Path:
    path = directory / name
    path.write_text(document, encoding="utf-8", newline="\n")
    assert path.read_text(encoding="utf-8") == document, f"{path} lost the document it was given"
    return path.resolve()


def resolve_at(
    directory: Path,
    *,
    cli: cli_layer.CliInput | None = None,
    environ: Mapping[str, str] | None = None,
) -> FoundryConfig:
    return resolve(cli_overrides=cli, environ={} if environ is None else environ, cwd=directory)


def option_input(spelling: str, value: str) -> cli_layer.CliInput:
    return cli_layer.from_mapping({spelling: value})


def assert_source(
    config: FoundryConfig, dotted: str, *, layer: str, detail: str | None = None
) -> None:
    source = config.source_of(dotted)
    assert str(source.layer) == layer, f"{dotted} comes from {source.layer!r}, not {layer!r}"
    if detail is not None:
        assert source.detail == detail, f"{dotted} detail is {source.detail!r}, not {detail!r}"


# --- The four-layer ladder (AAP 0.1.2, AAP 0.14.1 criterion 4) --------------

Rung: TypeAlias = tuple[bool, Mapping[str, str], Mapping[str, str], float, str | None]
LADDER: Final[Mapping[str, Rung]] = {
    "default": (False, {}, {}, DEFAULT_TIMEOUT, ""),
    "file": (True, {}, {}, FILE_TIMEOUT, None),
    "env": (True, ENVIRONMENT_OVERRIDE, {}, ENVIRONMENT_TIMEOUT, TIMEOUT_VARIABLE),
    "cli": (True, ENVIRONMENT_OVERRIDE, OPTION_OVERRIDE, OPTION_TIMEOUT, TIMEOUT_OPTION),
}


@pytest.mark.parametrize("layer", list(LADDER))
def test_the_worked_example_resolves_one_layer_above_the_other(
    clean_foundry_env: None, tmp_path: Path, layer: str
) -> None:
    staged_file, environ, options, expected, detail = LADDER[layer]
    config_path = write_file(tmp_path, CONFIG_FILE_NAME, FILE_DOCUMENT) if staged_file else None
    cli = cli_layer.from_mapping(dict(options)) if options else None
    config = resolve_at(tmp_path, cli=cli, environ=environ)
    assert config.http.timeout == expected, f"http.timeout is {config.http.timeout!r}"
    assert config.profile is ProfileName.DEFAULT, f"profile is {config.profile!r}"
    if staged_file:
        # The layer below the winner is still read: the file's other key arrives untouched.
        assert config.http.retries.max_attempts == FILE_MAX_ATTEMPTS, "the file's other key"
    resolved_detail = str(config_path) if detail is None else detail
    assert_source(config, TIMEOUT_KEY, layer=layer, detail=resolved_detail)


@pytest.mark.parametrize("spelling", [TIMEOUT_VARIABLE, "foundry_http__timeout"])
def test_a_variable_name_is_matched_case_insensitively(
    clean_foundry_env: None, tmp_path: Path, spelling: str
) -> None:
    write_file(tmp_path, CONFIG_FILE_NAME, FILE_DOCUMENT)
    config = resolve_at(tmp_path, environ={spelling: str(ENVIRONMENT_TIMEOUT)})
    assert config.http.timeout == ENVIRONMENT_TIMEOUT, f"{spelling} gave {config.http.timeout!r}"
    assert_source(config, TIMEOUT_KEY, layer="env", detail=spelling)


ProfileCase: TypeAlias = tuple[str, str, Mapping[str, str], float, bool]
PROFILE_CASES: Final[tuple[ProfileCase, ...]] = (
    (CONFIG_FILE_NAME, PROFILE_DOCUMENT, {}, PROFILE_TIMEOUT, True),
    (CONFIG_FILE_NAME, PROFILE_DOCUMENT, ENVIRONMENT_OVERRIDE, ENVIRONMENT_TIMEOUT, True),
    (PYPROJECT_FILE_NAME, PYPROJECT_PROFILE_DOCUMENT, {}, PYPROJECT_PROFILE_TIMEOUT, False),
)


@pytest.mark.parametrize("case", PROFILE_CASES)
def test_a_profile_table_sits_between_the_file_and_the_environment(
    clean_foundry_env: None, tmp_path: Path, case: ProfileCase
) -> None:
    file_name, document, environ, expected, declares_retries = case
    config_path = write_file(tmp_path, file_name, document)
    config = resolve_at(tmp_path, environ=environ)
    assert config.profile is ProfileName.CI, f"profile is {config.profile!r}"
    assert config.http.timeout == expected, f"http.timeout is {config.http.timeout!r}"
    origin = ("env", TIMEOUT_VARIABLE) if environ else ("profile:ci", ProfileName.CI.value)
    assert_source(config, TIMEOUT_KEY, layer=origin[0], detail=origin[1])
    if declares_retries:
        # A key no profile table touches still arrives from the file, with the file's attribution.
        assert config.http.retries.max_attempts == FILE_MAX_ATTEMPTS, "the file's other key"
        assert_source(config, "http.retries.max_attempts", layer="file", detail=str(config_path))


# --- File discovery, one adjacent pair at a time (AAP 0.4.5) ----------------


@pytest.mark.parametrize("staged", [tuple(DISCOVERY_CANDIDATES)[:n] for n in (4, 3, 2, 1, 0)])
def test_discovery_answers_with_the_first_candidate_that_applies(
    clean_foundry_env: None, tmp_path: Path, staged: tuple[str, ...]
) -> None:
    candidates = [(source, DISCOVERY_CANDIDATES[source]) for source in staged]
    paths = {s: write_file(tmp_path, c.file_name, c.document) for s, c in candidates}
    cli = option_input(CONFIG_OPTION, str(paths["cli"])) if "cli" in paths else None
    environ = {"FOUNDRY_CONFIG": str(paths["env"])} if "env" in paths else None
    config = resolve_at(tmp_path, cli=cli, environ=environ)
    winner = DISCOVERY_CANDIDATES[staged[0]] if staged else None
    expected = DEFAULT_TIMEOUT if winner is None else winner.timeout
    assert config.http.timeout == expected, f"http.timeout is {config.http.timeout!r}"
    # The defaults rung has no file to attribute, so it names the layer alone.
    layer = "default" if winner is None else "file"
    detail = "" if winner is None else str(paths[staged[0]])
    assert_source(config, TIMEOUT_KEY, layer=layer, detail=detail)


def test_an_explicitly_named_file_that_is_absent_is_refused(
    clean_foundry_env: None, tmp_path: Path
) -> None:
    missing = (tmp_path / "absent.toml").resolve()
    with pytest.raises(ConfigError) as raised:
        resolve_at(tmp_path, cli=option_input(CONFIG_OPTION, str(missing)))
    error = raised.value
    assert str(error) == f"config file not found: {missing}", f"the fault reads {error}"
    assert (error.path, str(error.layer)) == (missing, "file"), f"the fault is {error}"


ShowCase: TypeAlias = tuple[bool, str, float, str]
SHOW_CASES: Final[tuple[ShowCase, ...]] = (
    (True, "json", OPTION_TIMEOUT, "cli"),
    (True, "toml", OPTION_TIMEOUT, "cli"),
    (False, "json", FILE_TIMEOUT, "file"),
)


@pytest.mark.parametrize("case", SHOW_CASES)
def test_config_show_attributes_each_value_to_its_layer(
    clean_foundry_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: ShowCase
) -> None:
    resolved, output_format, expected, layer = case
    config_path = write_file(tmp_path, CONFIG_FILE_NAME, FILE_DOCUMENT)
    monkeypatch.chdir(tmp_path)
    flags = ["--resolved"] if resolved else []
    arguments = ["config", "show", *flags, TIMEOUT_OPTION, str(OPTION_TIMEOUT)]
    result = CliRunner().invoke(build_group(), [*arguments, "--format", output_format])
    assert result.exit_code == 0, f"exit {result.exit_code}: {result.output}"
    assert result.stderr == "", f"the command wrote to stderr: {result.stderr!r}"
    document = (
        json.loads(result.stdout) if output_format == "json" else tomllib.loads(result.stdout)
    )
    values = document["resolved"] if output_format == "json" else document
    assert values["http"]["timeout"] == expected, f"http.timeout is {values['http']['timeout']!r}"
    detail = TIMEOUT_OPTION if resolved else str(config_path)
    assert document["sources"][TIMEOUT_KEY] == {"layer": layer, "detail": detail}
    if resolved:
        assert values["http"]["retries"]["max_attempts"] == FILE_MAX_ATTEMPTS
    else:
        # Without --resolved the override is reported beside the resolution rather than applied.
        assert document["not_applied"] == {TIMEOUT_KEY: str(OPTION_TIMEOUT)}
