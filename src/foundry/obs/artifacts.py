"""Per-test artifact capture: the layout, the redaction and the module's one filesystem write.

A run records what each test produced under ``artifacts/<run_id>/<sanitized-nodeid>/`` (AAP 0.4.6):
``request.json``, ``response.json``, ``stdout.log``, ``stderr.log``, ``traceback.txt``,
``log_tail.log`` and ``manifest.json``. This is the artifact-owning module of the three AAP 0.4.8
permits writes in, so every artifact reaches the disk through :class:`ArtifactWriter`, which writes
each final name directly rather than through a temporary file and rename, since a worker writes only
inside its own item's directory. :meth:`ArtifactWriter.ensure_run_dir` is the one place the run
directory itself is made, so an unwritable ``artifacts.dir`` becomes the one warning of AAP 0.4.7
instead of a second write site. Payloads pass through :class:`foundry.obs.redaction.Redactor`,
the node id included, so a configured secret has no route onto the disk (AAP 0.3.2); a failing
write is reported once at ``WARNING``, disabling capture for the run while the manifest is still
attempted as the record of the fault (AAP 0.4.7); an escaping path, a nameless kind or an unbound
writer raises ``ArtifactError``. ``sort_keys=True`` JSON, one trailing newline, explicit UTF-8 and
sorted artifact tuples keep two runs byte-identical (AAP 0.14.3); importing the module defines
names only (AAP 0.4.8).
"""

from __future__ import annotations

import json
import types
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

ARTIFACT_FILENAMES: Final[Mapping[ArtifactKind, str]] = types.MappingProxyType(
    {
        "request": "request.json",
        "response": "response.json",
        "stdout": "stdout.log",
        "stderr": "stderr.log",
        "traceback": "traceback.txt",
        "log_tail": "log_tail.log",
        "manifest": "manifest.json",
    }
)
"""The file each :data:`foundry.core.types.ArtifactKind` is written as (AAP 0.4.6).

One name per kind, published as a mapping proxy so nothing may rename one; ``log_tail.log`` is the
seventh, the last ``logging.capture_lines`` captured records, referenced with ``kind: "log_tail"``.
"""

KEPT_WHEN_PASSED: Final[frozenset[str]] = frozenset({MANIFEST_FILENAME})
"""The files a passing test keeps with ``artifacts.keep_passed`` ``false`` (AAP 0.4.5)."""

_ENCODING: Final[str] = "utf-8"
"""The encoding of every artifact file, and therefore of the bytes each manifest digest covers."""


def node_dir(run_dir: str | Path, nodeid: str) -> Path:
    """Return the artifact directory one test writes into, without creating it (AAP 0.4.6).

    The pure half of :meth:`ArtifactWriter.dir_for`, named by the encoded node id. The encoding rule
    stays in :func:`foundry.core.paths.sanitize_nodeid` alone, and the writer scrubs the node id
    first, so a caller rebuilding a path from a raw node id must too; ``FoundryError`` follows an
    empty node id, ``ArtifactError`` a path resolving outside ``run_dir`` that a symlink can cause.
    """
    return artifact_dir_for(Path(run_dir), nodeid)


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """One artifact file this run wrote, as ``results.json`` records it (AAP 0.4.6).

    Frozen because three renderers read it; only relative paths keep a result document comparable
    between hosts, and ``redacted`` records that the redaction engine changed the payload.
    """

    kind: ArtifactKind
    path: str
    bytes: int
    sha256: str
    redacted: bool

    def as_manifest_entry(self) -> ManifestEntry:
        """Return this artifact as the ``manifest.json`` entry describing it.

        ``path`` and :attr:`foundry.obs.manifest.ManifestEntry.file` are one value under two names.
        :func:`foundry.obs.manifest.entry_for` is not used: the writer hashed the bytes it wrote.
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

    ``disabled`` stops all but the manifest write; ``warned`` reports a non-disabling fault once.
    """

    disabled: bool = False
    warned: bool = False


