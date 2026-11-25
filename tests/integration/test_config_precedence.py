"""The four-layer configuration ladder, end to end, with the attribution of every value it resolves.

AAP 0.4.5 fixes the resolution order -- field ``default``, then the configuration file with its
selected ``[profiles.<name>]`` table, then the ``FOUNDRY_*`` environment, then the ``--foundry-*``
command line -- and AAP 0.1.2 states the worked example that has to hold verbatim: with a
``foundry.toml`` carrying ``http.timeout = 2.0``, ``FOUNDRY_HTTP__TIMEOUT=3.5`` and
``--foundry-http.timeout 1.0`` the resolved value is ``1.0``; dropping the command line yields
``3.5``, dropping the environment yields ``2.0``, and with no file the ``10.0`` default applies.
This module is the integration evidence for AAP 0.14.1 criterion 4, which adds the second half of
the claim: ``foundry config show --resolved`` attributes each value to the layer that supplied it.

Every assertion drives a real entry point and no substitute -- the ladder through
:func:`foundry.config.merge.resolve`, the attribution through
:meth:`foundry.config.models.FoundryConfig.source_of`, and the ``cli`` attribution through the click
application :func:`foundry.cli.main.build_group` under a ``CliRunner``. Each rung is an independent
resolution in its own ``tmp_path``, given an explicit environment and invocation directory, so
nothing here depends on the developer's shell, on the process working directory or on a resolution
an earlier rung performed (AAP 0.13.1). A resolution reads files and a mapping and nothing else: no
file outside ``tmp_path`` is written and no socket is opened.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final

import pytest
from click.testing import CliRunner

from foundry.cli.main import build_group
from foundry.config import FoundryConfig, ProfileName, cli_layer, resolve
from foundry.core.exceptions import ConfigError

# --- The keys, options, variables and files every case resolves -------------

TIMEOUT_KEY: Final[str] = "http.timeout"
"""The key AAP 0.1.2's worked example resolves: the one key a layer is proved on."""

TIMEOUT_OPTION: Final[str] = "--foundry-http.timeout"
"""The generic option of :data:`TIMEOUT_KEY`, which the worked example types (AAP 0.4.5)."""

CONFIG_OPTION: Final[str] = "--foundry-config"
"""The option naming a configuration file: the first discovery step (AAP 0.4.5)."""

CONFIG_VARIABLE: Final[str] = "FOUNDRY_CONFIG"
"""The variable naming a configuration file: the second discovery step, and no key of its own."""

TIMEOUT_VARIABLE: Final[str] = "FOUNDRY_HTTP__TIMEOUT"
"""The variable spelling of :data:`TIMEOUT_KEY`: the prefix, then ``__`` between its segments."""

LOWER_TIMEOUT_VARIABLE: Final[str] = "foundry_http__timeout"
"""The same key in the other case, which the environment layer matches identically (AAP 0.4.5)."""

CONFIG_FILE_NAME: Final[str] = "foundry.toml"
"""Foundry's own configuration file: the third discovery step (AAP 0.4.5)."""

PYPROJECT_FILE_NAME: Final[str] = "pyproject.toml"
"""The file whose ``[tool.foundry]`` table configures Foundry: the fourth discovery step."""

MISSING_FILE_NAME: Final[str] = "absent.toml"
"""A file an operator names explicitly and that does not exist: a fault, not a fall-through."""

MISSING_FILE_TEMPLATE: Final[str] = "config file not found: {path}"
"""The wording AAP 0.1.2 pins for that fault, which a reader acts on by opening the path."""

ACCEPTED_TIMEOUT_RANGE: Final[str] = "0 < t <= 300"
"""How AAP 0.4.5 renders the range ``http.timeout`` accepts, quoted by an out-of-range message."""

RETRIES_ATTEMPTS_KEY: Final[str] = "http.retries.max_attempts"
"""A key the base file sets and no profile table touches, which pins a profile's attribution."""

# --- The values each layer of the worked example supplies -------------------

DEFAULT_TIMEOUT: Final[float] = 10.0
"""``http.timeout`` from the field default AAP 0.4.5 declares: no file, no variable, no option."""

FILE_TIMEOUT: Final[float] = 2.0
"""``http.timeout`` as the worked example's ``foundry.toml`` states it."""

ENVIRONMENT_TIMEOUT: Final[float] = 3.5
"""``http.timeout`` as ``FOUNDRY_HTTP__TIMEOUT`` states it: above the file, below the option."""

