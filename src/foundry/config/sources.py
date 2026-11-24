"""The per-key provenance record: which layer supplied a value, and what names it.

Every value a resolved configuration holds has exactly one origin -- the settings model's
own field default, the configuration file, a ``[profiles.<name>]`` table, a ``FOUNDRY_*``
environment variable or a ``--foundry-<dotted.key>`` option (AAP 0.4.5). A
:class:`ConfigSource` is that origin as a value: the layer, plus the detail that names what
inside the layer supplied it, which is what makes ``foundry config show`` able to attribute
each key and what makes the four-layer precedence inspectable rather than asserted
(implicit requirement I2).

The record exists because the settings model cannot produce it. ``pydantic-settings``
merges its sources itself and exposes no API naming the environment variable that matched a
field, so per-key attribution has to be recorded while each layer is applied.
:mod:`foundry.config.merge` is the only writer of the
:data:`ConfigSources` mapping, and :mod:`foundry.config.render`,
``foundry config show`` and the report are its readers.

``detail`` names a value and never holds one: a file path, an environment variable, an
option spelling or a profile name. It is printed by ``foundry config show`` and
``foundry doctor`` and can reach a report, so a value stored here would leak the way any
other configuration text does -- which is why the configuration carries the *name* of the
variable that holds a secret, never the secret (implicit requirement I9).

Importing this module performs no I/O, reads no environment variable, configures no logger,
parses nothing and holds no mutable state: the API is one frozen record, one type alias and
the constructors that build it (AAP 0.4.8). It imports no pydantic, so a reader of a source
map stays inside the plugin startup budget (AAP 0.14.2, implicit requirement I5), and it
delegates the label join to :func:`foundry.config.layers.layer_label` so the terminal, the
JSON document and the TOML document cannot drift apart.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TypeAlias

from foundry.config.layers import DEFAULT_DETAIL, ConfigLayer, layer_label

__all__: Final[tuple[str, ...]] = (
    "ConfigSource",
    "ConfigSources",
)

_PROFILE_LAYER_PREFIX: Final[str] = f"{ConfigLayer.PROFILE.value}:"
"""The prefix a profile-sourced layer carries its name inside (AAP 0.4.5).