class ArtifactWriter:
    """Write, redact and account for one run's per-test artifacts (AAP 0.7.1).

    Session-scoped over the artifacts root, built once per run with the root named by
    ``artifacts.dir`` and the run's id. Every writing method is total -- a failing write disables
    capture and returns ``None`` (AAP 0.4.7), the sole exception being ``ArtifactError``.
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

        Raises ``ArtifactError`` if ``run_id`` is blank or would place the run directory outside
        ``run_dir``; nothing is created either way.
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
        """Return the run directory, ``<run_dir>/<run_id>``, as a resolved absolute path."""
        return self._run_dir

    @property
    def nodeid(self) -> str | None:
        """Return the node id this writer is bound to, or ``None`` for a session-scoped writer."""
        return self._nodeid

    @property
    def disabled(self) -> bool:
        """Return whether artifact capture failed for this run (AAP 0.4.7).

        A disabled writer's records report ``artifacts_disabled: true`` and carry no artifact paths.
        """
        return self._state.disabled

    @property
    def manifest(self) -> tuple[ArtifactRef, ...]:
        """Return the artifacts written for the bound test, sorted by ``(kind, path)``.

        The references written for the bound node id, or ``()``, in artifact order (AAP 0.14.3).
        """
        return tuple(sorted(self._refs.values(), key=lambda ref: (ref.kind, ref.path)))

    def ensure_run_dir(self) -> bool:
        """Create this run's ``artifacts/<run_id>/`` directory and report whether capture is on.

        The one place the run directory is made: this module is one of the three AAP 0.4.8 permits a
        filesystem write in, and a run's artifact tree belongs to the writer that fills it. A
        directory that cannot be created is AAP 0.4.7's "artifacts directory not writable" row --
        the fault is reported once at ``WARNING`` through :meth:`_disable`, capture is disabled for
        the run so every record reports ``artifacts_disabled: true`` and carries no artifact paths,
        and the run continues with the exit code its tests alone decide. A second call on a disabled
        writer returns ``False`` without touching the filesystem or warning again, and such a writer
        still attempts its ``manifest.json`` as the record of the fault (AAP 0.4.6).

        Returns:
            ``True`` once the run directory exists, ``False`` when capture is disabled.
        """
        if self._state.disabled:
            return False
        try:
            self._run_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._disable(
                f"artifact capture disabled: cannot create {self._run_dir.as_posix()}", exc
            )
            return False
        return True

    def dir_for(self, nodeid: str) -> Path:
        """Return, and create, the artifact directory of one test, named by its scrubbed node id.

        Returned uncreated while capture is disabled; ``ArtifactError`` if the path escapes.
        """
        directory = node_dir(self._run_dir, self._redactor.text(nodeid))
        if self._state.disabled:
            return directory
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._disable(f"cannot create artifact directory {directory}", exc)
        return directory

    def for_node(self, nodeid: str) -> ArtifactWriter:
        """Return a writer bound to one test that shares this run's settings and fault state.

        Its own registry makes the manifest that test's alone; the shared state keeps one warning.
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
        """Redact ``data`` and write it as one artifact of the bound test (AAP 0.4.7).

        The reference comes back, or ``None`` when the write was refused: while capture is disabled
        every write but ``kind="manifest"`` returns ``None``, that manifest still attempted as the
        fault record. A ``name`` is an extra artifact: relative, inside the test's directory.
        """
        source = _decode(data)
        scrubbed = self._redactor.text(source)
        return self._store(kind, name, scrubbed, redacted=scrubbed != source)

    def write_json(
        self, kind: ArtifactKind, payload: object, name: str | None = None
    ) -> ArtifactRef | None:
        """Redact a JSON-shaped payload and write it as indented, key-sorted JSON.

        A payload the raw encoder cannot represent is still written, redaction rewriting it.
        """
        if self._state.disabled:
            return None
        scrubbed = _dump(self._redactor.json_value(payload), self._redactor)
        try:
            unchanged = scrubbed == _dump(payload, self._redactor)
        except (TypeError, ValueError, RecursionError):
            # A payload the raw encoder cannot represent has necessarily been rewritten.
            unchanged = False
        return self._store(kind, name, scrubbed, redacted=not unchanged)

    def write_text(
        self, kind: ArtifactKind, text: str, name: str | None = None
    ) -> ArtifactRef | None:
        """Redact captured text and write it as an artifact ending in exactly one newline.

        What ``stdout.log``, ``stderr.log``, ``traceback.txt`` and ``log_tail.log`` are written
        with. Trailing blank lines are normalised away. ``ArtifactError`` follows :meth:`write`.
        """
        if self._state.disabled:
            return None
        scrubbed = self._redactor.text(text)
        return self._store(kind, name, _one_newline(scrubbed), redacted=scrubbed != text)

    def write_log_tail(self, lines: Sequence[str]) -> ArtifactRef | None:
        """Write the captured log tail as ``log_tail.log``.

        An empty sequence writes nothing: capacity ``0`` or no logging leaves no tail to record.
        """
        if not lines:
            return None
        return self.write_text("log_tail", "\n".join(lines))

    def write_manifest(self) -> ArtifactRef | None:
        """Build and write this test's ``manifest.json``.

        The document carries the AAP 0.4.6 fields -- ``schema_version``, ``run_id``, the unsanitised
        node id, ``profile``, ``created_at``, ``artifacts_disabled`` as the run's capture state so a
        failed capture is recorded rather than lost -- and every artifact except the manifest
        itself, whose self-referential digest is impossible. It is redacted like any payload.
        """
        document = build_document(
            run_id=self._run_id,
            nodeid=self._bound_nodeid(),
            profile=self._profile,
            created_at=self._created_at if self._created_at is not None else timestamp(),
            artifacts_disabled=self._state.disabled,
            artifacts=[
                ref.as_manifest_entry() for ref in self._refs.values() if ref.kind != "manifest"
            ],
        )
        return self.write("manifest", None, render(document))

    def finalize(self, *, passed: bool) -> tuple[ArtifactRef, ...]:
        """Finish one test's artifacts: prune a passing test, then write the manifest.

        The coordinator's teardown step: the manifest comes after the prune, so it never lists a
        file the prune has just removed. Disabled capture skips the prune, still writes the
        manifest as the record of the fault (AAP 0.4.6) and returns ``()`` (AAP 0.4.7).
        """
        if not self._state.disabled:
            nodeid = self._bound_nodeid()
            if passed and not self._keep_passed:
                self._prune(nodeid)
        if self.write_manifest() is None or self._state.disabled:
            return ()
        return self.manifest

    def _store(
        self, kind: ArtifactKind, name: str | None, text: str, *, redacted: bool
    ) -> ArtifactRef | None:
        """Encode redacted text, write it, and record the reference: the module's single write.

        The digest is therefore always that of the bytes on disk, and a capture fault still attempts
        ``"manifest"``, directory and all, unregistered and unannounced (AAP 0.4.7).
        """
        if self._state.disabled and kind != "manifest":
            return None
        nodeid = self._bound_nodeid()
        directory = self.dir_for(nodeid)
        target = self._target(directory, kind, name)
        # Replacement rather than failure: a lone surrogate reaches here from a non-UTF-8 file name.
        payload = text.encode(_ENCODING, errors="replace")
        try:
            if target.parent != directory or not directory.is_dir():
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
        if not self._state.disabled:
            self._refs[path] = reference
            emit_artifact_written(nodeid, kind, path, reference.bytes, reference.sha256)
        return reference

    def _target(self, directory: Path, kind: ArtifactKind, name: str | None) -> Path:
        """Resolve the file one artifact is written to, inside the test's directory."""
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
        """Remove a passing test's artifacts, keeping :data:`KEPT_WHEN_PASSED`."""
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
        """Return the node id this writer is bound to, raising if it is bound to no test."""
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
        """Report a capture fault once for the run and stop writing artifacts."""
        if self._state.disabled:
            return
        self._state.disabled = True
        emit_plumbing_failure("artifacts", message, exc=exc)

    def _warn_once(self, message: str, exc: OSError | None = None) -> None:
        """Report a fault that does not disable capture, at most once for the run."""
        if self._state.warned:
            return
        self._state.warned = True
        emit_plumbing_failure("artifacts", message, exc=exc)


def _decode(data: str | bytes) -> str:
    """Return ``data`` as text, decoding bytes as UTF-8 with replacement instead of raising."""
    if isinstance(data, bytes):
        return bytes(data).decode(_ENCODING, errors="replace")
    return data


def _one_newline(text: str) -> str:
    """Return ``text`` with its trailing newlines normalised to exactly one."""
    if not text:
        return ""
    return text.rstrip("\r\n") + "\n"


def _dump(value: object, redactor: Redactor) -> str:
    """Serialise a JSON-shaped value as the exact text of a ``.json`` artifact."""
    rendered = json.dumps(
        value,
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
        default=lambda leaf: redactor.text(str(leaf)),
    )
    return rendered + "\n"