OPTION_TIMEOUT: Final[float] = 1.0
"""``http.timeout`` as ``--foundry-http.timeout`` states it: the value the full stack resolves."""

PROFILE_TIMEOUT: Final[float] = 4.0
"""``http.timeout`` as ``[profiles.ci]`` states it: above the base file, below the environment."""

PYPROJECT_PROFILE_TIMEOUT: Final[float] = 4.5
"""``http.timeout`` as ``[tool.foundry.profiles.ci]`` states it: the same feature, respelled."""

FILE_MAX_ATTEMPTS: Final[int] = 5
"""``http.retries.max_attempts`` as the documents state it, a key no profile table touches."""

# --- The documents every case writes into its own tmp_path ------------------

FILE_DOCUMENT: Final[str] = """\
[http]
timeout = 2.0

[http.retries]
max_attempts = 5
"""
"""A ``foundry.toml`` setting the worked example's key and one key no profile table touches."""

PROFILE_DOCUMENT: Final[str] = """\
profile = "ci"

[http]
timeout = 2.0

[http.retries]
max_attempts = 5

[profiles.ci.http]
timeout = 4.0
"""
"""A file selecting ``ci``, whose profile table overrides ``http.timeout`` and nothing else."""

PYPROJECT_PROFILE_DOCUMENT: Final[str] = """\
[tool.foundry]
profile = "ci"

[tool.foundry.http]
timeout = 2.0

[tool.foundry.profiles.ci.http]
timeout = 4.5
"""
"""The same selection and override spelled as ``[tool.foundry.profiles.ci]`` (AAP 0.4.5)."""

MISSPELT_KEY_DOCUMENT: Final[str] = """\
[http]
timout = 2.0
"""
"""A file whose only key is misspelt, which no layer may accept (AAP 0.4.5)."""

OUT_OF_RANGE_DOCUMENT: Final[str] = """\
[http]
timeout = -1
"""
"""AAP 0.4.5's own worked fault: a value outside the range ``http.timeout`` accepts."""


def timeout_document(timeout: float, *, table: str = "[http]") -> str:
    """Return a TOML document setting ``http.timeout`` inside ``table``.

    Args:
        timeout: The value the document declares.
        table: The table header the key sits under; a ``pyproject.toml`` candidate needs
            ``[tool.foundry.http]`` where a ``foundry.toml`` candidate needs ``[http]``.

    Returns:
        The TOML text, LF-terminated, so the discovery candidates differ only in their value.
    """
    return f"{table}\ntimeout = {timeout}\n"


@dataclass(frozen=True, slots=True)
class Candidate:
    """One configuration-file candidate of the five-step discovery order (AAP 0.4.5).

    Attributes:
        file_name: The name discovery looks for.
        document: The TOML text it holds.
        timeout: The ``http.timeout`` value it declares, distinct per candidate so the winner of a
            pair is unambiguous.
    """

    file_name: str
    document: str
    timeout: float


DISCOVERY_CANDIDATES: Final[Mapping[str, Candidate]] = MappingProxyType(
    {
        "cli": Candidate("from-option.toml", timeout_document(7.5), 7.5),
        "env": Candidate("from-variable.toml", timeout_document(6.5), 6.5),
        "toml": Candidate(CONFIG_FILE_NAME, timeout_document(5.5), 5.5),
        "pyproject": Candidate(
            PYPROJECT_FILE_NAME, timeout_document(4.25, table="[tool.foundry.http]"), 4.25
        ),
    }
)
"""The four file candidates in discovery order, keyed by the step that selects each one.

Every value is distinct and none is the field default, so a resolution that selected the wrong
candidate reports a value that cannot be mistaken for the right one.
"""


def write_config_file(directory: Path, name: str, document: str) -> Path:
    """Write ``document`` to ``directory / name`` and return the path it was written to.

    The text is written with an explicit encoding and LF endings and read straight back, so the
    bytes parsed are the bytes this module states whatever the host's defaults are.

    Args:
        directory: The invocation directory the resolution will read.
        name: The file's name, which discovery matches on.
        document: The TOML text the file holds.

    Returns:
        The resolved path of the written file, which is also the detail a ``file`` source records.
    """
    path = directory / name
    path.write_text(document, encoding="utf-8", newline="\n")
    assert path.read_text(encoding="utf-8") == document, f"{path} does not hold the document given"
    return path.resolve()


