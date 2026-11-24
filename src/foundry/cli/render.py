"""The one place Foundry renders terminal output.

Every command line module hands this renderer plain data -- rows, node ids, one message, one
document -- and receives nothing back: widths, colours, borders and the stdout/stderr choice live
here and nowhere else (AAP 0.7.1, AAP 0.7.4). The four surfaces are the ``Key`` / ``Value`` /
``Source`` table of ``foundry config show``, the ``Probe`` / ``Status`` / ``Detail`` table of
``foundry doctor``, the three labelled groups of ``foundry report compare``, and the plain lines,
messages, errors and documents the remaining commands print.

*Rich is imported lazily.* ``rich`` is imported inside the body of each function that needs it and
never at module scope, so ``import foundry.cli.render`` leaves ``rich`` out of :data:`sys.modules`
and the pytest activation path pays for nothing until a command actually renders (implicit
requirement I5, the 150 ms startup budget of AAP 0.14.2). ``click`` is imported at module scope for
the one thing that must not go through ``rich``: :func:`render_document`.

*Every rendered value is redacted.* Each function takes ``redactor`` and passes every cell, message,
hint and line through :meth:`foundry.obs.redaction.Redactor.text` before it reaches the console, so
the one redaction engine of AAP 0.3.2 covers the terminal as it covers every file (AAP 0.14.3). A
configuration error names the absent environment variable, never its value (implicit requirement
I9), and a rendering path here cannot reintroduce one.

*Output is deterministic.* No clock is read, nothing sleeps, no ``set`` or bare ``dict`` is
iterated on its way to the console, and every collection is printed in the order it arrived -- the
callers supply the sorted order (AAP 0.13.1). Machine-readable documents bypass ``rich`` entirely,
because a width-dependent wrap would corrupt a JSON or TOML document, which is why
:func:`render_document` writes through :func:`click.echo`.

*Presentation is plain English on rich's defaults.* Light, unstyled output with no custom theme and
no custom style table (AAP 0.6): ``pass``, ``warn`` and ``fail`` are distinguished by rich's own
built-in style names and by nothing else, and markup in a value is never interpreted -- every
value is wrapped in :class:`rich.text.Text` rather than handed to the markup parser, so a node id
such as ``test_login[case-1]`` prints as written.

*Faults are isolated.* Nothing here raises on a caller's data: a row that is not three cells
contributes the cells it has. A write fault such as :exc:`BrokenPipeError` or :exc:`OSError` is the
one thing allowed to leave a function, and it travels no further than the caller's guard, which
logs it at ``WARNING`` and changes neither a test's outcome nor an exit code (AAP 0.1.2).

Importing this module performs no I/O, reads no environment variable, configures no logger and holds
no mutable state (AAP 0.4.8); the only tables it populates are the read-only constants below.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Protocol, TypeAlias

import click

from foundry.obs.redaction import Redactor, default_redactor

if TYPE_CHECKING:
    from rich.console import Console
    from rich.table import Table
    from rich.text import Text

    from foundry.config.render import ConfigRow
    from foundry.diagnostics.report import ProbeResult
    from foundry.reporting.compare import ComparisonDiff

__all__: Final[tuple[str, ...]] = (
    "ComparisonGroups",
    "KeyValueSourceRow",
    "ProbeRow",
    "render_comparison",
    "render_config_table",
    "render_document",
    "render_error",
    "render_lines",
    "render_message",
    "render_not_applied",
    "render_probe_table",
)

_RowCells: TypeAlias = tuple[str, str, str]
"""The three text cells of one table row, the shape ``ConfigRow.as_tuple()`` returns."""

_CONFIG_COLUMNS: Final[tuple[str, str, str]] = ("Key", "Value", "Source")
"""The ``foundry config show`` headings, in order, as AAP 0.7.4 fixes them."""

_PROBE_COLUMNS: Final[tuple[str, str, str]] = ("Probe", "Status", "Detail")
"""The ``foundry doctor`` headings, in order, as AAP 0.7.4 fixes them."""

_NOT_APPLIED_TITLE: Final[str] = "not applied"
"""The heading of the separate section AAP 0.4.4 requires for overrides a command did not apply.

