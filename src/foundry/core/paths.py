"""The sole implementation of node id encoding and path containment (AAP 0.3.2, AAP 0.14.3).

Percent-encoding with an empty safe set is injective, so two node ids never share an artifact
directory, and every candidate path is resolved and rejected when it is not the run directory or a
descendant of it. The encoding is deterministic across processes, so the controller and every worker
derive one directory for one test, and nothing here creates, changes or removes a file.
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
truncation's separator and digest, and filesystem slack. One encoded character is one ASCII byte.
"""


def sanitize_nodeid(nodeid: str) -> str:
    """Encode a pytest node id as one deterministic, filesystem-safe path component.

    Percent-encoding with an empty safe set makes the mapping injective; an encoding longer than
    :data:`MAX_NODEID_NAME_LENGTH` keeps its first 180 characters and appends a hyphen plus eight
    hex digits of the node id's SHA-256. The function is **not idempotent** -- a second call escapes
    the ``%`` signs again -- so every caller applies it once, and the name is not the node id stored
    in ``results.json``. ``surrogatepass`` keeps a lone surrogate encodable.

    Raises:
        FoundryError: ``nodeid`` is empty or contains only whitespace.
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

    An absolute candidate is resolved on its own and a relative one joined to the resolved root.
    Resolution is non-strict, so a symlink is followed, which is what rejects an artifact directory
    pointing outside the run; a path equal to ``root`` is accepted.

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
    """Return the per-test artifact directory for ``nodeid``: ``artifacts/<run_id>/<name>/``.

    One directory per collected item, so parallel workers never contend for the same file; creating
    it belongs to the artifact writer. A node id is percent-encoded here and never redacted, so a
    caller rebuilding a live test's directory passes the node id pytest reported.

    Raises:
        FoundryError: ``nodeid`` is empty or whitespace-only.
        ArtifactError: The derived path escapes ``run_dir`` through a symlink inside it.
    """
    return resolve_under(run_dir, sanitize_nodeid(nodeid))


def relative_artifact_path(path: Path, run_dir: Path) -> str:
    """Return ``path`` relative to ``run_dir`` with forward slashes, for records and manifests.

    A path equal to the run directory yields ``"."``, and the ``..`` check fires only if a
    containment assumption has broken.

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
    """Return whether resolved ``path`` is ``root`` or lies beneath it.

    ``is_relative_to`` avoids the string-prefix mistake that would accept ``/tmp/run-other``.
    """
    return path == root or path.is_relative_to(root)