def resolve_at(
    directory: Path,
    *,
    cli: cli_layer.CliInput | None = None,
    environ: Mapping[str, str] | None = None,
) -> FoundryConfig:
    """Resolve one configuration with ``directory`` as the invocation directory.

    ``environ`` defaults to the empty mapping rather than to the process environment, because a
    supplied mapping *is* the whole environment for that resolution (AAP 0.4.5): that is what keeps
    every case here independent of the shell it runs in, with ``clean_foundry_env`` closing the door
    on the ambient variables the click application would otherwise read.

    Args:
        directory: The invocation directory file discovery and a relative path resolve against.
        cli: The command line's coerced overrides, or ``None`` for a run without one.
        environ: The environment to read ``FOUNDRY_*`` variables from, or ``None`` for none at all.

    Returns:
        The resolved configuration, carrying one source record per published key.

    Raises:
        foundry.core.exceptions.ConfigError: For the first fatal fault, as the resolver reports it.
    """
    return resolve(cli_overrides=cli, environ={} if environ is None else environ, cwd=directory)


def option_input(spelling: str, value: str) -> cli_layer.CliInput:
    """Return the command line carrying one ``--foundry-*`` option and the value typed after it.

    Built through :func:`foundry.config.cli_layer.from_mapping`, which owns the coercions and the
    spelling rules both surfaces share, so a case never hand-builds an override the parser would
    have represented differently.

    Args:
        spelling: The option as an operator types it, for example ``--foundry-http.timeout``.
        value: The value as text, exactly as a shell would hand it over.

    Returns:
        The overrides that command line supplies, with the spelling behind each one.
    """
    return cli_layer.from_mapping({spelling: value})


def stage_candidates(directory: Path, sources: Sequence[str]) -> Mapping[str, Path]:
    """Write every candidate ``sources`` names and return the path each one was written to.

    Args:
        directory: The invocation directory to write into.
        sources: The discovery steps whose candidates exist, in the order they would win.

    Returns:
        One path per staged source, keyed by the same name.
    """
    return {
        source: write_config_file(
            directory,
            DISCOVERY_CANDIDATES[source].file_name,
            DISCOVERY_CANDIDATES[source].document,
        )
        for source in sources
    }


def assert_source(
    config: FoundryConfig, dotted: str, *, layer: str, detail: str | None = None
) -> None:
    """Assert the layer -- and, when one is given, the detail -- that supplied ``dotted``.

    Args:
        config: The resolved configuration to read.
        dotted: The dotted key whose origin is under test.
        layer: The expected label: ``default``, ``file``, ``env``, ``cli`` or ``profile:<name>``.
        detail: The expected name inside that layer -- a path, a variable, an option or a profile --
            or ``None`` to assert the layer alone.
    """
    source = config.source_of(dotted)
    assert str(source.layer) == layer, (
        f"{dotted} is attributed to layer {str(source.layer)!r}, not {layer!r} (record {source!r})"
    )
    if detail is not None:
        assert source.detail == detail, (
            f"inside layer {layer!r}, {dotted} is attributed to {source.detail!r}, not {detail!r}"
        )


# --- The four-layer ladder (AAP 0.1.2, AAP 0.14.1 criterion 4) --------------


@pytest.mark.parametrize(
    ("staged_file", "environ", "options", "expected", "layer", "detail"),
    [
        pytest.param(False, {}, {}, DEFAULT_TIMEOUT, "default", "", id="defaults"),
        pytest.param(True, {}, {}, FILE_TIMEOUT, "file", None, id="file"),
        pytest.param(
            True,
            {TIMEOUT_VARIABLE: str(ENVIRONMENT_TIMEOUT)},
            {},
            ENVIRONMENT_TIMEOUT,
            "env",
            TIMEOUT_VARIABLE,
            id="environment",
        ),
        pytest.param(
            True,
            {TIMEOUT_VARIABLE: str(ENVIRONMENT_TIMEOUT)},
            {TIMEOUT_OPTION: str(OPTION_TIMEOUT)},
            OPTION_TIMEOUT,
            "cli",
            TIMEOUT_OPTION,
            id="command-line",
        ),
    ],
)
def test_the_worked_example_resolves_one_layer_above_the_other(
    clean_foundry_env: None,
    tmp_path: Path,
    staged_file: bool,
    environ: Mapping[str, str],
    options: Mapping[str, str],
    expected: float,
    layer: str,
    detail: str | None,
) -> None:
    """Each rung of AAP 0.1.2's example is one independent resolution in its own directory.

    With no file the ``10.0`` default applies; the file makes it ``2.0``; adding
    ``FOUNDRY_HTTP__TIMEOUT=3.5`` makes it ``3.5``; adding ``--foundry-http.timeout 1.0`` makes it
    ``1.0``. The four parameters are four resolutions, each with its own environment and invocation
    directory, so no rung is read through another's leftovers.
    """
    config_path = (
        write_config_file(tmp_path, CONFIG_FILE_NAME, FILE_DOCUMENT) if staged_file else None
    )
    config = resolve_at(
        tmp_path,
        cli=cli_layer.from_mapping(dict(options)) if options else None,
        environ=environ,
    )

    assert config.http.timeout == expected, (
        f"http.timeout resolved to {config.http.timeout!r}, not {expected!r}"
    )
    assert config.profile is ProfileName.DEFAULT, f"profile resolved to {config.profile!r}"
    if staged_file:
        # The layer below the winner is still read: the file's other key arrives untouched, so a
        # higher layer overrode one value rather than replacing the whole document.
        assert config.http.retries.max_attempts == FILE_MAX_ATTEMPTS, (
            f"http.retries.max_attempts resolved to {config.http.retries.max_attempts!r}"
        )
    if detail is None:
        assert config_path is not None, "the file rung stages a file, so its path is known"
        detail = str(config_path)
    assert_source(config, TIMEOUT_KEY, layer=layer, detail=detail)


