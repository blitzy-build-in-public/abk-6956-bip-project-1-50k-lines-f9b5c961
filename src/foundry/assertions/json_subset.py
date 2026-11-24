"""Subset assertions over decoded JSON values, reported at the first mismatching path.

:func:`assert_json_subset` proves that a decoded JSON document *contains* an expected
fragment. Containment is not equality: keys that only ``actual`` carries are ignored, so a
fragment may name a handful of fields out of a large response body, while a mapping in
``expected`` must appear in ``actual`` with the same keys and values, a list must match element
for element and with the same length, and a scalar must match under type-aware equality - a JSON
``true`` never satisfies an expected ``1``, even though Python's ``True == 1``.

Whatever goes wrong, the failure names the *first* mismatching JSON path. Mappings are walked in
sorted key order and lists in index order, so "first" depends on the two values alone: the same
document and fragment report the same path on every run and on every xdist worker, which is what
makes a failure reproducible in CI.

Every value quoted in a message is rendered through the redaction engine
(:func:`foundry.obs.redaction.default_redactor`) and bounded by
:data:`foundry.obs.redaction.MAX_EXCERPT_BYTES`, because a response body under test often
carries a token and a subset failure must not print one. A value sitting under a secret-bearing
key is reported as ``***`` outright: a bare string carries no name for the engine to match on,
so rendering it verbatim would print the credential.

``actual`` and ``expected`` are annotated ``object`` rather than ``Any``. AAP 0.4.3 writes the
signature with ``Any``, while AAP 0.4.1 and 0.13.1 forbid ``Any`` in a public signature and
require a clean ``mypy --strict``; the JSON-shape rule - ``None``, ``bool``, ``int``, ``float``,
``str``, ``list`` or a mapping with ``str`` keys - is therefore documented here and enforced at
runtime instead. The name, the parameter shape including the keyword-only ``path`` and the
``None`` return are the ones AAP 0.4.3 and 0.4.9 fix for ``0.1.0``.
"""

from __future__ import annotations

import json
import keyword
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, cast

from foundry.obs.redaction import MAX_EXCERPT_BYTES, REDACTED, default_redactor

__all__ = ["assert_json_subset"]

#: Prefix carried by every message from this module, so a failure is attributable to the helper
#: that raised it without the reader having to open the traceback.
_PREFIX: Final[str] = "foundry.assertions.assert_json_subset:"

#: Suffix appended to a rendering that had to be cut. Three ASCII dots rather than the redaction
#: engine's longer marker, so a quoted value stays inside :data:`MAX_EXCERPT_BYTES`.
_TRUNCATION_SUFFIX: Final[str] = "..."

#: Marker for "this key is absent from the actual object", so a key holding ``None`` is not
#: reported as missing.
_MISSING: Final[object] = object()


def assert_json_subset(actual: object, expected: object, *, path: str = "$") -> None:
    """Assert that ``actual`` contains the JSON fragment ``expected``.

    ``expected`` is a fragment, not a whole document: keys and list elements it does not mention
    are never inspected, while every key and element it does mention must match. A failure names
    the first disagreement in sorted traversal order, so two runs over the same values produce
    the same message.

    Args:
        actual: The decoded JSON value under test, typically ``FoundryResponse.json()``.
        expected: The JSON fragment that must be contained in ``actual``.
        path: Rendering prefix for every reported path. It must be a ``str`` beginning with
            ``"$"``, so ``path="$.data"`` makes a nested check report ``$.data.items[0].price``.

    Returns:
        None. The call reports its result through the exception it raises, which is what lets a
        test use it as a bare statement.

    Raises:
        AssertionError: When ``actual`` does not contain ``expected``. The message begins
            ``foundry.assertions.assert_json_subset: JSON subset mismatch at <path>:`` and quotes
            only redacted, bounded values.
        AssertionError: When the call itself is wrong - a ``path`` that is not a ``str``
            beginning with ``"$"``, an ``expected`` value that is not JSON (a ``set``, ``bytes``,
            a dataclass), or a mapping in ``expected`` with a non-``str`` key. The message begins
            ``foundry.assertions.assert_json_subset:`` and names what is wrong.

    Examples:
        With ``response`` holding ``{"id": 7, "items": [{"sku": "a", "price": 9.99}]}`` plus a
        ``trace`` object the fragment never mentions:

            assert_json_subset(response, {"id": 7, "items": [{"sku": "a"}]})  # returns None
            assert_json_subset(response, {"items": [{"price": 1.0}]})
            # raises AssertionError naming $.items[0].price: expected 1.0, actual 9.99
    """
    if not isinstance(path, str) or not path.startswith("$"):
        raise _misuse(f"path must be a str beginning with '$', got {_render(path)}")
    mismatch = _compare(expected, actual, path)
    if mismatch is not None:
        raise AssertionError(mismatch.message())


