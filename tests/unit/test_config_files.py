"""The file layer of ``src/foundry/config/files.py``: a candidate that does not parse is a fault and
never a silent return to the defaults, a ``pyproject.toml`` without ``[tool.foundry]`` is no
configuration file, and a section header is checked as a key of its own (AAP 0.4.5, AAP 0.4.7)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Final

import pytest

from foundry.config import files, merge
from foundry.config.models import FoundryConfig
from foundry.core.exceptions import ConfigError

_FOUNDRY_TOML: Final[str] = files.FOUNDRY_TOML
_PYPROJECT_TOML: Final[str] = files.PYPROJECT_TOML
_KEYLESS_TABLES: Final[tuple[tuple[str, str | None, str | None], ...]] = (
    ("[data.bogus]\n", "data.bogus", "file"),
    ("[data.bogus.deep]\n", "data.bogus.deep", "file"),
    ("[http.retries.bogus]\n", "http.retries.bogus", "file"),
    ("[artefacts]\n", "artefacts", "file"),
    ("[profiles.ci.data.bogus]\n", "data.bogus", "profile:ci"),
    ("[data.bogus]\nk = 1\n", "data.bogus.k", "file"),
    ("[data]\n[http]\n[http.auth]\n[http.retries]\n[http.headers]\n[profiles.ci]\n", None, None),
)


def _resolve(directory: Path) -> FoundryConfig:
    return merge.resolve(cli_overrides=None, environ={}, cwd=directory)


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
    assert error.layer == layer
    assert error.hint is not None and str(path.resolve()) in error.hint
