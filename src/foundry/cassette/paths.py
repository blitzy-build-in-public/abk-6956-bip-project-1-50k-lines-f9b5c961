"""Where a cassette document lives: the single mapping from node id to file path.

AAP 0.4.6 fixes ``cassettes/<sanitized-nodeid>.json``: one document per test, one path function.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from foundry.core.limits import CASSETTE_DIR_DEFAULT
from foundry.core.paths import resolve_under, sanitize_nodeid

__all__: Final[tuple[str, ...]] = (
    "CASSETTE_SUFFIX",
    "DEFAULT_CASSETTE_DIR",
    "cassette_filename",
    "cassette_path",
    "cassette_relpath",
)

DEFAULT_CASSETTE_DIR: Final[str] = CASSETTE_DIR_DEFAULT
"""The ``cassette.dir`` default, defined once in :data:`foundry.core.limits.CASSETTE_DIR_DEFAULT`.

This package may not import :mod:`foundry.config` (AAP 0.4.8), so the value lives on the foundation
rung; a relative value is resolved against the invocation directory by :func:`cassette_path`.
"""

CASSETTE_SUFFIX: Final[str] = ".json"
"""The extension of a cassette document, added by :func:`cassette_filename` so both ends agree."""


def cassette_filename(nodeid: str) -> str:
    """Return the file name of the cassette document that holds ``nodeid``'s recording.

    The name is :func:`~foundry.core.paths.sanitize_nodeid` applied to the node id plus
    :data:`CASSETTE_SUFFIX`. Sanitisation percent-encodes with an empty safe set, so the mapping is
    injective: two distinct node ids never share one cassette, and the result is one path component.
    The function is pure: it reads no file, creates no directory and keeps no state.

    Args:
        nodeid: The pytest node id of the item whose cassette file is named.

    Returns:
        One file name, at most :data:`foundry.core.paths.MAX_NODEID_NAME_LENGTH` characters of
        encoded node id plus the truncation suffix and :data:`CASSETTE_SUFFIX`.

    Raises:
        FoundryError: If ``nodeid`` is empty or whitespace-only.
    """
    return f"{sanitize_nodeid(nodeid)}{CASSETTE_SUFFIX}"


def cassette_path(nodeid: str, *, cassette_dir: str | Path = DEFAULT_CASSETTE_DIR) -> Path:
    """Return the resolved path of the cassette document that holds ``nodeid``'s recording.

    A relative ``cassette_dir`` is resolved against the current working directory, and the result
    passes through :func:`~foundry.core.paths.resolve_under`, so it stays inside the resolved
    cassette directory; resolution is non-strict, so a ``record`` run can name a file it is about to
    create.
    """
    return resolve_under(Path(cassette_dir), cassette_filename(nodeid))


def cassette_relpath(nodeid: str) -> str:
    """Return the cassette's location relative to the invocation directory, for messages.

    A message that names the recording must not carry the absolute path of the machine that produced
    it, so the form is always ``cassettes/<name>.json`` with a forward slash.
    """
    return f"{DEFAULT_CASSETTE_DIR}/{cassette_filename(nodeid)}"
