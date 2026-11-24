"""The configuration-layer vocabulary and the per-layer value carriers.

Every value Foundry resolves is produced by one of the layers AAP 0.4.5 fixes, and each hands its
contribution on as a :class:`LayerPayload`: what it read in flat, dotted-key form, plus the detail a
person needs in order to inspect the result -- the file path, environment variable or option
spelling the value came from. :class:`ConfigLayer` names the layers, :data:`LAYER_ORDER` states the
order they win in, and :func:`layer_label` composes the Source spelling ``foundry config show``
prints, so the precedence rule of AAP 0.4.5 has exactly one edit site in the package.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from foundry.config import errors
from foundry.config.keys import spec_for
from foundry.core.exceptions import ConfigError

__all__: Final[tuple[str, ...]] = (
    "ConfigLayer",
    "DEFAULT_DETAIL",
    "LAYER_ORDER",
    "LayerPayload",
    "flatten",
    "layer_label",
    "unflatten",
)

_KEY_SEPARATOR: Final[str] = "."
"""The separator between the segments of a dotted configuration key."""

_LABEL_SEPARATOR: Final[str] = ":"
"""The separator between a layer's name and its detail in a source label (AAP 0.7.4)."""

_ROOT_KEY: Final[str] = "<root>"
"""The key a shape fault names when it is about a layer's top-level table."""

_TABLE_EXPECTED: Final[str] = "a table of keys rather than a value"
"""Expected shape for a key that another key descends into."""

_ONCE_EXPECTED: Final[str] = "the key once, as either a value or a table"
"""Expected shape for a key defined twice in one payload."""

_DOTTED_EXPECTED: Final[str] = "a non-empty dotted key"
"""Expected shape for the key :func:`unflatten` is given."""

_MAPPING_KIND: Final[str] = "mapping"
"""The registry kind of a key whose value is a table, such as ``http.headers``."""

DEFAULT_DETAIL: Final[str] = ""
"""The detail of a layer that has nothing more specific to name.

The ``default`` layer supplies the values that came from the settings model's own fields,
so there is no path, variable or option behind them: it carries this empty detail, and
:func:`layer_label` renders it as the bare word ``default`` (AAP 0.4.5, AAP 0.7.4).
"""


class ConfigLayer(StrEnum):
    """The four resolution layers plus the ``default`` the resolution starts from.

    The members are the layer names AAP 0.4.5 and AAP 0.7.4 publish, each a :class:`enum.StrEnum`
    member and therefore *is* its own text, so a resolved configuration reports a per-key origin
    as a plain string without a second conversion. ``PROFILE`` is a layer of its own rather than a
    part of ``FILE``: a ``[profiles.<name>]`` table overrides the base file and is overridden by the
    environment and the CLI, and that position in :data:`LAYER_ORDER` is the whole
    profile-precedence rule. The five values are part of the published promise (AAP 0.4.9).

    A value's layer is one of the five spellings AAP 0.4.5 publishes, and the fifth --
    ``profile:<name>`` -- carries the selected profile's own name, so no fixed member can hold it:
    :meth:`_missing_` answers that lookup with an instance of this class that *is* the spelling,
    which is what lets :attr:`foundry.config.sources.ConfigSource.layer` be typed as this enum while
    ``config show`` keeps printing ``profile:ci``.
    """

    DEFAULT = "default"
    FILE = "file"
    PROFILE = "profile"
    ENV = "env"
    CLI = "cli"

    @classmethod
    def _missing_(cls, value: object) -> ConfigLayer | None:
        """Return the instance for a profile-labelled spelling, or ``None`` to refuse it.

        :meth:`enum.Enum.__new__` asks this when a value matches no member, which is exactly
        the case AAP 0.4.5 describes for a value a ``[profiles.<name>]`` table supplied: its
        layer is ``profile:<name>`` for whichever name the run selected.

        Args:
            value: The value a caller looked a member up by.

        Returns:
            A :class:`ConfigLayer` that is ``profile:<name>`` for a non-empty name, and
            ``None`` for anything else, so ``profile`` alone, an empty name and a value
            that is not text still raise the ``ValueError`` the enum itself spells.
        """
        if not isinstance(value, str) or not value.startswith(_PROFILE_LABEL_PREFIX):
            return None
        if not value[len(_PROFILE_LABEL_PREFIX) :]:
            return None
        member = str.__new__(cls, value)
        object.__setattr__(member, "_name_", value)
        object.__setattr__(member, "_value_", value)
        return member


