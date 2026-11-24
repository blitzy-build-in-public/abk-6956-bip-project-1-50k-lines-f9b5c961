"""The cassette store: the one module that turns captured interactions into a file and back.

``foundry.cassette`` owns record/replay (AAP 0.3.2), and :class:`CassetteStore` is the only
component that touches the filesystem in it: :meth:`CassetteStore.load` reads the JSON document
AAP 0.4.6 fixes for one test, :meth:`CassetteStore.save` writes it, and
:func:`~foundry.cassette.paths.cassette_path` decides where it lives. Nothing else in the package
opens a file, so a reader looking for what a cassette run does to disk has one class to read.

Redaction happens here, at capture, and nowhere later
    A recorded cassette must not hold ``Authorization``, ``Proxy-Authorization``, ``Cookie``,
    ``Set-Cookie``, ``X-Api-Key`` or any name in ``secrets.redact_keys`` (implicit requirement
    I4, AAP 0.3.2 "Security architecture"). :func:`redact_interaction` therefore replaces every
    denied header value with :data:`~foundry.obs.redaction.REDACTED` and scrubs a JSON response
    body structure-first, recording the names it scrubbed in the document's ``redacted_keys``.
    :class:`~foundry.obs.redaction.Redactor` decides what is secret; no deny-list is restated
    here, because a second copy of it would drift from the engine every other sink uses.

    Two values deliberately survive untouched. The request body itself is never stored -- only its
    SHA-256, which is an inherent redaction win -- and that hash is copied from the capture rather
    than recomputed, so a body holding a secret still matches on replay while the secret is never
    written. A request's query pairs are stored as recorded, because the matcher compares them:
    rewriting a value here would make a cassette unplayable for the very request that produced it,
    so what a capture keeps at all is decided on the capture side, not at this write.

Both directions fail in the way their caller can act on
    ``load`` fails loudly (AAP 0.14.3): a cassette that is not valid JSON, is not an object, or
    does not match the documented shape raises :class:`~foundry.core.exceptions.FoundryError`
    naming the file and the offending key, because a truncated or hand-edited cassette must not
    replay as an empty recording. ``save`` fails by the failure-isolation rule (AAP 0.1.2): an
    unwritable directory is reported as exactly one ``WARNING`` on the ``foundry`` logger and the
    write is abandoned, because a cassette that could not be recorded must never turn a passing
    test into an error. :class:`~foundry.core.exceptions.CassetteMissError` belongs to replay and
    is raised by the transport, never here.

Deterministic bytes
    One serialiser writes every document (:func:`foundry.cassette.models.to_json_text`): sorted
    keys, two-space indent, readable non-ASCII, exactly one trailing newline, UTF-8 (AAP 0.13.1).
    Two runs of one recording therefore produce byte-identical files apart from ``recorded_at``.

Import direction and import-time discipline
    The standard library, :mod:`foundry.core`, :mod:`foundry.obs` and the two sibling modules
    (AAP 0.4.8). ``foundry.config`` is never imported: the cassette directory and the
    :class:`~foundry.obs.redaction.Redactor` arrive as constructor arguments, as the resolved
    configuration reaches every sink. Importing the module performs no I/O, reads no environment
    variable, configures no logger and keeps no module-level state.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Final

from foundry.cassette.models import (
    CASSETTE_ENCODING,
    CassetteDocument,
    Interaction,
    RecordedRequest,
    RecordedResponse,
    empty_document,
    from_json_text,
    to_json_text,
)
from foundry.cassette.paths import DEFAULT_CASSETTE_DIR, cassette_path
from foundry.core.exceptions import FoundryError
from foundry.core.ids import utc_now
from foundry.obs.events import emit_plumbing_failure
from foundry.obs.redaction import MAX_DEPTH, REDACTED, Redactor, default_redactor

__all__: Final[tuple[str, ...]] = (
    "CassetteStore",
    "redact_interaction",
    "redact_interactions",
)

_COMPONENT: Final[str] = "cassette.store"
"""The component name :func:`~foundry.obs.events.emit_plumbing_failure` reports."""

_RECORD_HINT: Final[str] = "re-record the cassette with --foundry-record"
"""The next step a malformed-cassette error offers, when the shape error carries none of its own."""

_PLUMBING_ERRORS: Final[tuple[type[BaseException], ...]] = (
    FoundryError,
    TypeError,
    ValueError,
    KeyError,
    AttributeError,
    RecursionError,
    RuntimeError,
    OSError,
)
"""Faults :meth:`CassetteStore.save` reports as a warning instead of raising.

