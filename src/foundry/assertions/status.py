"""The ``assert_status`` helper: one HTTP status assertion, one redacted failure message.

:func:`assert_status` returns when the response's status is one of the accepted codes and otherwise
raises ``AssertionError`` naming the received status, the expected set and a bounded, redacted body
excerpt (AAP 0.4.3, AAP 0.7.3, AAP 0.14.3): ``AssertionError`` is the only type it raises, and
response headers are never rendered. Its name and parameter shape are fixed by AAP 0.4.9.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from foundry.core.types import HttpResponseLike
from foundry.obs.redaction import MAX_EXCERPT_BYTES, Redactor, default_redactor

__all__ = ["assert_status"]

#: Prefix of every message this module raises. Written once so a test can assert on it and a
#: reader can tell at a glance which helper spoke.
_PREFIX: Final[str] = "foundry.assertions.assert_status:"

#: Lowest HTTP status code the ``expected`` argument accepts, and the lowest code that exists.
_MIN_STATUS: Final[int] = 100

#: Highest HTTP status code ``expected`` accepts (600 is not a status code).
_MAX_STATUS: Final[int] = 599

#: Characters of the raw body handed to the redaction engine. Bounded before redaction as well
#: as after it, so a multi-megabyte body is never walked whole to build a 200-byte excerpt; a
#: character cut is never a shorter byte cut, so the engine's byte budget stays satisfiable.
_EXCERPT_INPUT_LIMIT: Final[int] = MAX_EXCERPT_BYTES * 4

#: The ``source`` values ``FoundryResponse`` declares (AAP 0.4.3). Any other value is left out
#: of a message rather than echoed into it, so only a documented literal reaches a report.
_SOURCE_VALUES: Final[frozenset[str]] = frozenset({"live", "cassette"})


def _misuse(detail: str) -> AssertionError:
    """Build the ``AssertionError`` that reports a misuse of the ``expected`` argument.

    A single builder keeps the prefix and the wording of every misuse message in one place, so
    the three rejections of ``expected`` cannot drift apart.

    Args:
        detail: The sentence describing the misuse, without the helper prefix.

    Returns:
        The exception for the caller to raise.
    """
    return AssertionError(f"{_PREFIX} {detail}")


def _type_label(value: object) -> str:
    """Render a rejected value as its type name and ``repr``, for a misuse message.

    Args:
        value: The value ``expected`` was given in place of a status code.

    Returns:
        Text of the form ``"str 'ok'"`` or ``"bool True"`` -- the type name then the repr, so
        an operator sees both what arrived and what shape it had.
    """
    return f"{type(value).__name__} {value!r}"


def _reject_expected(value: object) -> AssertionError:
    """Build the misuse error for a value that is not an int status code at all.

    Args:
        value: The offending value: the whole ``expected`` argument when it is neither an int
            nor an iterable, or one non-int element of it.

    Returns:
        The ``AssertionError`` naming the value's type and repr.
    """
    return _misuse(
        f"expected must be an int or an iterable of int status codes, got {_type_label(value)}"
    )


def _normalise_expected(expected: int | Iterable[int]) -> tuple[int, ...]:
    """Return the expected status codes as a sorted, de-duplicated tuple.

    The normalisation is what makes a failure message stable: two calls that accept the same
    codes render the same set in the same order, whatever container or ordering the caller
    passed (AAP 0.13.1). An arbitrary iterable is materialised exactly once, so a one-shot
    generator works and is never iterated a second time.

    Args:
        expected: A single status code, or a ``set``, ``frozenset``, ``list``, ``tuple``,
            ``range`` or one-shot iterable of them.

    Returns:
        The accepted codes in ascending order, without duplicates.

    Raises:
        AssertionError: If ``expected`` is a ``bool``, a ``str``, ``bytes`` or ``bytearray``, a
            value that is not iterable, an iterable holding a non-int element or a ``bool``, a
            code outside 100-599, or an iterable holding no code at all. Every one of these is a
            misuse of the helper, so it is reported as ``AssertionError`` with the helper prefix
            rather than as a builtin type error (AAP 0.14.3).
    """
    if isinstance(expected, bool):
        raise _reject_expected(expected)
    values: tuple[object, ...]
    if isinstance(expected, int):
        values = (expected,)
    elif isinstance(expected, str | bytes | bytearray):
        # An Iterable, but a sequence of characters rather than a set of status codes.
        raise _reject_expected(expected)
    elif isinstance(expected, Iterable):
        try:
            values = tuple(expected)
        except (TypeError, ValueError):
            # A broken iterator is still a misuse of `expected`, not a Foundry fault.
            raise _reject_expected(expected) from None
    else:
        raise _reject_expected(expected)
    codes: set[int] = set()
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int):
            raise _reject_expected(value)
        if not _MIN_STATUS <= value <= _MAX_STATUS:
            raise _misuse(
                f"expected contains an invalid HTTP status code: {value} "
                f"(valid range {_MIN_STATUS}-{_MAX_STATUS})"
            )
        codes.add(value)
    if not codes:
        raise _misuse("expected must name at least one HTTP status code")
    return tuple(sorted(codes))


def _format_expected(allowed: tuple[int, ...]) -> str:
    """Render the expected codes as the set of a failure message, such as ``{200, 201}``.

    Args:
        allowed: The normalised codes, ascending and de-duplicated.

    Returns:
        The codes in braces, ascending, separated by a comma and a space.
    """
    return "{" + ", ".join(str(code) for code in allowed) + "}"


def _source_label(response: HttpResponseLike) -> str:
    """Render where a response came from as a message fragment, or nothing when it says nothing.

    The attribute is read defensively: ``source`` belongs to ``FoundryResponse`` (AAP 0.4.3) and
    to the protocol only incidentally, so a response without it, or with a non-``str`` value,
    must produce no fragment and no exception. Only the two documented values are rendered, so
    no undocumented text reaches a message.

    Args:
        response: The response being asserted against.

    Returns:
        ``"; source=live"`` or ``"; source=cassette"``, or the empty string.
    """
    value = getattr(response, "source", None)
    if isinstance(value, str) and value in _SOURCE_VALUES:
        return f"; source={value}"
    return ""


def _body_excerpt(response: HttpResponseLike, redactor: Redactor | None = None) -> str:
    """Return a redacted, bounded excerpt of the response body for a failure message.

    The raw body is cut to :data:`_EXCERPT_INPUT_LIMIT` characters first, which bounds the bytes
    handed to the engine too, so a large body is never walked whole; the redaction engine then
    applies the configured and built-in deny-lists and the
    :data:`~foundry.obs.redaction.MAX_EXCERPT_BYTES` byte cap (AAP 0.7.3). The body may be empty;
    an empty body renders as the empty string. Reading ``response.text`` is assumed not to raise,
    because a response that cannot report its own body is a fault of the HTTP layer rather than of
    this assertion, and this helper does not mask it.

    Args:
        response: The response whose body is quoted.
        redactor: The run's redaction engine, built from ``secrets.redact_keys``, or ``None`` to
            build one carrying only the built-in deny-lists.

    Returns:
        The redacted excerpt, at most :data:`~foundry.obs.redaction.MAX_EXCERPT_BYTES` bytes plus
        the redaction engine's truncation marker.
    """
    engine = default_redactor() if redactor is None else redactor
    body: object = response.text
    if isinstance(body, str):
        return engine.body_excerpt(body[:_EXCERPT_INPUT_LIMIT])
    if isinstance(body, bytes | bytearray | memoryview):
        return engine.body_excerpt(bytes(body)[:_EXCERPT_INPUT_LIMIT])
    return engine.body_excerpt(str(body)[:_EXCERPT_INPUT_LIMIT])


def assert_status(
    response: HttpResponseLike,
    expected: int | Iterable[int],
    *,
    redactor: Redactor | None = None,
) -> None:
    """Assert that a response's status code is one of the expected codes (AAP 0.4.3).

    Returns ``None`` on a match. Otherwise it raises ``AssertionError`` naming the received status,
    the expected set and a redacted, bounded body excerpt (AAP 0.14.3, AAP 0.7.3); ``redactor`` is
    additive over the AAP 0.4.3 signature (AAP 0.4.9).

    Example:
        Accept either success code for a created resource::

            assert_status(foundry_api.post("/carts", json={"items": []}), {200, 201})
    """
    allowed = _normalise_expected(expected)
    status = response.status_code
    if status in allowed:
        return
    excerpt = _body_excerpt(response, redactor)
    message = (
        f"{_PREFIX} expected HTTP status in {_format_expected(allowed)} "
        f"but received {status}{_source_label(response)}; "
        f"body excerpt (redacted, up to {MAX_EXCERPT_BYTES} bytes): {excerpt!r}"
    )
    raise AssertionError(message)