It is the same spelling :data:`foundry.config.render.NOT_APPLIED_SECTION` uses; it is restated here
rather than imported, because that module is an annotation-time dependency of this one only.
"""

_NEWLY_FAILING: Final[str] = "Newly failing"
"""The first comparison group's label, the regressions (AAP 0.7.4)."""

_NEWLY_PASSING: Final[str] = "Newly passing"
"""The second comparison group's label, the recoveries (AAP 0.7.4)."""

_STILL_FAILING: Final[str] = "Still failing"
"""The third comparison group's label, the failures both runs report (AAP 0.7.4)."""

_NONE_MARKER: Final[str] = "(none)"
"""What an empty comparison group prints instead of being omitted (AAP 0.7.4)."""

_LINE_INDENT: Final[str] = "  "
"""The two-space indent of a grouped node id under its label."""

_HINT_INDENT: Final[str] = "  "
"""The two-space indent of an error's hint, the shape ``FoundryError.format_message()`` prints."""

_PASS: Final[str] = "pass"
"""The lowercase word of a passing probe, the ``Status`` cell and the count line (AAP 0.7.4)."""

_WARN: Final[str] = "warn"
"""The lowercase word of a probe that reported something an operator should read as a warning."""

_FAIL: Final[str] = "fail"
"""The lowercase word of a failing probe, the one state that can clear the doctor verdict."""

_STATUS_WORDS: Final[tuple[str, str, str]] = (_PASS, _WARN, _FAIL)
"""The three states a probe reports, in ascending severity."""

_STATUS_STYLES: Final[Mapping[str, str]] = MappingProxyType(
    {_PASS: "green", _WARN: "yellow", _FAIL: "red"}
)
"""rich's built-in style names that distinguish the three states; no custom style is set."""


class KeyValueSourceRow(Protocol):
    """The structure of one ``foundry config show`` row, satisfied by ``ConfigRow``.

    It is a protocol rather than a concrete type because this renderer owns layout and not data:
    :func:`foundry.config.render.rows` produces the rows and this module only prints them, so the
    two can be tested apart without either importing the other (AAP 0.4.8).

    The members are read-only properties so that a frozen, slotted row -- which is what
    ``ConfigRow`` is, and what keeps a renderer from altering what a second reader sees -- satisfies
    the protocol; a mutable attribute would reject every frozen row.
    """

    @property
    def key(self) -> str:
        """Return the dotted configuration key, for example ``http.timeout``."""
        ...

    @property
    def value(self) -> str:
        """Return the value as the table prints it, already rendered for a human reader."""
        ...

    @property
    def source(self) -> str:
        """Return the layer label that supplied the value, such as ``env:FOUNDRY_HTTP__TIMEOUT``."""
        ...


class ProbeRow(Protocol):
    """The structure of one ``foundry doctor`` row, satisfied by ``ProbeResult``.

    ``ProbeResult.as_row()`` returns the same three cells as one plain ``(name, status,
    detail)`` tuple, so a caller may hand this renderer either the probe model or the row tuple it
    produced.
    """

    @property
    def name(self) -> str:
        """Return the probe's stable identifier, printed in the ``Probe`` column."""
        ...

    @property
    def status(self) -> str:
        """Return the probe's state: ``pass``, ``warn`` or ``fail``."""
        ...

    @property
    def detail(self) -> str:
        """Return the one-line explanation printed in the ``Detail`` column."""
        ...


class ComparisonGroups(Protocol):
    """The three node-id sets ``foundry report compare`` prints, satisfied by ``ComparisonDiff``.

    The renderer never re-sorts: each sequence is printed in the order it arrives, and the owners
    supply ascending, de-duplicated tuples (AAP 0.13.1).
    """

    @property
    def newly_failing(self) -> Sequence[str]:
        """Return the node ids that fail in the newer run and did not fail in the baseline."""
        ...

    @property
    def newly_passing(self) -> Sequence[str]:
        """Return the node ids the baseline reported as failing that the newer run does not."""
        ...

    @property
    def still_failing(self) -> Sequence[str]:
        """Return the node ids failing in both documents."""
        ...


