"""Deterministic ordering and rendering of JSON Schema violations.

``jsonschema`` yields validation errors in an order it does not document, and the relevance
ranking it offers is an explicit heuristic, so the same invalid document and schema are free
to report a different error on each run. Foundry's published contract is that a failure names
*the first violation having its JSON path*, reproducibly, so the rule is owned here instead of
being delegated to the library:

1. shallowest JSON path first -- a complaint about the document outranks a complaint inside it;
2. then the path compared structurally, so an index orders numerically (``[2]`` before ``[10]``)
   rather than as text;
3. then the rendered JSON path, the message, the validator name and the schema path, which
   exist so that two errors agreeing on everything before them still order deterministically.

The comparison is total, which is what makes the reported violation identical in every process,
on every worker, in a serial run and in a sharded one. :func:`sort_violations` and
:func:`choose_first` are the only implementations of that rule:
``foundry.contract.validate.first_violation`` delegates here rather than restating it.

Three further responsibilities live in this module because they belong to the same contract and
must not be duplicated:

* **Rendering.** :func:`render_violation` formats ``<json_path>: <message> (schema path: ...,
  validator: ...)``, and :func:`to_contract_error` is the single place a
  :class:`~foundry.core.exceptions.ContractError` is built, so ``json_path``, ``detail`` and the
  message cannot diverge between the response validator, the data loader and the schema
  assertion.
* **Redaction.** A validator message quotes instance values (``'example-secret-value' is not of
  type 'integer'``) and that text reaches ``results.json``, ``junit.xml``, ``report.html`` and the
  terminal. Redaction therefore happens on the way *out*, in the render functions, through the
  engine :mod:`foundry.obs.redaction` owns.
* **Excerpts.** A body that was expected to be JSON but was not is reported with its first 200
  bytes, redacted, through :func:`excerpt`.

Type aliases of the public surface: :data:`PathPart` is one path step (a mapping key as ``str``
or a sequence index as ``int``), and :data:`RedactorCallable` is the ``str -> str`` scrubbing
callable a caller holding configured ``secrets.redact_keys`` injects into the render functions.

The module does no work at import time: no I/O, no environment read, no logger configuration and
no module-level mutable state. It imports :mod:`foundry.core.exceptions` at module scope and
reaches :mod:`foundry.obs.redaction` through one lazy import inside :func:`_resolve_redactor`,
which keeps the redaction engine off the plugin's startup path; ``jsonschema`` itself is imported
only under ``TYPE_CHECKING`` for annotations, so importing this module costs a handful of stdlib
lookups. It raises only :class:`~foundry.core.exceptions.ContractError`.

That obs import is the one deliberate exception to the permitted-import column of AAP 0.4.8, which
lists ``core`` alone. It points downward into the foundation rung, which the capability row of
AAP 0.3.2 permits by letting a capability package import ``core``, ``obs`` and its siblings on the
same rung, and AAP 0.14.3 makes :mod:`foundry.obs.redaction` the single engine every sink calls, so
the :class:`~foundry.core.exceptions.ContractError` detail a failure displays is scrubbed here,
before pytest renders it. A layering check written from AAP 0.4.8 carries exactly this one
exception for this package.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, TypeAlias

from foundry.core.exceptions import ContractError

if TYPE_CHECKING:
    from jsonschema.exceptions import ValidationError

__all__: Final[tuple[str, ...]] = (
    "PathPart",
    "RedactorCallable",
    "Violation",
    "choose_first",
    "excerpt",
    "format_schema_path",
    "json_path_from_parts",
    "order_key",
    "render_violation",
    "sort_violations",
    "to_contract_error",
    "violation_from_error",
)

PathPart: TypeAlias = str | int
"""One step of a JSON path: a mapping key, or a sequence index."""

RedactorCallable: TypeAlias = Callable[[str], str]
"""A text-scrubbing callable, normally ``foundry.obs.redaction.Redactor.text``.

