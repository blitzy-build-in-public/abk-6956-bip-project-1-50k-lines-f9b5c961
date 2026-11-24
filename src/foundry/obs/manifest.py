"""The per-test ``manifest.json`` document: its field order, hashing and one serialisation.

``manifest.json`` is the seventh file of the artifact layout (AAP 0.4.6). It names every artifact
Foundry wrote for one test with the size and digest of each, so a reviewer can prove a file on disk
is the one the run recorded, and it records whether redaction changed the bytes and whether
artifact capture was disabled. This module owns that document's shape: the two frozen dataclasses,
the digest helper, the deterministic ordering and the exact text :func:`render` produces.

Shape only, never bytes on disk
    Nothing here opens, creates or removes a file. AAP 0.4.8 permits filesystem writes only in
    :mod:`foundry.obs.artifacts`, :mod:`foundry.cassette.store` and
    :mod:`foundry.reporting.store`, so the artifact writer builds a :class:`ManifestDocument`,
    calls :func:`render` and writes the string it returns. That split keeps the document testable
    without a filesystem, and leaves the one failure it can meet -- a run directory that is not
    writable, which becomes the ``artifacts_disabled`` state of AAP 0.4.7 -- to the writer.

Foundation imports only
    ``foundry.obs`` may import ``foundry.core`` and nothing else of Foundry (AAP 0.4.8), so
    :data:`~foundry.core.types.ArtifactKind` is imported rather than redeclared, the clock helpers
    come from :mod:`foundry.core.ids`, and the rest is the standard library. No third-party package
    is imported at any point: the models are frozen dataclasses rather than pydantic models, which
    keeps ``import foundry.obs.manifest`` free of ``pydantic`` on the plugin's 150 ms startup path
    (implicit requirement I5).

Byte-identical between runs
    Two runs with the same entries render identical text (AAP 0.14.3): entries are sorted before
    the document is built, key order follows the field order of AAP 0.4.6 rather than
    ``sort_keys``, indentation is fixed, and exactly one trailing newline is written. No timestamp
    is generated inside :func:`render`; ``created_at`` is an argument, so a test pins it.
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
"""The ``schema_version`` every manifest this release writes carries.

AAP 0.4.6 fixes the value at ``1``. A reader that later has to tell two shapes apart compares
against this number rather than inferring the shape from which keys are present, and a change to
the document's meaning arrives as a new number in a release that is allowed to break the promise
(AAP 0.4.9).
"""

MANIFEST_FILENAME: Final[str] = "manifest.json"
"""The name of the manifest inside a test's artifact directory (AAP 0.4.6).

The name is fixed rather than derived: the artifact layout the acceptance criteria describe lists
it beside ``request.json`` and ``stdout.log``, and the JUnit writer and the report refer to an
artifact by the name recorded in this document.
"""

_RELATIVE_ANCHOR: Final[Path] = Path()
"""The value reported as ``run_dir`` when a manifest path fails its containment check.

