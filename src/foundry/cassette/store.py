"""The cassette store: the one module that turns captured interactions into a file and back.

:class:`CassetteStore` is the one place a capture is redacted before it reaches disk, so no secret
arrives in a cassette (AAP 0.3.2, AAP 0.4.8).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Final

from foundry.cassette.matcher import mask_query
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
from foundry.obs.redaction import MAX_DEPTH, Redactor, default_redactor

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
"""Faults the two write methods report as a warning instead of raising.

The same band :mod:`foundry.obs.events` treats as isolation faults: a value that cannot be
coerced, a structure that cannot be walked, or a filesystem that refuses the write. A redaction
fault is a fault in Foundry's own plumbing, so it may not change a test's outcome (AAP 0.1.2).
"""


# --- Redaction at capture --------------------------------------------------


def _redact_headers(
    headers: Mapping[str, str], redactor: Redactor, *, found: set[str]
) -> dict[str, str]:
    """Return ``headers`` with every secret-bearing header masked, and no secret left elsewhere.

    A name the redactor considers secret-bearing holds the redaction marker, and one it does not
    recognise still has its value scrubbed as free text, because a deny-list cannot enumerate every
    header a peer invents. What counts as secret is asked of the redactor and never restated here.
    """
    redacted: dict[str, str] = {}
    for name in sorted(headers, key=str.casefold):
        value = headers[name]
        masked = redactor.header(name, value)
        if masked != value:
            found.add(name.strip().casefold())
        redacted[name] = masked
    return redacted


def _collect_secret_names(
    value: object, redactor: Redactor, *, found: set[str], depth: int = 0
) -> None:
    """Add the secret-bearing key names anywhere inside a JSON value to ``found``.

    The walk mirrors :meth:`~foundry.obs.redaction.Redactor.json_value`, which is what scrubs the
    values: the same maximum depth, the same recursion, the same question asked of every key, and
    the same reading of a two-element ``[name, value]`` pair whose name answers it.
    Names are recorded case-folded for the reason :func:`_redact_headers` gives.
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
        if len(value) == 2 and isinstance(value[0], str) and redactor.redacts(value[0]):
            # The engine replaced this pair's second element whole, so nothing inside it survived
            # to be named; walking on would report the key names of a value the file does not hold.
            found.add(value[0].strip().casefold())
            return
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
    however deeply it is nested. A body that is not JSON -- empty, HTML, plain text -- goes through
    the redactor's text path, which scrubs header lines, ``Bearer`` credentials, embedded URLs and
    ``name=value`` pairs. A JSON body in which nothing secret was found is stored exactly as the
    peer sent it: rewriting every recorded body would make a replay differ from the live response.

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

    The single place a capture is redacted and the single place its scrubbed names are collected, so
    ``redacted_keys`` can never disagree with the document it describes. The request's query pairs
    go through :func:`foundry.cassette.matcher.mask_query`, the helper the replay lookup masks
    the arriving request with as well (AAP 0.2.1).

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
                query=mask_query(request.query, redactor, found=found),
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

    Both values a cassette needs come out of one pass, which is why
    :meth:`CassetteStore.save_document` has a single call to make.

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
    answers where a test's cassette lives (:meth:`path_for`), whether it exists (:meth:`exists`) and
    what it holds (:meth:`load`). Both write methods funnel into :meth:`save_document`, so the tree
    has exactly one place that creates a directory, redacts a capture and writes a cassette file
    (AAP 0.4.8).

    Attributes:
        _cassette_dir: The directory the documents live in, as given to the constructor.
        _redactor: The redaction engine applied by :meth:`save_document`, the one write path.
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
            invocation
                directory when a path is built, so nothing is resolved here.
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

    @property
    def redactor(self) -> Redactor:
        """The redaction engine every document this store writes is masked with.

        A caller that must mask a value the store will later compare -- the replay lookup's query
        fingerprint, whose marker has to equal the one written beside it -- asks for exactly this
        engine rather than building a second one, so one run masks one way. Reading the property
        cannot change what the store does: the :class:`~foundry.obs.redaction.Redactor` resolved by
        the constructor is immutable, and this hands out that same object.
        """
        return self._redactor

    def path_for(self, nodeid: str) -> Path:
        """Return the resolved path of ``nodeid``'s cassette document.

        Delegated to :func:`~foundry.cassette.paths.cassette_path`, so the store, the replay
        transport's miss error and every message name the same file, and the single containment
        check in :mod:`foundry.core.paths` is the only one applied.

        Raises:
            FoundryError: ``nodeid`` is empty or whitespace-only.
            ArtifactError: The resolved path escapes the cassette directory, which only a symlinked
                directory can cause.
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

        A missing file is not an error: a test that made no request has no recording, and a replay
        of it succeeds because no request is ever matched. Any request the test then makes produces
        the typed :class:`~foundry.core.exceptions.CassetteMissError` naming this path, raised by
        the replay transport rather than here. A file that exists but cannot be read or does not
        hold a cassette is an
        error: a truncated or hand-edited recording must not replay as if nothing had been recorded.

        Returns:
            The stored document, or :func:`~foundry.cassette.models.empty_document` when the file
            does not exist.

        Raises:
            FoundryError: ``nodeid`` is empty, the file cannot be read or decoded, or its content is
            not a
                valid cassette. The message names the file and the offending key, and never quotes a
                stored value, which may be a secret.
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
        """Write a capture as ``nodeid``'s cassette document and return its path.

        The interactions are handed on as captured: :meth:`save_document` redacts them before
        serialisation, so no denied header value and no secret-bearing JSON key reaches the file
        (implicit requirement I4). A capture that cannot be turned into a document and a write that
        cannot be redacted or completed each produce exactly one ``WARNING`` on the ``foundry``
        logger and a ``None`` return, never an exception, because a cassette that could not be
        recorded must not change a test's outcome (AAP 0.1.2, AAP 0.14.3).

        Returns:
            The path the document was written to, or ``None`` when it could not be written.

        Raises:
            FoundryError: ``nodeid`` is empty or whitespace-only.
            ArtifactError: The resolved path escapes the cassette directory.
        """
        try:
            document = CassetteDocument(
                recorded_at=utc_now() if recorded_at is None else recorded_at,
                interactions=tuple(interactions),
            )
        except _PLUMBING_ERRORS as exc:
            emit_plumbing_failure(
                _COMPONENT, f"could not build the cassette document for {nodeid}", exc=exc
            )
            return None
        return self.save_document(nodeid, document)

    def save_document(self, nodeid: str, document: CassetteDocument) -> Path | None:
        """Redact ``document``, write it as ``nodeid``'s cassette and return its path.

        This is the single write funnel and therefore the single redaction point, so no public write
        path can store an unredacted capture (implicit requirement I4). The written
        ``redacted_keys`` is the sorted union of the names this pass scrubbed and the names the
        document already carried. A redaction fault and an ``OSError`` each produce one ``WARNING``
        and a ``None`` return.
        """
        path = self.path_for(nodeid)
        try:
            redacted, names = redact_interactions(document.interactions, self._redactor)
            document = replace(
                document,
                redacted_keys=(*document.redacted_keys, *names),
                interactions=redacted,
            )
        except _PLUMBING_ERRORS as exc:
            emit_plumbing_failure(
                _COMPONENT, f"could not redact the cassette capture for {nodeid}", exc=exc
            )
            return None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(to_json_text(document), encoding=CASSETTE_ENCODING)
        except OSError as exc:
            emit_plumbing_failure(_COMPONENT, f"could not write cassette {path}", exc=exc)
            return None
        return path
