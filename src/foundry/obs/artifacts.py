"""Per-test artifact capture: the layout, the redaction and the module's one filesystem write.

A run records what each test produced under ``artifacts/<run_id>/<sanitized-nodeid>/`` (AAP
0.4.6): ``request.json``, ``response.json``, ``stdout.log``, ``stderr.log``, ``traceback.txt``,
``log_tail.log`` and ``manifest.json``. Filesystem writes are permitted in only three modules
(AAP 0.4.8) and this is the one that owns that layout, so every artifact a test produces reaches
the disk through :class:`ArtifactWriter` and nothing else does.

One directory per test
    A worker writes only inside its own item's directory, so two workers never contend for one
    file, and that is why :meth:`ArtifactWriter.write` writes the final name with
    :meth:`pathlib.Path.write_bytes` rather than a temporary file plus a rename.

Redaction first
    Every payload passes through :class:`foundry.obs.redaction.Redactor` before it is encoded, so a
    configured secret has no route onto the disk (AAP 0.3.2): :attr:`ArtifactRef.redacted` records
    that the engine ran and the manifest entry carries the flag on (AAP 0.4.6).

Failure isolation
    A directory or file that cannot be created or written is reported once at ``WARNING`` through
    :func:`foundry.obs.events.emit_plumbing_failure` and changes neither a test outcome nor an exit
    code (AAP 0.4.7); capture is disabled for the run from then on, so every later call returns
    ``None`` and every record reports ``artifacts_disabled: true``. A contract violation surfaces
    instead: a path resolving outside the run directory, a kind with no file name, or a write on a
    writer bound to no test raises :class:`foundry.core.exceptions.ArtifactError`.

Determinism
    Fixed file names, ``sort_keys=True`` JSON, exactly one trailing newline, explicit UTF-8 and
    sorted artifact tuples, so two runs write byte-identical artifacts (AAP 0.14.3). Importing the
    module does none of that: it defines names only (AAP 0.4.8).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

from foundry.core.exceptions import ArtifactError
from foundry.core.paths import artifact_dir_for, relative_artifact_path, resolve_under
from foundry.core.types import ArtifactKind
from foundry.obs.events import emit_artifact_written, emit_plumbing_failure
from foundry.obs.manifest import (
    MANIFEST_FILENAME,
    ManifestEntry,
    build_document,
    render,
    sha256_hex,
    timestamp,
)
from foundry.obs.redaction import Redactor

__all__: Final[tuple[str, ...]] = (
    "ARTIFACT_FILENAMES",
    "ArtifactRef",
    "ArtifactWriter",
    "KEPT_WHEN_PASSED",
    "node_dir",
)

ARTIFACT_FILENAMES: Final[Mapping[ArtifactKind, str]] = {
    "request": "request.json",
    "response": "response.json",
    "stdout": "stdout.log",
    "stderr": "stderr.log",
    "traceback": "traceback.txt",
    "log_tail": "log_tail.log",
    "manifest": "manifest.json",
}
"""The file each :data:`foundry.core.types.ArtifactKind` is written as (AAP 0.4.6).

One name per kind, so the layout is a function of the kind rather than of what a caller passed.
``log_tail.log`` is the layout's seventh file: the last ``logging.capture_lines`` captured records,
written as their own artifact and referenced from a record with ``kind: "log_tail"``. Published so
a report or a reviewer can name a file without repeating the string; nothing may rename one.
"""

KEPT_WHEN_PASSED: Final[frozenset[str]] = frozenset({MANIFEST_FILENAME})
"""The files a passing test keeps when ``artifacts.keep_passed`` is ``false`` (AAP 0.4.5).

