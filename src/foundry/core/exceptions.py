"""The sole definition site of Foundry's exception types (AAP 0.4.9, AAP 0.14.3).

Only these types cross a module boundary (AAP 0.13.1): ``str()`` renders the message alone, so the
texts the behaviour contract pins reach a terminal unchanged, while the hint and the structured
context stay on the attributes and in ``repr()``, which echoes a value only in its bounded token
shape and describes any other by its length, keeping a credential out of a file, the terminal and a
report (AAP 0.1.1, AAP 0.14.3). A capability package composes the message from the context it alone
holds, so a caller branches on ``DataError.column`` rather than parsing prose.
:class:`FieldValueError` and :class:`ElementTypeError` serve modules that must raise a
``ValueError``- or ``TypeError``-compatible fault, and both stay out of ``__all__``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

from foundry.core.types import AttemptRecord

__all__: Final[tuple[str, ...]] = (
    "ArtifactError",
    "CassetteMissError",
    "ConfigError",
    "ContractError",
    "DataError",
    "FoundryError",
    "RetryExhaustedError",
    "TransportError",
)


_ECHOABLE_ATTRIBUTE: Final[re.Pattern[str]] = re.compile(r"[^\s\x00-\x1f\x7f]{1,120}")
"""The shape a value needs before ``repr()`` echoes it: one visible token, at most 120 chars."""

_MASKED_ATTRIBUTE: Final[str] = "<redacted: {length} characters>"
"""What ``repr()`` shows instead of free text: the value's length alone."""


def _present(**values: object) -> tuple[tuple[str, object], ...]:
    """Return the keyword arguments whose value is not ``None``, in declaration order."""
    return tuple((name, value) for name, value in values.items() if value is not None)


def _render_attribute(name: str, value: object) -> str:
    """Return one ``name=value`` pair for :meth:`FoundryError.__repr__`, and never raise.

    Only text in the shape :data:`_ECHOABLE_ATTRIBUTE` describes is echoed; any other text becomes
    its length, so fault detail or a credential never reaches the terminal.
    """
    if not isinstance(value, str) or _ECHOABLE_ATTRIBUTE.fullmatch(value) is not None:
        return f"{name}={value!r}"
    return f"{name}={_MASKED_ATTRIBUTE.format(length=len(value))!r}"


class FoundryError(Exception):
    """Base class of every exception Foundry raises (AAP 0.4.9).

    ``message`` is the text the raising module composed, which ``str()`` renders exactly, and
    ``hint`` an actionable next step or ``None``. No attribute of any subclass holds a credential:
    the configuration keeps the *names* of the environment variables that hold secrets, not their
    values (implicit requirement I9).
    """

    message: str
    hint: str | None

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        """Store the message and its optional hint."""
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        """Return the message alone, without the hint or the structured context."""
        return self.message

    def format_message(self) -> str:
        """Return the message, then the hint on an indented second line; no trailing newline."""
        if self.hint is None:
            return self.message
        return f"{self.message}\n  {self.hint}"

    def __repr__(self) -> str:
        """Return ``ClassName(message=..., <attribute>=...)``: deterministic, and never raising.

        The attributes come in declaration order, a default never passed left out, and one is echoed
        only in its class's token shape, so free text and credentials stay off the terminal.
        """
        parts = [f"message={self.message!r}"]
        parts.extend(_render_attribute(name, value) for name, value in self._attribute_items())
        return f"{type(self).__name__}({', '.join(parts)})"

    def _attribute_items(self) -> tuple[tuple[str, object], ...]:
        """Return the structured attributes :meth:`__repr__` includes."""
        return ()


class ConfigError(FoundryError):
    """A configuration fault, raised by :mod:`foundry.config`; the command line maps it to exit 2.

    ``key``, ``layer`` and ``path`` name the dotted key at fault (``http.timeout``), the resolution
    layer (``default``, ``file``, ``profile:<name>``, ``env``, ``cli``) and the file, each ``None``
    when it does not apply. An absent auth variable is named by variable, never by value.
    """

    key: str | None
    layer: str | None
    path: Path | None

    def __init__(
        self,
        message: str,
        *,
        key: str | None = None,
        layer: str | None = None,
        path: Path | None = None,
        hint: str | None = None,
    ) -> None:
        """Store the message and the location the fault came from."""
        super().__init__(message, hint=hint)
        self.key = key
        self.layer = layer
        self.path = path

    def _attribute_items(self) -> tuple[tuple[str, object], ...]:
        """Return ``key``, ``layer`` and ``path`` in declaration order."""
        return _present(key=self.key, layer=self.layer, path=self.path)


class DataError(FoundryError):
    """A data-file fault, raised by :mod:`foundry.data` while it collects cases.

    The location is mandatory: pytest reports the fault as a collection error, exit ``2`` when
    nothing else was collected. ``file``, ``line`` and ``column`` carry it, the last two ``None``
    for a file-wide fault.
    """

    file: Path
    line: int | None
    column: str | None

    def __init__(
        self,
        message: str,
        *,
        file: Path,
        line: int | None = None,
        column: str | None = None,
        hint: str | None = None,
    ) -> None:
        """Store the message and the location in the data file."""
        super().__init__(message, hint=hint)
        self.file = file
        self.line = line
        self.column = column

    def _attribute_items(self) -> tuple[tuple[str, object], ...]:
        """Return ``file``, ``line`` and ``column`` in declaration order."""
        return _present(file=self.file, line=self.line, column=self.column)