@pytest.mark.parametrize("spelling", [TIMEOUT_VARIABLE, LOWER_TIMEOUT_VARIABLE])
def test_a_variable_name_is_matched_case_insensitively(
    clean_foundry_env: None, tmp_path: Path, spelling: str
) -> None:
    """Both cases of one variable name set the same key, and the source names the spelling used.

    AAP 0.4.5 makes ``FOUNDRY_*`` names case-insensitive, and the recorded detail is the operator's
    own spelling rather than a normalised one, so ``config show`` prints the variable they exported.
    """
    write_config_file(tmp_path, CONFIG_FILE_NAME, FILE_DOCUMENT)
    config = resolve_at(tmp_path, environ={spelling: str(ENVIRONMENT_TIMEOUT)})

    assert config.http.timeout == ENVIRONMENT_TIMEOUT, (
        f"{spelling} resolved http.timeout to {config.http.timeout!r}"
    )
    assert_source(config, TIMEOUT_KEY, layer="env", detail=spelling)


# --- The profile table merges between the file and the environment ----------


def test_a_profile_table_overrides_the_base_file_it_sits_in(
    clean_foundry_env: None, tmp_path: Path
) -> None:
    """``[profiles.ci]`` wins over the base table key by key, and the attribution follows."""
    config_path = write_config_file(tmp_path, CONFIG_FILE_NAME, PROFILE_DOCUMENT)
    config = resolve_at(tmp_path)

    assert config.profile is ProfileName.CI, f"profile resolved to {config.profile!r}, not ci"
    assert config.http.timeout == PROFILE_TIMEOUT, (
        f"http.timeout resolved to {config.http.timeout!r}, not the profile's {PROFILE_TIMEOUT!r}"
    )
    assert_source(config, TIMEOUT_KEY, layer="profile:ci", detail=ProfileName.CI.value)
    assert config.http.retries.max_attempts == FILE_MAX_ATTEMPTS, (
        f"http.retries.max_attempts resolved to {config.http.retries.max_attempts!r}"
    )
    assert_source(config, RETRIES_ATTEMPTS_KEY, layer="file", detail=str(config_path))


def test_the_environment_outranks_the_selected_profile_table(
    clean_foundry_env: None, tmp_path: Path
) -> None:
    """The environment sits above the profile table, which is what fixes the profile's position."""
    write_config_file(tmp_path, CONFIG_FILE_NAME, PROFILE_DOCUMENT)
    config = resolve_at(tmp_path, environ={TIMEOUT_VARIABLE: str(ENVIRONMENT_TIMEOUT)})

    assert config.profile is ProfileName.CI, f"profile resolved to {config.profile!r}, not ci"
    assert config.http.timeout == ENVIRONMENT_TIMEOUT, (
        f"http.timeout resolved to {config.http.timeout!r}, not the environment's "
        f"{ENVIRONMENT_TIMEOUT!r} over the profile's {PROFILE_TIMEOUT!r}"
    )
    assert_source(config, TIMEOUT_KEY, layer="env", detail=TIMEOUT_VARIABLE)


