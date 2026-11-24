"""The ``assert_status`` helper: one HTTP status assertion, one redacted failure message.

:func:`assert_status` does one job. It compares a response's status code against the set of
codes a test accepts, returns when the status is one of them, and otherwise raises
``AssertionError`` naming the received status, the expected set and a bounded, redacted excerpt
of the response body (AAP 0.4.3, AAP 0.7.3).

What it raises is fixed by the exception boundary of AAP 0.14.3: ``AssertionError`` and nothing
else -- never a Foundry error, never another builtin, never a new exception class. A misused
``expected`` argument surfaces as ``AssertionError`` too, carrying the prefix
``foundry.assertions.assert_status:`` so a misuse is unmistakable against a mismatched status.

The excerpt comes from the single redaction engine, :mod:`foundry.obs.redaction`, so it is
scrubbed of the configured secrets before it is shown and is bounded by
:data:`~foundry.obs.redaction.MAX_EXCERPT_BYTES` (200). Response headers are never rendered at
all: a message that cannot hold a header value cannot leak one, which is the structural reason
a configured secret header cannot reach ``results.json``, ``junit.xml``, ``report.html`` or the
terminal through this helper.

A non-2xx response is an ordinary value rather than a fault: the HTTP layer of AAP 0.4.3
returns every status in a ``FoundryResponse`` and raises only on a transport error, so a 500
arrives here intact and is asserted against like any other code.

The name, the parameter shape and the behaviour are part of Foundry's compatibility promise
(AAP 0.4.9): a later release may add to them, never rename or remove them. The response
parameter is annotated with :class:`foundry.core.types.HttpResponseLike` rather than with
``FoundryResponse`` because the layering contract of AAP 0.4.8 keeps this package from
importing :mod:`foundry.http`; the real response type satisfies that protocol structurally.

Importing this module defines names and constants and nothing else -- no I/O, no environment
read, no logger configuration and no module-level mutable state (AAP 0.4.8).

Example:
    Accept either success code for a created resource::

        response = foundry_api.post("/carts", json={"items": []})
        assert_status(response, {200, 201})
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from foundry.core.types import HttpResponseLike
from foundry.obs.redaction import MAX_EXCERPT_BYTES, default_redactor

__all__ = ["assert_status"]

#: Prefix of every message this module raises. Written once so a test can assert on it and a
#: reader can tell at a glance which helper spoke.
_PREFIX: Final[str] = "foundry.assertions.assert_status:"

#: Lowest HTTP status code the ``expected`` argument accepts, and the lowest code that exists.
_MIN_STATUS: Final[int] = 100

#: Highest HTTP status code ``expected`` accepts (600 is not a status code).
_MAX_STATUS: Final[int] = 599

#: Characters of the raw body handed to the redaction engine. Bounded before redaction as well
#: as after it, so a multi-megabyte body is never walked whole to build a 200-character excerpt.
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


def _body_excerpt(response: HttpResponseLike) -> str:
    """Return a redacted, bounded excerpt of the response body for a failure message.

    The raw body is cut to :data:`_EXCERPT_INPUT_LIMIT` characters first, so a large body is
    never walked whole, and the redaction engine then applies the configured and built-in
    deny-lists and the :data:`~foundry.obs.redaction.MAX_EXCERPT_BYTES` cap (AAP 0.7.3). The
    body may be empty; an empty body renders as the empty string. Reading ``response.text`` is
    assumed not to raise, because a response that cannot report its own body is a fault of the
    HTTP layer rather than of this assertion, and this helper does not mask it.

    Args:
        response: The response whose body is quoted.

    Returns:
        The redacted excerpt, at most :data:`~foundry.obs.redaction.MAX_EXCERPT_BYTES`
        characters plus the redaction engine's truncation marker.
    """
    redactor = default_redactor()
    body: object = response.text
    if isinstance(body, str):
        return redactor.body_excerpt(body[:_EXCERPT_INPUT_LIMIT])
    if isinstance(body, bytes | bytearray | memoryview):
        return redactor.body_excerpt(bytes(body)[:_EXCERPT_INPUT_LIMIT])
    return redactor.body_excerpt(str(body)[:_EXCERPT_INPUT_LIMIT])


def assert_status(response: HttpResponseLike, expected: int | Iterable[int]) -> None:
    """Assert that a response's status code is one of the expected codes.

    This is Foundry's public status assertion (AAP 0.4.3): it accepts every status the HTTP
    layer returns, including 4xx and 5xx, and it raises ``AssertionError`` rather than a Foundry
    error so that any test runner reports an ordinary assertion failure (AAP 0.14.3).

    Args:
        response: The response to check. Anything satisfying
            :class:`~foundry.core.types.HttpResponseLike` is accepted, which includes
            ``foundry.http.FoundryResponse``.
        expected: One status code, or a ``set``, ``frozenset``, ``list``, ``tuple``, ``range``
            or one-shot iterable of them.

    Returns:
        ``None`` when the status is one of the expected codes. Nothing else is read on that
        path: a passing assertion touches neither the body nor the redaction engine, because
        this helper sits on the hot path of every user test.

    Raises:
        AssertionError: When the status is not one of the expected codes. The message names the
            received status, the expected set, where the response came from when it reports a
            documented source, and a redacted body excerpt of at most
            :data:`~foundry.obs.redaction.MAX_EXCERPT_BYTES` characters. It is also raised, with
            the same ``foundry.assertions.assert_status:`` prefix, when ``expected`` is a
            ``bool``, a string, an iterable holding a non-int or an out-of-range code, or an
            iterable holding no code at all.

    Example:
        Accept either success code for a created resource::

            assert_status(foundry_api.post("/carts", json={"items": []}), {200, 201})
    """
    allowed = _normalise_expected(expected)
    status = response.status_code
    if status in allowed:
        return
    message = (
        f"{_PREFIX} expected HTTP status in {_format_expected(allowed)} "
        f"but received {status}{_source_label(response)}; "
        f"body excerpt (redacted, up to {MAX_EXCERPT_BYTES} bytes): {_body_excerpt(response)!r}"
    )
    raise AssertionError(message)
