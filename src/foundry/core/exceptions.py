"""The sole definition site of Foundry's exception types.

Eight public types live here, and no other module in the tree defines one: every
failure Foundry reports crosses a module boundary as one of them (AAP 0.13.1), and a
handler either raises one of these or lets the original exception propagate. The names,
the bases and the attributes below are part of the published compatibility promise, so a
later release may add to them but may never rename or remove them (AAP 0.4.9).

A capability package composes the message from the context it alone holds and attaches
that context as keyword arguments, so a caller branches on ``DataError.column`` instead
of parsing prose while the text a person reads is exactly what the raising module wrote.
``str()`` renders the message alone: pytest formats an exception as ``E
foundry.core.exceptions.ConfigError: <str(exc)>``, and the texts the behaviour contract
pins -- ``config file not found: <path>``, ``no cases in <file>``, ``configuration valid
(profile=<name>)`` -- must reach the terminal and a CI log unchanged. The hint and the
structured context stay on the attributes and in ``repr()``.

This module raises nothing itself, imports only the standard library and its sibling
:mod:`foundry.core.types`, and does no work at import time: no I/O, no environment read,
no logger configuration and no module-level mutable state beyond ``__all__``. Keeping
that discipline is what lets the plugin import it inside the startup budget.
"""

from __future__ import annotations

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


def _present(**values: object) -> tuple[tuple[str, object], ...]:
    """Return the keyword arguments whose value is not ``None``, in declaration order.

    Each subclass calls this with its attributes spelled out in the order the class
    declares them, so the order a ``repr`` shows is stated once, at the call site, and a
    default that was never passed is simply absent. Keyword-argument order is preserved
    by the language, which is what makes that call site the declaration of the order.

    Args:
        **values: The attributes to consider, named as the class names them.

    Returns:
        One ``(name, value)`` pair per argument whose value is not ``None``.
    """
    return tuple((name, value) for name, value in values.items() if value is not None)


class FoundryError(Exception):
    """Base class of every exception Foundry raises.

    Only these types cross a module boundary (AAP 0.13.1): a handler raises one of them
    with the structured context its contract promises, or lets the original propagate.
    Nothing catches a test's own exception and rewrites it, and nothing swallows a
    failure to keep a run green.

    ``hint`` carries an actionable next step for the person reading the failure -- the
    spelling of a close configuration key, the command that re-records a cassette -- and
    never a secret value. No attribute of any subclass may hold a credential: the
    configuration stores the *names* of the environment variables that hold secrets, not
    the secrets themselves, so a configuration echo, a result record and a report stay
    safe to inspect (implicit requirement I9).

    Attributes:
        message: The text the raising module composed; ``str()`` renders exactly this.
        hint: An actionable next step, or ``None`` when the message stands alone.
    """

    message: str
    hint: str | None

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        """Store the message and its optional hint.

        Args:
            message: The complete message, composed by the raising module. It is
                forwarded to ``BaseException`` so ``exc.args == (message,)`` holds for
                every subclass and a re-raise keeps the original text.
            hint: An actionable next step, or ``None``.
        """
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        """Return the message alone, without the hint or the structured context.

        pytest renders an exception as ``E   foundry.core.exceptions.ConfigError:
        <str(exc)>``, and the behaviour contract pins several texts that must survive
        that rendering byte for byte. Anything appended here -- the hint, the offending
        file, the JSON path -- would change what a reviewer greps for, so it stays off
        ``str()`` and on the attributes instead.

        Returns:
            The message exactly as the raising module composed it.
        """
        return self.message

    def format_message(self) -> str:
        """Return the message and, when present, the hint on an indented second line.

        This is the single seam the command line uses to print an error: the message on
        stderr, then an indented hint line when one exists. Keeping the layout here means
        no subcommand duplicates it and every surface indents the hint the same two
        spaces the report and CLI tables use.

        Returns:
            The message alone when there is no hint, otherwise the message, a newline,
            two spaces and the hint. No trailing newline is included, so a caller decides
            how the line ends.
        """
        if self.hint is None:
            return self.message
        return f"{self.message}\n  {self.hint}"

    def __repr__(self) -> str:
        """Return a stable representation of the error and the context it carries.

        A ``repr`` is read in a debugger and in a failing test's summary, so it is
        deterministic: the class name, the message, then the subclass's attributes in
        declaration order, with a default that was never passed left out rather than
        shown as ``None``.

        Returns:
            A string such as
            ``ConfigError(message='unknown key: htpp', key='htpp', layer='file')``.
        """
        parts = [f"message={self.message!r}"]
        parts.extend(f"{name}={value!r}" for name, value in self._attribute_items())
        return f"{type(self).__name__}({', '.join(parts)})"

    def _attribute_items(self) -> tuple[tuple[str, object], ...]:
        """Return the structured attributes :meth:`__repr__` should include.

        The base class declares none of its own beyond the message, so it returns an
        empty tuple; each subclass overrides this to list the attributes it declares, in
        declaration order.

        Returns:
            An empty tuple.
        """
        return ()


