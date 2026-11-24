"""The three renderings ``foundry config show`` prints: the table and the two documents.

AAP 0.3.2 gives this module the display side of a resolved configuration: one row of Key, Value and
Source per key (AAP 0.7.4), the JSON document ``{"resolved": ..., "sources": ...}`` and the TOML
document carrying the same values with a companion ``[sources]`` table (AAP 0.4.5). Each
machine-readable form is one document and nothing else, printed verbatim by
:mod:`foundry.cli.config_cmd` (AAP 0.7.3). Every value passes through
:class:`foundry.obs.redaction.Redactor` before it is rendered (AAP 0.3.2); every collection is
sorted first so two runs are byte-identical (AAP 0.13.1); no table layout lives here, widths and
colours belonging to :mod:`foundry.cli.render`, which consumes :func:`rows`.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Final

from foundry.config import keys, layers
from foundry.obs.redaction import Redactor, default_redactor

if TYPE_CHECKING:
    from foundry.config.cli_layer import CliInput
    from foundry.config.models import FoundryConfig

__all__: Final[tuple[str, ...]] = (
    "ConfigRow",
    "NOT_APPLIED_SECTION",
    "NOT_APPLIED_SOURCE_PREFIX",
    "RESOLVED_SECTION",
    "SHOW_COLUMNS",
    "SOURCES_TABLE",
    "not_applied_values",
    "redactor_for",
    "render_value",
    "resolved_document",
    "rows",
    "sources_document",
    "to_json_document",
    "to_json_text",
    "to_toml_text",
)

SHOW_COLUMNS: Final[tuple[str, str, str]] = ("Key", "Value", "Source")
"""The three columns of the ``foundry config show`` table, in order (AAP 0.7.4)."""

SOURCES_TABLE: Final[str] = "sources"
"""The name of the per-key provenance map, as a JSON member and as a TOML table (AAP 0.4.5)."""

RESOLVED_SECTION: Final[str] = "resolved"
"""The name of the member holding the resolved values (AAP 0.4.5)."""

NOT_APPLIED_SECTION: Final[str] = "not applied"
"""The heading of the table naming overrides a run without ``--resolved`` did not apply.

AAP 0.4.4 requires those overrides to be listed apart from the resolution the run reports.
"""

NOT_APPLIED_SOURCE_PREFIX: Final[str] = "not-applied:"
"""Prefix of a not-applied row's Source column, so it cannot be read as a layer (AAP 0.7.4)."""

_NOT_APPLIED_KEY: Final[str] = "_".join(NOT_APPLIED_SECTION.split())
"""``not_applied``: the TOML table and JSON member name of the not-applied section."""

_UNSET: Final[str] = "<unset>"
"""How a key with no value renders in the table, such as an unset ``http.base_url``."""

_TRUE: Final[str] = "true"
"""Text of a boolean that is on, so a value reads as the configuration file spells it."""

_FALSE: Final[str] = "false"
"""Text of a boolean that is off."""

_LIST_SEPARATOR: Final[str] = ","
"""Separator between the elements of a list, which is how the environment layer spells one (A3)."""

_PAIR_SEPARATOR: Final[str] = ", "
"""Separator between the entries of a header table, which reads as a table cell."""

_JSON_INDENT: Final[int] = 2
"""Indentation of the JSON document, so two runs of one configuration diff line by line."""

_TOML_PROBE_KEY: Final[str] = "v"
"""The key :func:`_toml_string` wraps a value in, to have ``tomli-w`` quote it."""

_TEXT_KINDS: Final[frozenset[str]] = frozenset({"str", "path"})
"""Kinds whose value is operator-authored text that can carry a credential inside a URL."""


@dataclass(frozen=True, slots=True)
class ConfigRow:
    """One row of the ``foundry config show`` table (AAP 0.7.4).

    Plain text in three columns, and the layout is not its business: :func:`rows` produces the rows
    and :mod:`foundry.cli.render` decides widths, colours and borders, so the table can be printed
    with ``rich`` and inspected as tuples in a test without the two drifting apart.

    Attributes:
        key: The dotted configuration key, for example ``http.timeout``.
        value: The resolved value, rendered for a person reading it, or the unset placeholder.
        source: The label of the layer that supplied it -- ``default``, ``file:<path>``,
            ``profile:<name>``, ``env:<VARIABLE>`` or ``cli:<--option>`` (AAP 0.7.4) -- or a
            ``not-applied:<--option>`` label for an override the run did not apply.
    """

    key: str
    value: str
    source: str

    def as_tuple(self) -> tuple[str, str, str]:
        """Return the three cells in the order of :data:`SHOW_COLUMNS`.

        The form :mod:`foundry.cli.render` hands straight to ``rich``.
        """
        return (self.key, self.value, self.source)


