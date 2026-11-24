"""Where a cassette document lives: the single mapping from node id to file path.

``foundry.cassette`` owns record/replay (AAP 0.3.2), and this is the module that answers one
question for the whole package: which file holds a given test's cassette. AAP 0.4.6 fixes the
location as ``cassettes/<sanitized-nodeid>.json`` -- one JSON document per test, named after the
test's node id, so parallel workers never contend for one file and a reviewer can find a named
test's recording by reading the tree. :func:`cassette_path` is the only function that answers it,
so the store that loads and saves, the replay transport's miss error and every diagnostic message
name the same file.

Both halves of the mapping are delegated, never re-implemented
    :func:`foundry.core.paths.sanitize_nodeid` turns the node id into one injective,
    separator-free path component, and :func:`foundry.core.paths.resolve_under` is the tree's
    single containment check. A hostile node id -- ``../../etc/passwd``, an absolute path, a
    name whose encoding cannot stay inside the directory -- therefore cannot name a file outside
    the cassette directory: a resolved path that escapes it raises
    :class:`foundry.core.exceptions.ArtifactError`, carrying the rejected path as
    ``attempted_path`` and the cassette directory as ``run_dir``. Composing the path by string
    concatenation here would be a second path-safety rule that ``tests/unit/test_layering.py``
    and the containment tests cannot see, so it is not done.

Pure path arithmetic, and no configuration
    Nothing here creates, reads, changes or removes a file or a directory: the cassette directory
    is created by the writer that first saves a cassette, and replay runs against a directory that
    already exists. Importing the module performs no I/O, no environment read and no logger
    configuration, and no function keeps module-level state, so two callers holding the same
    arguments receive the same path.

    The resolved ``cassette.dir`` arrives as an ordinary argument rather than being read here.
    AAP 0.4.8 lets ``foundry.cassette`` import the foundation rung only, so this package must
    never import :mod:`foundry.config`; the store and the replay transport hold the resolved
    configuration and pass the directory down, exactly as
    :class:`foundry.obs.redaction.Redactor` receives ``secrets.redact_keys`` as a plain iterable
    of names.

Cassettes are JSON, never YAML
    AAP 0.5.1 excludes the YAML cassette format the surrounding ecosystem defaults to, because
    the document is written and read by Foundry alone and JSON parses without a loader choice to
    get wrong. :data:`CASSETTE_SUFFIX` states the extension once, so a reader of the tree and a
    caller building a message cannot disagree about it.
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
"""The ``cassette.dir`` default, used when a caller passes no directory.

Defined once, in :data:`foundry.core.limits.CASSETTE_DIR_DEFAULT`: the value has to exist before
any configuration layer is resolved, and this package may not import :mod:`foundry.config`
(AAP 0.4.8), so the foundation rung is where it lives. Naming it here means a caller that supplies
nothing builds the same path the configuration would. A relative value, like every path in the
configuration, is resolved against the invocation directory by :func:`cassette_path`.
"""

CASSETTE_SUFFIX: Final[str] = ".json"
"""The extension of a cassette document: one JSON object per test.

The suffix is added by :func:`cassette_filename` rather than by a caller, so the store's save and
load paths cannot drift apart on the spelling of the extension.
"""


def cassette_filename(nodeid: str) -> str:
    """Return the file name of the cassette document that holds ``nodeid``'s recording.

    The name is :func:`~foundry.core.paths.sanitize_nodeid` applied to the node id and then
    :data:`CASSETTE_SUFFIX`. Sanitisation percent-encodes with an empty safe set, which makes the
    mapping injective: ``/`` becomes ``%2F``, ``::`` becomes ``%3A%3A`` and ``[`` becomes ``%5B``,
    so two distinct node ids can never share one cassette, and the result is a single path
    component with no separator in it. Because the encoding is injective, a cassette directory
    restored from a backup or written by another worker holds one file per test and no collisions.

    The function is pure: it reads no file, creates no directory and keeps no state.

    Args:
        nodeid: The pytest node id of the item whose cassette file is named, for example
            ``tests/test_api.py::test_checkout``.

    Returns:
        One file name, at most :data:`foundry.core.paths.MAX_NODEID_NAME_LENGTH` characters of
        encoded node id plus the truncation suffix and :data:`CASSETTE_SUFFIX`.

    Raises:
        FoundryError: If ``nodeid`` is empty or whitespace-only, because no meaningful cassette
            name can be derived from it and a silent fallback name would let unrelated items share
            one document. Raised by :func:`~foundry.core.paths.sanitize_nodeid`.
    """
    return f"{sanitize_nodeid(nodeid)}{CASSETTE_SUFFIX}"


def cassette_path(nodeid: str, *, cassette_dir: str | Path = DEFAULT_CASSETTE_DIR) -> Path:
    """Return the resolved path of the cassette document that holds ``nodeid``'s recording.

    ``cassette_dir`` may be absolute or relative; a relative one is resolved against the current
    working directory, which for a pytest run is the invocation directory. The document is the
    directory joined with :func:`cassette_filename`, and the result passes through
    :func:`~foundry.core.paths.resolve_under`, so the returned path is guaranteed to be inside the
    resolved cassette directory. Resolution is non-strict: the document and the directory need not
    exist yet, which is what lets a ``record`` run name a file it is about to create.

    The keyword-only directory is the seam that keeps this package free of the configuration
    layer: the caller holds the resolved ``cassette.dir`` and passes it in, and the default covers
    the callers that have nothing to pass.

    The function is pure: it creates no directory, opens no file and keeps no state.

    Args:
        nodeid: The pytest node id of the item whose cassette path is requested.
        cassette_dir: The cassette directory to resolve the document against, normally the
            resolved ``cassette.dir``. Defaults to :data:`DEFAULT_CASSETTE_DIR`.

    Returns:
        The resolved absolute path of the document, equal to the resolved cassette directory or a
        descendant of it.

    Raises:
        FoundryError: If ``nodeid`` is empty or whitespace-only.
        ArtifactError: If the resolved path is outside the cassette directory, which a node id
            alone cannot cause but a symlinked directory can. The error carries the rejected path
            as ``attempted_path`` and the resolved cassette directory as ``run_dir``.
    """
    return resolve_under(Path(cassette_dir), cassette_filename(nodeid))


def cassette_relpath(nodeid: str) -> str:
    """Return the cassette's location relative to the invocation directory, for messages.

    A message that names the recording a test replays is read in a terminal, a result document and
    a report, so it must not carry the absolute path of the machine that produced it: an absolute
    path makes two runs of the same suite differ and leaks the layout of the host into a shared
    log. The returned form is therefore always ``cassettes/<name>.json`` with a forward slash,
    independent of where ``cassette.dir`` actually points.

    This is the message form only. A caller that needs to open the document uses
    :func:`cassette_path` and its resolved path, so it stays bound to the configured directory.
    The function is pure: it reads no configuration, touches no file and keeps no state.

    Args:
        nodeid: The pytest node id of the item whose cassette location is described.

    Returns:
        ``f"{DEFAULT_CASSETTE_DIR}/{cassette_filename(nodeid)}"`` -- a relative, POSIX-style path
        that never starts with a separator.

    Raises:
        FoundryError: If ``nodeid`` is empty or whitespace-only.
    """
    return f"{DEFAULT_CASSETTE_DIR}/{cassette_filename(nodeid)}"
