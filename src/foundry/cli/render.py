"""The one place Foundry renders terminal output.

Every CLI module hands this renderer plain data -- rows, node ids, one message, one document -- and
receives nothing back: widths, colours, borders and the stdout/stderr choice live here. ``rich`` is
imported inside each function that needs it, so the pytest activation path pays for nothing until a
command renders, and every cell, message and line passes through the redactor before the console.
"""

from __future__ import annotations

import re
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

_CONTROL_ESCAPE: Final[re.Pattern[str]] = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
r"""Every control character with no visible form: all of C0 except tab (``\x09``) and newline
(``\x0a``), which carry the line structure, plus carriage return (``\x0d``, which would otherwise
overwrite a line and which ``rich`` silently drops), ``DEL`` and the C1 range.

``rich`` 13.9.4 drops only ``\x07``, ``\x08``, ``\x0b``, ``\x0c`` and ``\x0d`` from a ``Text``, so a
control code left in the string reaches the operator's terminal as a byte -- an ``ESC`` sequence
included, which can colour or otherwise manipulate the screen. Tab and newline stay because the
terminal surface really does span lines: click hands a group's help text over as one multi-line
string and a refusal folds its hint into an indented second line (AAP 0.7.4), so
:func:`_single_line_text` spells the line breaks of the one surface that must not.
"""

_LINE_BREAKS: Final[re.Pattern[str]] = re.compile(r"[\x09\x0a]")
r"""The control characters that carry line structure: tab and newline, kept by every other sink.

The terminal keeps them because a click help text and an error's hint are genuinely multi-line
(AAP 0.7.4), so :func:`_single_line_text` spells them at the one call site that needs a single line
rather than :func:`_scrubbed_text` spelling them everywhere.
"""


class KeyValueSourceRow(Protocol):
    """The structure of one ``foundry config show`` row, satisfied by ``ConfigRow``.

    It is a protocol rather than a concrete type because this renderer owns layout and not data:
    :func:`foundry.config.render.describe` produces the rows and this module only prints them, so
    the two can be tested apart without either importing the other (AAP 0.4.8). Its members are
    read-only properties, which is what lets a frozen, slotted ``ConfigRow`` satisfy it.
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

    ``ProbeResult.as_row()`` returns the same three cells as one plain ``(name, status, detail)``
    tuple, so a caller may hand this renderer either the probe model or the row tuple it produced.
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

    The single ``rich.console.Console`` construction site in the tree. ``highlight``, ``emoji`` and
    ``soft_wrap`` are off so two runs of one command capture identical bytes, and ``stderr``
    selects the stream, which is how an error reaches stderr while a result reaches stdout.
    ``rich`` is imported here rather than at module scope, so the pytest activation path never pays
    for it, and rich resolves the stream when it prints.
    """
    from rich.console import Console

    return Console(highlight=False, emoji=False, soft_wrap=False, stderr=stderr)


def _redactor(redactor: Redactor | None) -> Redactor:
    """Return ``redactor``, or :func:`foundry.obs.redaction.default_redactor` when none was given.

    The default carries the built-in header and query deny-lists, covering every default name.
    """
    return default_redactor() if redactor is None else redactor


def _spell(match: re.Match[str]) -> str:
    r"""Return one matched character as its ``\xNN`` escape, the spelling every terminal sink uses.

    The substitution callable rather than a lambda at each site, so the spelling a reader searches
    for -- a backslash, an ``x`` and two lowercase hex digits -- is written once.

    Args:
        match: One match of a pattern whose group is exactly one character.

    Returns:
        The character's code as a backslash, an ``x`` and two lowercase hex digits.
    """
    return f"\\x{ord(match.group()):02x}"