@dataclass(frozen=True, slots=True)
class _Mismatch:
    """The first place where an expected fragment and an actual value disagree.

    Attributes:
        path: The JSON path of the disagreement, already rendered (``$.items[1].price``).
        detail: A single-line description of what differs, secret-free and bounded.
    """

    path: str
    detail: str

    def message(self) -> str:
        """Return the assertion message this mismatch is raised with.

        Returns:
            The message, beginning with :data:`_PREFIX` and naming the path and the detail.
        """
        return f"{_PREFIX} JSON subset mismatch at {self.path}: {self.detail}"


def _compare(
    expected: object,
    actual: object,
    path: str,
    *,
    secret: bool = False,
) -> _Mismatch | None:
    """Compare one expected fragment with the actual value, returning the first mismatch.

    Recursion follows ``expected``, so the fragment's own depth bounds the walk and no depth
    guard is needed: a self-referential ``actual`` is never traversed.

    Args:
        expected: The fragment that must be contained in ``actual``.
        actual: The decoded JSON value found at ``path``.
        path: JSON path of this pair, used verbatim in the mismatch it may produce.
        secret: True when this subtree sits under a secret-bearing key, in which case quoted
            values are withheld.

    Returns:
        The first :class:`_Mismatch` in sorted traversal order, or ``None`` when the fragment is
        contained.

    Raises:
        AssertionError: When ``expected`` is not a JSON value, or a mapping in it carries a
            non-``str`` key.
    """
    if isinstance(expected, Mapping):
        return _compare_object(_string_keyed(expected, path), actual, path, secret=secret)
    if isinstance(expected, list | tuple):
        return _compare_list(expected, actual, path, secret=secret)
    if expected is None or isinstance(expected, bool | int | float | str):
        return _compare_scalar(expected, actual, path, secret=secret)
    raise _misuse(
        f"expected value at {path} is not a JSON value: {_type_name(expected)} {_render(expected)}"
    )


def _compare_object(
    expected: Mapping[str, object],
    actual: object,
    path: str,
    *,
    secret: bool,
) -> _Mismatch | None:
    """Compare an expected object with the actual value, key by key in sorted order.

    Keys present only in ``actual`` are ignored, which is what makes the check a containment
    check rather than an equality check.

    Args:
        expected: The expected object, already proven to be string-keyed.
        actual: The decoded JSON value found at ``path``.
        path: JSON path of this pair.
        secret: True when this subtree sits under a secret-bearing key.

    Returns:
        The first :class:`_Mismatch` in sorted key order, or ``None`` when every expected key is
        present with a matching value.

    Raises:
        AssertionError: Propagated when a nested fragment is not a JSON value.
    """
    if not isinstance(actual, Mapping):
        return _Mismatch(path, f"expected object, actual {_type_name(actual)}")
    mapping = cast("Mapping[str, object]", actual)
    for key in sorted(expected):
        child = _child(path, key)
        found = mapping.get(key, _MISSING)
        if found is _MISSING:
            return _Mismatch(child, f"expected key {key!r} is absent from the actual object")
        mismatch = _compare(expected[key], found, child, secret=secret or _redacts(key))
        if mismatch is not None:
            return mismatch
    return None


def _compare_list(
    expected: Sequence[object],
    actual: object,
    path: str,
    *,
    secret: bool,
) -> _Mismatch | None:
    """Compare an expected list with the actual value, index by index.

    Args:
        expected: The expected list. A ``str`` is never treated as one, because a string in
            ``expected`` is a scalar.
        actual: The decoded JSON value found at ``path``.
        path: JSON path of this pair.
        secret: True when this subtree sits under a secret-bearing key.

    Returns:
        The first :class:`_Mismatch` in index order, or ``None`` when both lists are the same
        length and every element matches.
    """
    if not isinstance(actual, list | tuple):
        return _Mismatch(path, f"expected list, actual {_type_name(actual)}")
    if len(expected) != len(actual):
        return _Mismatch(
            path,
            f"expected list of {len(expected)} elements, actual list of {len(actual)} elements",
        )
    for index, item in enumerate(expected):
        mismatch = _compare(item, actual[index], _index(path, index), secret=secret)
        if mismatch is not None:
            return mismatch
    return None


def _compare_scalar(
    expected: object,
    actual: object,
    path: str,
    *,
    secret: bool,
) -> _Mismatch | None:
    """Compare an expected scalar with the actual value using type-aware equality.

    A difference of kind is reported by type name alone, because the types locate the bug in one
    line: ``expected string, actual number`` says more than the two renderings would. Two values
    of the same kind are reported by their redacted renderings.

    Args:
        expected: The expected scalar: ``None``, a ``bool``, a number or a ``str``.
        actual: The decoded JSON value found at ``path``.
        path: JSON path of this pair.
        secret: True when this subtree sits under a secret-bearing key.

    Returns:
        ``None`` when the two are equal, otherwise the :class:`_Mismatch` describing them.
    """
    if _equal(expected, actual):
        return None
    expected_kind = _type_name(expected)
    actual_kind = _type_name(actual)
    if expected_kind != actual_kind:
        return _Mismatch(path, f"expected {expected_kind}, actual {actual_kind}")
    return _Mismatch(
        path,
        f"expected {_render_leaf(expected, secret=secret)}, "
        f"actual {_render_leaf(actual, secret=secret)}",
    )


