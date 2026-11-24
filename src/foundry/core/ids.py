"""The identifiers Foundry emits, and the tree's only wall-clock reader.

Three kinds of identifier leave this framework: the run id that names one run and its
artifact directory, the case ids that name a data-driven test case, and the ISO 8601
timestamps every report carries. All three are produced here, so a shape changes in one
place.

The module also owns the clock read: :func:`utc_now` is the only place in ``src/foundry``
that reads the wall clock (AAP 0.14.3), and ``tests/unit/test_sleep_policy.py`` fails the
build when a direct read appears elsewhere.

Identifiers are derived from content alone -- a file stem, a row index, the axis values
of a matrix case -- and never from a worker id, a host name, a directory listing or the
clock, so a serial run and every xdist worker collect the same ids in the same order
(implicit requirement I6). The run id is the one identifier that involves time and
randomness, and both are injectable, so a test pins it without patching either.

The module obeys the foundation rule of the layering contract (AAP 0.4.8): it imports the
standard library and its sibling :mod:`foundry.core.exceptions`, and importing it does
nothing but compile the run-id pattern.
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Final

from foundry.core.exceptions import FoundryError

__all__: Final[tuple[str, ...]] = (
    "RUN_ID_FORMAT",
    "RUN_ID_PATTERN",
    "RUN_ID_TOKEN_HEX_DIGITS",
    "case_id_for_matrix",
    "case_id_for_row",
    "is_valid_run_id",
    "parse_iso8601",
    "run_id",
    "to_iso8601",
    "utc_now",
)

# --- The clock seam --------------------------------------------------------


def utc_now() -> datetime:
    """Return the current instant as a timezone-aware UTC datetime.

    This is the one permitted wall-clock read in ``src/foundry`` (AAP 0.14.3). Every other
    module calls it, so a test that needs a fixed date requests the ``foundry_clock``
    fixture instead and the suite stays independent of the host's date, timezone and
    locale.

    Returns:
        The current instant, timezone-aware and in UTC.
    """
    return datetime.now(UTC)


def _as_utc(moment: datetime) -> datetime:
    """Normalise a datetime to a timezone-aware UTC value.

    A naive value carries no offset, so it is read as UTC rather than as local time, which
    would make two machines disagree about the same value. An aware value is converted,
    which is a no-op when it already names UTC.

    Args:
        moment: The datetime to normalise.

    Returns:
        The same instant with ``UTC`` attached and any other offset converted.
    """
    if moment.utcoffset() is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


# --- ISO 8601 timestamps ---------------------------------------------------


def to_iso8601(moment: datetime) -> str:
    """Render a datetime as a fixed-shape ISO 8601 UTC timestamp.

    Fixed at millisecond precision with an explicit ``+00:00`` offset and never a ``Z``
    suffix: ``started_at``, ``finished_at`` and ``created_at`` stay comparable across runs,
    and ``results.json`` stays byte-stable once the determinism criterion's four fields are
    replaced.

    Args:
        moment: The instant to render. A naive value is treated as UTC.

    Returns:
        The timestamp, for example ``2025-01-02T03:04:05.123+00:00``.
    """
    return _as_utc(moment).isoformat(timespec="milliseconds")


def parse_iso8601(text: str) -> datetime:
    """Read an ISO 8601 timestamp produced by :func:`to_iso8601` or a close relative.

    The single reader of the shape :func:`to_iso8601` writes, so a stored ``results.json``
    is never parsed a second, subtly different way. A trailing ``Z`` and an absent offset
    are both accepted, and the result is always timezone-aware UTC.

    Args:
        text: The timestamp to read.

    Returns:
        The instant the text names, timezone-aware and in UTC.

    Raises:
        FoundryError: The text is not an ISO 8601 timestamp. The parser's own
            ``ValueError`` never escapes this module (AAP 0.14.3).
    """
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise FoundryError(
            f"not an ISO 8601 timestamp: {text!r}",
            hint="expected a value such as 2025-01-02T03:04:05.123+00:00",
        ) from exc
    return _as_utc(parsed)


# --- The run id ------------------------------------------------------------

RUN_ID_FORMAT: Final[str] = "%Y%m%dT%H%M%S"
"""The ``strftime`` pattern the timestamp half of a run id is rendered with, in UTC."""

RUN_ID_TOKEN_HEX_DIGITS: Final[int] = 6
"""The number of lowercase hexadecimal characters a run id suffix carries (AAP 0.4.3)."""

RUN_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\d{8}T\d{6}-[0-9a-f]{6}$")
"""The shape of a run id: a compact UTC timestamp, a hyphen and six lowercase hex digits.