A manifest's ``file`` values are relative to a run directory this module is never given, so the
rejected path is reported against the notional root it was relative to instead of against a
resolved directory this model does not know.
"""


def _require_relative(file: str) -> None:
    """Reject a manifest path that could name a location outside the run directory.

    The producer of every ``file`` value is
    :func:`foundry.core.paths.relative_artifact_path`, which proves containment itself; this check
    exists because the manifest is a published document (AAP 0.4.6) and one naming
    ``../secrets.json`` or ``/etc/passwd`` would be read, by a reviewer and by a later tool, as a
    path Foundry sanctioned. The check is forward-slash only, which is the form
    :func:`~foundry.core.paths.relative_artifact_path` records on every platform, and it is applied
    where a set of entries becomes a document rather than in :func:`entry_for`, because an entry is
    a description of one file while a document is the artifact a reviewer opens.

    Args:
        file: The candidate value of a :attr:`ManifestEntry.file`.

    Raises:
        ArtifactError: If ``file`` is blank, names the run directory itself, is absolute, or has a
            ``..`` segment. The contracted class is raised rather than ``ValueError`` because only
            the eight types in :mod:`foundry.core.exceptions` cross a module boundary (AAP 0.14.3).
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
    """Validate the entries a document will carry and return them in a deterministic order.

    Ordering by ``(kind, file)`` is what makes a document's ``artifacts`` tuple a function of its
    content rather than of the order a writer happened to append files in, so two runs of one suite
    serialise identically (AAP 0.14.3). The sort is stable, so two entries that share a kind and a
    file keep the order they arrived in and a deliberate duplicate stays visible rather than being
    collapsed.

    Args:
        artifacts: The entries to check and order, in any order.

    Returns:
        The entries as a tuple, sorted by ``(kind, file)``.

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

    The five fields and their declaration order are the published shape of an ``artifacts``
    element, so a later release may add a key but may never rename or remove one (AAP 0.4.9). The
    class is frozen and slotted: nothing needs to mutate an entry once the file is written, and a
    fixed field set is what lets the artifact writer, the report and a comparison read the same
    record without copying it.

    Attributes:
        kind: Which artifact this is, from :data:`foundry.core.types.ARTIFACT_KINDS`.
        file: The file name inside the test's artifact directory, relative to the run directory and
            with forward slashes.
        bytes: The size of the file, taken from the redacted content. The name shadows the builtin
            deliberately, because it is the field name AAP 0.4.6 publishes.
        sha256: The lowercase hexadecimal SHA-256 digest of the same redacted content.
        redacted: Whether the redaction engine changed the content before it was written.
    """

    kind: ArtifactKind
    file: str
    bytes: int
    sha256: str
    redacted: bool

    def as_dict(self) -> dict[str, object]:
        """Return the entry as a JSON-serialisable mapping.

        The keys are ``kind``, ``file``, ``bytes``, ``sha256`` and ``redacted``, in the field order
        of AAP 0.4.6, and the mapping keeps that order because a JSON object renders in insertion
        order. ``kind`` is a plain string at runtime, so no custom encoder is needed.

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

    One document describes one collected item: the run it belongs to, the item's node id, the
    profile, when the manifest was created, whether artifact capture was disabled for the run, and
    the entries for the files that were written. It is frozen because the same instance feeds
    ``results.json``, the JUnit writer and the HTML renderer, which must agree (AAP 0.4.8), and
    because a document that could change after it was rendered would make the files on disk and the
    record of them disagree.

    Attributes:
        schema_version: The document's shape, :data:`MANIFEST_SCHEMA_VERSION` for this release.
        run_id: The identifier that names the run and its artifact directory (AAP 0.4.3).
        nodeid: The unsanitised pytest node id of the test this manifest belongs to.
        profile: The active profile name: ``default``, ``local``, ``ci`` or ``staging``.
        created_at: The ISO 8601 UTC timestamp, as :func:`timestamp` produced it.
        artifacts_disabled: ``True`` when the artifact directory was not writable and no artifact
            file was written (AAP 0.4.7), in which case ``artifacts`` is empty.
        artifacts: The recorded entries, sorted by ``(kind, file)``.
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

        The keys are ``schema_version``, ``run_id``, ``nodeid``, ``profile``, ``created_at``,
        ``artifacts_disabled`` and ``artifacts``, in the field order of AAP 0.4.6. That order is
        the contract, which is why :func:`render` does not sort the keys of this mapping.

        Returns:
            The document as a mapping of scalars plus a list of entry mappings.
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

        Because the document is frozen, a writer that records an artifact after the first render
        produces a new document instead of mutating a shared one. Everything except the two
        artifact fields is carried over unchanged, ``created_at`` included, so appending an
        artifact never re-stamps the run. The new entries are validated and sorted by the same path
        :func:`build_document` uses, so an appended document obeys the same rules as a fresh one.

        Args:
            artifacts: The complete new set of entries, in any order.
            artifacts_disabled: The new disabled flag, or ``None`` to keep this document's value.

        Returns:
            A new document, whose ``schema_version`` is :data:`MANIFEST_SCHEMA_VERSION`.

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
    """Return the lowercase hexadecimal SHA-256 digest of ``data``.

    A manifest records the digest of the bytes that are on disk, so the caller passes them *after*
    redaction (AAP 0.3.2): a value that was stripped never reaches a digest either, and a reviewer
    who recomputes the digest of an artifact file gets the recorded value back.

    Args:
        data: The exact bytes written to the artifact file.

    Returns:
        Sixty-four lowercase hexadecimal characters, as :func:`hashlib.sha256` renders them.
    """
    return hashlib.sha256(data).hexdigest()


def entry_for(kind: ArtifactKind, file: str, data: bytes, *, redacted: bool) -> ManifestEntry:
    """Describe one artifact file as a manifest entry.

    The function is a pure constructor by design: containment of ``file`` is checked when entries
    become a document (:func:`build_document`), because the writer knows the run directory and a
    single entry does not, and because a document is the artifact a reviewer opens.

    Args:
        kind: Which artifact this is; one of the seven values of
            :data:`~foundry.core.types.ARTIFACT_KINDS`, including ``log_tail`` for the captured log
            tail and ``manifest`` for this manifest.
        file: The file name inside the test's artifact directory, relative to the run directory and
            with forward slashes.
        data: The bytes written to that file, already redacted by the caller.
        redacted: Whether redaction changed those bytes before they were written.

    Returns:
        The entry, carrying the length of ``data`` and its digest.
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

    The clock is read through :func:`foundry.core.ids.utc_now`, the one permitted wall-clock read
    in the tree (AAP 0.14.3), and rendered through :func:`foundry.core.ids.to_iso8601`, so a
    manifest timestamp is the same fixed shape as ``started_at`` and ``finished_at`` in
    ``results.json`` and stays comparable across runs.

    Args:
        now: The instant to record. Defaults to the current UTC instant; a test passes a fixed
            value instead of patching the clock. A naive value is read as UTC.

    Returns:
        The instant as ISO 8601 text with millisecond precision and an explicit ``+00:00`` offset,
        for example ``2025-01-02T03:04:05.123+00:00``.
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

    This function establishes the two invariants of AAP 0.4.6 that a document depends on: every
    ``file`` is a path inside the run directory, and the ``artifacts`` tuple is sorted by
    ``(kind, file)`` so that two runs with the same entries serialise identically. It derives
    nothing else: ``artifacts_disabled`` is the caller's statement about the run, so a writer that
    could not create the directory passes ``True`` with an empty tuple and a run with nothing to
    record passes ``False`` with one, without either case being special-cased here.

    Args:
        run_id: The run identifier that names the artifact directory (AAP 0.4.3).
        nodeid: The unsanitised pytest node id of the test the manifest belongs to.
        profile: The active profile name, one of ``default``, ``local``, ``ci`` or ``staging``.
        created_at: The ISO 8601 timestamp :func:`timestamp` produced for this document.
        artifacts_disabled: ``True`` when the artifact directory was not writable and no artifact
            file was written (AAP 0.4.7).
        artifacts: The entries to record, in any order.

    Returns:
        The document, with ``schema_version`` set to :data:`MANIFEST_SCHEMA_VERSION`.

    Raises:
        ArtifactError: If an entry's ``file`` is blank, names the run directory itself, is absolute,
            or contains a ``..`` segment.
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

    The text is :func:`json.dumps` of :meth:`ManifestDocument.as_dict` with a two-space indent and
    ``ensure_ascii=False``, so a non-ASCII node id stays readable in the file, followed by one
    trailing newline. ``sort_keys`` is deliberately *not* used: the key order is the field order of
    AAP 0.4.6 -- ``schema_version`` first, ``artifacts_disabled`` before ``artifacts`` -- and
    sorting the keys would silently reorder a published document.

    Rendering is a pure function of the document, which is what makes two runs produce
    byte-identical files (AAP 0.14.3). Writing the text belongs to the artifact writer (AAP 0.4.8):
    this function returns it, so nothing here can leave a half-written manifest behind.

    Args:
        document: The document to render.

    Returns:
        The JSON text, ending in exactly one newline.
    """
    return json.dumps(document.as_dict(), indent=2, ensure_ascii=False) + "\n"