def _equal(expected: object, actual: object) -> bool:
    """Compare two JSON scalars, refusing the ``True == 1`` false pass.

    Args:
        expected: The expected scalar.
        actual: The value found in ``actual``.

    Returns:
        True when the two are equal. A ``bool`` equals only a ``bool`` of the same value, which
        is why the comparison is not left to ``==`` alone; ``int`` and ``float`` still compare
        numerically with each other, and ``str`` and ``None`` compare with ``==``.
    """
    if isinstance(expected, bool) or isinstance(actual, bool):
        return isinstance(expected, bool) and isinstance(actual, bool) and expected == actual
    return expected == actual


def _string_keyed(source: Mapping[object, object], path: str) -> Mapping[str, object]:
    """Return ``source`` once its keys are proven to be strings.

    Args:
        source: The expected object, whose keys must be JSON object keys.
        path: JSON path of ``source``, named in the misuse message.

    Returns:
        The same mapping, typed as string-keyed. It is not copied, so nothing this module does
        is observable through it.

    Raises:
        AssertionError: When a key is not a ``str``.
    """
    for key in source:
        if not isinstance(key, str):
            raise _misuse(
                f"expected object at {path} must use string keys, found a "
                f"{_type_name(key)} key {_render(key)}"
            )
    return cast("Mapping[str, object]", source)


def _child(path: str, key: str) -> str:
    """Return the JSON path of ``key`` inside the object at ``path``.

    A key that reads as a Python identifier renders in dotted form (``$.items``); anything else
    renders as a quoted subscript (``$["a b"]``, ``$["a.b"]``), so a key holding a dot or a space
    cannot be mistaken for a nesting level.

    Args:
        path: JSON path of the object holding ``key``.
        key: The key to descend to.

    Returns:
        The child path.
    """
    if key.isidentifier() and not keyword.iskeyword(key):
        return f"{path}.{key}"
    return f"{path}[{json.dumps(key, ensure_ascii=False)}]"


def _index(path: str, index: int) -> str:
    """Return the JSON path of ``index`` inside the list at ``path``.

    Args:
        path: JSON path of the list.
        index: Zero-based element index.

    Returns:
        The element path, such as ``$.items[1]``.
    """
    return f"{path}[{index}]"


def _render(value: object) -> str:
    """Render a value for a message: redacted, JSON-encoded, single-line and bounded.

    Redaction runs before truncation, because cutting first can leave the prefix of a credential
    inside the excerpt.

    Args:
        value: Any value, whether JSON-shaped or not.

    Returns:
        The redacted rendering, at most :data:`MAX_EXCERPT_BYTES` characters including the
        ``...`` suffix when the value had to be cut, and free of newlines.
    """
    try:
        encoded = json.dumps(
            default_redactor().json_value(value),
            sort_keys=True,
            ensure_ascii=False,
            default=repr,
        )
    except (TypeError, ValueError):
        # A cyclic or otherwise unencodable value still has to produce a usable message.
        encoded = repr(value)
    rendered = " ".join(encoded.splitlines())
    if len(rendered) <= MAX_EXCERPT_BYTES:
        return rendered
    return rendered[: MAX_EXCERPT_BYTES - len(_TRUNCATION_SUFFIX)] + _TRUNCATION_SUFFIX


def _render_leaf(value: object, *, secret: bool) -> str:
    """Render a scalar for a message, withholding it when its key is secret-bearing.

    Args:
        value: The scalar to render.
        secret: True when the value sits under a key that
            :meth:`foundry.obs.redaction.Redactor.redacts` recognises. A bare string carries no
            name for the redaction engine to match on, so rendering it would print the
            credential it may well be.

    Returns:
        Either :data:`foundry.obs.redaction.REDACTED` or the bounded rendering of ``value``.
    """
    if secret:
        return REDACTED
    return _render(value)


def _redacts(name: str) -> bool:
    """Report whether the redaction engine treats ``name`` as secret-bearing.

    Args:
        name: A JSON object key.

    Returns:
        True when the built-in deny-lists or the credential name pattern recognise the key, so
        the values under it are withheld from every message.
    """
    return default_redactor().redacts(name)


def _type_name(value: object) -> str:
    """Return the JSON type name of a value.

    Args:
        value: Any value.

    Returns:
        ``object``, ``list``, ``string``, ``number``, ``boolean`` or ``null`` for a JSON value,
        and the Python type name for anything else. The reader of a failure message is comparing
        it against a response body, so the vocabulary is JSON's rather than Python's.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, list | tuple):
        return "list"
    return type(value).__name__


def _misuse(detail: str) -> AssertionError:
    """Build the error raised when the helper is called with something it cannot check.

    Args:
        detail: What is wrong with the call, as a single line.

    Returns:
        The error, for the caller to raise. It is returned rather than raised here so the
        ``raise`` statement stays visible at every call site.
    """
    return AssertionError(f"{_PREFIX} {detail}")
