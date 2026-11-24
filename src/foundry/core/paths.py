"""The sole implementation of node id encoding and path containment.

Two rules live here so that a consumer which builds a path itself is a bug: percent-encoding
with an empty safe set is injective, so two node ids never share an artifact directory, and
every candidate path is resolved and rejected when the resolved location is not the run
directory or a descendant of it (AAP 0.3.2). The encoding is deterministic across processes,
so the controller and every parallel worker derive the same directory for the same test, and
nothing here creates, changes or removes a file.
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

A POSIX name is limited to 255 bytes; 180 leaves room for the ``.json`` suffix a cassette adds,
truncation's separator and digest, and the slack a filesystem or archive tool may reserve. The limit
counts encoded characters, and :func:`quote_from_bytes` emits one ASCII character per non-unreserved
byte, so the character count is also the byte count.
"""


def sanitize_nodeid(nodeid: str) -> str:
    """Encode a pytest node id as one deterministic, filesystem-safe name.

    Percent-encoding with an empty safe set escapes every character outside ``A-Za-z0-9_.-~``, so
    the mapping is injective and the result is a single component with no path separator. An
    encoding longer than :data:`MAX_NODEID_NAME_LENGTH` keeps its first 180 characters and appends
    a hyphen plus eight hex digits of the node id's SHA-256, which keeps truncation deterministic
    and keeps two long ids with a shared prefix apart.

    The function is **not idempotent** -- a second call escapes the ``%`` signs again, so ``%2F``
    becomes ``%252F`` -- so every caller applies it exactly once. The value is a file name, not the
    node id stored in ``results.json``, which carries the unsanitised id.

    A node id derived from a file name that is not valid UTF-8 carries lone surrogates, which
    ``surrogatepass`` encodes so the mapping stays injective and the function is total instead of
    raising ``UnicodeEncodeError`` into artifact capture.

    Returns:
        One path component, at most :data:`MAX_NODEID_NAME_LENGTH` characters plus nine.

    Raises:
        FoundryError: ``nodeid`` is empty or contains only whitespace, because no meaningful
            artifact directory can be derived from it.
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

    An absolute candidate is resolved on its own and a relative one is joined to the resolved root,
    so both forms pass the same check. Resolution is non-strict, so a symlink that does exist is
    followed, which is what rejects an artifact directory pointing outside the run; a path equal to
    ``root`` is accepted. This is the single containment check for artifact and cassette paths.

    Returns:
        The resolved absolute path, ``root`` itself or a descendant of it.

    Raises:
        ArtifactError: The resolved path is outside ``root``; it carries ``attempted_path`` and
            ``run_dir``.
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

    Implements the ``artifacts/<run_id>/<sanitized-nodeid>/`` layout: one directory per collected
    item, so parallel workers never contend for the same file. Creating it belongs to the artifact
    writer.

    The artifact writer renders a node id through the redaction engine first, so a parametrised id
    carrying a credential cannot name a directory and a caller rebuilding a live test's directory
    must pass that same scrubbed node id; the final component is :func:`sanitize_nodeid` of
    ``nodeid``, applied exactly once.

    Returns:
        The resolved absolute directory inside ``run_dir``, named by :func:`sanitize_nodeid`.

    Raises:
        FoundryError: ``nodeid`` is empty or whitespace-only.
        ArtifactError: The derived path escapes ``run_dir`` through a symlink inside it.
    """
    return resolve_under(run_dir, sanitize_nodeid(nodeid))


def relative_artifact_path(path: Path, run_dir: Path) -> str:
    """Return ``path`` relative to ``run_dir`` using forward slashes.

    This produces the ``path`` field of an artifact entry in ``results.json`` and the ``file`` value
    in a manifest, so it is comparable between macOS and Linux. A path equal to the run directory
    yields ``"."``; the ``..`` check states the rule that a stored artifact path never contains one,
    and it fires only if a containment assumption has broken.

    Returns:
        The resolved path relative to ``run_dir``, with ``/`` separators and no leading one.

    Raises:
        ArtifactError: ``path`` escapes ``run_dir``, or the relative path contains ``..``.
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

    Both arguments are already resolved; equality covers the root and
    :meth:`pathlib.Path.is_relative_to` avoids the string-prefix mistake that would accept
    ``/tmp/run-other`` for ``/tmp/run``.

    Returns:
        ``True`` when ``path`` equals ``root`` or is a descendant of it.
    """
    return path == root or path.is_relative_to(root)