The id is also the artifact directory name (AAP 0.4.6), so the pattern admits nothing
outside ``[0-9a-z-]`` and no uppercase, and it is matched against stored values too.
"""

_RUN_ID_TOKEN_BYTES: Final[int] = RUN_ID_TOKEN_HEX_DIGITS // 2
"""Bytes a suffix source must produce; every byte renders as two hex characters."""

_RUN_ID_SUFFIX_PATTERN: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{6}")
"""The shape an injected suffix must have; :func:`run_id` checks it with ``fullmatch``."""


def run_id(
    *,
    now: datetime | None = None,
    token_hex: Callable[[int], str] = secrets.token_hex,
) -> str:
    """Build a run identifier of the form ``YYYYmmddTHHMMSS-<6 hex>``.

    The id is generated once per session and names the run's artifact directory
    (AAP 0.4.6), so it has to be unique, filesystem-safe and sortable by time: the
    timestamp orders it and the suffix makes it unique within a second. The suffix comes
    from a CSPRNG-backed source rather than a process-salted digest, which keeps two
    workers on one host from colliding. Both inputs are seams, so a caller that needs a
    reproducible id passes a fixed ``now`` and a fixed ``token_hex``.

    Args:
        now: The instant to timestamp. An aware datetime is converted to UTC and a
            naive one is read as UTC; defaults to :func:`utc_now`.
        token_hex: A callable taking a byte count and returning that many bytes as
            lowercase hexadecimal text. Defaults to :func:`secrets.token_hex`.

    Returns:
        The run id, for example ``20250102T030405-9f3c1a``.

    Raises:
        FoundryError: The injected suffix is not six lowercase hexadecimal characters.
            Validating the seam means a bad fake in a test fails where it is passed
            rather than producing an id that :data:`RUN_ID_PATTERN` rejects later.
    """
    moment = utc_now() if now is None else _as_utc(now)
    suffix = token_hex(_RUN_ID_TOKEN_BYTES)
    if _RUN_ID_SUFFIX_PATTERN.fullmatch(suffix) is None:
        raise FoundryError(
            f"invalid run id suffix: {suffix!r}",
            hint="token_hex must return six lowercase hexadecimal characters",
        )
    value = f"{moment.strftime(RUN_ID_FORMAT)}-{suffix}"
    if RUN_ID_PATTERN.fullmatch(value) is None:
        raise FoundryError(
            f"malformed run id: {value!r}",
            hint="a run id is a UTC timestamp, a hyphen and six lowercase hex characters",
        )
    return value


def is_valid_run_id(value: str) -> bool:
    """Report whether a string has the shape of a run id.

    Shape validation only: a value whose timestamp half names no real calendar instant
    still passes, because once the id has been written into ``results.json`` it is an
    opaque label that a comparison reads back rather than a moment the code reinterprets.

    Args:
        value: The candidate run id.

    Returns:
        ``True`` when the value matches :data:`RUN_ID_PATTERN`, otherwise ``False``.
    """
    return RUN_ID_PATTERN.fullmatch(value) is not None


# --- Stable case ids -------------------------------------------------------


def _require_stem(stem: str) -> None:
    """Reject a blank data-file stem before it reaches an id.

    Every case id starts with the stem, so an empty or whitespace-only stem would produce
    an id that begins with a separator and reads as a different file's case.

    Args:
        stem: The candidate stem, normally a data file's ``Path.stem``.

    Raises:
        FoundryError: The stem is empty or contains only whitespace.
    """
    if not stem.strip():
        raise FoundryError(
            "case id requires a file stem",
            hint="pass the data file's stem, for example cart",
        )


def _canonical_json(value: object, axis: str) -> str:
    """Render a matrix axis value as canonical JSON text.

    The rendering is the value's identity inside a case id, so it must not depend on dict
    order, locale or the interpreter's default separators: ``sort_keys`` fixes the member
    order of a mapping, the explicit separators drop the padding and ``ensure_ascii``
    keeps a non-ASCII string out of the locale's hands. A value JSON cannot render is
    passed through ``str`` as a last resort rather than dropped, because two ids that
    cannot be told apart would collapse two cases.

    Args:
        value: The value the axis holds for this case.
        axis: The axis name, used only to name the axis in an error message.

    Returns:
        The canonical JSON text of the value: ``1`` for an integer, ``"x"`` for a
        string, ``{"a":1}`` for a mapping.

    Raises:
        FoundryError: The value cannot be rendered as JSON even with ``str`` as the
            fallback.
    """
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            default=str,
        )
    except (TypeError, ValueError) as exc:
        raise FoundryError(
            f"matrix value for axis {axis!r} is not serialisable: {exc}",
            hint="use a value JSON can render, or pass the text form of it",
        ) from exc


def case_id_for_row(stem: str, row_index: int) -> str:
    """Build the stable id of one row of a tabular data file.

    ``foundry.data.ids.case_id`` delegates here rather than restating the format, so the
    shape is defined once. The id depends on the file's stem and the row's position and on
    nothing else -- not the clock, the worker, the host or the file's directory -- so a
    serial run and every xdist worker agree (implicit requirement I6). It is a label, not a
    path: turning a node id into an artifact directory name belongs to
    ``foundry.core.paths.sanitize_nodeid``.

    Args:
        stem: The data file's stem, for example ``cart`` for ``cart.csv``.
        row_index: The 0-based index into the data rows, with the header excluded. This
            differs from ``Case.line``, which carries the 1-based line in the file
            (AAP 0.7.2).

    Returns:
        The case id, for example ``cart-0`` for the first data row of ``cart.csv``.

    Raises:
        FoundryError: The stem is blank or the row index is negative.
    """
    _require_stem(stem)
    if row_index < 0:
        raise FoundryError(
            f"row index must be zero or greater: {row_index}",
            hint="row_index counts data rows from zero; Case.line is the 1-based line",
        )
    return f"{stem}-{row_index}"


def case_id_for_matrix(stem: str, values: Mapping[str, object]) -> str:
    """Build the stable id of one expanded matrix case.

    ``foundry.data.ids.case_id`` delegates here rather than restating the format, so the
    shape is defined once. The id is derived only from the stem and the axis values, and
    the axes are sorted by name rather than left in the mapping's own order: sorting is
    what makes two collections agree however the axes were written down, which keeps a
    sharded run identical to a serial one (I6). An empty mapping is the degenerate single
    case, whose id is the bare stem.

    Args:
        stem: The matrix file's stem, for example ``matrix`` for ``matrix.yaml``.
        values: The axis values this case fixes, for example ``{"size": "L", "colour":
            "red"}``.

    Returns:
        The case id, for example ``matrix-colour="red"-size="L"``: axes sorted by name
        and each value rendered as canonical JSON, so ``"1"``, ``1`` and ``1.0`` stay
        distinct.

    Raises:
        FoundryError: The stem is blank, an axis name is not a string, or an axis value
            cannot be rendered as JSON.
    """
    _require_stem(stem)
    for name in values:
        if not isinstance(name, str):
            raise FoundryError(
                f"matrix axis names must be strings: {name!r}",
                hint="name every axis of a matrix case, for example {'size': 'L'}",
            )
    if not values:
        return stem
    rendered = (f"{axis}={_canonical_json(values[axis], axis)}" for axis in sorted(values))
    return f"{stem}-{'-'.join(rendered)}"