def test_a_pyproject_profile_table_is_applied_the_same_way(
    clean_foundry_env: None, tmp_path: Path
) -> None:
    """``[tool.foundry.profiles.ci]`` is ``[profiles.ci]`` respelled for a pyproject (0.4.5)."""
    write_config_file(tmp_path, PYPROJECT_FILE_NAME, PYPROJECT_PROFILE_DOCUMENT)
    config = resolve_at(tmp_path)

    assert config.profile is ProfileName.CI, f"profile resolved to {config.profile!r}, not ci"
    assert config.http.timeout == PYPROJECT_PROFILE_TIMEOUT, (
        f"http.timeout resolved to {config.http.timeout!r}, not {PYPROJECT_PROFILE_TIMEOUT!r}"
    )
    assert_source(config, TIMEOUT_KEY, layer="profile:ci", detail=ProfileName.CI.value)


# --- File discovery, one adjacent pair at a time (AAP 0.4.5) ----------------


@pytest.mark.parametrize(
    "staged",
    [
        pytest.param(("cli", "env", "toml", "pyproject"), id="option-beats-variable"),
        pytest.param(("env", "toml", "pyproject"), id="variable-beats-toml"),
        pytest.param(("toml", "pyproject"), id="toml-beats-pyproject"),
        pytest.param(("pyproject",), id="pyproject-beats-defaults"),
        pytest.param((), id="defaults"),
    ],
)
def test_discovery_answers_with_the_first_candidate_that_applies(
    clean_foundry_env: None, tmp_path: Path, staged: tuple[str, ...]
) -> None:
    """First match wins, so each step is asserted against the one immediately below it.

    Every staged candidate declares a different ``http.timeout``, so the winner is unambiguous: the
    named option beats ``FOUNDRY_CONFIG``, which beats ``foundry.toml``, which beats
    ``[tool.foundry]``, which beats the field defaults. A case stages only the candidates it names,
    so no case observes a file another case wrote.
    """
    paths = stage_candidates(tmp_path, staged)
    config = resolve_at(
        tmp_path,
        cli=option_input(CONFIG_OPTION, str(paths["cli"])) if "cli" in paths else None,
        environ={CONFIG_VARIABLE: str(paths["env"])} if "env" in paths else None,
    )

    if not staged:
        assert config.http.timeout == DEFAULT_TIMEOUT, (
            f"with no candidate staged http.timeout resolved to {config.http.timeout!r}, "
            f"not the default {DEFAULT_TIMEOUT!r}"
        )
        assert_source(config, TIMEOUT_KEY, layer="default", detail="")
        return

    winner = DISCOVERY_CANDIDATES[staged[0]]
    assert config.http.timeout == winner.timeout, (
        f"http.timeout resolved to {config.http.timeout!r}, not the {staged[0]!r} candidate's "
        f"{winner.timeout!r} that discovery had to select first"
    )
    assert_source(config, TIMEOUT_KEY, layer="file", detail=str(paths[staged[0]]))


# --- Configuration faults (AAP 0.4.5, AAP 0.4.7) ---------------------------


def test_an_explicitly_named_file_that_is_absent_is_refused(
    clean_foundry_env: None, tmp_path: Path
) -> None:
    """A named file is never passed over: the message is ``config file not found: <path>``."""
    missing = (tmp_path / MISSING_FILE_NAME).resolve()
    with pytest.raises(ConfigError) as raised:
        resolve_at(tmp_path, cli=option_input(CONFIG_OPTION, str(missing)))

    error = raised.value
    assert str(error) == MISSING_FILE_TEMPLATE.format(path=missing), (
        f"the fault reads {str(error)!r}, not the documented wording for {missing}"
    )
    assert error.path == missing, f"the fault names {error.path!r}, not the path typed {missing}"
    assert str(error.layer) == "file", f"the fault is attributed to {str(error.layer)!r}"


def test_a_misspelt_key_is_refused_with_the_closest_valid_spelling(
    clean_foundry_env: None, tmp_path: Path
) -> None:
    """An unknown key is refused in every layer, naming the key and the nearest valid spelling."""
    write_config_file(tmp_path, CONFIG_FILE_NAME, MISSPELT_KEY_DOCUMENT)
    with pytest.raises(ConfigError) as raised:
        resolve_at(tmp_path)

    error = raised.value
    assert error.key == "http.timout", f"the fault names key {error.key!r}, not the key written"
    assert "http.timout" in str(error), f"the message {str(error)!r} does not name the key"
    assert error.hint is not None, "an unknown key carries the closest valid spelling as its hint"
    assert TIMEOUT_KEY in error.hint, f"the hint {error.hint!r} does not suggest {TIMEOUT_KEY!r}"