:func:`foundry.config.errors.profile_layer` spells the same prefix for the messages that
name a profile table, and this module spells it for the ``layer`` field of a record whose
value came from that table; the two must therefore agree.
:func:`foundry.config.layers.layer_label` owns the ``<layer>:<detail>`` join both are read
through, so a rendered label agrees with either spelling.
"""


@dataclass(frozen=True, slots=True)
class ConfigSource:
    """Where one resolved configuration value came from (AAP 0.4.5, AAP 0.4.9).

    ``layer`` is a :class:`foundry.config.layers.ConfigLayer` carrying one of the five
    spellings AAP 0.4.5 publishes -- ``default``, ``file``, ``env``, ``cli`` or
    ``profile:<name>``. The profile form carries its name inside the field because that is
    how the plan spells that layer, and ``detail`` repeats the name so the record describes
    itself without a lookup.

    ``detail`` names what inside the layer supplied the value: the configuration file's
    path, the environment variable as it was spelled, the ``--foundry-*`` option as it was
    typed, or the profile's name. The ``default`` layer has nothing finer to name and
    carries :data:`foundry.config.layers.DEFAULT_DETAIL`.

    The record is frozen and slotted: a resolved configuration is read by ``config show``,
    ``doctor``, the report and a test comparing two resolutions, none of which may see it
    change under them. It is also hashable and compares by value, so two resolutions of the
    same inputs compare equal entry by entry.

    Build one through the constructors rather than the field list: they are where the five
    published spellings are written, and :func:`foundry.config.merge` uses them to record
    which layer won a key.

    Attributes:
        layer: The layer the value came from, carrying one of the five published spellings.
        detail: What the layer names the value by, or
            :data:`foundry.config.layers.DEFAULT_DETAIL`.
    """

    layer: ConfigLayer
    detail: str = DEFAULT_DETAIL

    def label(self) -> str:
        """Return the source label ``foundry config show`` prints in its Source column.

        AAP 0.7.4 renders that column, and the layered section of the report, as
        ``default``, ``file:<path>``, ``profile:<name>``, ``env:<VARIABLE>`` or
        ``cli:<--option>``. The join belongs to :func:`foundry.config.layers.layer_label`,
        so this method adds only what that function cannot know: a profile-sourced record
        already carries its name in :attr:`layer` and therefore renders as it stands.

        Returns:
            The layer's label, for example ``env:FOUNDRY_HTTP__TIMEOUT``; ``default`` when
            the value came from a field default, and never a trailing separator.
        """
        if self.layer.startswith(_PROFILE_LAYER_PREFIX):
            return str(self.layer)
        try:
            layer = ConfigLayer(self.layer)
        except ValueError:
            # A layer spelled outside the published vocabulary still renders rather than
            # raising: every caller is a renderer -- config show, doctor, the report -- and
            # Foundry's own plumbing never fails a run (AAP 0.1.2 failure isolation).
            return str(self.layer)
        return layer_label(layer, self.detail)

    def as_dict(self) -> dict[str, str]:
        """Return the record as the JSON object ``config show --format json`` emits.

        AAP 0.4.5 fixes the document: a source is ``{"layer": "env", "detail":
        "FOUNDRY_HTTP__TIMEOUT"}`` -- exactly these two keys, in this order, with string
        values. ``foundry config show --format json`` and ``--format toml`` both serialise
        this result, so a key added here would change the published document.

        Returns:
            A fresh two-key mapping; mutating it leaves the record unchanged.
        """
        return {"layer": str(self.layer), "detail": self.detail}

    def is_default(self) -> bool:
        """Return whether the value came from the settings model's own field default.

        A default-sourced value has no file, variable or option behind it, so a reader that
        lists origins -- the ``config show`` table, the doctor report -- can tell the field
        default apart from a value an operator set to the same number.

        Returns:
            ``True`` when :attr:`layer` is the ``default`` layer.
        """
        return self.layer == ConfigLayer.DEFAULT

    @classmethod
    def default(cls) -> ConfigSource:
        """Build the record of a value that came from its settings field's default.

        AAP 0.4.5 gives the ``default`` layer nothing finer to name, so the record carries
        an empty detail and renders as the bare word ``default``.

        Returns:
            The ``default`` record.
        """
        return cls(layer=ConfigLayer.DEFAULT, detail=DEFAULT_DETAIL)

    @classmethod
    def from_file(cls, path: Path | str) -> ConfigSource:
        """Build the record of a value a configuration file supplied.

        The detail is the path as text, spelled as the caller discovered it: ``foundry
        config show`` prints ``file:<path>``, and an operator has to be able to open that
        path from the row they are reading.

        Args:
            path: The configuration file the value came from -- ``foundry.toml`` or a
                ``pyproject.toml`` carrying ``[tool.foundry]``.

        Returns:
            The ``file`` record naming ``path``.
        """
        return cls(layer=ConfigLayer.FILE, detail=str(path))

    @classmethod
    def from_profile(cls, name: str) -> ConfigSource:
        """Build the record of a value a ``[profiles.<name>]`` table supplied.

        A profile overrides the base file it sits in and is overridden by the environment
        and the command line (AAP 0.4.5), so its value must stay distinguishable from a
        base-file value: the name rides in :attr:`layer` as ``profile:<name>``, which is
        what makes the row read ``profile:ci`` rather than ``file:<path>``. That spelling is
        an instance of :class:`foundry.config.layers.ConfigLayer` built from the profile
        layer's own prefix, and a record with no name carries the plain ``profile`` layer.

        Args:
            name: The profile whose table supplied the value, for example ``ci``.

        Returns:
            The record whose layer is a :class:`foundry.config.layers.ConfigLayer` spelling
            ``profile:<name>`` and whose detail is ``name``.
        """
        layer = ConfigLayer(f"{_PROFILE_LAYER_PREFIX}{name}") if name else ConfigLayer.PROFILE
        return cls(layer=layer, detail=name)

    @classmethod
    def from_env(cls, variable: str) -> ConfigSource:
        """Build the record of a value a ``FOUNDRY_*`` environment variable supplied.

        The detail is the variable's spelling exactly as the environment layer matched it.
        Upper case is the canonical spelling of a ``FOUNDRY_*`` name, but nothing is
        normalised here: an operator who exported ``foundry_http__timeout`` reads that
        spelling back in the row that attributes their value (AAP 0.4.5).

        Args:
            variable: The environment variable's name, for example
                ``FOUNDRY_HTTP__TIMEOUT``.

        Returns:
            The ``env`` record naming ``variable``.
        """
        return cls(layer=ConfigLayer.ENV, detail=variable)

    @classmethod
    def from_cli(cls, option: str) -> ConfigSource:
        """Build the record of a value a ``--foundry-*`` option supplied.

        The detail is the option spelling verbatim, so an override given as
        ``--foundry-http.timeout`` is attributed to ``cli:--foundry-http.timeout`` with no
        further formatting (AAP 0.4.4, AAP 0.7.4).

        Args:
            option: The option as the operator typed it, for example
                ``--foundry-http.timeout``.

        Returns:
            The ``cli`` record naming ``option``.
        """
        return cls(layer=ConfigLayer.CLI, detail=option)


ConfigSources: TypeAlias = Mapping[str, ConfigSource]
"""The per-key provenance of a resolved configuration, as
:attr:`foundry.config.models.FoundryConfig.sources` holds it.

Keys are flat dotted configuration keys -- the form :mod:`foundry.config.layers` merges and
:mod:`foundry.config.keys` names, for example ``http.retries.max_attempts`` -- so one key is
attributed to one layer as a whole, including a table-valued key such as ``http.headers``.
Every leaf key the configuration publishes has an entry, which is what lets
``foundry_config.source_of(dotted_key)`` answer for any key an operator asks about
(AAP 0.4.5, AAP 0.7.2).

The mapping is read-only by type: :mod:`foundry.config.merge` builds it while it applies the
layers and hands it over, and every later reader only looks keys up. A renderer that writes
a document from it sorts the keys, so two runs print the same rows in the same order
(AAP 0.13.1).
"""
