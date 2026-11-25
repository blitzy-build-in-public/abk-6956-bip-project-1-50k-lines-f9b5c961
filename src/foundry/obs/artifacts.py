"""Per-test artifact capture: the layout, the redaction and the module's one filesystem write.

A run records each test's artifacts under ``artifacts/<run_id>/<sanitized-nodeid>/`` (AAP 0.4.6)
through :class:`ArtifactWriter`: node ids are percent-encoded and never redacted, and payload text
passes through :class:`foundry.obs.redaction.Redactor` (AAP 0.3.2), a failed write disabling
capture at ``WARNING`` (AAP 0.4.7). Importing it binds names only (AAP 0.4.8).
"""

from __future__ import annotations

import json
import types
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

import pytest

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
    "SESSION_RUN_ID",
    "active_writer",
    "node_dir",
    "set_active_writer",
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
"""The file each :data:`foundry.core.types.ArtifactKind` is written as (AAP 0.4.6)."""

KEPT_WHEN_PASSED: Final[frozenset[str]] = frozenset({MANIFEST_FILENAME})
"""The files a passing test keeps with ``artifacts.keep_passed`` ``false`` (AAP 0.4.5)."""

_ENCODING: Final[str] = "utf-8"
"""The encoding of every artifact file, and therefore of the bytes each manifest digest covers."""


def node_dir(run_dir: str | Path, nodeid: str) -> Path:
    """Return one test's artifact directory from its percent-encoded node id (AAP 0.4.6)."""
    return artifact_dir_for(Path(run_dir), nodeid)


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """One artifact file this run wrote, as ``results.json`` records it (AAP 0.4.6)."""

    kind: ArtifactKind
    path: str
    bytes: int
    sha256: str
    redacted: bool

    def as_manifest_entry(self) -> ManifestEntry:
        """Return this artifact as its ``manifest.json`` entry, hashed from the bytes it wrote."""
        return ManifestEntry(
            kind=self.kind,
            file=self.path,
            bytes=self.bytes,
            sha256=self.sha256,
            redacted=self.redacted,
        )


@dataclass(slots=True)
class _WriterState:
    """The fault state one run's writers share; mutable because the first fault flips it."""

    disabled: bool = False
    warned: bool = False


class ArtifactWriter:
    """Write, redact and account for one run's per-test artifacts (AAP 0.7.1).

    Every writing method is total, a failing write disabling capture and returning ``None``
    (AAP 0.4.7); ``ArtifactError`` is the one exception raised. A writer bound to one test writes
    into its directory, and the session-scoped writer answers for the test in flight.
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
        """Build a writer for one run; raises ``ArtifactError`` on a blank or escaping id."""
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
        """Return whether artifact capture failed for this run (AAP 0.4.7)."""
        return self._state.disabled

    @property
    def manifest(self) -> tuple[ArtifactRef, ...]:
        """Return the artifacts written for one test, sorted by ``(kind, path)``.

        A writer bound to one test answers for it; the session-scoped writer answers for the test
        in flight, which is the published member ``foundry_artifacts.manifest`` (AAP 0.4.3).

        Raises:
            ArtifactError: The writer is session scoped and no test is capturing artifacts.
        """
        if self._nodeid is None:
            if self._state.disabled:
                return ()
            return self._scoped().manifest
        return tuple(sorted(self._refs.values(), key=lambda ref: (ref.kind, ref.path)))

    def ensure_run_dir(self) -> bool:
        """Create this run's ``artifacts/<run_id>/`` directory and report whether capture is on.

        A directory that cannot be created is AAP 0.4.7's "artifacts directory not writable" row,
        reported once at ``WARNING``.
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
        """Return, and create, the artifact directory of one test; raises ``ArtifactError``."""
        directory = node_dir(self._run_dir, nodeid)
        if self._state.disabled:
            return directory
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._disable(f"cannot create artifact directory {directory}", exc)
        return directory

    def for_node(self, nodeid: str) -> ArtifactWriter:
        """Return a writer bound to one test that shares this run's settings and fault state."""
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

    def _scoped(self) -> ArtifactWriter:
        """Return the writer the published members address for the test in flight.

        ``self`` when this writer is already bound to one test, which is what keeps the delegation
        from recursing. The session-scoped writer answers through the bound writer the driver
        publishes for the running item (AAP 0.4.8), so ``foundry_artifacts.write`` and
        ``foundry_artifacts.manifest`` address the caller's own test directory as AAP 0.4.3
        publishes them, and the reference lands in that test's manifest and result record.

        Raises:
            ArtifactError: No test is capturing artifacts, so no per-test directory exists to write
                into; ``dir_for(nodeid)`` and ``for_node(nodeid)`` address one test from outside a
                test body.
        """
        if self._nodeid is not None:
            return self
        writer = active_writer()
        if writer is None or writer is self:
            raise ArtifactError(
                "no test is capturing artifacts, so the session-scoped writer has no test to "
                "write for",
                attempted_path=self._run_dir,
                run_dir=self._run_dir,
                hint=(
                    "the session-scoped writer answers write and manifest for the test in flight, "
                    "from inside a test body; dir_for(nodeid) and for_node(nodeid) address one "
                    "test's artifacts from anywhere else"
                ),
            )
        return writer

    def write(self, kind: ArtifactKind, name: str | None, data: str | bytes) -> ArtifactRef | None:
        """Redact ``data`` and write it as one artifact of the bound test (AAP 0.4.7).

        The reference comes back, or ``None`` while capture is disabled (AAP 0.4.3).

        Raises:
            ArtifactError: The writer is session scoped and no test is capturing artifacts.
        """
        if self._nodeid is None:
            if self._state.disabled:
                return None
            return self._scoped().write(kind, name, data)
        source = _decode(data)
        scrubbed = self._redactor.text(source)
        return self._store(kind, name, scrubbed, redacted=scrubbed != source)

    def write_json(
        self, kind: ArtifactKind, payload: object, name: str | None = None
    ) -> ArtifactRef | None:
        """Redact a JSON payload and write it as indented, key-sorted JSON.

        Raises ArtifactError when the writer is session scoped and no test is capturing artifacts.
        """
        if self._state.disabled:
            return None
        if self._nodeid is None:
            return self._scoped().write_json(kind, payload, name)
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
        """Redact captured text and write it as an artifact ending in one newline.

        Raises ArtifactError when the writer is session scoped and no test is capturing artifacts.
        """
        if self._state.disabled:
            return None
        if self._nodeid is None:
            return self._scoped().write_text(kind, text, name)
        scrubbed = self._redactor.text(text)
        return self._store(kind, name, _one_newline(scrubbed), redacted=scrubbed != text)

    def write_log_tail(self, lines: Sequence[str]) -> ArtifactRef | None:
        """Write the captured log tail as ``log_tail.log``; an empty sequence writes nothing.

        This is the single scrub of those lines: ``foundry.reporting.panels`` renders the written
        bytes as they stand, so no earlier pass may be dropped from here.
        """
        if not lines:
            return None
        scrubbed = [_scrub_log_line(line, self._redactor) for line in lines]
        text = _one_newline("\n".join(scrubbed))
        return self._store("log_tail", None, text, redacted=scrubbed != list(lines))

    def write_manifest(self) -> ArtifactRef | None:
        """Build and write this test's JSON ``manifest.json``, never redacted (AAP 0.4.6).

        The document carries the AAP 0.4.6 fields and every artifact but the manifest itself; a
        session-scoped writer with no test capturing artifacts raises ArtifactError.
        """
        if self._nodeid is None:
            if self._state.disabled:
                return None
            return self._scoped().write_manifest()
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
        return self._store("manifest", None, render(document), redacted=False)

    def finalize(self, *, passed: bool) -> tuple[ArtifactRef, ...]:
        """Finish one test's artifacts: prune a passing test, then write the manifest.

        Disabled capture skips the prune, still writes the manifest and returns ``()`` (AAP 0.4.7);
        a session-scoped writer with no test capturing artifacts raises ArtifactError.
        """
        if self._nodeid is None:
            if self._state.disabled:
                return ()
            return self._scoped().finalize(passed=passed)
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

        The digest is that of the bytes on disk; a capture fault still attempts ``"manifest"``.
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
                "this artifact writer is bound to no test, so it has no per-test directory",
                attempted_path=self._run_dir,
                run_dir=self._run_dir,
                hint=(
                    "the session-scoped writer answers write and manifest for the test in flight; "
                    "dir_for(nodeid) and for_node(nodeid) address one test's artifacts directly"
                ),
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


