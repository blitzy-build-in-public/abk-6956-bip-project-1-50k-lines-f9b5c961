"""Deterministic ordering, rendering and redaction of JSON Schema violations (AAP 0.4.3).

Shallowest path first, then structurally, then the key; redaction runs on the way out (AAP 0.14.3).
"""

from __future__ import annotations

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

A caller holding the configured ``secrets.redact_keys`` injects its own redactor here, so a
secret-bearing JSON key the configuration adds is scrubbed out of a violation message.
"""

#: A key that can follow a dot in a JSON path without quoting.
_IDENTIFIER_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: Appended to an excerpt that had to be cut. ASCII only, so the marker costs three bytes of the
#: budget the behaviour contract fixes and no width is left to the platform's encoding.
_EXCERPT_MARKER: Final[str] = "..."


# --- JSON path rendering ---------------------------------------------------


def _escape_key(key: str) -> str:
    r"""Escape a mapping key for the single-quoted JSON-path form.

    ``\`` becomes ``\\`` and ``'`` becomes ``\'``, so a key that contains a quote cannot close the
    quoted form early.
    """
    return key.replace("\\", "\\\\").replace("'", "\\'")


def _pointer_part(part: PathPart) -> str:
    """Escape one step for the RFC 6901 JSON pointer: ``~`` -> ``~0``, ``/`` -> ``~1``."""
    return str(part).replace("~", "~0").replace("/", "~1")


def _part_suffix(part: PathPart) -> str:
    """Render one JSON path step: ``.key``, ``['key']`` or ``[3]`` as the step requires."""
    if isinstance(part, int):
        return f"[{int(part)}]"
    if _IDENTIFIER_KEY_PATTERN.fullmatch(part) is not None:
        return f".{part}"
    return f"['{_escape_key(part)}']"


def json_path_from_parts(parts: Sequence[PathPart]) -> str:
    """Render a violation path as a JSON path of Foundry's own form.

    The path comes from the validator's structured steps, never from the library's rendering, so its
    spelling is Foundry's contract and cannot drift with a ``jsonschema`` upgrade.

    Returns:
        ``$`` for the document root, then one fragment per step: ``.key`` for an identifier,
        ``['key']`` for any other key, and ``[3]`` for an index.
    """
    return "$" + "".join(_part_suffix(part) for part in parts)


def format_schema_path(parts: Sequence[PathPart]) -> str:
    """Render a schema location as a ``#``-rooted JSON pointer.

    Returns:
        ``#`` for the schema root, otherwise ``#/`` followed by the escaped steps joined with
        ``/``.
    """
    if not parts:
        return "#"
    return "#/" + "/".join(_pointer_part(part) for part in parts)


# --- The violation record --------------------------------------------------


@dataclass(frozen=True, slots=True)
class Violation:
    """One JSON Schema violation, in the shape Foundry orders, renders and reports it.

    A violation is immutable, so a report renderer, a JUnit writer and a compare step cannot alter
    what was captured. ``message`` holds the validator's text **raw**, because only a caller holding
    the configured ``secrets.redact_keys`` can redact it correctly: everything written out goes
    through :meth:`render`, :func:`render_violation`, :func:`to_contract_error` or :func:`excerpt`.

    Attributes:
        json_path: The path Foundry reports, rendered from :attr:`path_parts`.
        path_parts: The path steps as reported, so :mod:`foundry.data` can name a column.
    """

    json_path: str
    schema_path: str
    validator: str
    message: str
    path_parts: tuple[PathPart, ...]

    def render(self, *, redact: RedactorCallable | None = None) -> str:
        """Render the violation as the single line Foundry reports.

        ``redact`` is the callable that scrubs secrets out of the message, normally the redactor
        built from ``secrets.redact_keys``; ``None`` applies the built-in deny-lists.
        """
        return render_violation(self, redact=redact)

    def __str__(self) -> str:
        """Return :meth:`render` with the built-in redaction applied."""
        return self.render()


# --- Ordering: Foundry's own rule, never the library's ----------------------


def _path_part_key(part: PathPart) -> tuple[int, int, str]:
    """Return the type-stable sort key of one path step.

    The leading integer is a type discriminant, so an index and a key never reach a comparison
    against each other: indexes rank first and compare numerically, keys compare as text.
    """
    if isinstance(part, int):
        return (0, part, "")
    return (1, 0, part)


def _path_parts(parts: Iterable[object]) -> tuple[PathPart, ...]:
    """Normalise a validator path to its tuple of ``str`` and ``int`` steps.

    A step that is neither a ``str`` nor an ``int`` is rendered as text, so the tuple always
    satisfies :data:`PathPart`.
    """
    return tuple(part if isinstance(part, str | int) else str(part) for part in parts)


def violation_from_error(error: ValidationError) -> Violation:
    """Build a :class:`Violation` from a ``jsonschema`` validation error.

    Only the error's structured parts are read -- its path steps, schema path steps, validator name
    and message -- so a path renders as Foundry spells it and the message stays raw for the caller's
    redactor. Any object exposing those four documented attributes is accepted.

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

    The components, in order: the number of path steps, so the shallowest complaint wins; the steps
    compared structurally, so ``[2]`` orders before ``[10]``; the rendered JSON path; the message;
    the validator name; and the schema path. That is what makes the comparison total and the
    reported violation reproducible across processes and xdist workers; the library's yield order is
    never trusted and its relevance ranking is never used.

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

    Returns:
        A new tuple in :func:`order_key` order -- a tuple rather than a set, because callers index
        the first element and iterate the rest, and a set would discard duplicates.
    """
    return tuple(sorted(violations, key=order_key))


def choose_first(errors: Iterable[ValidationError]) -> Violation | None:
    """Return the first violation among the errors Foundry would report.

    This is the single implementation of the rule; :func:`foundry.contract.validate.first_violation`
    delegates to it rather than restating the ordering.

    Returns:
        The violation with the lowest :func:`order_key`, or ``None`` when no error was given.
    """
    ordered = sort_violations(violation_from_error(error) for error in errors)
    return ordered[0] if ordered else None


# --- Rendering, contract errors and excerpts -------------------------------


def _resolve_redactor() -> RedactorCallable:
    """Return the deny-list-only redactor that serves a caller holding no configured keys.

    This package may not import :mod:`foundry.config` (AAP 0.4.8), so the engine built from
    ``secrets.redact_keys`` cannot be constructed here: every caller holding those keys supplies its
    own redactor through the ``redact`` argument. The import sits inside the function because a
    module-scope one would put the engine on the plugin's startup path; the edge is this package's
    sanctioned exception to AAP 0.4.8 and serves the single redaction engine of AAP 0.14.3.
    """
    from foundry.obs.redaction import default_redactor

    return default_redactor().text


def _redact_text(text: str, redact: RedactorCallable | None) -> str:
    """Scrub secret values out of text before it reaches a sink.

    ``redact`` is the caller's redactor, or ``None`` for the built-in deny-lists. One that raises
    falls back to them rather than propagating, because a redaction fault must not change a test's
    outcome or the run's exit code; that the fallback happened is the one ``DEBUG`` record emitted.
    """
    if redact is None:
        return _resolve_redactor()(text)
    try:
        return redact(text)
    except Exception:  # a foreign callable's fault must not reach the test's outcome
        from foundry.obs.events import LEVEL_DEBUG, LifecycleEvent, log_event

        log_event(
            LifecycleEvent(
                name="redaction_fallback",
                level=LEVEL_DEBUG,
                fields=(("component", "contract.violations"),),
            )
        )
        return _resolve_redactor()(text)


def _single_line(text: str) -> str:
    r"""Escape the characters that would let text occupy or forge more than one line.

    A validator message can embed a multi-line instance value and a body excerpt can be anything;
    escaping backslashes too keeps a literal ``\n`` distinguishable from a real newline.
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
    """Decode a body to text, undecodable bytes replaced rather than raising."""
    if isinstance(payload, str):
        return payload
    try:
        return bytes(payload).decode("utf-8", errors="replace")
    except (TypeError, ValueError):
        return ""


