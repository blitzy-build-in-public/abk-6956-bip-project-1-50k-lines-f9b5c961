"""The ``results.json`` store: the writer and the reader of a run's result document.

:func:`write_text` is the single text writer of the whole ``foundry.reporting`` package (AAP 0.4.8),
so the JUnit writer and the HTML renderer hand it their text rather than opening a file. Records are
sorted by node id and run errors by worker id and message, never by ``recorded_at``, so two runs of
one suite are byte-identical once the normalised fields are replaced (AAP 0.14.1), and every
free-text value passes through :class:`foundry.obs.redaction.Redactor` first (AAP 0.3.2). A refused
write reports one ``WARNING`` and returns ``None`` (AAP 0.14.3); :func:`read` raises ``DataError``
so ``foundry report compare`` can exit ``2`` (AAP 0.4.4).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from foundry.core.exceptions import DataError, ElementTypeError
from foundry.obs.events import emit_plumbing_failure, emit_report_written
from foundry.obs.redaction import REDACTED, Redactor, default_redactor
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

_TEMP_SUFFIX: Final[str] = ".tmp"
"""Suffix of the sibling file a write goes through before it is renamed into place."""

_DOCUMENT_HINT: Final[str] = (
    "results.json holds a JSON array of objects whose record_type is 'result' or 'run_error'; "
    "regenerate it with a Foundry run"
)
"""Next step offered with every :class:`DataError` raised by :func:`loads`."""

_REDACTION_FAULT: Final[str] = (
    "could not redact a value, so the document carries the redaction marker instead"
)
"""Logged when scrubbing one value raised; the value is replaced rather than written (AAP 0.1.2)."""


def dumps(elements: Sequence[ResultElement], *, redactor: Redactor | None = None) -> str:
    """Render result elements as the exact text of a ``results.json`` document.

    Result records come first, sorted by node id, and the run-error elements follow them, sorted by
    worker id and message, so the document is the same whoever wrote it.

    Args:
        elements: The run's elements: result records, run errors, or both in any order.
        redactor: The run's redactor, built from ``secrets.redact_keys``. ``None`` uses the
            built-in header and query deny-lists.

    Returns:
        The document as text: a JSON array indented by two spaces, with sorted keys, non-ASCII
        characters unescaped and a trailing newline.

    Raises:
        ElementTypeError: An element is neither a :class:`ResultRecord` nor a :class:`RunError`.
        TypeError: An element serialises to a value that is not JSON-native, which is a defect in
            the record models rather than a filesystem fault, so it is not absorbed.
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
    """Decode a stored ``results.json`` document into its validated elements, in document order.

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

    Returns:
        The path written, or ``None`` when the write failed and was reported at ``WARNING``.
    """
    return write_text(dumps(records, redactor=redactor), Path(path))


def write_text(text: str, path: str | Path) -> Path | None:
    """Write ``text`` to ``path`` as UTF-8 with LF line endings: this package's only writer.

    The content goes to a sibling temporary file that is renamed into place, so a reader never sees
    a half-written document and a re-run of the same content produces identical bytes. Text UTF-8
    cannot encode -- a lone surrogate decoded from a path, say -- is written with the ``replace``
    handler and reported as one ``WARNING`` naming ``path``, so the character degrades instead of
    the document being lost and the degradation is not silent.

    Returns:
        The path written, or ``None`` when the write was refused. A refusal is reported once at
        ``WARNING``, leaves no temporary file behind and changes neither a test's outcome nor the
        run's exit code (AAP 0.14.3).
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
    """Read a result document from disk into its validated elements, in document order.

    Unlike :func:`write`, this raises: a document a person asked to compare cannot be silently
    absent, and the command line turns the failure into exit code ``2``.

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


def _scrubbed(scrub: Redactor, text: str) -> str:
    """Return ``text`` redacted, or the redaction marker when the scrubber itself raised.

    The document is the run's machine-readable record, so one unscrubbable value costs that value
    and one ``WARNING`` rather than the whole file: the value is replaced, and nothing the scrubber
    could not clean is written anywhere (AAP 0.1.2, 0.14.3).
    """
    try:
        return scrub.text(text)
    except Exception as error:
        # Failure-isolation rule: an unscrubbable value is replaced, never written as it stands.
        emit_plumbing_failure(_COMPONENT, _REDACTION_FAULT, exc=error)
        return REDACTED


def _redact_record(record: ResultRecord, scrub: Redactor) -> dict[str, object]:
    """Return ``record``'s serialised form with every free-text value redacted.

    Only the fields that can quote run data are scrubbed, and each is replaced where the record
    already carries it, so the document keeps exactly the shape
    :meth:`foundry.reporting.record.ResultRecord.to_dict` defines: a node id, an outcome, a
    duration or a marker is a contract value that redaction must not touch.
    """
    payload = record.to_dict()
    payload["failures"] = [_redact_failure(failure, scrub) for failure in record.failures]
    payload["artifacts"] = [_redact_artifact(artifact, scrub) for artifact in record.artifacts]
    return payload


def _redact_failure(failure: FailureRecord, scrub: Redactor) -> dict[str, object]:
    """Return one failure entry with its message and its traceback path redacted."""
    payload = failure.to_dict()
    payload["message"] = _scrubbed(scrub, failure.message)
    if failure.traceback_path is not None:
        payload["traceback_path"] = _scrubbed(scrub, failure.traceback_path)
    return payload


def _redact_artifact(artifact: ArtifactRecord, scrub: Redactor) -> dict[str, object]:
    """Return one artifact reference with its run-relative path redacted.

    A node id can carry a name that looks like a credential's, and the path is built from one, so
    the path is scrubbed like any other text before it reaches the document.
    """
    payload = artifact.to_dict()
    payload["path"] = _scrubbed(scrub, artifact.path)
    return payload


def _redact_run_error(error: RunError, scrub: Redactor) -> dict[str, object]:
    """Return one run-error element with its message redacted."""
    payload = error.to_dict()
    payload["message"] = _scrubbed(scrub, error.message)
    return payload


# --- Filesystem ------------------------------------------------------------


def _is_encodable(text: str) -> bool:
    """Return whether UTF-8 can encode ``text`` as it stands.

    Asking first lets :func:`write_text` report a degradation with a single ``WARNING``.
    The answer depends on the text alone, so the write stays deterministic (AAP 0.14.1).
    """
    try:
        text.encode(_ENCODING)
    except UnicodeEncodeError:
        return False
    return True


def _discard(temporary: Path) -> None:
    """Remove a temporary file a failed write left behind, ignoring a second failure.

    A write that could not be renamed into place must not leave a stray ``.tmp`` beside the
    document, and if the file cannot be removed either there is nothing further to report without
    turning one isolated failure into two. A name that cannot be encoded is covered as well as a
    filesystem refusal, so the cleanup is never the step that lets that fault reach the session
    (AAP 0.1.2, AAP 0.14.3).
    """
    try:
        temporary.unlink(missing_ok=True)
    except (OSError, UnicodeEncodeError):
        # Failure-isolation rule: the warning for the original fault has already been emitted, so
        # a refused cleanup ends the attempt quietly rather than raising into the pytest session.
        return