def _visible_control_characters(text: str) -> str:
    r"""Return ``text`` with every :data:`_CONTROL_ESCAPE` character spelled as its ``\xNN`` escape.

    The terminal is a user surface AAP 0.7.4 fixes, and a result document handed to ``foundry
    report compare`` is operator-named input rather than trusted text: ``rich`` drops only ``\x07``,
    ``\x08``, ``\x0b``, ``\x0c`` and ``\x0d`` from a ``Text``, so a ``NUL``, ``ESC``, ``DEL`` or C1
    byte in a node id still reaches the terminal as a byte, and an ``ESC`` sequence can colour or
    otherwise manipulate the operator's screen. Each becomes a backslash, an ``x`` and two
    lowercase hex digits a reader can search for -- the spelling
    :func:`foundry.reporting.panels.visible_control_characters` already uses for the HTML report.
    Tab and newline are left to the caller, because they are line structure here (AAP 0.7.4) and
    :func:`_single_line_text` spells them for the one surface that must stay on one line.

    Args:
        text: The already-redacted cell, message or line that is about to reach the terminal.

    Returns:
        ``text`` with every :data:`_CONTROL_ESCAPE` match replaced in place, else unchanged.
    """
    return _CONTROL_ESCAPE.sub(_spell, text)


def _scrubbed_text(
    text: str, redactor: Redactor, *, style: str | None = None, diagnostic: bool = False
) -> Text:
    r"""Return ``text`` redacted and wrapped as rich ``Text``, optionally in a built-in style.

    Wrapping rather than passing a ``str`` keeps markup out of the console: rich parses
    ``[``-delimited tags in a ``str`` but never in ``Text``, so a parametrised node id such as
    ``test_login[case-1]`` prints exactly as written. ``diagnostic`` scrubs through
    :meth:`Redactor.diagnostic` rather than :meth:`Redactor.text`, which keeps the key, closest
    match and accepted values AAP 0.4.5 makes part of a configuration diagnostic readable, while the
    values such a message carries are still masked. Every control character with no visible form
    that the scrubbed text carries is spelled as its ``\xNN`` escape by
    :func:`_visible_control_characters`, because this function is the one place terminal text
    becomes a ``Text`` and no cell may colour or manipulate the operator's terminal (AAP 0.7.4).
    Tab and newline pass through as the line structure a click help text and an error's hint really
    carry, and :func:`_single_line_text` spells them for a surface that must stay on one line.
    """
    from rich.text import Text

    redacted = redactor.diagnostic(text) if diagnostic else redactor.text(text)
    scrubbed = _visible_control_characters(redacted)
    if style is None:
        return Text(scrubbed)
    return Text(scrubbed, style=style)


def _single_line_text(text: str, redactor: Redactor) -> Text:
    r"""Return ``text`` as one terminal line: its line breaks spelled, then redacted and wrapped.

    AAP 0.7.4 makes a node id one entry on one line of one group, and a result document handed to
    ``foundry report compare`` is operator-named input rather than trusted text, so the line breaks
    such a document puts in a node id are data and are spelled here: the ``\x0a`` or ``\x09`` a node
    id carries reaches the terminal as ``\x0a`` or ``\x09`` rather than as a line or a column
    break, which keeps a hostile document from forging a line of the operator's own output ahead of
    a group it does not belong to. The shared sink keeps tab and newline because a click help text
    and a refusal's hint really do span lines (AAP 0.7.4), which is why this spelling lives at the
    surface that needs a single line rather than in :func:`_scrubbed_text`.

    Args:
        text: The assembled line, a node id included, that must reach the terminal as one line.
        redactor: The redactor the line passes through.

    Returns:
        The line as rich ``Text``, every line break it carried spelled as ``\x09`` or ``\x0a``.
    """
    return _scrubbed_text(_LINE_BREAKS.sub(_spell, text), redactor)


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
        title=None if title is None else _scrubbed_text(title, redactor), show_header=True
    )
    for column in columns:
        table.add_column(_scrubbed_text(column, redactor))
    return table


def _config_cells(row: KeyValueSourceRow | _RowCells) -> _RowCells:
    """Return the three cells of one configuration row, whichever of its two shapes arrived."""
    if isinstance(row, tuple):
        return _tuple_cells(row)
    return (row.key, row.value, row.source)


def _probe_cells(row: ProbeRow | _RowCells) -> _RowCells:
    """Return the three cells of one doctor row, whichever of its two shapes arrived."""
    if isinstance(row, tuple):
        return _tuple_cells(row)
    return (row.name, row.status, row.detail)