class ContractError(FoundryError):
    """A contract violation, raised by :mod:`foundry.contract` and the schema assertion.

    ``json_path`` is the deterministic first violation -- shallowest path, then path, then message
    -- and ``detail`` names the validator that rejected it.
    """

    json_path: str
    detail: str

    def __init__(
        self,
        message: str,
        *,
        json_path: str,
        detail: str,
        hint: str | None = None,
    ) -> None:
        """Store the message and the violation it reports."""
        super().__init__(message, hint=hint)
        self.json_path = json_path
        self.detail = detail

    def _attribute_items(self) -> tuple[tuple[str, object], ...]:
        """Return ``json_path`` and ``detail`` in declaration order."""
        return _present(json_path=self.json_path, detail=self.detail)


class TransportError(FoundryError):
    """A fault below the HTTP protocol, raised by :mod:`foundry.http`.

    A 4xx or 5xx response is returned, not raised. ``method`` and ``path`` locate the request;
    ``cause`` is a token by contract -- ``timeout``, ``connect``, ``status_5xx``, ``status_429`` or
    a transport exception's class name such as ``ConnectError`` -- and never free text.
    """

    method: str
    path: str
    cause: str

    def __init__(
        self,
        message: str,
        *,
        method: str,
        path: str,
        cause: str,
        hint: str | None = None,
    ) -> None:
        """Store the message and the request the fault happened on."""
        super().__init__(message, hint=hint)
        self.method = method
        self.path = path
        self.cause = cause

    def _attribute_items(self) -> tuple[tuple[str, object], ...]:
        """Return ``method``, ``path`` and ``cause`` in declaration order."""
        return _present(method=self.method, path=self.path, cause=self.cause)


class RetryExhaustedError(TransportError):
    """Every attempt at a request failed, raised by :mod:`foundry.http.retries`.

    ``attempts`` holds one immutable record per attempt, the first included, copied on construction
    so a caller appending to its own list cannot change the error raised.
    """

    attempts: list[AttemptRecord]

    def __init__(
        self,
        message: str,
        *,
        method: str,
        path: str,
        cause: str,
        attempts: list[AttemptRecord],
        hint: str | None = None,
    ) -> None:
        """Store the message, the request and the attempts it took."""
        super().__init__(message, method=method, path=path, cause=cause, hint=hint)
        self.attempts = list(attempts)

    def _attribute_items(self) -> tuple[tuple[str, object], ...]:
        """Return the inherited request attributes and then ``attempts``."""
        return _present(
            method=self.method, path=self.path, cause=self.cause, attempts=self.attempts
        )


class CassetteMissError(FoundryError):
    """A replay found no recorded interaction, raised by :mod:`foundry.cassette`.

    ``method`` and ``path`` are the request that found no match and ``cassette_path`` the document
    searched. No socket is opened here, which is what makes a replay run provably offline.
    """

    method: str
    path: str
    cassette_path: Path

    def __init__(
        self,
        message: str,
        *,
        method: str,
        path: str,
        cassette_path: Path,
        hint: str | None = None,
    ) -> None:
        """Store the message and the request no interaction matched."""
        super().__init__(message, hint=hint)
        self.method = method
        self.path = path
        self.cassette_path = cassette_path

    def _attribute_items(self) -> tuple[tuple[str, object], ...]:
        """Return ``method``, ``path`` and ``cassette_path`` in declaration order."""
        return _present(method=self.method, path=self.path, cassette_path=self.cassette_path)


class ArtifactError(FoundryError):
    """An artifact or cassette path escaped its directory, raised by :mod:`foundry.obs`.

    ``attempted_path`` is the resolved path that was rejected and ``run_dir`` the directory it had
    to stay inside. The writer logs it at ``WARNING`` and disables the record's artifacts rather
    than failing the test, so a hostile node id cannot change an outcome.
    """

    attempted_path: Path
    run_dir: Path

    def __init__(
        self,
        message: str,
        *,
        attempted_path: Path,
        run_dir: Path,
        hint: str | None = None,
    ) -> None:
        """Store the message and the path that was rejected."""
        super().__init__(message, hint=hint)
        self.attempted_path = attempted_path
        self.run_dir = run_dir

    def _attribute_items(self) -> tuple[tuple[str, object], ...]:
        """Return ``attempted_path`` and ``run_dir`` in declaration order."""
        return _present(attempted_path=self.attempted_path, run_dir=self.run_dir)


class FieldValueError(FoundryError, ValueError):
    """A field of a record holds a value the field's documented form does not allow.

    :mod:`foundry.reporting.record` raises it for each rule it states. The ``ValueError`` base lets
    pydantic wrap it into one ``ValidationError``; the class is additive under the compatibility
    promise (AAP 0.4.9) and declared here as the only definition site (AAP 0.14.3).
    """


class ElementTypeError(FoundryError, TypeError):
    """A serialiser was handed an element that is not one of the types it accepts.

    :func:`foundry.reporting.store.dumps` renders ``results.json`` from result records and run-error
    elements only, so any other element is a caller defect and surfaces. The ``TypeError`` base
    keeps an existing ``except TypeError`` working; additive under AAP 0.4.9, defined here only.
    """