The same band :mod:`foundry.obs.events` treats as isolation faults: a value that cannot be
coerced, a structure that cannot be walked, or a filesystem that refuses the write. A redaction
fault is a fault in Foundry's own plumbing, so it may not change a test's outcome (AAP 0.1.2).
"""


# --- Redaction at capture --------------------------------------------------


def _redact_headers(
    headers: Mapping[str, str], redactor: Redactor, *, found: set[str]
) -> dict[str, str]:
    """Return ``headers`` with every secret-bearing value replaced by the marker.

    The replacement and the recording of the scrubbed name happen in the same loop, so a name can
    never be reported as redacted without being redacted. What counts as secret is asked of the
    redactor, never restated here, and the marker is :data:`~foundry.obs.redaction.REDACTED` --
    the exact form the acceptance criteria require in every produced file. Names are recorded
    case-folded, because a header name's case is not significant and one run spelling it
    ``Authorization`` and another ``authorization`` must record the same ``redacted_keys``.

    Args:
        headers: The header mapping as captured. It is never mutated.
        redactor: The redaction engine built from ``secrets.redact_keys``.
        found: The accumulator the scrubbed names are added to.

    Returns:
        A new mapping of the same header names, case-insensitively ordered, with every
        secret-bearing value replaced.
    """
    redacted: dict[str, str] = {}
    for name in sorted(headers, key=str.casefold):
        if redactor.redacts(name):
            found.add(name.strip().casefold())
            redacted[name] = REDACTED
        else:
            redacted[name] = headers[name]
    return redacted


def _collect_secret_names(
    value: object, redactor: Redactor, *, found: set[str], depth: int = 0
) -> None:
    """Add the secret-bearing key names anywhere inside a JSON value to ``found``.

    The walk mirrors :meth:`~foundry.obs.redaction.Redactor.json_value`, which is what scrubs the
    values: the same maximum depth, the same recursion over mappings and sequences, and the same
    question asked of every key. Names are recorded case-folded for the reason
    :func:`_redact_headers` gives.

    Args:
        value: A JSON-shaped value, as :func:`json.loads` returned it.
        redactor: The redaction engine that decides whether a key is secret-bearing.
        found: The accumulator the scrubbed names are added to.
        depth: The current recursion depth; a caller leaves it at ``0``.
    """
    if depth >= MAX_DEPTH:
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            name = key if isinstance(key, str) else str(key)
            if redactor.redacts(name):
                found.add(name.strip().casefold())
            _collect_secret_names(item, redactor, found=found, depth=depth + 1)
        return
    if isinstance(value, list | tuple):
        for item in value:
            _collect_secret_names(item, redactor, found=found, depth=depth + 1)


def _canonical_json(value: object) -> str:
    """Render a redacted JSON value as compact, key-sorted text.

    Sorting the keys is what makes the stored body independent of the order the peer happened to
    serialise it in, so two recordings of one interaction produce the same bytes (AAP 0.13.1).
    """
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _redact_body(body: str, redactor: Redactor) -> tuple[str, frozenset[str]]:
    """Return the response body to store and the key names scrubbed out of it.

    A body that parses as JSON is redacted structure-first, so a secret-bearing key loses its value
    however deeply it is nested, and the scrubbed key names are reported for ``redacted_keys``. A
    body that is not JSON -- empty, HTML, plain text -- carries no keys to name, so it goes through
    the redactor's text path, which scrubs header lines, ``Bearer`` credentials, embedded URLs and
    ``name=value`` pairs.

    A JSON body in which nothing secret was found is stored exactly as the peer sent it: rewriting
    every recorded body would make a replay differ from the live response for no benefit.

    Args:
        body: The response body as text.
        redactor: The redaction engine to scrub it with.

    Returns:
        The body to persist and the frozenset of key names that were scrubbed from it.
    """
    try:
        parsed: object = json.loads(body)
    except ValueError:
        # ``json.JSONDecodeError`` is a ``ValueError``, and the default ``parse_constant`` of
        # ``json.loads`` rejects the non-standard literals, so anything unparsable lands here.
        return redactor.text(body), frozenset()
    scrubbed = redactor.json_value(parsed)
    if scrubbed == parsed:
        return body, frozenset()
    found: set[str] = set()
    _collect_secret_names(parsed, redactor, found=found)
    return _canonical_json(scrubbed), frozenset(found)


def _scrub_interaction(
    interaction: Interaction, redactor: Redactor
) -> tuple[Interaction, frozenset[str]]:
    """Return one interaction with every secret-bearing value replaced, and the names scrubbed.

    The single place a capture is redacted and the single place its scrubbed names are collected,
    so ``redacted_keys`` can never disagree with the document it describes.

    Args:
        interaction: The interaction as captured.
        redactor: The redaction engine to scrub it with.

    Returns:
        The redacted interaction and the frozenset of names that were scrubbed.
    """
    found: set[str] = set()
    request = interaction.request
    response = interaction.response
    body, body_names = _redact_body(response.body, redactor)
    found.update(body_names)
    return (
        Interaction(
            # ``body_sha256`` is copied from the capture, never recomputed from the redacted
            # body: it is the hash of the request as it went on the wire, computed by
            # ``transport.fingerprint_from_request``, so replay matching is unaffected by
            # redaction and the body itself is never persisted.
            request=RecordedRequest(
                method=request.method,
                path=request.path,
                query=request.query,
                body_sha256=request.body_sha256,
                headers_subset=_redact_headers(request.headers_subset, redactor, found=found),
            ),
            response=RecordedResponse(
                status=response.status,
                headers=_redact_headers(response.headers, redactor, found=found),
                body=body,
            ),
        ),
        frozenset(found),
    )


def redact_interaction(interaction: Interaction, redactor: Redactor) -> Interaction:
    """Return ``interaction`` with every secret-bearing header and body value replaced.

    Args:
        interaction: The interaction as captured. It is never mutated: a new
            :class:`~foundry.cassette.models.Interaction` is returned.
        redactor: The redaction engine built from ``secrets.redact_keys``.

    Returns:
        The redacted interaction, ready to store. The request's ``body_sha256`` is unchanged.
    """
    return _scrub_interaction(interaction, redactor)[0]


def redact_interactions(
    interactions: Iterable[Interaction], redactor: Redactor
) -> tuple[tuple[Interaction, ...], tuple[str, ...]]:
    """Redact a capture and return it with the sorted, de-duplicated names it scrubbed.

    Both values a cassette needs come out of one pass, which is why ``save`` has a single call to
    make: the redacted interactions in recording order, and the ``redacted_keys`` that belong with
    them.

    Args:
        interactions: The interactions in the order the test issued them.
        redactor: The redaction engine built from ``secrets.redact_keys``.

    Returns:
        The redacted interactions as a tuple, and the sorted tuple of scrubbed names.
    """
    redacted: list[Interaction] = []
    names: set[str] = set()
    for interaction in interactions:
        scrubbed, found = _scrub_interaction(interaction, redactor)
        redacted.append(scrubbed)
        names.update(found)
    return tuple(redacted), tuple(sorted(names))


# --- Reading and writing ---------------------------------------------------


def _as_directory(value: str | Path) -> Path:
    """Return a cassette directory as a :class:`~pathlib.Path`.

    Raises:
        FoundryError: ``value`` is a blank or whitespace-only string. The path itself is taken
            as given rather than stripped, so a directory name is never silently altered.
    """
    if isinstance(value, str):
        if not value.strip():
            raise FoundryError(
                "cassette directory must not be blank",
                hint="pass the resolved cassette.dir, or omit it for 'cassettes'",
            )
        return Path(value)
    return Path(value)


class CassetteStore:
    """The cassette documents of one run, read and written through one directory.

    A store holds the resolved ``cassette.dir`` and the redactor every write passes through, and
    answers the three questions the record/replay paths ask: where a test's cassette lives
    (:meth:`path_for`), whether it exists (:meth:`exists`) and what it holds (:meth:`load`). The
    two write methods funnel into :meth:`save_document`, so the tree has exactly one place that
    creates a directory and writes a cassette file (AAP 0.4.8 "Where state may be held").

    Attributes:
        _cassette_dir: The directory the documents live in, as given to the constructor.
        _redactor: The redaction engine applied by :meth:`save`.
    """

    _cassette_dir: Path
    _redactor: Redactor

    def __init__(
        self,
        cassette_dir: str | Path = DEFAULT_CASSETTE_DIR,
        redactor: Redactor | None = None,
    ) -> None:
        """Bind the store to a cassette directory and a redactor.

        Args:
            cassette_dir: The resolved ``cassette.dir``. A relative value is resolved against the
                invocation directory when a path is built, so nothing is resolved here. Defaults
                to :data:`~foundry.cassette.paths.DEFAULT_CASSETTE_DIR`.
            redactor: The redaction engine built from ``secrets.redact_keys``. Defaults to
                :func:`~foundry.obs.redaction.default_redactor`, which carries the built-in
                deny-lists, so a store always redacts even when a caller passes nothing.

        Raises:
            FoundryError: ``cassette_dir`` is a blank string.
        """
        self._cassette_dir = _as_directory(cassette_dir)
        self._redactor = default_redactor() if redactor is None else redactor

    @property
    def cassette_dir(self) -> Path:
        """The cassette directory this store reads and writes, as given."""
        return self._cassette_dir

    def path_for(self, nodeid: str) -> Path:
        """Return the resolved path of ``nodeid``'s cassette document.

        Delegated to :func:`~foundry.cassette.paths.cassette_path`, so the store, the replay
        transport's miss error and every message name the same file and the single containment
        check in :mod:`foundry.core.paths` is the only one applied.

        Args:
            nodeid: The pytest node id of the item.

        Returns:
            The resolved absolute path of the document, inside the cassette directory.

        Raises:
            FoundryError: ``nodeid`` is empty or whitespace-only.
            ArtifactError: The resolved path escapes the cassette directory, which only a
                symlinked directory can cause. The error carries the rejected path and the
                directory.
        """
        return cassette_path(nodeid, cassette_dir=self._cassette_dir)

    def exists(self, nodeid: str) -> bool:
        """Report whether ``nodeid``'s cassette document is present as a regular file.

        Raises:
            FoundryError: ``nodeid`` is empty or whitespace-only.
            ArtifactError: The resolved path escapes the cassette directory.
        """
        return self.path_for(nodeid).is_file()

    def load(self, nodeid: str) -> CassetteDocument:
        """Read ``nodeid``'s cassette document, or an empty document when there is none.

        A missing file is not an error: a test that made no request has no recording, and a
        replay of it succeeds because no request is ever matched. Any request the test then makes
        produces the typed :class:`~foundry.core.exceptions.CassetteMissError` naming this path,
        raised by the replay transport rather than here. A file that exists but cannot be read or
        does not hold a cassette is an error: a truncated or hand-edited recording must not replay
        as if nothing had been recorded (AAP 0.14.3).

        Args:
            nodeid: The pytest node id of the item.

        Returns:
            The stored document, or :func:`~foundry.cassette.models.empty_document` when the file
            does not exist.

        Raises:
            FoundryError: ``nodeid`` is empty, the file cannot be read or decoded, or its content
                is not a valid cassette. The message names the file and the offending key, and
                never quotes a stored value, which may be a secret.
            ArtifactError: The resolved path escapes the cassette directory.
        """
        path = self.path_for(nodeid)
        if not path.exists():
            return empty_document(utc_now())
        if not path.is_file():
            raise FoundryError(
                f"cassette path {path} is not a regular file",
                hint="remove it, or point cassette.dir at a directory of cassette documents",
            )
        try:
            text = path.read_text(encoding=CASSETTE_ENCODING)
        except UnicodeDecodeError as exc:
            raise FoundryError(
                f"cassette document {path} is not UTF-8 text: {exc.reason} at byte {exc.start}",
                hint=_RECORD_HINT,
            ) from exc
        except OSError as exc:
            raise FoundryError(
                f"cassette document {path} could not be read: {type(exc).__name__}",
                hint="check the file's permissions",
            ) from exc
        try:
            return from_json_text(text)
        except FoundryError as exc:
            raise FoundryError(
                f"cassette document {path} is not a valid recording: {exc.message}",
                hint=exc.hint or _RECORD_HINT,
            ) from exc

    def save(
        self,
        nodeid: str,
        interactions: Sequence[Interaction],
        *,
        recorded_at: datetime | None = None,
    ) -> Path | None:
        """Redact a capture, write it as ``nodeid``'s cassette document and return its path.

        Redaction happens before serialisation, so no denied header value and no secret-bearing
        JSON key reaches the file (implicit requirement I4). The document records the sorted names
        that were scrubbed, which is what AAP 0.4.6 publishes as ``redacted_keys``.

        Failure isolation (AAP 0.1.2, AAP 0.14.3): a redaction fault or an unwritable directory is
        reported as exactly one ``WARNING`` on the ``foundry`` logger and the method returns
        ``None``. It never raises, because a cassette that could not be recorded must not change a
        test's outcome or the run's exit code.

        Args:
            nodeid: The pytest node id of the item whose capture this is.
            interactions: The interactions to record, in the order the test issued them.
            recorded_at: The moment to stamp the document with. Defaults to
                :func:`~foundry.core.ids.utc_now`, which is the only clock read on this path.

        Returns:
            The path the document was written to, or ``None`` when it could not be written.

        Raises:
            FoundryError: ``nodeid`` is empty or whitespace-only.
            ArtifactError: The resolved path escapes the cassette directory.
        """
        try:
            redacted, names = redact_interactions(interactions, self._redactor)
            document = CassetteDocument(
                recorded_at=utc_now() if recorded_at is None else recorded_at,
                redacted_keys=names,
                interactions=redacted,
            )
        except _PLUMBING_ERRORS as exc:
            emit_plumbing_failure(
                _COMPONENT, f"could not redact the cassette capture for {nodeid}", exc=exc
            )
            return None
        return self.save_document(nodeid, document)

    def save_document(self, nodeid: str, document: CassetteDocument) -> Path | None:
        """Write an already-redacted document as ``nodeid``'s cassette and return its path.

        The document is written exactly as it stands -- nothing is redacted here -- so this is the
        entry point for a caller holding a document that was redacted elsewhere, such as a
        recorded capture being re-based. :meth:`save` is the capture path, and it is the one that
        guarantees redaction before the write.

        The directory is created when it does not exist, and the text comes from the single
        serialiser in :mod:`foundry.cassette.models`, so two runs of one recording produce
        byte-identical files apart from ``recorded_at`` (AAP 0.13.1).

        Failure isolation (AAP 0.1.2, AAP 0.14.3): an ``OSError`` -- an unwritable directory, a
        full disk, a path that is not a directory -- is reported as exactly one ``WARNING`` naming
        the path and the method returns ``None`` instead of raising.

        Args:
            nodeid: The pytest node id of the item whose capture this is.
            document: The document to write, already redacted.

        Returns:
            The path the document was written to, or ``None`` when it could not be written.

        Raises:
            FoundryError: ``nodeid`` is empty or whitespace-only.
            ArtifactError: The resolved path escapes the cassette directory.
        """
        path = self.path_for(nodeid)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(to_json_text(document), encoding=CASSETTE_ENCODING)
        except OSError as exc:
            emit_plumbing_failure(_COMPONENT, f"could not write cassette {path}", exc=exc)
            return None
        return path