def test_a_value_outside_its_range_names_the_key_and_the_accepted_range(
    clean_foundry_env: None, tmp_path: Path
) -> None:
    """``http.timeout = -1`` is AAP 0.4.5's worked fault: the key and the range, nothing else."""
    write_config_file(tmp_path, CONFIG_FILE_NAME, OUT_OF_RANGE_DOCUMENT)
    with pytest.raises(ConfigError) as raised:
        resolve_at(tmp_path)

    error = raised.value
    assert error.key == TIMEOUT_KEY, f"the fault names key {error.key!r}, not {TIMEOUT_KEY!r}"
    assert TIMEOUT_KEY in str(error), f"the message {str(error)!r} does not name the key"
    assert ACCEPTED_TIMEOUT_RANGE in str(error), (
        f"the message {str(error)!r} does not state the accepted range {ACCEPTED_TIMEOUT_RANGE!r}"
    )
    assert str(error.layer) == "file", f"the fault is attributed to {str(error.layer)!r}"


# --- The cli attribution through the real command (AAP 0.4.4, AAP 0.7.4) ----


def test_config_show_resolved_attributes_the_override_to_the_option(
    clean_foundry_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``config show --resolved`` applies the override and attributes it to ``cli:<option>``.

    The whole of stdout is one JSON object, so the document a consumer parses is the document this
    command prints and nothing else; the file's value is overridden rather than replaced, which the
    untouched second key in the same document shows.
    """
    write_config_file(tmp_path, CONFIG_FILE_NAME, FILE_DOCUMENT)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        build_group(),
        ["config", "show", "--resolved", TIMEOUT_OPTION, str(OPTION_TIMEOUT), "--format", "json"],
    )

    assert result.exit_code == 0, f"the command exited {result.exit_code}: {result.output}"
    assert result.stderr == "", f"the command also wrote to stderr: {result.stderr!r}"
    document = json.loads(result.stdout)
    assert document["resolved"]["http"]["timeout"] == OPTION_TIMEOUT, (
        f"the document resolved http.timeout to {document['resolved']['http']['timeout']!r}"
    )
    assert document["resolved"]["http"]["retries"]["max_attempts"] == FILE_MAX_ATTEMPTS
    assert document["sources"][TIMEOUT_KEY] == {"layer": "cli", "detail": TIMEOUT_OPTION}, (
        f"http.timeout is attributed to {document['sources'][TIMEOUT_KEY]!r}"
    )


def test_config_show_without_resolved_keeps_the_file_value_and_defers_the_override(
    clean_foundry_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ``--resolved`` the resolution stops below the command line and says so separately."""
    config_path = write_config_file(tmp_path, CONFIG_FILE_NAME, FILE_DOCUMENT)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        build_group(),
        ["config", "show", TIMEOUT_OPTION, str(OPTION_TIMEOUT), "--format", "json"],
    )

    assert result.exit_code == 0, f"the command exited {result.exit_code}: {result.output}"
    document = json.loads(result.stdout)
    assert document["resolved"]["http"]["timeout"] == FILE_TIMEOUT, (
        f"the deferred command resolved http.timeout to {document['resolved']['http']['timeout']!r}"
    )
    assert document["sources"][TIMEOUT_KEY] == {"layer": "file", "detail": str(config_path)}
    assert document["not_applied"] == {TIMEOUT_KEY: str(OPTION_TIMEOUT)}, (
        f"the override is reported as {document.get('not_applied')!r}"
    )


def test_config_show_toml_carries_the_same_attribution(
    clean_foundry_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--format toml`` emits the resolved values with the companion ``[sources]`` table."""
    write_config_file(tmp_path, CONFIG_FILE_NAME, FILE_DOCUMENT)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        build_group(),
        ["config", "show", "--resolved", TIMEOUT_OPTION, str(OPTION_TIMEOUT), "--format", "toml"],
    )

    assert result.exit_code == 0, f"the command exited {result.exit_code}: {result.output}"
    document = tomllib.loads(result.stdout)
    assert document["http"]["timeout"] == OPTION_TIMEOUT
    assert document["sources"][TIMEOUT_KEY] == {"layer": "cli", "detail": TIMEOUT_OPTION}, (
        f"http.timeout is attributed to {document['sources'][TIMEOUT_KEY]!r}"
    )