if TYPE_CHECKING:
    # The three owners' values must stay assignable to the protocols above; this body is checked by
    # mypy and never executed, so the annotation-time imports cost nothing at run time.
    def _conformance(
        row: ConfigRow, probe: ProbeResult, diff: ComparisonDiff
    ) -> tuple[KeyValueSourceRow, ProbeRow, ComparisonGroups]:
        """Return the owners' values as the protocols this renderer consumes."""
        return (row, probe, diff)


# --- The console and the conversions every rendering shares ------------------


def _console(stderr: bool = False) -> Console:
    """Return the console one rendering writes through.

    This is the single ``rich.console.Console`` construction site in the tree (AAP 0.7.1).
    ``highlight``, ``emoji`` and ``soft_wrap`` are off so two runs of one command capture identical
    bytes, and ``stderr`` selects the stream, which is how an error reaches stderr while a result
    reaches stdout (AAP 0.7.4). No theme and no style is passed: rich's defaults are the whole of
    the presentation (AAP 0.6). ``rich`` is imported here rather than at module scope, so a command
    pays for it and the pytest activation path never does (implicit requirement I5).

    Args:
        stderr: Whether the console writes to stderr instead of stdout.

    Returns:
        A console over ``sys.stdout``, or over ``sys.stderr`` when ``stderr`` is true. rich resolves
        the stream when it prints, so a stream a caller replaced after this call is still honoured.
    """
    from rich.console import Console

    return Console(highlight=False, emoji=False, soft_wrap=False, stderr=stderr)


def _redactor(redactor: Redactor | None) -> Redactor:
    """Return ``redactor``, or the default one for a caller that supplied none.

    Args:
        redactor: The redactor a caller passed, or ``None``.

    Returns:
        ``redactor`` unchanged, or :func:`foundry.obs.redaction.default_redactor`, which carries the
        built-in header and query deny-lists and therefore covers every default secret name.
    """
    return default_redactor() if redactor is None else redactor


def _scrubbed_text(text: str, redactor: Redactor, *, style: str | None = None) -> Text:
    """Return ``text`` redacted and wrapped as rich ``Text``, optionally in a built-in style.

    Wrapping rather than passing a ``str`` is what keeps markup out of the console: rich parses
    ``[``-delimited tags in a ``str`` but never in ``Text``, so a parametrised node id such as
    ``test_login[case-1]`` prints exactly as written instead of losing a fragment or raising.

    Args:
        text: The value to print, which may hold a credential.
        redactor: The redactor to scrub ``text`` through before it reaches the console.
        style: A rich built-in style name such as ``red``, or ``None`` for the default style.

    Returns:
        The redacted value as a renderable that rich prints without interpreting its markup.
    """
    from rich.text import Text

    scrubbed = redactor.text(text)
    if style is None:
        return Text(scrubbed)
    return Text(scrubbed, style=style)


def _table(title: str | None, columns: _RowCells, redactor: Redactor) -> Table:
    """Return an empty rich table with the given three headings and an optional title.

    Args:
        title: A heading to print above the table, or ``None`` for a table with none.
        columns: The three column headings, in order.
        redactor: The redactor every heading and the title pass through.

    Returns:
        A table with three empty columns and rich's default header style, ready for rows.
    """
    from rich.table import Table

    table = Table(
        title=None if title is None else _scrubbed_text(title, redactor),
        show_header=True,
    )
    for column in columns:
        table.add_column(_scrubbed_text(column, redactor))
    return table


def _config_cells(row: KeyValueSourceRow | _RowCells) -> _RowCells:
    """Return the three cells of one configuration row, whichever of its two shapes arrived.

    Args:
        row: A row with the attributes :class:`KeyValueSourceRow` describes, as
            :func:`foundry.config.render.rows` produces, or the same three cells as a tuple.

    Returns:
        ``(key, value, source)``.
    """
    if isinstance(row, tuple):
        return _tuple_cells(row)
    return (row.key, row.value, row.source)


