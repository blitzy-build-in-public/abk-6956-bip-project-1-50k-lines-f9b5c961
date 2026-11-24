"""The sole implementation of node id encoding and path containment.

Two rules decide where every per-test file Foundry writes may live, and both are
implemented here so that a consumer which builds a path itself is a bug rather than an
alternative. The first rule turns a pytest node id into one filesystem-safe component:
percent-encoding with an empty safe set is injective, so two different node ids can never
share an artifact directory, and the encoding contains no path separator. The second
rule resolves a candidate path and rejects it when the resolved location is not the run
directory or one of its descendants, which also catches a symlinked directory that
points outside the run.

The encoding is deterministic across processes: it uses no process-salted builtin and
truncates long names with a stable digest, so the controller and every parallel worker
derive the same directory for the same test. :func:`sanitize_nodeid` is not idempotent,
and every caller applies it exactly once; :func:`artifact_dir_for` here and the cassette
path helper are the two callers that do.

Nothing in this module creates, changes or removes a file or a directory. Resolution is
the only filesystem interaction, it is non-strict, and the target need not exist yet.
Importing the module performs no work of its own beyond defining names: no I/O, no
environment read and no module-level mutable state. Keeping that discipline is what lets
the plugin import it inside the startup budget.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Final
from urllib.parse import quote_from_bytes

from foundry.core.exceptions import ArtifactError, FoundryError

__all__: Final[tuple[str, ...]] = (
    "MAX_NODEID_NAME_LENGTH",
    "artifact_dir_for",
    "relative_artifact_path",
    "resolve_under",
    "sanitize_nodeid",
)

MAX_NODEID_NAME_LENGTH: Final[int] = 180
"""Maximum length of an encoded node id used as a single path component.