# --- The run this process's artifacts belong to -----------------------------

SESSION_RUN_ID: Final[pytest.StashKey[str]] = pytest.StashKey[str]()
"""The run id the plugin bootstrapped for this session, published for the fixtures that report it.

Declared here rather than beside the session state in :mod:`foundry.execution.state` because AAP
0.4.8 forbids ``foundry.fixtures`` importing ``foundry.execution``, and ``foundry.obs`` is the
lowest package both sides may reach: the module that names the run directory declares the one
session value a public fixture reads. The value keeps its AAP 0.4.8 home in ``config.stash``,
one declaration only because two would be two different keys, and ``stash_session`` stores it
beside the session state in every process, the xdist worker included, so one run publishes one
id and one ``artifacts/<run_id>/`` root (AAP 0.4.3, AAP 0.4.6).
"""


# --- The test whose artifacts a request may record into ---------------------

_ACTIVE_WRITER: Final[ContextVar[ArtifactWriter | None]] = ContextVar(
    "foundry_active_artifact_writer", default=None
)
"""The writer of the test currently executing, for a caller that holds no item (AAP 0.4.8).

A :class:`~contextvars.ContextVar` rather than a module global: it is per execution context, so two
worker processes or a later threaded runner keep their own, it needs no lock and there is no
import-time state to reset; the value is the *bound* writer of the test in flight.
"""


def set_active_writer(writer: ArtifactWriter | None) -> None:
    """Make ``writer`` the writer of the test in flight, or unbind with ``None``.

    ``foundry.execution.coordinator`` binds it before an item's first attempt and unbinds it after
    the last, so nothing another test writes lands here. Overwriting rather than resetting a token
    is deliberate: a writer bound for a test that never finished would be worse.
    """
    _ACTIVE_WRITER.set(writer)


def active_writer() -> ArtifactWriter | None:
    """Return the writer of the test in flight, or ``None`` when no test is capturing.

    ``None`` is the ordinary answer outside a test and for a run whose artifact directory could not
    be created, and every caller treats it as "record nothing" rather than as a fault.
    """
    return _ACTIVE_WRITER.get()


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


def _scrub_log_line(line: str, redactor: Redactor) -> str:
    """Scrub a log line, keeping a Foundry record's ``<LEVELNAME> <logger>: `` label in place."""
    head, sep, message = line.partition(": ")
    level, _, logger = head.partition(" ")
    if sep and level.isupper() and level.isalpha() and logger.split(".")[0] == "foundry":
        return f"{redactor.text(head)}{sep}{redactor.text(message)}"
    return redactor.text(line)


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