_PROFILE_LABEL_PREFIX: Final[str] = f"{ConfigLayer.PROFILE.value}{_LABEL_SEPARATOR}"
"""The prefix a profile-labelled layer spelling carries its profile's name after.

AAP 0.4.5 spells that layer ``profile:<name>``, so the prefix is the ``PROFILE`` member's own
value joined by the one label separator this module fixes. It is built here, from the member
rather than from a second literal, because it is the member's published spelling that
:meth:`ConfigLayer._missing_` has to recognise, and it is defined after the class because it
is derived from it.
"""


LAYER_ORDER: Final[tuple[ConfigLayer, ...]] = (
    ConfigLayer.DEFAULT,
    ConfigLayer.FILE,
    ConfigLayer.PROFILE,
    ConfigLayer.ENV,
    ConfigLayer.CLI,
)
"""The layers in the order they are applied, lowest precedence first.

AAP 0.4.5 fixes this order: a field default, then the file -- its base table with the
selected profile table applied over it -- then the environment, then the command line. A
later payload therefore wins over an earlier one for the same dotted key, and
:mod:`foundry.config.merge` obtains that by iterating this tuple rather than by naming the
layers itself.

The one upward edge in the request's chain is visible here: a profile overrides the file
it sits in, not the operator's environment variable, which is exactly the position
:attr:`ConfigLayer.PROFILE` occupies between :attr:`ConfigLayer.FILE` and
:attr:`ConfigLayer.ENV`.
"""


def layer_label(layer: ConfigLayer, detail: str = DEFAULT_DETAIL) -> str:
    """Return the source label of a layer together with the detail that names it.

    AAP 0.7.4 renders the Source column of ``foundry config show``, and the layered
    section of the HTML report, as ``default``, ``file:<path>``, ``profile:<name>``,
    ``env:<VARIABLE>`` or ``cli:<--option>``. This function is the one composition of
    those five spellings, so the terminal, the JSON document and the TOML document cannot
    drift apart.

    :attr:`ConfigLayer.DEFAULT` has nothing finer to name, so its detail is ignored and
    the bare word comes back; for the other four an empty detail yields the bare layer
    name rather than a dangling separator. A detail that already carries the label -- the
    ``profile:ci`` form :func:`foundry.config.errors.profile_layer` produces -- is
    returned unchanged, so a value can be labelled on the layer it came from and labelled
    again by a renderer without the prefix doubling.

    Args:
        layer: The layer the value came from.
        detail: What that layer names the value by -- a file path, an environment
            variable, an option spelling, a profile name -- or :data:`DEFAULT_DETAIL`.

    Returns:
        ``default`` for :attr:`ConfigLayer.DEFAULT`; otherwise ``<layer>`` when the detail
        is empty and ``<layer>:<detail>`` when it is not.
    """
    if layer is ConfigLayer.DEFAULT:
        return str(ConfigLayer.DEFAULT.value)
    if not detail:
        return str(layer.value)
    prefix = f"{layer.value!s}{_LABEL_SEPARATOR}"
    if detail.startswith(prefix):
        return detail
    return f"{prefix}{detail}"