def redactor_for(config: FoundryConfig) -> Redactor:
    """Return the redactor every rendering of ``config`` passes its values through.

    One engine serves every sink (AAP 0.3.2), built from the resolved ``secrets.redact_keys`` plus
    the deny-lists it adds itself, so a project that widens the list widens what ``config show``
    prints without a change here.
    """
    return Redactor(config.secrets.redact_keys)


def render_value(spec: keys.KeySpec, value: object, redactor: Redactor) -> str:
    """Render one configuration value for the Value column of the table.

    The shape of the value decides the rendering: a boolean as the configuration file spells it, an
    enumeration by the member's value rather than its name, a path in POSIX form, a number in its
    shortest exact form, a list comma-joined in the order it was authored -- the environment layer's
    form (assumption A3) -- and a header table as ``name=value`` pairs sorted by name.

    Two shapes can carry a credential, and both are redacted here rather than by the caller: the
    ``http.headers`` table through :meth:`foundry.obs.redaction.Redactor.header`, and the text a
    ``str``- or ``path``-kind key holds through ``Redactor.text``, which drops URL userinfo and
    redacts deny-listed query parameters (AAP 0.2.1, AAP 0.3.2).

    Returns:
        The value as text for the table, ``<unset>`` for ``None``.
    """
    if value is None:
        return _UNSET
    if isinstance(value, Mapping):
        return _header_pairs(value, redactor)
    if isinstance(value, (list, tuple, set, frozenset)):
        return _LIST_SEPARATOR.join(_element_text(element) for element in _ordered(value))
    if isinstance(value, bool):
        return _TRUE if value else _FALSE
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return redactor.text(value) if spec.kind in _TEXT_KINDS else value
    return redactor.text(_text_of(value))


def rows(config: FoundryConfig, *, not_applied: CliInput | None = None) -> tuple[ConfigRow, ...]:
    """Return the table ``foundry config show`` prints: one row per key, in sorted key order.

    The rows are derived from :func:`foundry.config.keys.leaf_keys`, so a key the published surface
    does not declare has no row even if the settings model holds a value for it, and each Source
    cell is :meth:`foundry.config.sources.ConfigSource.label`.

    ``not_applied`` carries the overrides of a command that resolved without them: AAP 0.4.4 has
    those listed separately rather than folded into the resolution, so their rows come after every
    resolved row and their Source cell reads ``not-applied:<--option>``, which no layer label can be
    mistaken for.

    Returns:
        One row per leaf key in sorted key order, then one row per not-applied override.
    """
    redactor = redactor_for(config)
    values = _leaf_values(config)
    rendered: list[ConfigRow] = [
        ConfigRow(
            key=spec.dotted,
            value=render_value(spec, values.get(spec.dotted), redactor),
            source=config.source_of(spec.dotted).label(),
        )
        for spec in _sorted_specs()
    ]
    if not_applied is not None:
        deferred = not_applied_values(not_applied, redactor=redactor)
        for dotted, value in deferred.items():
            spelling = not_applied.spelling_for(dotted) or keys.cli_option(dotted)
            rendered.append(
                ConfigRow(
                    key=dotted,
                    value=value,
                    source=f"{NOT_APPLIED_SOURCE_PREFIX}{spelling}",
                )
            )
    return tuple(rendered)


def not_applied_values(cli: CliInput, *, redactor: Redactor | None = None) -> dict[str, str]:
    """Return the overrides ``cli`` carries, rendered, by dotted key.

    The shared source of the not-applied section in all three renderings, so the table, the JSON
    document and the TOML document list the same keys in the same order with the same values. An
    override is a scalar -- no header table has a single-token CLI form -- so a configured key name
    cannot apply to one, and callers that hold a configuration pass its redactor anyway, so every
    value a run renders goes through one engine.
    """
    scrub = default_redactor() if redactor is None else redactor
    rendered: dict[str, str] = {}
    for dotted in cli.dotted_keys():
        value = cli.overrides[dotted]
        spec = keys.spec_for(dotted)
        if spec is None:
            # A CliInput built by the layer only records registered keys, so this is unreachable
            # for one. A hand-built input still renders rather than raising: every caller of this
            # module is a renderer, and Foundry's own plumbing never fails a run (AAP 0.1.2).
            rendered[dotted] = scrub.text(_text_of(value))
            continue
        rendered[dotted] = render_value(spec, value, scrub)
    return rendered