A caller that holds the configured ``secrets.redact_keys`` injects its own redactor here, so a
secret-bearing JSON key the configuration adds is scrubbed out of a violation message; a caller
that holds none passes ``None`` and the built-in deny-lists apply.
"""

_LOGGER: Final[logging.Logger] = logging.getLogger(__name__)

#: A key that can follow a dot in a JSON path without quoting.
_IDENTIFIER_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: Appended to an excerpt that had to be cut. ASCII only, so the marker costs three bytes of the
#: budget the behaviour contract fixes and no width is left to the platform's encoding.
_EXCERPT_MARKER: Final[str] = "..."


# --- JSON path rendering ---------------------------------------------------


def _escape_key(key: str) -> str:
    r"""Escape a mapping key for the single-quoted JSON-path form.

    Args:
        key: The key to escape.

    Returns:
        The key with ``\`` written as ``\\`` and ``'`` as ``\'``, so the quoted form cannot be
        closed early by a key that contains a quote.
    """
    return key.replace("\\", "\\\\").replace("'", "\\'")


def _pointer_part(part: PathPart) -> str:
    """Escape one step for the ``#``-rooted JSON pointer a schema path uses (RFC 6901).

    Args:
        part: The schema path step.

    Returns:
        The step as text, with ``~`` written as ``~0`` and ``/`` as ``~1``.
    """
    return str(part).replace("~", "~0").replace("/", "~1")


def _part_suffix(part: PathPart) -> str:
    """Render one JSON path step as the fragment that follows the previous one.

    Args:
        part: A mapping key or a sequence index.

    Returns:
        ``.key`` for an identifier-shaped key, ``['key']`` for any other key and ``[3]`` for
        an index.
    """
    if isinstance(part, int):
        return f"[{int(part)}]"
    if _IDENTIFIER_KEY_PATTERN.fullmatch(part) is not None:
        return f".{part}"
    return f"['{_escape_key(part)}']"


def json_path_from_parts(parts: Sequence[PathPart]) -> str:
    """Render a violation path as a JSON path of Foundry's own form.

    The path is derived from the validator's structured steps rather than from the rendering
    the library supplies, so the spelling of a reported path is Foundry's contract and cannot
    drift with a ``jsonschema`` upgrade.

    Args:
        parts: The path steps, mapping keys as ``str`` and sequence indexes as ``int``.

    Returns:
        ``$`` for the document root, then one fragment per step: ``.key`` for an identifier,
        ``['key']`` for any other key, and ``[3]`` for an index.

    Examples:
        >>> json_path_from_parts(())
        '$'
        >>> json_path_from_parts(("items", 2, "price"))
        '$.items[2].price'
        >>> print(json_path_from_parts(("a.b",)))
        $['a.b']
    """
    return "$" + "".join(_part_suffix(part) for part in parts)


def format_schema_path(parts: Sequence[PathPart]) -> str:
    """Render a schema location as a ``#``-rooted JSON pointer.

    Args:
        parts: The schema path steps the validator reported, such as
            ``("properties", "items", "items", "type")``.

    Returns:
        ``#`` for the schema root, otherwise ``#/`` followed by the escaped steps joined with
        ``/``.

    Examples:
        >>> format_schema_path(())
        '#'
        >>> format_schema_path(("properties", "items", "type"))
        '#/properties/items/type'
    """
    if not parts:
        return "#"
    return "#/" + "/".join(_pointer_part(part) for part in parts)


# --- The violation record --------------------------------------------------


@dataclass(frozen=True, slots=True)
class Violation:
    """One JSON Schema violation, in the shape Foundry orders, renders and reports it.

    A violation is immutable, so it can be handed to a report renderer, a JUnit writer and a
    compare step without any of them being able to alter what was captured.

    ``message`` holds the validator's text **raw**, including whatever instance value it
    quotes, because a caller that holds the configured ``secrets.redact_keys`` is the only
    party able to redact it correctly. Everything written out goes through :meth:`render`,
    :func:`render_violation`, :func:`to_contract_error` or :func:`excerpt`, which redact on the
    way out; nothing in the tree may print :attr:`message` itself.

    Attributes:
        json_path: The path Foundry reports, such as ``$.items[2].price``, rendered from
            :attr:`path_parts` rather than taken from the library.
        schema_path: The schema location as a ``#``-rooted pointer, such as
            ``#/properties/items/items/type``.
        validator: The keyword that rejected the instance -- ``type``, ``required``,
            ``minimum`` -- or ``""`` when the validator reported none.
        message: The validator's message, unredacted. See the note above.
        path_parts: The path steps as reported, kept so :mod:`foundry.data` can turn a
            violation into a :class:`~foundry.core.exceptions.DataError` naming its column.
    """

    json_path: str
    schema_path: str
    validator: str
    message: str
    path_parts: tuple[PathPart, ...]

    def render(self, *, redact: RedactorCallable | None = None) -> str:
        """Render the violation as the single line Foundry reports.

        Args:
            redact: The callable that scrubs secrets out of the message, normally the
                redactor built from ``secrets.redact_keys``. ``None`` applies the built-in
                deny-lists.

        Returns:
            ``<json_path>: <redacted message> (schema path: <schema_path>, validator:
            <validator>)`` on one line.
        """
        return render_violation(self, redact=redact)

    def __str__(self) -> str:
        """Return :meth:`render` with the built-in redaction applied.

        Returns:
            The rendered violation.
        """
        return self.render()


# --- Ordering: Foundry's own rule, never the library's ----------------------


def _path_part_key(part: PathPart) -> tuple[int, int, str]:
    """Return the type-stable sort key of one path step.

    The leading integer is a type discriminant, so an index and a key never reach a comparison
    against each other: indexes rank first and compare numerically, keys compare as text.

    Args:
        part: A mapping key or a sequence index.

    Returns:
        ``(0, index, "")`` for an index and ``(1, 0, key)`` for a key.
    """
    if isinstance(part, int):
        return (0, part, "")
    return (1, 0, part)


def _path_parts(parts: Iterable[object]) -> tuple[PathPart, ...]:
    """Normalise a validator path to its tuple of ``str`` and ``int`` steps.

    Args:
        parts: The steps the validator reported, normally a ``deque`` of keys and indexes.

    Returns:
        The steps as a tuple, with anything that is neither a ``str`` nor an ``int`` rendered
        as text so the tuple always satisfies :data:`PathPart`.
    """
    return tuple(part if isinstance(part, str | int) else str(part) for part in parts)


def violation_from_error(error: ValidationError) -> Violation:
    """Build a :class:`Violation` from a ``jsonschema`` validation error.

    Only the error's structured parts are read -- its path steps, schema path steps, validator
    name and message -- so the rendering of a path is Foundry's and the message stays raw for
    the caller's redactor.

    Args:
        error: The error ``jsonschema`` yielded. It is annotated with the library's
            ``ValidationError`` but only the four documented attributes above are used, so any
            object exposing them is accepted.

    Returns:
        The equivalent :class:`Violation`.
    """
    path_parts = _path_parts(error.absolute_path)
    validator = error.validator
    message = error.message
    return Violation(
        json_path=json_path_from_parts(path_parts),
        schema_path=format_schema_path(_path_parts(error.absolute_schema_path)),
        validator=validator if isinstance(validator, str) else "",
        message=message if isinstance(message, str) else str(message),
        path_parts=path_parts,
    )


def order_key(violation: Violation) -> tuple[object, ...]:
    """Return the total sort key that selects Foundry's first violation.

    The components are, in order: the number of path steps, so the shallowest complaint wins;
    the steps compared structurally, so ``[2]`` orders before ``[10]``; the rendered JSON path;
    the message; the validator name; and the schema path. The last three exist only so that two
    errors agreeing on everything before them still order deterministically, which is what
    makes the comparison total and the reported violation reproducible across processes and
    across xdist workers.

    The library's own yield order is never trusted and its relevance ranking is not used.

    Args:
        violation: The violation to key.

    Returns:
        A tuple of comparable components, usable as the ``key`` of :func:`sorted`.
    """
    return (
        len(violation.path_parts),
        tuple(_path_part_key(part) for part in violation.path_parts),
        violation.json_path,
        violation.message,
        violation.validator,
        violation.schema_path,
    )


def sort_violations(violations: Iterable[Violation]) -> tuple[Violation, ...]:
    """Return the violations in Foundry's reporting order.

    Args:
        violations: The violations to order. A generator is consumed once.

    Returns:
        A new tuple in :func:`order_key` order. It is a tuple rather than a set or a generator
        because callers index the first element and iterate the rest, and because a set would
        discard the duplicates that make ordering observable.
    """
    return tuple(sorted(violations, key=order_key))


def choose_first(errors: Iterable[ValidationError]) -> Violation | None:
    """Return the first violation among the errors Foundry would report.

    This is the single implementation of the rule:
    :func:`foundry.contract.validate.first_violation` delegates to it rather than restating the
    ordering.

    Args:
        errors: The errors produced by validating one instance against one schema. The
            iterable may be empty and may arrive in any order.

    Returns:
        The violation with the lowest :func:`order_key`, or ``None`` when no error was given.
    """
    ordered = sort_violations(violation_from_error(error) for error in errors)
    return ordered[0] if ordered else None


# --- Rendering, contract errors and excerpts -------------------------------


def _resolve_redactor() -> RedactorCallable:
    """Return the deny-list-only redactor that serves a caller holding no configured keys.

    This package may not import :mod:`foundry.config` (AAP 0.4.8), so the engine built from
    ``secrets.redact_keys`` cannot be constructed here: every caller that holds those keys --
    the data-file loader, the response validator, the schema assertion -- supplies its own
    redactor through the ``redact`` argument of :func:`render_violation`,
    :func:`to_contract_error`, :func:`excerpt` and :meth:`Violation.render`. This fallback
    exists only for a caller that holds no configured keys, and it applies the built-in header
    and query deny-lists, which configuration can widen but never narrow.

    This is the only place in :mod:`foundry.contract` that touches
    :mod:`foundry.obs.redaction`, and the import sits inside the function on purpose: importing
    the engine at module scope would put it on the plugin's startup path. The edge is also the
    package's single sanctioned exception to the permitted-import column of AAP 0.4.8, kept
    function-scope here and required by the single redaction engine AAP 0.14.3 makes
    authoritative.

    Returns:
        A callable that scrubs secret values out of free text using the built-in header and
        query deny-lists.
    """
    from foundry.obs.redaction import default_redactor

    return default_redactor().text


def _redact_text(text: str, redact: RedactorCallable | None) -> str:
    """Scrub secret values out of text before it reaches a sink.

    Args:
        text: The text to scrub, normally a validator message quoting an instance value.
        redact: The caller's redactor, which knows the configured ``secrets.redact_keys``, or
            ``None`` to apply the built-in deny-lists. A redactor that raises falls back to
            those deny-lists rather than propagating, because a redaction fault must not change
            a test's outcome or the run's exit code.

    Returns:
        The scrubbed text.
    """
    if redact is None:
        return _resolve_redactor()(text)
    try:
        return redact(text)
    except Exception:  # a foreign callable's fault must not reach the test's outcome
        _LOGGER.debug("supplied redactor raised; falling back to the deny-list engine")
        return _resolve_redactor()(text)


def _single_line(text: str) -> str:
    r"""Escape the characters that would let text occupy or forge more than one line.

    A validator message can embed a multi-line instance value, and a body excerpt can be
    anything at all. Escaping backslashes as well as the control characters keeps a literal
    ``\n`` in the payload distinguishable from a real newline, so the result reads the same way
    it was written.

    Args:
        text: The text to flatten.

    Returns:
        The text with ``\`` doubled and every control character escaped, on one line.
    """
    pieces: list[str] = []
    for character in text:
        if character == "\\":
            pieces.append("\\\\")
        elif character == "\n":
            pieces.append("\\n")
        elif character == "\r":
            pieces.append("\\r")
        elif character == "\t":
            pieces.append("\\t")
        elif character.isprintable():
            pieces.append(character)
        else:
            pieces.append(f"\\u{ord(character):04x}")
    return "".join(pieces)


def _decode(payload: bytes | str) -> str:
    """Decode a body to text, tolerating any byte sequence.

    Args:
        payload: The body as bytes or text.

    Returns:
        The text, with bytes decoded as UTF-8 and undecodable bytes replaced rather than
        raising.
    """
    if isinstance(payload, str):
        return payload
    try:
        return bytes(payload).decode("utf-8", errors="replace")
    except (TypeError, ValueError):
        return ""


def _truncate_utf8(text: str, limit: int) -> tuple[str, bool]:
    """Cut text to at most ``limit`` UTF-8 bytes without splitting a character.

    Args:
        text: The text to cut.
        limit: The byte budget, already clamped to zero or more.

    Returns:
        The cut text, decoded with the trailing partial character discarded rather than
        replaced, and whether anything was cut.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, False
    return encoded[:limit].decode("utf-8", errors="ignore"), True