class ConfigError(FoundryError):
    """A configuration fault, raised by :mod:`foundry.config`.

    It covers every way a configuration can fail to resolve: an unknown key in any
    layer, a value outside its accepted range (``http.timeout = -1``), an unparseable
    environment value, a missing explicitly named configuration file, and an absent
    environment variable that a configured auth strategy names. In the last case the
    message names the variable and never its value, because the value would otherwise
    reach the terminal, a CI log and a report. The command line maps the class to exit
    code ``2`` and reports it before pytest starts, so no collection happens.

    Attributes:
        key: The dotted configuration key at fault (``http.timeout``), or ``None`` when
            the fault is not about one key, such as a file that does not exist.
        layer: The resolution layer the fault came from -- ``default``, ``file``,
            ``profile:<name>``, ``env`` or ``cli`` -- or ``None``.
        path: The configuration file at fault, or ``None``.
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
        """Store the message and the location the fault came from.

        Args:
            message: The complete message; for a range fault it names the key and the
                accepted range, and for an unknown key it names the closest valid key.
            key: The dotted configuration key at fault, or ``None``.
            layer: The resolution layer the fault came from, or ``None``.
            path: The configuration file at fault, or ``None``.
            hint: An actionable next step, or ``None``.
        """
        super().__init__(message, hint=hint)
        self.key = key
        self.layer = layer
        self.path = path

    def _attribute_items(self) -> tuple[tuple[str, object], ...]:
        """Return ``key``, ``layer`` and ``path`` in declaration order.

        Returns:
            One pair per attribute that was passed.
        """
        return _present(key=self.key, layer=self.layer, path=self.path)


class DataError(FoundryError):
    """A data-file fault, raised by :mod:`foundry.data` while it collects cases.

    It covers a row that is missing a required column, a row whose value does not match
    the declared schema, and a file whose extension no loader is registered for. The
    location is mandatory rather than optional: naming the file, the line and the column
    is the whole point of the class, so ``file`` may not be defaulted and the raising
    loader passes ``line`` and ``column`` whenever it knows them. pytest reports it as a
    collection error, which ends the run with exit code ``2`` when nothing else was
    collected and ``1`` when the rest of the session ran.

    Attributes:
        file: The data file the fault is in.
        line: The 1-based line the fault is on, or ``None`` when the fault is about the
            file as a whole, such as an unsupported extension.
        column: The name of the column at fault, or ``None``.
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
        """Store the message and the location in the data file.

        Args:
            message: The complete message, naming the file, the line and the column.
            file: The data file the fault is in.
            line: The 1-based line the fault is on, or ``None`` for a file-wide fault.
            column: The name of the column at fault, or ``None``.
            hint: An actionable next step, or ``None``.
        """
        super().__init__(message, hint=hint)
        self.file = file
        self.line = line
        self.column = column

    def _attribute_items(self) -> tuple[tuple[str, object], ...]:
        """Return ``file``, ``line`` and ``column`` in declaration order.

        Returns:
            One pair per attribute that was passed, with ``file`` always present.
        """
        return _present(file=self.file, line=self.line, column=self.column)


class ContractError(FoundryError):
    """A contract violation, raised by :mod:`foundry.contract` and the schema assertion.

    Both locations are mandatory. ``json_path`` is the deterministic first violation's
    path -- shallowest path first, then path, then message -- rather than whichever
    error the validator happened to yield, so the same invalid document always reports
    the same violation. ``detail`` says what the violation is, and a body that was
    expected to be JSON but was not carries a redacted excerpt of its first bytes in the
    message.

    Attributes:
        json_path: The JSON path of the first violation, such as ``$.items[0].id``.
        detail: What is wrong at that path, naming the validator that rejected it.
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
        """Store the message and the violation it reports.

        Args:
            message: The complete message, quoting the path and a redacted excerpt where
                a body is involved.
            json_path: The JSON path of the first violation.
            detail: What is wrong at that path.
            hint: An actionable next step, or ``None``.
        """
        super().__init__(message, hint=hint)
        self.json_path = json_path
        self.detail = detail

    def _attribute_items(self) -> tuple[tuple[str, object], ...]:
        """Return ``json_path`` and ``detail`` in declaration order.

        Returns:
            The violation's path and detail.
        """
        return _present(json_path=self.json_path, detail=self.detail)


class TransportError(FoundryError):
    """A fault below the HTTP protocol, raised by :mod:`foundry.http`.

    :meth:`foundry.http.FoundrySession.request` returns every status code, including
    4xx and 5xx, as a response object rather than raising; this class and
    :class:`RetryExhaustedError` are the only escapes from that method. It covers a
    connection that could not be established, a timeout, and a response body the
    transport could not read to completion, so a test can distinguish "the service said
    no" from "the service was never reached".

    Attributes:
        method: The request method, such as ``GET``.
        path: The request path, relative to ``http.base_url``.
        cause: The kind of fault, from the retry predicates -- ``timeout``,
            ``connect`` -- or the transport exception's class name.
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
        """Store the message and the request the fault happened on.

        Args:
            message: The complete message, naming the method, the path and the cause.
            method: The request method.
            path: The request path.
            cause: The kind of fault, or the transport exception's class name.
            hint: An actionable next step, or ``None``.
        """
        super().__init__(message, hint=hint)
        self.method = method
        self.path = path
        self.cause = cause

    def _attribute_items(self) -> tuple[tuple[str, object], ...]:
        """Return ``method``, ``path`` and ``cause`` in declaration order.

        Returns:
            The three attributes naming the failed request.
        """
        return _present(method=self.method, path=self.path, cause=self.cause)


