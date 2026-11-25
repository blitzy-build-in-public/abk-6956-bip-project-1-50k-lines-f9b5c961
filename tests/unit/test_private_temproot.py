"""The private temporary root that keeps a session off pytest's predictable base directory.

pytest at the release AAP 0.5.1 fixes builds the predictable ``<tempdir>/pytest-of-<user>`` that
GHSA-6w46-j5rx-g56g concerns, so ``tests/conftest.py`` pins an unpredictable root instead and gives
the session a base directory inside it. These tests assert the pin through the session running them,
which is the observation AAP 0.14.5 accepts. A caller that names ``PYTEST_DEBUG_TEMPROOT`` or passes
``--basetemp`` keeps its own base directory, so only the properties that still hold are asserted.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def _pinned_root() -> Path:
    """The private root this process runs under, as ``tests/conftest.py`` published it."""
    return Path(os.environ["PYTEST_DEBUG_TEMPROOT"]).resolve()


def _suite_owns_the_root() -> bool:
    """Whether this suite created the root, as opposed to a caller naming one of its own."""
    return Path(os.environ["PYTEST_DEBUG_TEMPROOT"]).name.startswith("foundry-pytest-")


def test_the_pinned_root_is_private() -> None:
    """The root ``PYTEST_DEBUG_TEMPROOT`` names exists, belongs to this user, and is private."""
    root = _pinned_root()
    assert root.is_dir(), f"PYTEST_DEBUG_TEMPROOT is {str(root)!r}, not a directory"
    info = root.stat()
    assert info.st_uid == os.getuid(), f"{root} is owned by uid {info.st_uid}"
    assert info.st_mode & 0o077 == 0, f"{root} carries mode {oct(info.st_mode & 0o777)}"


def test_tmp_path_stays_inside_the_session_basetemp(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    """A given base directory is used directly, so the session never enters the fallback branch."""
    resolved = tmp_path.resolve()
    given = request.config.option.basetemp
    if given is None:
        # A caller named PYTEST_DEBUG_TEMPROOT, so pytest numbers its tree inside that private root.
        assert resolved.is_relative_to(_pinned_root()), f"{resolved} is outside {_pinned_root()}"
        return
    basetemp = Path(str(given)).resolve()
    assert resolved.is_relative_to(basetemp), f"{resolved} is outside {basetemp}"


def test_the_suite_gives_its_session_a_basetemp(request: pytest.FixtureRequest) -> None:
    """The hook supplies a base directory inside the private root, so the fallback is skipped."""
    if not _suite_owns_the_root():
        pytest.skip("a caller pinned PYTEST_DEBUG_TEMPROOT, so it owns the base directory")
    given = request.config.option.basetemp
    if given is not None and not Path(str(given)).resolve().is_relative_to(_pinned_root()):
        pytest.skip("a caller supplied the session's base directory")
    assert given is not None, "no basetemp was supplied, so pytest's predictable fallback ran"
    assert Path(str(given)).resolve().is_relative_to(_pinned_root()), f"{given} is outside the pin"
