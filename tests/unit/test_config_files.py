"""The file layer of ``src/foundry/config/files.py``: discovery's faults and its table walk.

AAP 0.4.5 fixes the five-step discovery order and makes an unknown key in any layer exit 2; AAP
0.4.7 fixes what a run reports when no step matches. This module is the contract of the faults that
arise while a candidate is discovered and read: a ``pyproject.toml`` that does not parse faults
exactly as a malformed ``foundry.toml`` does instead of resolving the defaults in silence, a
well-formed ``pyproject.toml`` carrying no ``[tool.foundry]`` still falls through to the defaults
under the debug line ``foundry: no config file found``, and a section header that holds nothing is
refused at its dotted path unless it names a section the registry declares -- because a table name
is a key in TOML terms, so an unknown one is an unknown key in every layer.

Its siblings carry the rest: ``tests/unit/test_config_constants.py`` proves the registry and the
single source of the published numbers agree, ``tests/unit/test_config_model.py`` pins the 28 keys
with their defaults and both ends of every range, and ``tests/unit/test_config.py`` holds the engine
around this layer -- the environment encoding, the four-layer ladder, the profile merge and the two
``config show`` documents. Everything here is driven through the
package's own calls, :func:`foundry.config.files.discover_file` where a fault belongs to discovery
and :func:`foundry.config.merge.resolve` over a directory under ``tmp_path``, so no case reads the
ambient environment, opens a socket or depends on another case's files (AAP 0.14.3).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Final

import pytest

from foundry.config import files, merge
from foundry.config.models import FoundryConfig
from foundry.core.exceptions import ConfigError

# The two file names the third and fourth discovery steps look for (AAP 0.4.5), read from the
# module under test so a rename cannot leave a case writing a file nothing looks for.
_FOUNDRY_TOML: Final[str] = files.FOUNDRY_TOML
_PYPROJECT_TOML: Final[str] = files.PYPROJECT_TOML

_KEYLESS_TABLE_CASES: Final[tuple[tuple[str, str | None, str | None], ...]] = (
    ("[data.bogus]\n", "data.bogus", "file"),
    ("[data.bogus.deep]\n", "data.bogus.deep", "file"),
    ("[http.retries.bogus]\n", "http.retries.bogus", "file"),
    ("[artefacts]\n", "artefacts", "file"),
    ("[profiles.ci.data.bogus]\n", "data.bogus", "profile:ci"),
    ("[data.bogus]\nk = 1\n", "data.bogus.k", "file"),
    ("[data]\n", None, None),
    ("[http]\n", None, None),
    ("[http.auth]\n", None, None),
    ("[http.retries]\n", None, None),
    ("[http.headers]\n", None, None),
    ("[profiles.ci]\n", None, None),
)
"""Section headers that hold nothing, beside the empty headers the registry does declare.

The cases with a key are refused at the dotted path the message names, under the layer that
supplied them, because a table name is a key in TOML terms (AAP 0.4.5); ``[data.bogus]`` with a
key under it is the control, whose leaf rather than its table is what the flat check names. The
cases without one are the empty spelling of a real section -- ``[http.headers]`` is a table-valued
key -- and must keep resolving from the defaults.
"""


def _resolve(directory: Path) -> FoundryConfig:
    """Resolve one configuration over ``directory``, reading no environment and no override.

    With no command line and an empty environment mapping, the file layer is the only input beside
    the field defaults, so what the directory holds is what the resolution reports.
    """
    return merge.resolve(cli_overrides=None, environ={}, cwd=directory)


def test_an_unparseable_pyproject_faults_like_a_malformed_foundry_toml(
    tmp_path: Path, clean_foundry_env: None, write_project: Callable[..., Path]
) -> None:
    """A ``pyproject.toml`` that does not parse is a fault, never a silent return to defaults.

    Discovery step 4 reads the candidate to decide whether it carries ``[tool.foundry]``, so a
    syntax fault there is reported exactly as the same fault in ``foundry.toml`` is: AAP 0.4.5
    makes that file a configuration file on this run, and losing the operator's whole table
    without a diagnostic is what the strict reading of it rules out.
    """
    write_project(tmp_path, files={_PYPROJECT_TOML: "[tool.foundry\n"})
    path = tmp_path / _PYPROJECT_TOML

    with pytest.raises(ConfigError) as from_discovery:
        files.discover_file(environ={}, cwd=tmp_path)

    assert from_discovery.value.message.startswith("invalid TOML in ")
    assert str(path.resolve()) in from_discovery.value.message
    assert from_discovery.value.layer == "file"

    with pytest.raises(ConfigError) as caught:
        _resolve(tmp_path)

    error = caught.value
    assert error.message.startswith("invalid TOML in ")
    assert str(path.resolve()) in error.message
    assert error.path == path.resolve()
    assert error.layer == "file"


def test_a_pyproject_without_the_foundry_table_resolves_the_defaults(
    tmp_path: Path,
    clean_foundry_env: None,
    write_project: Callable[..., Path],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A well-formed ``pyproject.toml`` with no ``[tool.foundry]`` is not a configuration file.

    The fourth discovery step matches only a file that configures Foundry, so a project using that
    name for its own packaging resolves from the field defaults and logs the AAP 0.4.7 DEBUG line
    ``foundry: no config file found`` rather than faulting.
    """
    write_project(tmp_path, files={_PYPROJECT_TOML: '[project]\nname = "something-else"\n'})
    caplog.set_level(logging.DEBUG, logger="foundry")

    assert files.discover_file(environ={}, cwd=tmp_path) is None
    assert _resolve(tmp_path).http.timeout == 10.0
    assert "foundry: no config file found" in [record.getMessage() for record in caplog.records]


@pytest.mark.parametrize(("toml_text", "key", "layer"), _KEYLESS_TABLE_CASES)
def test_a_section_header_is_checked_as_a_key_of_its_own(
    toml_text: str,
    key: str | None,
    layer: str | None,
    tmp_path: Path,
    clean_foundry_env: None,
    write_project: Callable[..., Path],
) -> None:
    """A section header that holds nothing is refused at its dotted path (AAP 0.4.5).

    Flattening keeps one entry per leaf, so ``[data.bogus]`` contributes no key at all and would
    pass the flat unknown-key check as though the header were absent; the pre-flatten walk refuses
    it instead, naming the closest registered spelling and the file to edit. A header the registry
    does declare is the empty spelling of a real section and still resolves from the defaults.
    """
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
    assert error.key == key
    assert error.message == f"unknown configuration key: {key}"
    assert error.layer == layer
    assert error.hint is not None
    assert str(path.resolve()) in error.hint