class RetryExhaustedError(TransportError):
    """Every attempt at a request failed, raised by :mod:`foundry.http.retries`.

    It carries the attempts in order, so a failing test reports what each one did --
    which predicate triggered the next, how long it took and how it ended -- instead of
    only the last failure. The list is copied on construction: a caller that appends to
    its own list afterwards cannot change the error it has already raised, and each
    attempt record is itself immutable.

    Attributes:
        method: The request method, inherited from :class:`TransportError`.
        path: The request path, inherited from :class:`TransportError`.
        cause: The kind of the final fault, inherited from :class:`TransportError`.
        attempts: One record per attempt, including the first.
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
        """Store the message, the request and the attempts it took.

        Args:
            message: The complete message, listing the attempts or their count.
            method: The request method.
            path: The request path.
            cause: The kind of the final fault.
            attempts: One record per attempt, including the first. The list is copied,
                so later mutation by the caller leaves the error unchanged.
            hint: An actionable next step, or ``None``.
        """
        super().__init__(message, method=method, path=path, cause=cause, hint=hint)
        self.attempts = list(attempts)

    def _attribute_items(self) -> tuple[tuple[str, object], ...]:
        """Return the inherited request attributes and then ``attempts``.

        Returns:
            ``method``, ``path``, ``cause`` and ``attempts``, in declaration order.
        """
        return _present(
            method=self.method,
            path=self.path,
            cause=self.cause,
            attempts=self.attempts,
        )


class CassetteMissError(FoundryError):
    """A replay found no recorded interaction, raised by :mod:`foundry.cassette`.

    It names the method, the path and the cassette file that was searched, because a
    miss is almost always one of three things: the cassette was never recorded, the
    test now issues a request the recording does not hold, or the cassette is in a
    different directory than ``cassette.dir`` names. No socket is opened on this path,
    which is what makes a replay run provably offline.

    Attributes:
        method: The request method that found no match.
        path: The request path that found no match.
        cassette_path: The cassette document that was searched.
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
        """Store the message and the request no interaction matched.

        Args:
            message: The complete message, naming the method, the path and the cassette.
            method: The request method that found no match.
            path: The request path that found no match.
            cassette_path: The cassette document that was searched.
            hint: An actionable next step, or ``None``.
        """
        super().__init__(message, hint=hint)
        self.method = method
        self.path = path
        self.cassette_path = cassette_path

    def _attribute_items(self) -> tuple[tuple[str, object], ...]:
        """Return ``method``, ``path`` and ``cassette_path`` in declaration order.

        Returns:
            The three attributes naming the miss.
        """
        return _present(method=self.method, path=self.path, cassette_path=self.cassette_path)


class ArtifactError(FoundryError):
    """An artifact or cassette path escaped its directory, raised by :mod:`foundry.obs`.

    Every path Foundry writes under the run directory is built from a test's node id,
    which a project controls, so the path is resolved before use and this class is
    raised when the resolution lands outside the run directory or the cassette
    directory. The writer logs it at ``WARNING`` and marks the record's artifacts
    disabled rather than failing the test, so a hostile test id cannot change an outcome.

    Attributes:
        attempted_path: The resolved path that was rejected.
        run_dir: The directory the path had to stay inside.
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
        """Store the message and the path that was rejected.

        Args:
            message: The complete message, naming the attempted path and the directory.
            attempted_path: The resolved path that was rejected.
            run_dir: The directory the path had to stay inside.
            hint: An actionable next step, or ``None``.
        """
        super().__init__(message, hint=hint)
        self.attempted_path = attempted_path
        self.run_dir = run_dir

    def _attribute_items(self) -> tuple[tuple[str, object], ...]:
        """Return ``attempted_path`` and ``run_dir`` in declaration order.

        Returns:
            The rejected path and the directory it had to stay inside.
        """
        return _present(attempted_path=self.attempted_path, run_dir=self.run_dir)
