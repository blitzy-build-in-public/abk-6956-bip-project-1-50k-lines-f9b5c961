"""Per-test ``manifest.json``: the field order, the hashing and the one serialisation.

``manifest.json`` is the seventh file of the artifact layout (AAP 0.4.6): every artifact Foundry
wrote for one test with its size and digest, and whether redaction changed the bytes or capture was
disabled (AAP 0.4.7). Nothing here opens or removes a file, because AAP 0.4.8 permits writes only
in the artifact, cassette and report stores. Entries are sorted before the document is built, and
key order is the field order of AAP 0.4.6 rather than ``sort_keys``; the models are frozen
dataclasses rather than pydantic ones, so two runs render identical text (AAP 0.14.3).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Final

from foundry.core.exceptions import ArtifactError
from foundry.core.ids import to_iso8601, utc_now
from foundry.core.types import ArtifactKind

__all__: Final[tuple[str, ...]] = (
    "MANIFEST_FILENAME",
    "MANIFEST_SCHEMA_VERSION",
    "ManifestDocument",
    "ManifestEntry",
    "build_document",
    "entry_for",
    "render",
    "sha256_hex",
    "timestamp",
)

MANIFEST_SCHEMA_VERSION: Final[int] = 1
"""The ``schema_version`` every manifest this release writes carries; a change arrives as a new
number in a release allowed to break the promise (AAP 0.4.6, 0.4.9)."""

MANIFEST_FILENAME: Final[str] = "manifest.json"
"""The manifest's name inside a test's artifact directory, fixed rather than derived (AAP 0.4.6)."""

_RELATIVE_ANCHOR: Final[Path] = Path()
"""The ``run_dir`` reported when a manifest path fails containment: this module knows no run dir."""


def _require_relative(file: str) -> None:
    """Reject a manifest path that could name a location outside the run directory.

    Raises:
        ArtifactError: If ``file`` is blank, names the run directory, is absolute or has ``..``; a
            published manifest (AAP 0.4.6) must not name a path a reviewer would take as sanctioned.
    """
    relative = PurePosixPath(file)
    escaped = (
        not file.strip() or str(relative) == "." or relative.is_absolute() or ".." in relative.parts
    )
    if escaped:
        raise ArtifactError(
            f"manifest path is not a file inside the run directory: {file!r}",
            attempted_path=Path(file),
            run_dir=_RELATIVE_ANCHOR,
            hint="manifest paths are relative to the run directory, with no '..' segment",
        )


def _ordered_entries(artifacts: Iterable[ManifestEntry]) -> tuple[ManifestEntry, ...]:
    """Validate the entries a document carries and return them sorted by ``(kind, path)``.

    Raises:
        ArtifactError: If any entry's ``file`` fails :func:`_require_relative`.
    """
    entries = tuple(artifacts)
    for entry in entries:
        _require_relative(entry.file)
    return tuple(sorted(entries, key=lambda entry: (entry.kind, entry.file)))


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """One artifact file recorded in a manifest (AAP 0.4.6).

    The five fields and their order are the published shape of an ``artifacts`` element, so a later
    release may add a key but never rename one (AAP 0.4.6; AAP 0.4.9).
    """

    kind: ArtifactKind
    file: str
    bytes: int
    sha256: str
    redacted: bool

    def as_dict(self) -> dict[str, object]:
        """Return the entry as a JSON-serialisable mapping.

        Returns:
            One mapping per element of a manifest document's ``artifacts`` list.
        """
        return {
            "kind": self.kind,
            "file": self.file,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "redacted": self.redacted,
        }


@dataclass(frozen=True, slots=True)
class ManifestDocument:
    """The whole ``manifest.json`` document for one test (AAP 0.4.6).

    One document describes one collected item: the run it names (AAP 0.4.3), the node id, the
    profile, its creation time, whether capture was disabled (AAP 0.4.7) and the entries written;
    it is frozen because ``results.json``, the JUnit writer and the HTML renderer share it (AAP
    0.4.8).
    """

    schema_version: int
    run_id: str
    nodeid: str
    profile: str
    created_at: str
    artifacts_disabled: bool
    artifacts: tuple[ManifestEntry, ...]

    def as_dict(self) -> dict[str, object]:
        """Return the document as a JSON-serialisable mapping.

        Returns:
            The document in the field order of AAP 0.4.6: scalars plus a list of entry mappings.
        """
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "nodeid": self.nodeid,
            "profile": self.profile,
            "created_at": self.created_at,
            "artifacts_disabled": self.artifacts_disabled,
            "artifacts": [entry.as_dict() for entry in self.artifacts],
        }

    def with_artifacts(
        self,
        artifacts: Iterable[ManifestEntry],
        *,
        artifacts_disabled: bool | None = None,
    ) -> ManifestDocument:
        """Return a copy of this document carrying a different artifact set.

        Returns:
            A new document whose ``schema_version`` is :data:`MANIFEST_SCHEMA_VERSION`.

        Raises:
            ArtifactError: If an entry's ``file`` fails :func:`_require_relative`.
        """
        disabled = self.artifacts_disabled if artifacts_disabled is None else artifacts_disabled
        return build_document(
            run_id=self.run_id,
            nodeid=self.nodeid,
            profile=self.profile,
            created_at=self.created_at,
            artifacts_disabled=disabled,
            artifacts=artifacts,
        )


def sha256_hex(data: bytes) -> str:
    """Return the lowercase hexadecimal SHA-256 digest of the bytes written after redaction.

    Returns:
        Sixty-four lowercase characters; a stripped value never reaches the digest (AAP 0.3.2).
    """
    return hashlib.sha256(data).hexdigest()


def entry_for(kind: ArtifactKind, file: str, data: bytes, *, redacted: bool) -> ManifestEntry:
    """Describe one artifact file as a manifest entry.

    Returns:
        The entry carrying the length of ``data`` and its digest; ``file`` is checked by
        :func:`build_document`.
    """
    return ManifestEntry(
        kind=kind,
        file=file,
        bytes=len(data),
        sha256=sha256_hex(data),
        redacted=redacted,
    )


def timestamp(now: datetime | None = None) -> str:
    """Return the ``created_at`` value a manifest document records.

    Returns:
        ISO 8601 text with millisecond precision and a ``+00:00`` offset, the one clock read the
        tree permits (AAP 0.14.3).
    """
    return to_iso8601(utc_now() if now is None else now)


def build_document(
    *,
    run_id: str,
    nodeid: str,
    profile: str,
    created_at: str,
    artifacts_disabled: bool,
    artifacts: Iterable[ManifestEntry],
) -> ManifestDocument:
    """Assemble a manifest document from a run's identifiers and its artifact entries.

    It holds the two invariants of AAP 0.4.6: every ``file`` is inside the run directory, and
    ``artifacts`` is sorted by ``(kind, file)``.

    Returns:
        The document, with ``schema_version`` set to :data:`MANIFEST_SCHEMA_VERSION`.

    Raises:
        ArtifactError: If an entry's ``file`` is blank, names the run directory itself, is
            absolute or contains a ``..`` segment.
    """
    return ManifestDocument(
        schema_version=MANIFEST_SCHEMA_VERSION,
        run_id=run_id,
        nodeid=nodeid,
        profile=profile,
        created_at=created_at,
        artifacts_disabled=artifacts_disabled,
        artifacts=_ordered_entries(artifacts),
    )


def render(document: ManifestDocument) -> str:
    """Render a document as the exact text of ``manifest.json``.

    :func:`json.dumps` of :meth:`ManifestDocument.as_dict` with a two-space indent,
    ``ensure_ascii=False`` and one trailing newline. ``sort_keys`` is deliberately *not* used, since
    key order is the field order of AAP 0.4.6; rendering is pure, so two runs write identical bytes
    (AAP 0.14.3), and the write belongs to the artifact writer (AAP 0.4.8).

    Returns:
        The JSON text, ending in exactly one newline.
    """
    return json.dumps(document.as_dict(), indent=2, ensure_ascii=False) + "\n"