def render_violation(violation: Violation, *, redact: RedactorCallable | None = None) -> str:
    """Render a violation as the one line Foundry reports.

    Redaction happens here, on the way out, because the message may quote an instance value:
    scrubbing at this single point is what keeps the same text in ``results.json``,
    ``junit.xml``, ``report.html``, a :class:`~foundry.core.exceptions.ContractError` detail
    and the terminal.

    Args:
        violation: The violation to render.
        redact: The caller's redactor, or ``None`` for the built-in deny-lists.

    Returns:
        ``<json_path>: <redacted message> (schema path: <schema_path>, validator:
        <validator>)``, with the message flattened to one line.
    """
    message = _single_line(_redact_text(violation.message, redact))
    return (
        f"{violation.json_path}: {message} "
        f"(schema path: {violation.schema_path}, validator: {violation.validator})"
    )


def to_contract_error(
    violation: Violation, *, redact: RedactorCallable | None = None
) -> ContractError:
    """Build the contract error a violation raises.

    This is the single construction site for a :class:`~foundry.core.exceptions.ContractError`,
    so the path a caller branches on, the detail it reads and the message it greps for cannot
    diverge between the response validator, the data loader and the schema assertion.

    Args:
        violation: The first violation, as chosen by :func:`choose_first`.
        redact: The caller's redactor, applied to the rendered detail.

    Returns:
        A ``ContractError`` whose ``json_path`` is the violation's path and whose ``detail`` is
        :func:`render_violation`. The text is deterministic: it carries no timestamp, no run id
        and no object address.
    """
    return ContractError(
        f"contract validation failed at {violation.json_path}",
        json_path=violation.json_path,
        detail=render_violation(violation, redact=redact),
    )


def excerpt(
    payload: bytes | str, *, limit: int = 200, redact: RedactorCallable | None = None
) -> str:
    """Return a short, redacted, single-line excerpt of a body that is not JSON.

    Used where a payload was expected to be JSON and was not, so the failure can quote what
    actually arrived. Redaction runs before truncation for the reason the redaction engine
    documents: cutting first would leave the prefix of a secret that straddles the cut behind.

    Args:
        payload: The body as bytes or text. Bytes are decoded as UTF-8 with undecodable bytes
            replaced, so a binary body still yields text.
        limit: The byte budget. The default of 200 is the number the behaviour contract fixes
            for such a body.
        redact: The caller's redactor, or ``None`` for the built-in deny-lists.

    Returns:
        At most ``limit`` bytes of UTF-8, redacted, with control characters escaped so the
        excerpt occupies exactly one line, followed by ``...`` when anything was cut.
    """
    budget = max(0, limit)
    scrubbed = _single_line(_redact_text(_decode(payload), redact))
    body, truncated = _truncate_utf8(scrubbed, budget)
    return f"{body}{_EXCERPT_MARKER}" if truncated else body
