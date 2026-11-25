"""The identifiers Foundry emits, and the tree's only wall-clock reader.

Run ids, case ids and timestamps are derived from content alone -- never a worker id, host
name or the clock -- so a serial run and every xdist worker agree (implicit requirement I6).
``utc_now`` is the one wall-clock read in ``src/foundry`` (AAP 0.14.3, AAP 0.4.8).
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


def utc_now() -> datetime:
    """Return the current instant as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def _as_utc(moment: datetime) -> datetime:
    """Normalise a datetime to timezone-aware UTC, reading a naive value as UTC."""
    if moment.utcoffset() is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def to_iso8601(moment: datetime) -> str:
    """Render a datetime as an ISO 8601 UTC timestamp at millisecond precision, never ``Z``."""
    return _as_utc(moment).isoformat(timespec="milliseconds")


def parse_iso8601(text: str) -> datetime:
    """Read an ISO 8601 timestamp, accepting a trailing ``Z`` and no offset.

    Returns it timezone-aware and in UTC, and raises FoundryError when the text is not an ISO 8601
    timestamp (AAP 0.14.3).
    """
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise FoundryError(
            f"not an ISO 8601 timestamp: {text!r}",
            hint="expected a value such as 2025-01-02T03:04:05.123+00:00",
        ) from exc
    return _as_utc(parsed)


RUN_ID_FORMAT: Final[str] = "%Y%m%dT%H%M%S"
"""The ``strftime`` pattern the timestamp half of a run id is rendered with, in UTC."""

RUN_ID_TOKEN_HEX_DIGITS: Final[int] = 6
"""The number of lowercase hexadecimal characters a run id suffix carries (AAP 0.4.3)."""

RUN_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\d{8}T\d{6}-[0-9a-f]{6}$")
"""The shape of a run id (AAP 0.4.6): a UTC timestamp, a hyphen and six lowercase hex digits."""

_RUN_ID_TOKEN_BYTES: Final[int] = RUN_ID_TOKEN_HEX_DIGITS // 2
"""Bytes a suffix source must produce; every byte renders as two hex characters."""

_RUN_ID_SUFFIX_PATTERN: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{6}")
"""The shape an injected suffix must have; :func:`run_id` uses ``fullmatch``."""


def run_id(
    *,
    now: datetime | None = None,
    token_hex: Callable[[int], str] = secrets.token_hex,
) -> str:
    """Build a run identifier of the form ``YYYYmmddTHHMMSS-<6 hex>``.

    A reproducible id is a fixed ``now`` and suffix, both of which are injected seams (AAP 0.4.6),
    and the result reads ``20250102T030405-9f3c1a``.

    Raises:
        FoundryError: The injected suffix is not six lowercase hexadecimal characters.
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
    """Report whether a string matches :data:`RUN_ID_PATTERN`."""
    return RUN_ID_PATTERN.fullmatch(value) is not None


def _require_stem(stem: str) -> None:
    """Reject a blank data-file stem before it reaches an id.

    Raises:
        FoundryError: The stem is empty or contains only whitespace.
    """
    if not stem.strip():
        raise FoundryError(
            "case id requires a file stem", hint="pass the data file's stem, for example cart"
        )


def _canonical_json(value: object, axis: str) -> str:
    """Render a matrix axis value as canonical JSON text, its identity inside a case id.

    Raises:
        FoundryError: The value cannot be rendered as JSON even with ``str`` as the fallback.
    """
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
        )
    except (TypeError, ValueError) as exc:
        raise FoundryError(
            f"matrix value for axis {axis!r} is not serialisable: {exc}",
            hint="use a value JSON can render, or pass the text form of it",
        ) from exc


def case_id_for_row(stem: str, row_index: int) -> str:
    """Return the case id of one row of a tabular data file, such as ``cart-0``.

    The id is the file stem and the 0-based row index.

    Raises:
        FoundryError: The stem is blank, or the row index is negative (``Case.line`` is 1-based,
            AAP 0.7.2).
    """
    _require_stem(stem)
    if row_index < 0:
        raise FoundryError(
            f"row index must be zero or greater: {row_index}",
            hint="row_index counts data rows from zero; Case.line is the 1-based line",
        )
    return f"{stem}-{row_index}"


def case_id_for_matrix(stem: str, values: Mapping[str, object]) -> str:
    """Return the case id of one expanded matrix case, such as ``matrix-colour="red"-size="L"``.

    The axes are sorted by name and each value is canonical JSON.

    Raises:
        FoundryError: The stem is blank, an axis name is not a string, or a value is not JSON.
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