def _probe_cells(row: ProbeRow | _RowCells) -> _RowCells:
    """Return the three cells of one doctor row, whichever of its two shapes arrived.

    :meth:`foundry.diagnostics.report.ProbeResult.as_row` returns the tuple form and
    :meth:`foundry.diagnostics.report.DoctorReport.as_rows` a tuple of them, so both are accepted.

    Args:
        row: A row with the attributes :class:`ProbeRow` describes, or the same cells as a tuple.

    Returns:
        ``(name, status, detail)``.
    """
    if isinstance(row, tuple):
        return _tuple_cells(row)
    return (row.name, row.status, row.detail)


def _tuple_cells(row: _RowCells) -> _RowCells:
    """Return exactly three cells from ``row``, padding a short row rather than raising.

    The padding is defensive: the annotation already says three, so this never fires for a caller
    that honours it, and a row that lost a cell prints a blank column instead of turning a display
    fault into a failed test (AAP 0.1.2).

    Args:
        row: The cells of one row.

    Returns:
        The first three cells, with empty ones appended when the row is shorter than three.
    """
    first = row[0] if len(row) > 0 else ""
    second = row[1] if len(row) > 1 else ""
    third = row[2] if len(row) > 2 else ""
    return (first, second, third)


# --- foundry config show ----------------------------------------------------


def render_config_table(
    rows: Sequence[KeyValueSourceRow | _RowCells],
    *,
    title: str | None = None,
    redactor: Redactor | None = None,
) -> None:
    """Print the ``foundry config show`` table of ``Key``, ``Value`` and ``Source``.

    The headings are exactly ``Key``, ``Value`` and ``Source`` (AAP 0.7.4), the rows print in the
    order they arrive -- the caller supplies the sorted key order of ``foundry.config.keys`` and
    this module never re-sorts -- and every cell passes through the redactor, because
    ``http.headers`` values and a ``http.base_url`` carrying URL userinfo are the configuration
    values that can hold a credential (AAP 0.3.2).

    Args:
        rows: The rows to print, either the frozen rows :func:`foundry.config.render.rows` returns
            or plain ``(key, value, source)`` triples.
        title: A heading to print above the table, redacted like every other value, or ``None``.
        redactor: The redactor to scrub every cell through; ``None`` selects the default one.

    Returns:
        ``None``; the table is written to stdout.
    """
    scrubbing = _redactor(redactor)
    table = _table(title, _CONFIG_COLUMNS, scrubbing)
    for row in rows:
        table.add_row(*(_scrubbed_text(cell, scrubbing) for cell in _config_cells(row)))
    _console().print(table)


def render_not_applied(
    rows: Sequence[KeyValueSourceRow | _RowCells],
    *,
    redactor: Redactor | None = None,
) -> None:
    """Print the separate section naming configuration overrides a command did not apply.

    ``foundry config show`` without ``--resolved`` resolves the configuration from defaults, file,
    profile and environment only, so the override options it was given change nothing and are
    listed apart from the resolution rather than folded into it (AAP 0.4.4). The section is titled
    with the literal words ``not applied`` and has the same three columns as the resolved table.

    Args:
        rows: One row per override the command did not apply; an empty sequence prints nothing, so a
            command that applied everything it was given needs no branch of its own.
        redactor: The redactor to scrub every cell through; ``None`` selects the default one.

    Returns:
        ``None``; the section is written to stdout.
    """
    if not rows:
        return
    render_config_table(rows, title=_NOT_APPLIED_TITLE, redactor=redactor)


# --- foundry doctor ---------------------------------------------------------