The manifest survives rather than the artifacts it describes: it proves the run recorded the test
and what it captured, without retaining the captured bytes of a suite that passed.
"""

_ENCODING: Final[str] = "utf-8"
"""The encoding of every artifact file, and therefore of the bytes each manifest digest covers."""


def node_dir(run_dir: str | Path, nodeid: str) -> Path:
    """Return the artifact directory one test writes into, without creating it.

    The pure half of :meth:`ArtifactWriter.dir_for`, for a caller that wants the path without a
    writer: a report link, a test asserting the layout, a diagnostic. The percent-encoding rule
    stays in :func:`foundry.core.paths.sanitize_nodeid` alone.

    Args:
        run_dir: The run directory, normally ``artifacts/<run_id>``.
        nodeid: The pytest node id of the item whose directory is requested.

    Returns:
        The resolved absolute directory path, inside ``run_dir`` and named by the encoded node id.
        The directory need not exist.

    Raises:
        FoundryError: If ``nodeid`` is empty or whitespace-only.
        ArtifactError: If the derived path escapes ``run_dir``, which a symlink inside it can
            cause.
    """
    return artifact_dir_for(Path(run_dir), nodeid)


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """One artifact file this run wrote, as ``results.json`` records it (AAP 0.4.6).

    Frozen because three renderers read it and the record must not drift from the bytes on disk; it
    carries no absolute path, so a result document stays comparable between runs and between hosts.

    Attributes:
        kind: Which artifact this is, from :data:`foundry.core.types.ARTIFACT_KINDS`.
        path: The file's path relative to the run directory, forward slashes, no ``..`` segment.
        bytes: The size of the written file; the name shadows the builtin because it is the field
            name AAP 0.4.6 publishes.
        sha256: Hexadecimal SHA-256 of the bytes on disk, computed after redaction and encoding, so
            a reviewer who recomputes it gets this value back.
        redacted: Whether the redaction engine changed the content before it was written.
    """

    kind: ArtifactKind
    path: str
    bytes: int
    sha256: str
    redacted: bool

    def as_manifest_entry(self) -> ManifestEntry:
        """Return this artifact as the ``manifest.json`` entry describing it.

        ``path`` and :attr:`foundry.obs.manifest.ManifestEntry.file` are the same value under two
        names, so this is a copy. The entry is built here rather than through
        :func:`foundry.obs.manifest.entry_for`, which hashes content: the writer hashed the bytes it
        wrote, and re-hashing would report whatever the file holds now.

        Returns:
            The entry for the document's ``artifacts`` list.
        """
        return ManifestEntry(
            kind=self.kind,
            file=self.path,
            bytes=self.bytes,
            sha256=self.sha256,
            redacted=self.redacted,
        )


@dataclass(slots=True)
class _WriterState:
    """The fault state one run's writers share; mutable because the first fault flips it.

    Attributes:
        disabled: ``True`` once artifact capture failed for the run, after which every write
            returns ``None``.
        warned: ``True`` once a fault that does not disable capture, such as a file that could not
            be removed, has been reported, so a second one stays quiet.
    """

    disabled: bool = False
    warned: bool = False


class ArtifactWriter:
    """Write, redact and account for one run's per-test artifacts (AAP 0.7.1).

    Session-scoped over the artifacts root: constructed once per run with the root named by
    ``artifacts.dir`` and the run's id, with a test's directory created lazily so a run that
    collects nothing touches no directory. :meth:`for_node` derives the per-test writer that the
    coordinator and the ``foundry_artifacts`` fixture use, which is what gives
    ``write(kind, name, data)`` its documented shape while still knowing which test it serves.

    Every writing method is total with respect to the filesystem: an uncreatable directory or an
    unwritable file disables capture and returns ``None`` (AAP 0.4.7). The one exception raised is
    :class:`foundry.core.exceptions.ArtifactError`, and only for a contract violation.
    """

    def __init__(
        self,
        run_dir: str | Path,
        *,
        run_id: str,
        profile: str,
        redactor: Redactor,
        nodeid: str | None = None,
        keep_passed: bool = False,
        created_at: str | None = None,
    ) -> None:
        """Build a writer for one run, optionally bound to one test.

        Args:
            run_dir: The artifacts root as ``artifacts.dir`` names it; the run directory used is
                ``<run_dir>/<run_id>``, the ``artifacts/<run_id>/`` layout of AAP 0.4.6.
            run_id: The run identifier, used as the run directory's name and recorded in every
                manifest.
            profile: The active profile name, recorded in every manifest.
            redactor: The engine every payload passes through before it is written.
            nodeid: The node id to bind this writer to, or ``None`` for the session-scoped writer
                that derives per-test writers with :meth:`for_node`.
            keep_passed: When ``False``, :meth:`finalize` prunes a passing test's artifacts back to
                :data:`KEPT_WHEN_PASSED`.
            created_at: The ``created_at`` a manifest records. ``None`` reads the clock through
                :func:`foundry.obs.manifest.timestamp` when the manifest is written; a test pins it
                to make the document reproducible.

        Raises:
            ArtifactError: If ``run_id`` is blank or would place the run directory outside
                ``run_dir``. Nothing is created either way.
        """
        if not run_id.strip():
            raise ArtifactError(
                "the artifact writer requires a non-empty run id",
                attempted_path=Path(run_dir),
                run_dir=Path(run_dir).resolve(),
                hint="the run id names the artifacts/<run_id>/ directory, so it cannot be blank",
            )
        self._root = Path(run_dir)
        self._run_id = run_id
        self._run_dir = resolve_under(self._root, run_id)
        self._profile = profile
        self._redactor = redactor
        self._nodeid = nodeid
        self._keep_passed = keep_passed
        self._created_at = created_at
        self._state = _WriterState()
        self._refs: dict[str, ArtifactRef] = {}

    @property
    def run_dir(self) -> Path:
        """Return the run directory, ``<run_dir>/<run_id>``, as a resolved absolute path.

        It need not exist yet.
        """
        return self._run_dir

    @property
    def nodeid(self) -> str | None:
        """Return the node id this writer is bound to, or ``None`` for a session-scoped writer."""
        return self._nodeid

    @property
    def disabled(self) -> bool:
        """Return whether artifact capture failed for this run.

        Once it is ``True``, every record built from this writer reports ``artifacts_disabled:
        true`` and carries no artifact paths (AAP 0.4.7).
        """
        return self._state.disabled

    @property
    def manifest(self) -> tuple[ArtifactRef, ...]:
        """Return the artifacts written for the bound test, sorted by ``(kind, path)``.

        The order follows the artifacts rather than the order they were written in, which keeps a
        rendered manifest and a result record identical between two runs (AAP 0.14.3). Reading it
        never raises and never touches the filesystem.

        Returns:
            The references written for the bound node id, or ``()`` when nothing was written.
        """
        return tuple(sorted(self._refs.values(), key=lambda ref: (ref.kind, ref.path)))

    def dir_for(self, nodeid: str) -> Path:
        """Return, and create, the artifact directory of one test.

        Args:
            nodeid: The pytest node id of the test whose directory is wanted.

        Returns:
            The node directory. When capture is disabled it is returned uncreated, so a caller can
            still compute the path without a write being attempted.

        Raises:
            FoundryError: If ``nodeid`` is empty or whitespace-only.
            ArtifactError: If the derived path escapes the run directory.
        """
        directory = node_dir(self._run_dir, nodeid)
        if self._state.disabled:
            return directory
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._disable(f"cannot create artifact directory {directory}", exc)
        return directory

    def for_node(self, nodeid: str) -> ArtifactWriter:
        """Return a writer bound to one test that shares this run's settings and fault state.

        The intended way to reach the documented ``write(kind, name, data)`` signature: the derived
        writer knows which directory to write into, keeps its own registry of what it wrote, and
        reports a manifest for that test alone. Sharing the state object is what keeps a broken run
        directory to one ``WARNING`` for the run instead of one per test.

        Args:
            nodeid: The pytest node id to bind the derived writer to.

        Returns:
            A new writer for ``nodeid``.
        """
        derived = ArtifactWriter(
            self._root,
            run_id=self._run_id,
            profile=self._profile,
            redactor=self._redactor,
            nodeid=nodeid,
            keep_passed=self._keep_passed,
            created_at=self._created_at,
        )
        derived._state = self._state
        return derived

    def write(self, kind: ArtifactKind, name: str | None, data: str | bytes) -> ArtifactRef | None:
        """Redact ``data`` and write it as one artifact of the bound test.

        The return type describes the success path; the disabled path of AAP 0.4.7 returns
        ``None``, because a run whose artifact directory proved unwritable records no artifact
        paths at all.

        Args:
            kind: Which artifact this is.
            name: The file to write, or ``None`` for :data:`ARTIFACT_FILENAMES` of ``kind``. An
                explicit name is for an extra artifact and must be a relative path inside the
                test's directory.
            data: The payload. ``bytes`` are decoded as UTF-8 with ``errors="replace"``, because
                every file of the layout is text; a binary payload is encoded by its caller.

        Returns:
            The reference to the written file, or ``None`` when capture is disabled.

        Raises:
            ArtifactError: If the writer is bound to no test, if ``kind`` has no file name, or if
                ``name`` would resolve outside the test's directory.
        """
        if self._state.disabled:
            return None
        source = _decode(data)
        scrubbed = self._redactor.text(source)
        return self._store(kind, name, scrubbed, redacted=scrubbed != source)

    def write_json(
        self, kind: ArtifactKind, payload: object, name: str | None = None
    ) -> ArtifactRef | None:
        """Redact a JSON-shaped payload and write it as indented, key-sorted JSON.

        What ``request.json`` and ``response.json`` are written with, and why two runs write
        byte-identical files: ``sort_keys=True``, a two-space indent, exactly one trailing newline.

        Args:
            kind: Which artifact this is.
            payload: Any JSON-shaped value. A leaf that is not JSON-serialisable is rendered as
                redacted text rather than raising, so an odd payload cannot fail a run.
            name: The file to write, or ``None`` for the kind's name in :data:`ARTIFACT_FILENAMES`.

        Returns:
            The reference to the written file, or ``None`` when capture is disabled.

        Raises:
            ArtifactError: If the writer is bound to no test, if ``kind`` has no file name, or if
                ``name`` would resolve outside the test's directory.
        """
        if self._state.disabled:
            return None
        scrubbed = _dump(self._redactor.json_value(payload), self._redactor)
        return self._store(
            kind, name, scrubbed, redacted=scrubbed != _dump(payload, self._redactor)
        )

    def write_text(
        self, kind: ArtifactKind, text: str, name: str | None = None
    ) -> ArtifactRef | None:
        """Redact captured text and write it as an artifact ending in exactly one newline.

        What ``stdout.log``, ``stderr.log``, ``traceback.txt`` and ``log_tail.log`` are written
        with: the report wrapper passes a failed test's ``capstdout``/``capstderr`` and the log ring
        passes its tail. Trailing blank lines are normalised away, so two runs write identical
        bytes.

        Args:
            kind: Which artifact this is.
            text: The captured text, of any length.
            name: The file to write, or ``None`` for the kind's name in :data:`ARTIFACT_FILENAMES`.

        Returns:
            The reference to the written file, or ``None`` when capture is disabled.

        Raises:
            ArtifactError: If the writer is bound to no test, if ``kind`` has no file name, or if
                ``name`` would resolve outside the test's directory.
        """
        if self._state.disabled:
            return None
        scrubbed = self._redactor.text(text)
        return self._store(kind, name, _one_newline(scrubbed), redacted=scrubbed != text)

    def write_log_tail(self, lines: Sequence[str]) -> ArtifactRef | None:
        """Write the captured log tail as ``log_tail.log``.

        An empty sequence writes nothing and returns ``None``: a ring of capacity ``0``
        (``logging.capture_lines == 0``) or a test that logged nothing has no tail to record, and
        an empty file would claim it captured one.

        Args:
            lines: The formatted log lines, oldest first.

        Returns:
            The reference to ``log_tail.log``, or ``None`` when there is no tail or capture is
            disabled.

        Raises:
            ArtifactError: If the writer is bound to no test.
        """
        if not lines:
            return None
        return self.write_text("log_tail", "\n".join(lines))

    def write_manifest(self) -> ArtifactRef | None:
        """Build and write this test's ``manifest.json``.

        The document lists every artifact written for the test except the manifest itself: a
        self-referential digest is impossible, and ``results.json`` already carries an
        ``artifacts`` entry of kind ``manifest`` for the record, so nothing is lost by leaving it
        out. The rendered document passes through the same redaction as any other payload, so a node
        id that embeds a credential is scrubbed here rather than persisted.

        Returns:
            The reference to ``manifest.json``, or ``None`` when capture is disabled.

        Raises:
            ArtifactError: If the writer is bound to no test, or if the manifest's path would
                resolve outside the test's directory.
        """
        if self._state.disabled:
            return None
        document = build_document(
            run_id=self._run_id,
            nodeid=self._bound_nodeid(),
            profile=self._profile,
            created_at=self._created_at if self._created_at is not None else timestamp(),
            artifacts_disabled=False,
            artifacts=[
                ref.as_manifest_entry() for ref in self._refs.values() if ref.kind != "manifest"
            ],
        )
        return self.write("manifest", None, render(document))

    def finalize(self, *, passed: bool) -> tuple[ArtifactRef, ...]:
        """Finish one test's artifacts: prune a passing test, then write the manifest.

        The teardown step the coordinator calls once per test. The order matters: the manifest is
        written after the prune, so it never lists a file the prune has just removed.

        Args:
            passed: Whether the test passed. With ``artifacts.keep_passed`` unset, a passing test
                keeps only :data:`KEPT_WHEN_PASSED`.

        Returns:
            The references that survive, sorted by ``(kind, path)``, or ``()`` when capture is
            disabled.

        Raises:
            ArtifactError: If the writer is bound to no test.
        """
        if self._state.disabled:
            return ()
        nodeid = self._bound_nodeid()
        if passed and not self._keep_passed:
            self._prune(nodeid)
        if self.write_manifest() is None:
            return ()
        return self.manifest

    def _store(
        self, kind: ArtifactKind, name: str | None, text: str, *, redacted: bool
    ) -> ArtifactRef | None:
        """Encode redacted text, write it, and record the reference to it.

        The module's single write: every public method redacts its payload and hands the result
        here, so one place encodes, one place writes and one place digests -- which is what makes
        the digest always that of the bytes on disk.

        Args:
            kind: Which artifact this is.
            name: The file to write, or ``None`` for the kind's name.
            text: The final, already redacted text.
            redacted: Whether redaction changed the payload.

        Returns:
            The reference to the written file, or ``None`` when capture is disabled.

        Raises:
            ArtifactError: If the writer is bound to no test, if ``kind`` has no file name, or if
                ``name`` would resolve outside the test's directory.
        """
        if self._state.disabled:
            return None
        nodeid = self._bound_nodeid()
        directory = self.dir_for(nodeid)
        if self._state.disabled:
            return None
        target = self._target(directory, kind, name)
        payload = text.encode(_ENCODING)
        try:
            if target.parent != directory:
                target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        except OSError as exc:
            self._disable(f"cannot write artifact file {target}", exc)
            return None
        path = relative_artifact_path(target, self._run_dir)
        reference = ArtifactRef(
            kind=kind,
            path=path,
            bytes=len(payload),
            sha256=sha256_hex(payload),
            redacted=redacted,
        )
        self._refs[path] = reference
        emit_artifact_written(nodeid, kind, path, reference.bytes, reference.sha256)
        return reference

    def _target(self, directory: Path, kind: ArtifactKind, name: str | None) -> Path:
        """Resolve the file one artifact is written to, inside the test's directory.

        Args:
            directory: The test's artifact directory.
            kind: Which artifact this is.
            name: The explicit file name, or ``None`` for the kind's name.

        Returns:
            The resolved absolute path of the file to write.

        Raises:
            ArtifactError: If ``kind`` has no file name, if ``name`` is blank, absolute or carries a
                ``..`` segment, or if the resolved path escapes the test's directory.
        """
        if name is None:
            filename = ARTIFACT_FILENAMES.get(kind)
            if filename is None:
                raise ArtifactError(
                    f"no artifact file name is defined for kind {kind!r}",
                    attempted_path=directory,
                    run_dir=self._run_dir,
                    hint="one of " + ", ".join(sorted(ARTIFACT_FILENAMES)),
                )
        else:
            filename = name
            candidate = PurePosixPath(filename)
            if not filename.strip() or candidate.is_absolute() or ".." in candidate.parts:
                raise ArtifactError(
                    f"artifact name is not a file inside the test's directory: {filename!r}",
                    attempted_path=directory / filename,
                    run_dir=self._run_dir,
                    hint="name an artifact with a relative path such as 'attachments/body.json'",
                )
        return resolve_under(directory, filename)

    def _prune(self, nodeid: str) -> None:
        """Remove a passing test's artifacts, keeping :data:`KEPT_WHEN_PASSED`.

        Args:
            nodeid: The test whose directory is pruned.

        Raises:
            FoundryError: If ``nodeid`` is empty or whitespace-only.
            ArtifactError: If the derived directory escapes the run directory.
        """
        directory = self.dir_for(nodeid)
        if self._state.disabled:
            return
        try:
            entries = sorted(directory.iterdir())
        except OSError as exc:
            self._warn_once(f"cannot list artifact directory {directory}", exc)
            return
        for entry in entries:
            if entry.name in KEPT_WHEN_PASSED or not entry.is_file():
                continue
            try:
                entry.unlink()
            except OSError as exc:
                self._warn_once(f"cannot remove artifact file {entry}", exc)
        self._refs = {
            path: reference
            for path, reference in self._refs.items()
            if (self._run_dir / path).is_file()
        }

    def _bound_nodeid(self) -> str:
        """Return the node id this writer is bound to.

        Returns:
            The bound node id.

        Raises:
            ArtifactError: If the writer is bound to no test, which means it was used where a
                writer from :meth:`for_node` was required.
        """
        nodeid = self._nodeid
        if nodeid is None:
            raise ArtifactError(
                "this artifact writer is not bound to a test",
                attempted_path=self._run_dir,
                run_dir=self._run_dir,
                hint="call for_node(nodeid) to get a writer for one test's artifacts",
            )
        return nodeid

    def _disable(self, message: str, exc: OSError | None = None) -> None:
        """Report a capture fault once for the run and stop writing artifacts.

        Args:
            message: What could not be done, naming the path and never a payload.
            exc: The fault, or ``None``; only its class name is logged.
        """
        if self._state.disabled:
            return
        self._state.disabled = True
        emit_plumbing_failure("artifacts", message, exc=exc)

    def _warn_once(self, message: str, exc: OSError | None = None) -> None:
        """Report a fault that does not disable capture, at most once for the run.

        Args:
            message: What could not be done, naming the path and never a payload.
            exc: The fault, or ``None``; only its class name is logged.
        """
        if self._state.warned:
            return
        self._state.warned = True
        emit_plumbing_failure("artifacts", message, exc=exc)


def _decode(data: str | bytes) -> str:
    """Return ``data`` as text, decoding bytes as UTF-8 with replacement.

    Args:
        data: The payload to decode.

    Returns:
        The payload as text. Bytes that are not valid UTF-8 become replacement characters, which
        keeps a stray binary payload from raising inside a test's teardown.
    """
    if isinstance(data, bytes):
        return bytes(data).decode(_ENCODING, errors="replace")
    return data


def _one_newline(text: str) -> str:
    """Return ``text`` with its trailing newlines normalised to exactly one.

    Args:
        text: The text to normalise.

    Returns:
        The text ending in a single newline, or the empty string when ``text`` was empty. Because
        the bytes reach the disk through :meth:`pathlib.Path.write_bytes`, nothing translates them
        afterwards, so this is the only thing that decides the file's ending.
    """
    if not text:
        return ""
    return text.rstrip("\r\n") + "\n"


def _dump(value: object, redactor: Redactor) -> str:
    """Serialise a JSON-shaped value as the exact text of a ``.json`` artifact.

    Args:
        value: The value to serialise, already redacted by the caller.
        redactor: The engine a non-serialisable leaf is rendered through, so a value the encoder
            cannot represent cannot carry a secret onto the disk either.

    Returns:
        The JSON text with a two-space indent, sorted keys and one trailing newline.
    """
    rendered = json.dumps(
        value,
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
        default=lambda leaf: redactor.text(str(leaf)),
    )
    return rendered + "\n"