def sources_document(config: FoundryConfig) -> dict[str, dict[str, str]]:
    """Return the provenance document: one ``{"layer", "detail"}`` record per leaf key.

    The shape AAP 0.4.5 publishes. Both values are names -- the layer, and the file path,
    environment variable, option spelling or profile name behind it -- so no configuration value
    can reach the document, which is what lets it be printed and recorded without a redaction step
    of its own (implicit requirement I9).
    """
    return {dotted: config.source_of(dotted).as_dict() for dotted in sorted(keys.leaf_keys())}


def resolved_document(config: FoundryConfig) -> dict[str, object]:
    """Return the resolved values as the nested document ``--format json`` and toml print.

    The nesting is restored by :func:`foundry.config.layers.unflatten`, which the merge also uses
    just before it constructs the settings model, so the document a person reads and the model the
    run uses describe the same table, keys sorted at every level.

    Values are rendered for a document rather than a terminal: an unset key stays ``None``, an
    enumeration becomes its value, a path its POSIX string, a header table a mapping of redacted
    values, and text is scrubbed (AAP 0.3.2). ``layers.flatten`` of the result yields exactly the
    keys of :func:`foundry.config.keys.leaf_keys`, which keeps the published key set and the
    rendered document in step (AAP 0.4.5).
    """
    redactor = redactor_for(config)
    values = _leaf_values(config)
    flat = {
        spec.dotted: _document_value(values.get(spec.dotted), redactor) for spec in _sorted_specs()
    }
    return layers.unflatten(flat)


def to_json_document(
    config: FoundryConfig, *, not_applied: CliInput | None = None
) -> dict[str, object]:
    """Return the object ``foundry config show --format json`` serialises.

    AAP 0.4.5 fixes the two members ``resolved`` and ``sources``, and AAP 0.4.4 requires a run
    without ``--resolved`` to report the overrides it did not apply. Those overrides ride a third
    member, ``not_applied``, that appears only when the command supplied at least one: adding a
    member leaves the documented shape intact for a consumer that reads the two it was promised
    (AAP 0.4.9), while rewriting them would not.
    """
    document: dict[str, object] = {
        RESOLVED_SECTION: resolved_document(config),
        SOURCES_TABLE: sources_document(config),
    }
    if not_applied is not None:
        deferred = not_applied_values(not_applied, redactor=redactor_for(config))
        if deferred:
            document[_NOT_APPLIED_KEY] = deferred
    return document


def to_json_text(config: FoundryConfig, *, not_applied: CliInput | None = None) -> str:
    """Return the JSON text ``foundry config show --format json`` writes to stdout.

    One document and nothing else (AAP 0.7.3): keys sorted at every level, two-space indentation,
    non-ASCII text left readable, and exactly one trailing newline. Sorting is also what places
    ``resolved`` and ``sources`` first whenever no override was deferred, and what makes two
    invocations with the same configuration byte-identical (AAP 0.13.1).

    Args:
        config: The resolved configuration to document.
        not_applied: The overrides the command supplied but did not apply, or ``None``.

    Returns:
        The document as JSON text, ending with one newline.
    """
    rendered = json.dumps(
        to_json_document(config, not_applied=not_applied),
        indent=_JSON_INDENT,
        sort_keys=True,
        ensure_ascii=False,
    )
    return f"{rendered}\n"