def render_probe_table(
    rows: Sequence[ProbeRow | _RowCells],
    *,
    ok: bool,
    redactor: Redactor | None = None,
) -> None:
    """Print the ``foundry doctor`` table of ``Probe``, ``Status`` and ``Detail``.

    The headings are exactly ``Probe``, ``Status`` and ``Detail`` and the rows print in the order
    the probe runner supplied them, which is the same order the ``--json`` document lists them in
    (AAP 0.7.4). A status reads as the literal lowercase word ``pass``, ``warn`` or ``fail``, and
    only rich's built-in ``green``, ``yellow`` and ``red`` distinguish them: no custom theme and no
    custom style is registered (AAP 0.6). A status outside those three words prints as it was given
    and counts towards none of them, so the closing line never invents a state. That line states how
    many probes reported each word and repeats the verdict ``ok`` maps to, which is the ``0`` or
    ``2`` exit code of AAP 0.4.4.

    Args:
        rows: One row per probe, either the models with the attributes :class:`ProbeRow` describes
            or the ``(name, status, detail)`` tuples ``ProbeResult.as_row()`` returns.
        ok: Whether no required probe failed; ``True`` prints the ``ok`` verdict, ``False`` prints
            ``failed``. A ``warn`` never clears it, so an unsupported platform or a missing socket
            guard reports a warning without failing the command.
        redactor: The redactor to scrub every cell through; ``None`` selects the default one.

    Returns:
        ``None``; the table and the closing count line are written to stdout.
    """
    scrubbing = _redactor(redactor)
    console = _console()
    table = _table(None, _PROBE_COLUMNS, scrubbing)
    counts: dict[str, int] = dict.fromkeys(_STATUS_WORDS, 0)
    for row in rows:
        name, status, detail = _probe_cells(row)
        word = scrubbing.text(status).strip().casefold()
        if word in counts:
            counts[word] += 1
        table.add_row(
            _scrubbed_text(name, scrubbing),
            _scrubbed_text(word, scrubbing, style=_STATUS_STYLES.get(word)),
            _scrubbed_text(detail, scrubbing),
        )
    console.print(table)
    console.print(_scrubbed_text(_probe_summary(counts, ok=ok), scrubbing), soft_wrap=True)


def _probe_summary(counts: Mapping[str, int], *, ok: bool) -> str:
    """Return the closing line of the doctor table: one count per state, then the verdict.

    Args:
        counts: How many probes reported each of the three states.
        ok: Whether no required probe failed.

    Returns:
        A single line such as ``probes: 4 pass, 1 warn, 0 fail (ok)``.
    """
    verdict = "ok" if ok else "failed"
    return (
        f"probes: {counts.get(_PASS, 0)} {_PASS}, "
        f"{counts.get(_WARN, 0)} {_WARN}, "
        f"{counts.get(_FAIL, 0)} {_FAIL} ({verdict})"
    )


# --- foundry report compare -------------------------------------------------


def render_comparison(groups: ComparisonGroups, *, redactor: Redactor | None = None) -> None:
    """Print the three labelled groups of ``foundry report compare`` in their fixed order.

    The labels are exactly ``Newly failing``, ``Newly passing`` and ``Still failing`` (AAP 0.7.4),
    each printed with the group's size and followed by its node ids. An empty group prints the
    ``(none)`` marker rather than disappearing, so the three groups are always all visible and a
    reader can tell an empty set from a missing one. The node ids print in the order they arrive --
    the comparator supplies ascending, de-duplicated tuples -- and each line is printed without
    wrapping, so a long parametrised node id stays one copyable line.

    Args:
        groups: The three node-id sequences, as :class:`foundry.reporting.compare.ComparisonDiff`
            holds them.
        redactor: The redactor to scrub every label and node id through; ``None`` selects the
            default one.

    Returns:
        ``None``; the three groups are written to stdout.
    """
    scrubbing = _redactor(redactor)
    console = _console()
    _render_group(console, _NEWLY_FAILING, groups.newly_failing, scrubbing)
    _render_group(console, _NEWLY_PASSING, groups.newly_passing, scrubbing)
    _render_group(console, _STILL_FAILING, groups.still_failing, scrubbing)


def _render_group(console: Console, label: str, nodeids: Sequence[str], redactor: Redactor) -> None:
    """Print one comparison group: its label and size, then its node ids or the empty marker.

    Args:
        console: The console to print through.
        label: The group's fixed label, such as ``Newly failing``.
        nodeids: The group's node ids, already in the order they are to print.
        redactor: The redactor every line passes through.
    """
    console.print(_scrubbed_text(f"{label} ({len(nodeids)}):", redactor), soft_wrap=True)
    if not nodeids:
        console.print(_scrubbed_text(f"{_LINE_INDENT}{_NONE_MARKER}", redactor), soft_wrap=True)
        return
    for nodeid in nodeids:
        console.print(_scrubbed_text(f"{_LINE_INDENT}{nodeid}", redactor), soft_wrap=True)