@dataclass(frozen=True, slots=True)
class LayerPayload:
    """One layer's contribution: its values as dotted keys, and what it names itself by.

    :mod:`foundry.config.files` builds one per configuration file and one for the selected
    ``[profiles.<name>]`` table, :mod:`foundry.config.env` one from the matching ``FOUNDRY_*``
    variables, and :mod:`foundry.config.cli_layer` one from the overrides an operator typed.
    :attr:`values` is always flat, because both facts merge records are per dotted key, and the
    nesting is restored once by :func:`unflatten`. :attr:`detail` is a *name* -- a file path,
    variable spelling, option spelling or profile name -- never a value, which is why the
    configuration stores the name of the variable that holds a secret (implicit requirement I9).
    """

    layer: ConfigLayer
    values: Mapping[str, object]
    detail: str = DEFAULT_DETAIL

    @classmethod
    def from_nested(
        cls,
        layer: ConfigLayer,
        table: Mapping[str, object],
        *,
        detail: str = DEFAULT_DETAIL,
    ) -> LayerPayload:
        """Build a payload from a nested table, flattening it on the way.

        Every producer but the environment layer reads its values nested -- a TOML
        document, a ``[profiles.<name>]`` section, a mapping of option to value -- and
        needs exactly this: flatten, then carry the layer and the detail. Keeping the
        step here means the flat invariant of :attr:`values` is established in one place
        instead of once per producer.

        Args:
            layer: The layer the table came from.
            table: The nested mapping to flatten.
            detail: What that layer names itself by, or :data:`DEFAULT_DETAIL`.

        Returns:
            The payload holding one dotted key per leaf of ``table``.

        Raises:
            foundry.core.exceptions.ConfigError: If ``table`` cannot be flattened, which
                :func:`flatten` reports and attributes to ``layer``.
        """
        return cls(layer=layer, values=flatten(table, layer=layer), detail=detail)

    def is_empty(self) -> bool:
        """Return whether the layer contributed no values at all.

        An empty payload is normal rather than faulty: a run with no configuration file
        has no file payload, and a command line with no ``--foundry-*`` override has no
        CLI payload. Callers use this to skip a layer before iterating it, so a debug log
        line or an empty section is not produced for a layer that read nothing.

        Returns:
            ``True`` when :attr:`values` holds no dotted key.
        """
        return len(self.values) == 0

    def dotted_keys(self) -> tuple[str, ...]:
        """Return the payload's dotted keys in sorted order.

        Sorted rather than insertion-ordered because the order reaches a message, a log
        line and the ``source`` table: two layers that read the same keys in a different
        order must produce the same text, and a TOML document's key order is chosen by
        whoever wrote the file (AAP 0.13.1).

        Returns:
            One dotted key per value, sorted, so two runs over the same payload yield
            identical tuples.
        """
        return tuple(sorted(self.values))


def flatten(
    values: Mapping[str, object],
    *,
    prefix: str = "",
    layer: ConfigLayer | None = None,
) -> dict[str, object]:
    """Flatten a nested mapping into one entry per leaf, keyed by its dotted path.

    ``{"http": {"retries": {"max_attempts": 5}}}`` becomes
    ``{"http.retries.max_attempts": 5}``. A value that is not a mapping is a leaf and is
    kept whole, which is what leaves the list-valued keys -- ``http.retries.retry_on`` and
    ``secrets.redact_keys`` -- as single values instead of splitting them into
    ``secrets.redact_keys.0`` and friends. A mapping is descended into, except where the
    registry declares the key itself table-valued: ``[http.headers]`` arrives as the one
    key ``http.headers``, whose value is the whole header table, and a header name inside
    it never becomes a key of its own.

    That exception is what keeps a payload's keys the keys of the configuration surface.
    ``foundry config show`` prints one row per registry key, and a source record attributes
    one key to one layer (AAP 0.4.5), so a header table split into ``http.headers.accept``
    and friends would be attributed in fragments and then rejected as an unknown key
    (implicit requirement I2). Keeping it whole also resolves a header table the way every
    other value resolves: the later layer replaces it.

    Keys are visited in sorted order, so the result is ordered deterministically and the
    key a shape fault names is the same on every run, whatever order the file held.

    Args:
        values: The nested mapping to flatten.
        prefix: The dotted path the mapping sits under, for a caller flattening one table
            of a larger document. It is joined to each key with a dot and may be empty.
        layer: The layer the mapping came from, so a shape fault names it, or ``None``
            when the caller does not know and the fault is reported as ``default``.

    Returns:
        One entry per leaf, keyed by the leaf's dotted path, in sorted order.

    Raises:
        foundry.core.exceptions.ConfigError: If a table has a key that is not a string, or
            if the mapping defines one key both as a value and as the prefix of another
            (``http`` beside ``http.timeout``). The message names the shorter of the two,
            because that is the key whose shape is wrong.
    """
    flat: dict[str, object] = {}
    _flatten_into(values, prefix=prefix, layer=layer, target=flat)
    return flat