def to_toml_text(config: FoundryConfig, *, not_applied: CliInput | None = None) -> str:
    """Return the TOML text ``foundry config show --format toml`` writes to stdout.

    The resolved values are written as the document they nest into, followed by the companion
    ``[sources]`` table AAP 0.4.5 requires and, when the command deferred overrides, a
    ``[not_applied]`` table. A key with no value is dropped rather than written, because TOML has no
    null.

    ``tomli-w`` writes the values, imported inside this function so a run that never renders TOML
    never imports it. It renders a nested table as a ``[sources."<key>"]`` section rather than the
    inline table the document publishes, so the source table is composed from the same quoting it
    applies -- one escaper, no second implementation of TOML strings.
    """
    import tomli_w

    document = _toml_ready(resolved_document(config))
    if not_applied is not None:
        deferred = not_applied_values(not_applied, redactor=redactor_for(config))
        if deferred:
            document[_NOT_APPLIED_KEY] = deferred
    rendered = tomli_w.dumps(document)
    return _with_sources_table(rendered, sources_document(config))


def _leaf_values(config: FoundryConfig) -> dict[str, object]:
    """Return the resolved values by the registry's leaf keys.

    The settings model knows its own fields; the registry knows the published surface. Selecting by
    the registry keeps a field that no key declares out of every rendering, which is the same rule
    :func:`rows` follows for its keys.

    Args:
        config: The resolved configuration to read.

    Returns:
        One entry per leaf key, holding the value the model resolved or ``None`` when the selected
        layers left it unset.
    """
    values = config.leaf_values()
    return {spec.dotted: values.get(spec.dotted) for spec in keys.KEY_SPECS}


def _sorted_specs() -> tuple[keys.KeySpec, ...]:
    """Return the registry's key specs ordered by dotted key.

    Returns:
        The 28 specs, sorted, which fixes the row order of the table and the insertion order of the
        document.
    """
    return tuple(sorted(keys.KEY_SPECS, key=_spec_dotted))


def _spec_dotted(spec: keys.KeySpec) -> str:
    """Return the dotted key a spec declares, as the sort key of :func:`_sorted_specs`.

    Args:
        spec: The registry entry to read.

    Returns:
        The dotted key, for example ``http.timeout``.
    """
    return spec.dotted