# --- single lines, errors, documents ----------------------------------------


def render_message(text: str, *, redactor: Redactor | None = None) -> None:
    """Print one plain information line on stdout.

    This is the sink for a success message such as ``configuration valid (profile=<name>)``: one
    line, no heading, no styling and no wrapping, so the text is exactly what the command reported.

    Args:
        text: The line to print.
        redactor: The redactor the line passes through; ``None`` selects the default one.

    Returns:
        ``None``; the line is written to stdout.
    """
    _console().print(_scrubbed_text(text, _redactor(redactor)), soft_wrap=True)


def render_error(
    message: str, hint: str | None = None, *, redactor: Redactor | None = None
) -> None:
    """Print an error's message on stderr, then its hint on an indented second line.

    The shape is the one :meth:`foundry.core.exceptions.FoundryError.format_message` already
    produces -- the message, then ``hint`` after two spaces of indent -- because a command reports a
    ``FoundryError`` and any other error through the same call site and the two must not diverge.
    Nothing else is printed and nothing goes to stdout: a hint that is absent or blank adds no line,
    and neither the message nor the hint is ever printed unredacted, since a configuration error
    names the absent environment variable rather than its value (implicit requirement I9).

    Args:
        message: The error's message, which is the text a person reads first.
        hint: An actionable next step, printed on the line below after two spaces, or ``None``.
        redactor: The redactor both lines pass through; ``None`` selects the default one.

    Returns:
        ``None``; the lines are written to stderr.
    """
    scrubbing = _redactor(redactor)
    console = _console(stderr=True)
    console.print(_scrubbed_text(message, scrubbing), soft_wrap=True)
    if hint is not None and hint.strip():
        console.print(_scrubbed_text(f"{_HINT_INDENT}{hint.strip()}", scrubbing), soft_wrap=True)


def render_lines(lines: Iterable[str], *, redactor: Redactor | None = None) -> None:
    """Print one line per item on stdout, in the order given.

    ``foundry init`` prints the paths it created through this function, so the order is the order
    the scaffolder wrote them and no line is wrapped or styled.

    Args:
        lines: The lines to print; an empty iterable prints nothing.
        redactor: The redactor every line passes through; ``None`` selects the default one.

    Returns:
        ``None``; each line is written to stdout.
    """
    scrubbing = _redactor(redactor)
    console = _console()
    for line in lines:
        console.print(_scrubbed_text(line, scrubbing), soft_wrap=True)


def render_document(text: str, *, redactor: Redactor | None = None) -> None:
    """Print exactly one machine-readable document on stdout and nothing else.

    ``foundry config show --format json|toml``, ``foundry doctor --json`` and ``foundry report
    compare --json`` each emit one document and no banner, no heading and no colour (AAP 0.7.3), so
    this function writes through :func:`click.echo` rather than the console: rich would wrap the
    text to the terminal width and could break a JSON or TOML line in half. A single trailing
    newline is added only when ``text`` does not already end with one, so the document reaches
    stdout byte for byte as its producer rendered it.

    The ``redactor`` keyword is accepted so every sink here has one signature, and it is
    deliberately not applied. A document is emitted verbatim, and the two facts leave no room for a
    scrubber: its producer has already redacted every value it wrote, and running the free-text
    engine over a serialised document would rewrite a quoted value into something that is no longer
    valid JSON -- ``"authorization": "Bearer abc"`` becomes two unterminated tokens -- which a
    consumer could no longer parse.

    Args:
        text: The complete document, exactly as it is to reach stdout.
        redactor: Accepted for a uniform sink signature and not applied; see above.

    Returns:
        ``None``; the document is written to stdout.
    """
    if text.endswith("\n"):
        click.echo(text, nl=False)
        return
    click.echo(text)