A POSIX file name is limited to 255 bytes. Keeping the encoded component at 180 leaves
room for the ``.json`` suffix a cassette adds, for truncation's separator and digest, and
for the small amount of slack a filesystem or archive tool may reserve. The limit counts
the encoded characters, and every character :func:`quote_from_bytes` emits for a
non-unreserved byte is ASCII, so the character count is also the byte count.
"""


def sanitize_nodeid(nodeid: str) -> str:
    """Encode a pytest node id as one deterministic, filesystem-safe name.

    Percent-encoding with an empty safe set escapes every character outside the
    unreserved set ``A-Za-z0-9_.-~``. That makes the mapping injective -- ``/`` becomes
    ``%2F``, ``::`` becomes ``%3A%3A``, ``[`` becomes ``%5B``, ``]`` becomes ``%5D`` and
    a literal ``%`` becomes ``%25`` -- so two distinct node ids cannot collide on one
    artifact directory. The result has no path separator and is a single component, never
    a path.

    An encoding longer than :data:`MAX_NODEID_NAME_LENGTH` keeps its first 180 characters
    and appends a hyphen plus the first eight hexadecimal characters of the SHA-256 digest
    of the original node id. Truncation is therefore deterministic: the same node id
    produces the same name on every worker, host and run, and the digest keeps two long
    node ids with a shared prefix apart.

    Two traps follow from that definition. The function is **not idempotent**: applying
    it to a name it already produced escapes the ``%`` signs a second time, so ``%2F``
    becomes ``%252F``. Every caller applies it exactly once. The returned value is also a
    directory or file name only; it is not the node id stored in ``results.json``, which
    carries the unsanitised id.

    A node id pytest derived from a file name that is not valid UTF-8 carries lone surrogate
    code points, because it decodes the name with ``surrogateescape``. Encoding with
    ``surrogatepass`` gives each of those code points its own three-byte sequence, which no
    valid UTF-8 text can produce, so the mapping stays injective and every valid node id
    encodes to the same bytes as before; it also cannot fail, so the function is total over
    every node id pytest produces instead of raising ``UnicodeEncodeError`` into artifact
    capture.

    Args:
        nodeid: The pytest node id to encode, for example
            ``tests/test_api.py::test_ok``.

    Returns:
        A single path component with no separator, at most
        :data:`MAX_NODEID_NAME_LENGTH` characters plus nine for the truncation suffix.

    Raises:
        FoundryError: If ``nodeid`` is empty or contains only whitespace, because no
            meaningful artifact directory can be derived from it and a silent fallback
            name would let unrelated items share one directory.
    """
    if not nodeid.strip():
        raise FoundryError(
            "cannot derive an artifact name from an empty node id",
            hint="pass the pytest node id, for example tests/test_api.py::test_ok",
        )

    encoded = quote_from_bytes(nodeid.encode("utf-8", "surrogatepass"), safe="")
    if len(encoded) <= MAX_NODEID_NAME_LENGTH:
        return encoded

    digest = hashlib.sha256(nodeid.encode("utf-8", "surrogatepass")).hexdigest()[:8]
    return f"{encoded[:MAX_NODEID_NAME_LENGTH]}-{digest}"


def resolve_under(root: Path, candidate: str | Path) -> Path:
    """Resolve ``candidate`` and require it to stay inside ``root``.

    The root is resolved first. An absolute candidate is resolved on its own and a
    relative one is joined to the resolved root before resolution, so both forms pass the
    same check. Resolution is non-strict: the target need not exist, but symlinks that do
    exist are followed, which is what rejects a symlinked artifact directory pointing
    outside the run. A path equal to ``root`` is accepted.

    This function never creates or modifies anything. It is the single containment check
    for artifact and cassette paths, so a caller that resolves a path by another route
    has left a traversal hole.

    Args:
        root: The directory the path must stay inside, such as the run directory.
        candidate: The absolute or relative path to resolve against ``root``.

    Returns:
        The resolved absolute path, guaranteed to be ``root`` itself or a descendant of
        it.

    Raises:
        ArtifactError: If the resolved path is outside ``root``, including when a
            symlink inside ``root`` points elsewhere. The exception carries the resolved
            candidate as ``attempted_path`` and the resolved root as ``run_dir``.
    """
    resolved_root = Path(root).resolve()
    candidate_path = Path(candidate)
    joined = candidate_path if candidate_path.is_absolute() else resolved_root / candidate_path
    resolved = joined.resolve()

    if not _is_within(resolved, resolved_root):
        raise ArtifactError(
            f"path escapes the run directory: {resolved} is not under {resolved_root}",
            attempted_path=resolved,
            run_dir=resolved_root,
            hint="artifact and cassette paths are derived from the run directory only",
        )
    return resolved


def artifact_dir_for(run_dir: Path, nodeid: str) -> Path:
    """Return the per-test artifact directory for ``nodeid``.

    This implements the ``artifacts/<run_id>/<sanitized-nodeid>/`` layout: one directory
    per collected item, so parallel workers never contend for the same file. The
    directory is only named here; creating it belongs to the artifact writer, which also
    decides what happens when the run directory is not writable.

    Args:
        run_dir: The run directory, normally ``artifacts/<run_id>``.
        nodeid: The pytest node id of the item whose directory is requested.

    Returns:
        The resolved absolute directory path, inside ``run_dir`` and named by
        :func:`sanitize_nodeid`.

    Raises:
        FoundryError: If ``nodeid`` is empty or whitespace-only.
        ArtifactError: If the derived path escapes ``run_dir``, which can only happen
            through a symlink inside it.
    """
    return resolve_under(run_dir, sanitize_nodeid(nodeid))


def relative_artifact_path(path: Path, run_dir: Path) -> str:
    """Return ``path`` relative to ``run_dir`` using forward slashes.

    This produces the ``path`` field of the artifact entries in ``results.json`` and the
    ``file`` values in a manifest, so it is always relative to the run directory and
    never carries a leading separator. Forward slashes are used on every platform, which
    keeps a result document comparable between macOS and Linux.

    A path equal to the run directory yields ``"."``. The final segment check cannot fail
    after the containment check, and it exists to state the rule that a stored artifact
    path never contains ``..``; if it ever fires, a containment assumption has broken and
    the caller must see that rather than receive a path outside the run.

    Args:
        path: The artifact path to express relative to ``run_dir``. It may be absolute or
            relative to ``run_dir``.
        run_dir: The run directory that ``path`` must stay inside.

    Returns:
        The resolved path relative to ``run_dir``, with ``/`` separators and no leading
        separator, or ``"."`` when the two resolve to the same directory.

    Raises:
        ArtifactError: If ``path`` escapes ``run_dir``, or if the computed relative path
            unexpectedly contains a ``..`` segment.
    """
    resolved_root = Path(run_dir).resolve()
    resolved = resolve_under(resolved_root, path)
    if resolved == resolved_root:
        return "."

    relative = resolved.relative_to(resolved_root).as_posix()
    if ".." in relative.split("/"):
        raise ArtifactError(
            f"relative artifact path must not contain '..': {relative}",
            attempted_path=resolved,
            run_dir=resolved_root,
            hint="artifact paths are recorded relative to the run directory only",
        )
    return relative


def _is_within(path: Path, root: Path) -> bool:
    """Return whether ``path`` is ``root`` or lies beneath it.

    Both arguments are expected to be resolved already. Path equality covers the root
    itself, and :meth:`pathlib.Path.is_relative_to` performs the component-wise
    descendant check without the string-prefix mistake that would accept
    ``/tmp/run-other`` for ``/tmp/run``.

    Args:
        path: The resolved path to test.
        root: The resolved directory to test against.

    Returns:
        ``True`` when ``path`` equals ``root`` or is a descendant of it, otherwise
        ``False``.
    """
    return path == root or path.is_relative_to(root)
