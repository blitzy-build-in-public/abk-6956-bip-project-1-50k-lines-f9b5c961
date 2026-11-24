"""The ``results.json`` store: the writer and the reader of a run's result document.

This module owns the run's result document. :func:`dumps` renders it, :func:`write` writes it,
:func:`loads` and :func:`read` decode one back, and :func:`write_text` is the single text writer
of the whole ``foundry.reporting`` package: the JUnit writer and the HTML renderer build their
documents as text and hand them here rather than opening a file themselves, because filesystem
writes are permitted in three modules only -- :mod:`foundry.obs.artifacts`,
:mod:`foundry.cassette.store` and this one (AAP 0.4.8).

Ordering and determinism
    Two runs of the same tests must produce the same bytes once ``run_id``, ``started_at``,
    ``finished_at`` and ``duration_ms`` are replaced (AAP 0.14.1 criterion 14), so nothing here
    may depend on the order a caller collected its records in. Result records are sorted by node
    id; the run-error elements follow every result record and are sorted by worker id and message,
    never by their ``recorded_at``, which the determinism criterion does not normalise. The file is
    a JSON array indented by two spaces, with object keys sorted, non-ASCII characters left
    unescaped, UTF-8 encoding and a trailing newline. An empty run is an empty array plus that
    newline, so the zero-tests and all-deselected states carry a valid document rather than a
    missing file.

Redaction
    Every value that can quote run data -- a failure message, a traceback path, an artifact path
    and a run error's message -- passes through :class:`foundry.obs.redaction.Redactor` before it
    is serialised, because a header or a URL echoed into a failure would otherwise be persisted
    verbatim (AAP 0.3.2). Contract values are deliberately left alone: redacting a node id would
    break ``foundry report compare``, and a duration or an outcome is not free text. Redaction is
    shape-preserving -- it replaces the value of a field the record already has and adds none --
    so the document stays exactly the schema :mod:`foundry.reporting.record` defines.

Failure isolation
    Writing is plumbing, and plumbing never changes a test's outcome or a run's exit code
    (AAP 0.14.3). A writer that cannot create its directory, cannot open its temporary file or
    cannot rename it reports one ``WARNING`` on the ``foundry`` logger and returns ``None``; it
    never raises into the pytest session and never calls ``sys.exit``. Both :exc:`OSError` and
    :exc:`UnicodeEncodeError` are isolated that way, so an encoding fault costs one document rather
    than every document after it. Text is written with the ``replace`` error handler, so a
    character UTF-8 cannot represent -- a lone surrogate decoded from a path with undecodable
    bytes, which a node id or a failure message can carry -- becomes the replacement character
    instead of losing the file, and the same input still produces the same bytes (AAP 0.14.1).
    Because that degradation must not be silent, it is reported as one ``WARNING`` naming the file
    while the write completes and returns its path. What is *not* isolated is a ``TypeError`` from
    ``json.dumps``: that is a defect in the record models, not a filesystem fault, and it surfaces
    instead of being hidden.

Reading
    :func:`read` is the one path the command line drives on a document it did not write, so every
    way it can fail -- a missing file, a directory, undecodable bytes, text that is not JSON, a
    JSON document that is not an array, an element that is not a Foundry result element -- becomes
    :class:`foundry.core.exceptions.DataError` naming the file. It is a typed ``FoundryError``,
    which is what lets ``foundry report compare`` exit ``2`` instead of crashing (AAP 0.4.4).

Import direction and import-time discipline
    :mod:`foundry.reporting` imports ``foundry.core`` and ``foundry.obs`` and no capability
    package (AAP 0.4.8), and importing this module only defines names: no I/O, no environment
    read, no logger configuration and no module-level mutable state.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from foundry.core.exceptions import DataError, ElementTypeError
from foundry.obs.events import emit_plumbing_failure, emit_report_written
from foundry.obs.redaction import Redactor, default_redactor
from foundry.reporting.record import (
    ArtifactRecord,
    FailureRecord,
    ResultElement,
    ResultRecord,
    RunError,
    parse_element,
)

__all__: Final[tuple[str, ...]] = ("dumps", "loads", "read", "write", "write_text")

_COMPONENT: Final[str] = "reporting.store"
"""Name this module reports itself under in a plumbing-failure event."""

_ENCODING: Final[str] = "utf-8"
"""Encoding of every document written and read here, so a non-ASCII test id survives."""

_NEWLINE: Final[str] = "\n"
"""Line terminator imposed on write, so a document is identical on any platform."""

_INDENT: Final[int] = 2
"""Indent width of the document: two spaces, one level per nesting depth."""

_TEMP_SUFFIX: Final[str] = ".tmp"
"""Suffix of the sibling file a write goes through before it is renamed into place."""

_DOCUMENT_HINT: Final[str] = (
    "results.json holds a JSON array of objects whose record_type is 'result' or 'run_error'; "
    "regenerate it with a Foundry run"
)
"""Next step offered with every :class:`DataError` raised by :func:`loads`."""


def dumps(elements: Sequence[ResultElement], *, redactor: Redactor | None = None) -> str:
    """Render result elements as the exact text of a ``results.json`` document.

    Result records come first, sorted by node id, and the run-error elements follow them, sorted
    by worker id and message. The sort keys are the records' own content, so the document is the
    same whoever wrote it and whichever worker produced which record.

    Args:
        elements: The run's elements: result records, run errors, or both in any order.
        redactor: The run's redactor, built from ``secrets.redact_keys``. ``None`` uses the
            built-in header and query deny-lists.

    Returns:
        The document as text: a JSON array indented by two spaces, with sorted keys, non-ASCII
        characters unescaped and a trailing newline. An empty ``elements`` yields the empty array
        plus that newline.

    Raises:
        ElementTypeError: An element is neither a
            :class:`~foundry.reporting.record.ResultRecord` nor a
            :class:`~foundry.reporting.record.RunError`. It is a
            :class:`~foundry.core.exceptions.FoundryError` that subclasses ``TypeError``, so a
            caller catching the documented caller defect still catches it, and it is not
            absorbed: a caller defect is not a filesystem fault.
        TypeError: An element serialises to a value that is not JSON-native, which ``json.dumps``
            rejects. That is a defect in the record models rather than a filesystem fault, so it
            is not absorbed either.
    """
    scrub = default_redactor() if redactor is None else redactor
    records: list[ResultRecord] = []
    errors: list[RunError] = []
    for element in elements:
        if isinstance(element, ResultRecord):
            records.append(element)
        elif isinstance(element, RunError):
            errors.append(element)
        else:
            raise ElementTypeError(
                "results.json holds only ResultRecord and RunError elements, "
                f"got {type(element).__name__}",
                hint="pass ResultRecord and RunError elements, as the run's recorder produces them",
            )
    document: list[dict[str, object]] = [
        _redact_record(record, scrub) for record in sorted(records, key=_record_sort_key)
    ]
    document.extend(
        _redact_run_error(error, scrub) for error in sorted(errors, key=_run_error_sort_key)
    )
    return json.dumps(document, indent=_INDENT, sort_keys=True, ensure_ascii=False) + _NEWLINE


def loads(text: str, *, source: Path) -> tuple[ResultElement, ...]:
    """Decode a stored ``results.json`` document into its validated elements.

    Args:
        text: The document's text, as :func:`read` obtained it.
        source: The document's path, named in the failure so a person can find the file.

    Returns:
        Every element of the document, in the order the document lists them.

    Raises:
        DataError: The text is not JSON, is not a JSON array, holds an element that is not an
            object, or holds an element that is not a Foundry result element. The message and the
            ``file`` attribute both name ``source``.
    """
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DataError(
            f"not a Foundry result document: {source}: not valid JSON: "
            f"{exc.msg} (line {exc.lineno}, column {exc.colno})",
            file=source,
            hint=_DOCUMENT_HINT,
        ) from exc
    if not isinstance(document, list):
        raise DataError(
            f"not a Foundry result document: {source}: expected a JSON array, "
            f"got {type(document).__name__}",
            file=source,
            hint=_DOCUMENT_HINT,
        )
    elements: list[ResultElement] = []
    for index, entry in enumerate(document):
        if not isinstance(entry, Mapping):
            raise DataError(
                f"not a Foundry result document: {source}: element {index} is not an object",
                file=source,
                hint=_DOCUMENT_HINT,
            )
        if "record_type" not in entry:
            raise DataError(
                f"not a Foundry result document: {source}: element {index} has no record_type",
                file=source,
                hint=_DOCUMENT_HINT,
            )
        elements.append(parse_element(entry, source=source))
    return tuple(elements)


def write(
    records: Sequence[ResultElement],
    path: str | Path,
    *,
    redactor: Redactor | None = None,
) -> Path | None:
    """Write the run's result document to ``path``, creating its directory if needed.

    The parent directory is created by :func:`write_text` inside its guarded region, so a path
    that cannot be created is isolated the same way a path that cannot be written is.

    Args:
        records: The run's elements, in any order; :func:`dumps` imposes the document's order.
        path: The document to write, normally ``results.json`` in the invocation directory or the
            path ``--foundry-json-report`` named.
        redactor: The run's redactor, or ``None`` for the built-in deny-lists.

    Returns:
        The path written, or ``None`` when the write failed and was reported at ``WARNING``.
    """
    return write_text(dumps(records, redactor=redactor), Path(path))


def write_text(text: str, path: str | Path) -> Path | None:
    """Write ``text`` to ``path`` as UTF-8 with LF line endings: this package's only writer.

    The JUnit writer and the HTML renderer return their documents as text and call this rather
    than opening a file, so every file :mod:`foundry.reporting` produces is written by one
    implementation (AAP 0.4.8). The content is written to a sibling temporary file and renamed
    into place, so a reader never sees a half-written document and a re-run of the same content
    produces byte-identical bytes.

    Text that UTF-8 cannot encode -- a lone surrogate, as a node id or a failure message decoded
    from a path with undecodable bytes carries -- is written with the ``replace`` error handler, so
    the character degrades into the replacement character rather than the document being lost. The
    degradation is reported as one ``WARNING`` on the ``foundry`` logger naming ``path``, and the
    write still completes.

    Args:
        text: The document's complete text, written as given -- including any trailing newline.
        path: The file to write. Missing parent directories are created.

    Returns:
        The path written, or ``None`` when the write was refused. A refusal -- a filesystem fault
        or, should one reach the encoder despite the ``replace`` handler, an encoding fault -- is a
        plumbing fault: it is reported once at ``WARNING`` on the ``foundry`` logger, no temporary
        file is left behind, and neither a test's outcome nor the run's exit code changes
        (AAP 0.14.3).
    """
    target = Path(path)
    temporary = target.with_name(target.name + _TEMP_SUFFIX)
    degraded = not _is_encodable(text)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("w", encoding=_ENCODING, newline=_NEWLINE, errors="replace") as handle:
            handle.write(text)
        temporary.replace(target)
    except (OSError, UnicodeEncodeError) as exc:
        emit_plumbing_failure(_COMPONENT, f"could not write report file {target}", exc=exc)
        _discard(temporary)
        return None
    if degraded:
        emit_plumbing_failure(
            _COMPONENT,
            f"report file {target} carries characters {_ENCODING} cannot encode, "
            "written as replacement characters",
        )
    emit_report_written(target.name, str(target))
    return target


def read(path: str | Path) -> tuple[ResultElement, ...]:
    """Read a result document from disk.

    Unlike :func:`write`, this raises: a document a person asked to compare cannot be silently
    absent, and the command line turns the failure into exit code ``2``.

    Args:
        path: The document to read, normally a ``results.json`` written by a previous run.

    Returns:
        Every element of the document, in the order the document lists them.

    Raises:
        DataError: The file cannot be read, cannot be decoded as UTF-8, or is not a Foundry result
            document. The message and the ``file`` attribute both name ``path``.
    """
    source = Path(path)
    try:
        text = source.read_text(encoding=_ENCODING)
    except OSError as exc:
        raise DataError(
            f"cannot read result document: {source}: {exc.strerror or type(exc).__name__}",
            file=source,
            hint="pass a results.json written by a Foundry run",
        ) from exc
    except UnicodeDecodeError as exc:
        raise DataError(
            f"not a Foundry result document: {source}: not valid UTF-8: {exc.reason}",
            file=source,
            hint=_DOCUMENT_HINT,
        ) from exc
    return loads(text, source=source)


# --- Ordering --------------------------------------------------------------


def _record_sort_key(record: ResultRecord) -> str:
    """Return the node id a result record is sorted by.

    Node ids are unique within a session, so the order is total and independent of the worker
    count: it is what makes two runs' documents comparable.
    """
    return record.nodeid


def _run_error_sort_key(error: RunError) -> tuple[str, str]:
    """Return the ``(worker_id, message)`` pair a run error is sorted by.

    ``recorded_at`` is deliberately not the key: the determinism criterion normalises the four
    timestamp fields and not this one, so ordering by it would make two runs differ.
    """
    return (error.worker_id, error.message)


# --- Redaction -------------------------------------------------------------


def _redact_record(record: ResultRecord, scrub: Redactor) -> dict[str, object]:
    """Return ``record``'s serialised form with every free-text value redacted.

    Only the fields that can quote run data are scrubbed, and each is replaced where the record
    already carries it, so the document keeps exactly the shape
    :meth:`foundry.reporting.record.ResultRecord.to_dict` defines: a node id, an outcome, a
    duration or a marker is a contract value that redaction must not touch.

    Args:
        record: The result record to serialise.
        scrub: The redactor to apply.

    Returns:
        The record as a JSON-ready mapping, with its failure messages, traceback paths and
        artifact paths redacted.
    """
    payload = record.to_dict()
    payload["failures"] = [_redact_failure(failure, scrub) for failure in record.failures]
    payload["artifacts"] = [_redact_artifact(artifact, scrub) for artifact in record.artifacts]
    return payload


def _redact_failure(failure: FailureRecord, scrub: Redactor) -> dict[str, object]:
    """Return one failure entry with its message and its traceback path redacted.

    Args:
        failure: The failure entry to serialise.
        scrub: The redactor to apply.

    Returns:
        The entry as a JSON-ready mapping, with the two text fields scrubbed and ``phase``
        untouched.
    """
    payload = failure.to_dict()
    payload["message"] = scrub.text(failure.message)
    if failure.traceback_path is not None:
        payload["traceback_path"] = scrub.text(failure.traceback_path)
    return payload


def _redact_artifact(artifact: ArtifactRecord, scrub: Redactor) -> dict[str, object]:
    """Return one artifact reference with its run-relative path redacted.

    A node id can carry a name that looks like a credential's, and the path is built from one, so
    the path is scrubbed like any other text before it reaches the document.

    Args:
        artifact: The artifact reference to serialise.
        scrub: The redactor to apply.

    Returns:
        The reference as a JSON-ready mapping, with its path scrubbed and ``bytes`` untouched.
    """
    payload = artifact.to_dict()
    payload["path"] = scrub.text(artifact.path)
    return payload


def _redact_run_error(error: RunError, scrub: Redactor) -> dict[str, object]:
    """Return one run-error element with its message redacted.

    Args:
        error: The run error to serialise.
        scrub: The redactor to apply.

    Returns:
        The element as a JSON-ready mapping, with its message scrubbed.
    """
    payload = error.to_dict()
    payload["message"] = scrub.text(error.message)
    return payload


# --- Filesystem ------------------------------------------------------------


def _is_encodable(text: str) -> bool:
    """Return whether UTF-8 can encode ``text`` as it stands.

    Asking before the file is opened is what lets :func:`write_text` report a degradation with one
    ``WARNING``: the document is encoded once here to find out, and the write itself is what
    imposes the ``replace`` handler, so nothing has to be re-read afterwards to compare. The
    answer depends on the text alone, so it keeps the write deterministic (AAP 0.14.1).
    """
    try:
        text.encode(_ENCODING)
    except UnicodeEncodeError:
        return False
    return True


def _discard(temporary: Path) -> None:
    """Remove a temporary file a failed write left behind, ignoring a second failure.

    A write that could not be renamed into place must not leave a stray ``.tmp`` beside the
    document, and the cleanup is itself plumbing: if the file cannot be removed either, there is
    nothing further to report without turning one isolated failure into two. A name that cannot be
    encoded is covered as well as a filesystem refusal, because the undecodable bytes that make a
    document's text unrepresentable can make its path unrepresentable too, and the cleanup must not
    be the step that lets that fault reach the session (AAP 0.1.2, AAP 0.14.3).
    """
    try:
        temporary.unlink(missing_ok=True)
    except (OSError, UnicodeEncodeError):
        # Failure-isolation rule: the warning for the original fault has already been emitted, so
        # a refused cleanup ends the attempt quietly rather than raising into the pytest session.
        return