def unflatten(
    values: Mapping[str, object],
    *,
    layer: ConfigLayer | None = None,
) -> dict[str, object]:
    """Rebuild the nested mapping that a set of dotted keys came from.

    The inverse of :func:`flatten`. Two consumers hold the result and no more:
    :mod:`foundry.config.merge`, immediately before it constructs the settings model, and
    :mod:`foundry.config.render`, which builds the nested ``resolved`` document
    ``foundry config show --format json`` prints (AAP 0.4.5). Both work from the merged
    flat map, so the nesting exists only where a model or a document needs it.

    A leaf value that is itself a mapping -- ``http.headers`` is a table of header names
    -- is stored whole, exactly as it was given, and is never merged with a dotted key
    that descends below it.

    Args:
        values: The flat dotted keys to nest.
        layer: The layer whose values these are, so a conflict names it, or ``None`` when
            the caller does not know and the fault is reported as ``default``.

    Returns:
        The nesting, with the keys of every table in sorted order.

    Raises:
        foundry.core.exceptions.ConfigError: If a key is empty or has an empty segment, if
            one key is both a value and the prefix of another (``http`` beside
            ``http.timeout``), or if a key descends below a key already given as a whole
            value (``http.headers`` beside ``http.headers.accept``). The message names the
            shorter key, which is the one whose shape is wrong.
    """
    nested: dict[str, object] = {}
    leaves: set[str] = set()
    for key in sorted(values):
        segments = _segments(key, layer=layer)
        for depth in range(1, len(segments)):
            prefix = _KEY_SEPARATOR.join(segments[:depth])
            if prefix in leaves:
                raise _shape_error(prefix, _TABLE_EXPECTED, layer)
        _place(nested, segments, values[key], layer=layer)
        leaves.add(key)
    return nested


def _flatten_into(
    values: Mapping[str, object],
    *,
    prefix: str,
    layer: ConfigLayer | None,
    target: dict[str, object],
) -> None:
    """Flatten ``values`` into ``target`` under ``prefix``, in sorted key order.

    Descending stops at a key the registry declares table-valued, so the table reaches the
    settings model and the source table as the one key it is (AAP 0.4.5).

    Args:
        values: The nested mapping to flatten.
        prefix: The dotted path the mapping sits under, empty at the top level.
        layer: The layer the mapping came from, carried to the shape fault.
        target: The flat mapping the leaves are added to, mutated in place.

    Raises:
        foundry.core.exceptions.ConfigError: If a key is not a string, or if a leaf
            collides with a key already in ``target``.
    """
    for key in values:
        if not isinstance(key, str):
            raise _shape_error(prefix, _TABLE_EXPECTED, layer)
    for key in sorted(values):
        dotted = f"{prefix}{_KEY_SEPARATOR}{key}" if prefix else key
        value = values[key]
        if isinstance(value, Mapping) and not _is_table_valued(dotted):
            _flatten_into(value, prefix=dotted, layer=layer, target=target)
            continue
        _insert_leaf(target, dotted, value, layer=layer)


def _insert_leaf(
    target: dict[str, object],
    dotted: str,
    value: object,
    *,
    layer: ConfigLayer | None,
) -> None:
    """Add one leaf to ``target``, refusing a leaf that a table already contradicts.

    A flat mapping can express both ``http`` and ``http.timeout``, but the nesting those
    keys describe cannot hold both: one of them would have to be a value and a table at
    the same time. The shorter key is the one reported, because it is the one whose shape
    was already established when the longer key arrived.

    Args:
        target: The flat mapping to add to, mutated through the caller's dict.
        dotted: The leaf's dotted key.
        value: The leaf's value.
        layer: The layer the value came from, carried to the shape fault.

    Raises:
        foundry.core.exceptions.ConfigError: If ``dotted`` is already present, or if it is
            a prefix of an existing key or an existing key is a prefix of it.
    """
    for existing in target:
        if existing == dotted:
            raise _shape_error(dotted, _ONCE_EXPECTED, layer)
        if existing.startswith(f"{dotted}{_KEY_SEPARATOR}"):
            raise _shape_error(dotted, _TABLE_EXPECTED, layer)
        if dotted.startswith(f"{existing}{_KEY_SEPARATOR}"):
            raise _shape_error(existing, _TABLE_EXPECTED, layer)
    target[dotted] = value