def _truncate_utf8(text: str, limit: int) -> tuple[str, bool]:
    """Cut text to at most ``limit`` UTF-8 bytes without splitting a character.

    Returns:
        The cut text, with the trailing partial character discarded rather than replaced, and
        whether anything was cut.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, False
    return encoded[:limit].decode("utf-8", errors="ignore"), True


def render_violation(violation: Violation, *, redact: RedactorCallable | None = None) -> str:
    """Render a violation as the one line Foundry reports.

    Redaction happens here, on the way out, because the message may quote an instance value:
    scrubbing once keeps one text in ``results.json``, ``junit.xml``, ``report.html``, a
    ContractError detail and the terminal; ``redact`` is the caller's redactor or ``None``.

    Returns:
        ``<json_path>: <redacted message> (schema path: <schema_path>, validator: <validator>)``,
        with the message flattened to one line.
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

    This is the single construction site for a :class:`~foundry.core.exceptions.ContractError`, so
    the path a caller branches on, the detail it reads and the message it greps for cannot diverge
    between the response validator, the data loader and the schema assertion.

    Returns:
        A ``ContractError`` whose ``json_path`` is the violation's path and whose ``detail`` is
        :func:`render_violation`; the text carries no timestamp, run id or address.
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
    ``limit`` is the byte budget -- 200 by default, the number the behaviour contract fixes --
    and ``redact`` is the caller's redactor, or ``None`` for the built-in deny-lists.

    Returns:
        At most ``limit`` bytes of UTF-8, redacted and on one line, followed by ``...`` when
        anything was cut.
    """
    budget = max(0, limit)
    scrubbed = _single_line(_redact_text(_decode(payload), redact))
    body, truncated = _truncate_utf8(scrubbed, budget)
    return f"{body}{_EXCERPT_MARKER}" if truncated else body