def _document_value(value: object, redactor: Redactor) -> object:
    """Return one resolved value in the shape a document holds it.

    The document form is not the table form: a value keeps its type where the document can carry it
    (a number stays a number, a boolean stays a boolean) and loses it only where the format has no
    equivalent -- an enumeration becomes its value, a path its POSIX string, a header table a
    mapping of redacted values.

    Args:
        value: The resolved value, or ``None`` for a key no layer set.
        redactor: The redactor to scrub credentials through.

    Returns:
        ``None``, a boolean, a number, a string, a list or a mapping, ready for
        :func:`json.dumps` or ``tomli-w``.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, Mapping):
        return dict(redactor.headers(_header_table(value)))
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_element_text(element) for element in _ordered(value)]
    if isinstance(value, str):
        return redactor.text(value)
    return redactor.text(_text_of(value))


def _ordered(value: Iterable[object]) -> tuple[object, ...]:
    """Return the elements of a sequence-valued key in a deterministic order.

    A list or a tuple keeps the order it was authored in, which is the order the environment layer
    sets and the document publishes. An unordered collection is sorted, so rendering cannot depend
    on hashing (AAP 0.13.1); the settings model parses both list-valued keys into lists, so this
    is a guard rather than the usual path.

    Args:
        value: The collection to order.

    Returns:
        The elements as a tuple, sorted when ``value`` is a set or a frozenset.
    """
    if isinstance(value, (set, frozenset)):
        return tuple(sorted(value, key=repr))
    return tuple(value)


def _element_text(element: object) -> str:
    """Render one element of a list-valued key.

    Both list-valued keys hold names -- ``http.retries.retry_on`` names predicates and
    ``secrets.redact_keys`` names the keys to redact -- so an element is rendered as its value and
    is deliberately not passed through the redactor, which would replace the deny-list's own
    entries in the row that publishes them.

    Args:
        element: One element of the list.

    Returns:
        The element as text, in the same spelling the table uses for a scalar.
    """
    if element is None:
        return _UNSET
    if isinstance(element, bool):
        return _TRUE if element else _FALSE
    if isinstance(element, Enum):
        return str(element.value)
    if isinstance(element, Path):
        return element.as_posix()
    return _text_of(element)


def _header_pairs(table: Mapping[object, object], redactor: Redactor) -> str:
    """Render a header table as ``name=value`` pairs, with secret values redacted.

    The pairs are ordered by case-folded name by
    :meth:`foundry.obs.redaction.Redactor.headers`, so the cell reads the same on two runs whatever
    order the file or the environment supplied the headers in (AAP 0.13.1), and a name in
    ``secrets.redact_keys`` or in the deny-list prints ``***`` (AAP 0.3.2).

    Args:
        table: The header table, name to value.
        redactor: The redactor that decides which values are printed.

    Returns:
        The pairs joined for a table cell, or an empty string for a table with no header.
    """
    redacted = redactor.headers(_header_table(table))
    return _PAIR_SEPARATOR.join(f"{name}={value}" for name, value in redacted.items())


def _header_table(table: Mapping[object, object]) -> dict[str, str]:
    """Return a header table as the ``str``-to-``str`` mapping the redactor accepts.

    Args:
        table: The header table as the settings model holds it.

    Returns:
        A fresh mapping whose names and values are text; a name or value of another type is
        rendered rather than dropped, so the row still shows that the header exists.
    """
    return {_text_of(name): _text_of(value) for name, value in table.items()}


def _text_of(value: object) -> str:
    """Return a value as text for a rendering, without raising.

    The value itself for a string, an empty string for ``None``, and its text form otherwise -- or
    an empty string when the value cannot describe itself, because a renderer must not fail the
    command that called it (AAP 0.1.2).
    """
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return str(value)
    except (TypeError, ValueError):
        return ""


def _toml_ready(document: Mapping[str, object]) -> dict[str, object]:
    """Return a nested document with every unset value dropped.

    TOML has no null, so a key no layer set -- an unset ``http.base_url`` or credential name -- is
    absent from the document rather than written empty. A table is kept even when the drop empties
    it, so the document keeps the shape of the configuration.

    Args:
        document: The nested document :func:`resolved_document` produced.

    Returns:
        The same nesting without any value that is ``None``.
    """
    ready: dict[str, object] = {}
    for key, value in document.items():
        if value is None:
            continue
        if isinstance(value, Mapping):
            ready[key] = _toml_ready(value)
        elif isinstance(value, list):
            ready[key] = [_toml_scalar(item) for item in value]
        else:
            ready[key] = value
    return ready


def _toml_scalar(value: object) -> object:
    """Return one element of a document list, dropping an unset value.

    Args:
        value: One element of a list-valued key.

    Returns:
        The element, or an empty string when it is ``None``: a TOML array cannot hold a null, and
        an empty element keeps the array's length.
    """
    return "" if value is None else value


def _with_sources_table(rendered: str, sources: Mapping[str, Mapping[str, str]]) -> str:
    """Append the companion ``[sources]`` table to a rendered TOML document.

    AAP 0.4.5 publishes one line per attributed key, ``"http.timeout" = { layer = "env", detail =
    "FOUNDRY_HTTP__TIMEOUT" }``, and ``tomli-w`` writes a nested table as a ``[sources."<key>"]``
    section instead of an inline table, so the table is composed here from the quoting it applies
    (:func:`_toml_string`). The entries are sorted by key, so two runs print the rows in the same
    order (AAP 0.13.1).

    Args:
        rendered: The document ``tomli-w`` produced for the resolved values.
        sources: The provenance document, key to ``{"layer", "detail"}``.

    Returns:
        The document followed by the source table, ending with one newline.
    """
    lines = [f"[{SOURCES_TABLE}]"]
    for dotted in sorted(sources):
        source = sources[dotted]
        layer = _toml_string(source.get("layer", ""))
        detail = _toml_string(source.get("detail", ""))
        lines.append(f"{_toml_string(dotted)} = {{ layer = {layer}, detail = {detail} }}")
    body = rendered if rendered.endswith("\n") else f"{rendered}\n"
    return f"{body}\n" + "\n".join(lines) + "\n"


def _toml_string(value: str) -> str:
    """Return ``value`` as a quoted, escaped TOML basic string.

    ``tomli-w`` is the declared TOML writer, so instead of growing a second implementation of the
    format's escaping the value is rendered as the value of a probe document and the quoted literal
    is taken from it. A key containing a dot and a path containing a quote or a backslash therefore
    come out as TOML's own grammar spells them.

    Args:
        value: The text to quote.

    Returns:
        The value as a TOML basic string, quotes included.
    """
    import tomli_w

    rendered = tomli_w.dumps({_TOML_PROBE_KEY: value})
    return rendered.partition("=")[2].strip()