def _segments(key: str, *, layer: ConfigLayer | None) -> tuple[str, ...]:
    """Split a dotted key into its non-empty segments.

    Args:
        key: The dotted key to split, for example ``http.retries.max_attempts``.
        layer: The layer the key came from, carried to the shape fault.

    Returns:
        The segments in order, never empty.

    Raises:
        foundry.core.exceptions.ConfigError: If the key, or any segment of it, is empty.
    """
    segments = tuple(key.split(_KEY_SEPARATOR))
    for segment in segments:
        if not segment:
            raise _shape_error(key, _DOTTED_EXPECTED, layer)
    return segments


def _place(
    nested: dict[str, object],
    segments: tuple[str, ...],
    value: object,
    *,
    layer: ConfigLayer | None,
) -> None:
    """Store ``value`` at the nesting described by ``segments``, creating tables as needed.

    Args:
        nested: The mapping being built, mutated in place.
        segments: The split dotted key, never empty.
        value: The leaf value to store at the end of the path.
        layer: The layer the value came from, carried to the shape fault.

    Raises:
        foundry.core.exceptions.ConfigError: If a segment on the way is already a value
            rather than a table, or if the last segment is already present.
    """
    node = nested
    path = ""
    for segment in segments[:-1]:
        path = f"{path}{_KEY_SEPARATOR}{segment}" if path else segment
        if segment not in node:
            created: dict[str, object] = {}
            node[segment] = created
            node = created
            continue
        child = node[segment]
        if not isinstance(child, dict):
            raise _shape_error(path, _TABLE_EXPECTED, layer)
        node = child
    last = segments[-1]
    if last in node:
        raise _shape_error(_KEY_SEPARATOR.join(segments), _ONCE_EXPECTED, layer)
    node[last] = value


def _is_table_valued(dotted: str) -> bool:
    """Return whether the registry declares ``dotted`` a key whose value is a table.

    ``http.headers`` is the only such key in the published surface (AAP 0.4.5), and the
    rule is stated as the registry's rather than as that key's name here: a later release
    that adds a table-valued key needs no change in this module.

    A key the registry does not know is not table-valued, so its entries keep their full
    spellings -- a misspelt section reports ``htpp.timeout`` rather than ``htpp``, which is
    what lets the nearest-key suggestion land on ``http.timeout``.

    Args:
        dotted: The dotted key that a mapping value sits under.

    Returns:
        ``True`` when the key's declared kind is a table.
    """
    spec = spec_for(dotted)
    return spec is not None and spec.kind == _MAPPING_KIND


def _shape_error(key: str, expected: str, layer: ConfigLayer | None) -> ConfigError:
    """Build the wrong-shape fault for ``key``, attributed to the layer it came from.

    The wording lives in :mod:`foundry.config.errors` and nowhere else (AAP 0.14.3), so
    this helper adds only the two facts the builders cannot know: which key, and which
    layer label that layer's detail yields.

    Args:
        key: The dotted key whose shape is wrong, or an empty prefix for the layer's own
            top-level table.
        expected: The shape the key accepts, phrased for a person reading the failure.
        layer: The layer the value came from, or ``None`` when the caller does not know.

    Returns:
        The :class:`foundry.core.exceptions.ConfigError` the caller raises.
    """
    label = errors.LAYER_DEFAULT if layer is None else layer_label(layer)
    return errors.invalid_shape(key or _ROOT_KEY, expected, layer=label)