def _tuple_cells(row: _RowCells) -> _RowCells:
    """Return exactly three cells from ``row``, padding a short row rather than raising.

    The padding is defensive: the annotation already says three, so this never fires for a caller
    that honours it, and a row that lost a cell prints a blank column instead of turning a display
    fault into a failed test.
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

    The headings are exactly those three words and the rows print in the order they arrive -- the
    caller supplies the sorted key order and this module never re-sorts. Every cell passes through
    the redactor, because ``http.headers`` values and a ``http.base_url`` carrying URL userinfo are
    the configuration values that can hold a credential. A ``title`` is redacted like every other
    value.
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
    listed apart. The section is titled with the literal words ``not applied`` and has the same
    three columns as the resolved table.
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
    the probe runner supplied them, the same order the ``--json`` document lists them in (AAP
    0.7.4). A status reads as the literal lowercase word ``pass``, ``warn`` or ``fail``, and only
    rich's built-in ``green``, ``yellow`` and ``red`` distinguish them: no custom theme or style is
    registered (AAP 0.6). A status outside those three words prints as given and counts towards none
    of them, so the closing line never invents a state; it states how many probes reported each word
    and repeats the verdict ``ok`` maps to, the ``0`` or ``2`` exit code of AAP 0.4.4.

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
    """Return the closing line of the doctor table: one count per state, then the verdict."""
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
    ``(none)`` marker rather than disappearing, so all three are always visible and an empty set
    reads differently from a missing one.

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

    A node id is one entry on one line of one group (AAP 0.7.4), so each prints through
    :func:`_single_line_text`: the line breaks a hostile result document put in a node id are
    spelled rather than forging a line of the operator's own output.
    """
    console.print(_scrubbed_text(f"{label} ({len(nodeids)}):", redactor), soft_wrap=True)
    if not nodeids:
        console.print(_single_line_text(f"{_LINE_INDENT}{_NONE_MARKER}", redactor), soft_wrap=True)
        return
    for nodeid in nodeids:
        console.print(_single_line_text(f"{_LINE_INDENT}{nodeid}", redactor), soft_wrap=True)


# --- single lines, errors, documents ----------------------------------------


def render_message(text: str, *, redactor: Redactor | None = None) -> None:
    """Print one plain information line on stdout.

    The sink for a success message such as ``configuration valid (profile=<name>)``: one line, no
    heading, styling or wrapping, so the text is exactly what the command reported.

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
    ``FoundryError`` and any other error through the same call site, so the two must not diverge.
    Nothing else is printed and nothing goes to stdout: an absent or blank hint adds no line. Both
    lines are scrubbed as a diagnostic, never as free text: AAP 0.4.5 makes an unknown key, its
    closest match and a key's accepted range part of the message -- ``unknown configuration key:
    http.timeot``, ``invalid value for option --foundry-http.auth.strategy: expected none, bearer,
    basic, header`` -- and the free-text ``name: value`` inference would mask exactly that, leaving
    an operator unable to name the key to fix. The values a message carries are still masked, and a
    configuration error names the absent environment variable rather than its value (implicit
    requirement I9).

    Args:
        message: The error's message, which is the text a person reads first.
        hint: An actionable next step, printed on the line below after two spaces, or ``None``.
        redactor: The redactor both lines pass through; ``None`` selects the default one.

    Returns:
        ``None``; the lines are written to stderr.
    """
    scrubbing = _redactor(redactor)
    console = _console(stderr=True)
    console.print(_scrubbed_text(message, scrubbing, diagnostic=True), soft_wrap=True)
    if hint is not None and hint.strip():
        console.print(
            _scrubbed_text(f"{_HINT_INDENT}{hint.strip()}", scrubbing, diagnostic=True),
            soft_wrap=True,
        )


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
    stdout byte for byte as its producer rendered it. The ``redactor`` keyword is accepted so every
    sink here has one signature, and it is deliberately not applied. A document is emitted verbatim,
    and the two facts leave no room for a scrubber: its producer has already redacted every value it
    wrote, and running the free-text engine over a serialised document would rewrite a quoted value
    into something that is no longer valid JSON -- ``"authorization": "Bearer abc"`` becomes two
    unterminated tokens -- which a consumer could no longer parse.

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
