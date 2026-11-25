"""The per-key provenance record: which layer supplied a value, and what names it.

Every value a resolved configuration holds has exactly one origin -- a field default, the
configuration file, a ``[profiles.<name>]`` table, a ``FOUNDRY_*`` variable or a
``--foundry-<dotted.key>`` option (AAP 0.4.5) -- and :mod:`foundry.config.merge` records it here
because ``pydantic-settings`` names no source it resolved from. ``detail`` names a value and never
holds one, which is why the configuration carries the *name* of the variable holding a secret.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TypeAlias

from foundry.config.layers import DEFAULT_DETAIL, ConfigLayer, layer_label

__all__: Final[tuple[str, ...]] = ("ConfigSource", "ConfigSources")

_PROFILE_LAYER_PREFIX: Final[str] = f"{ConfigLayer.PROFILE.value}:"
"""The prefix a profile-sourced layer carries its name inside (AAP 0.4.5).

:func:`foundry.config.errors.profile_layer` spells the same prefix for the messages that name a
profile table, so the two must agree.
"""


@dataclass(frozen=True, slots=True)
class ConfigSource:
    """Where one resolved configuration value came from (AAP 0.4.5, AAP 0.4.9).

    ``layer`` is a :class:`foundry.config.layers.ConfigLayer` carrying one of the five published
    spellings -- ``default``, ``file``, ``env``, ``cli`` or ``profile:<name>`` -- and ``detail``
    names what inside it supplied the value: the file's path, the variable as spelled, the option as
    typed, or the profile's name. Frozen, slotted and compared by value, so two resolutions of the
    same inputs compare equal entry by entry; build one through the constructors below.
    """

    layer: ConfigLayer
    detail: str = DEFAULT_DETAIL

    def label(self) -> str:
        """Return the source label ``foundry config show`` prints in its Source column.

        That column reads ``default``, ``file:<path>``, ``profile:<name>``, ``env:<VARIABLE>`` or
        ``cli:<--option>``. The join belongs to :func:`foundry.config.layers.layer_label`, so this
        method adds only what that function cannot know: a profile-sourced record already carries
        its name in :attr:`layer` and renders as it stands.

        Returns:
            The layer's label, for example ``env:FOUNDRY_HTTP__TIMEOUT``; ``default`` when the
            value came from a field default, and never a trailing separator.
        """
        if self.layer.startswith(_PROFILE_LAYER_PREFIX):
            return str(self.layer)
        try:
            layer = ConfigLayer(self.layer)
        except ValueError:
            # A layer spelled outside the published vocabulary still renders rather than raising:
            # every caller is a renderer, and Foundry's own plumbing never fails a run.
            return str(self.layer)
        return layer_label(layer, self.detail)

    def as_dict(self) -> dict[str, str]:
        """Return the record as the JSON object ``config show --format json`` emits.

        AAP 0.4.5 fixes that document to exactly the keys ``layer`` and ``detail``, in this order,
        and both output formats serialise this result, so a key added here would change it.

        Returns:
            A fresh two-key mapping; mutating it leaves the record unchanged.
        """
        return {"layer": str(self.layer), "detail": self.detail}

    def is_default(self) -> bool:
        """Return whether the value came from the settings model's own field default.

        A reader listing origins can then tell the field default from an operator's same value.

        Returns:
            ``True`` when :attr:`layer` is the ``default`` layer.
        """
        return self.layer == ConfigLayer.DEFAULT

    @classmethod
    def default(cls) -> ConfigSource:
        """Build the record of a value that came from its settings field's default.

        The ``default`` layer has nothing finer to name, so the detail is empty and the record
        renders as the bare word ``default``.

        Returns:
            The ``default`` record.
        """
        return cls(layer=ConfigLayer.DEFAULT, detail=DEFAULT_DETAIL)

    @classmethod
    def from_file(cls, path: Path | str) -> ConfigSource:
        """Build the record of a value a configuration file supplied.

        The detail is the path as text, spelled as the caller discovered it, so an operator can
        open the file from the row they are reading.

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

        A profile overrides the base file it sits in and is overridden by the environment and the
        command line (AAP 0.4.5), so the name rides in :attr:`layer` as ``profile:<name>`` and the
        row reads ``profile:ci`` rather than ``file:<path>``.

        Args:
            name: The profile whose table supplied the value, for example ``ci``.

        Returns:
            The record spelling ``profile:<name>``, with ``name`` as its detail.
        """
        layer = ConfigLayer(f"{_PROFILE_LAYER_PREFIX}{name}") if name else ConfigLayer.PROFILE
        return cls(layer=layer, detail=name)

    @classmethod
    def from_env(cls, variable: str) -> ConfigSource:
        """Build the record of a value a ``FOUNDRY_*`` environment variable supplied.

        The detail is the variable's spelling as the environment layer matched it, never
        normalised, so an operator who exported ``foundry_http__timeout`` reads that spelling back.

        Args:
            variable: The variable's name, for example ``FOUNDRY_HTTP__TIMEOUT``.

        Returns:
            The ``env`` record naming ``variable``.
        """
        return cls(layer=ConfigLayer.ENV, detail=variable)

    @classmethod
    def from_cli(cls, option: str) -> ConfigSource:
        """Build the record of a value a ``--foundry-*`` option supplied.

        The detail is the option spelling verbatim, so ``--foundry-http.timeout`` is attributed to
        ``cli:--foundry-http.timeout`` with no further formatting.

        Args:
            option: The option as the operator typed it, for example ``--foundry-http.timeout``.

        Returns:
            The ``cli`` record naming ``option``.
        """
        return cls(layer=ConfigLayer.CLI, detail=option)


ConfigSources: TypeAlias = Mapping[str, ConfigSource]
"""The per-key provenance of a resolved configuration, as :attr:`FoundryConfig.sources` holds it.

Keys are flat dotted configuration keys -- the form :mod:`foundry.config.layers` merges and
:mod:`foundry.config.keys` names -- so one key is attributed to one layer as a whole, including a
table-valued key such as ``http.headers``. Every leaf key the configuration publishes has an entry,
which is what lets ``foundry_config.source_of(dotted_key)`` answer for any key an operator asks
about.

The mapping is read-only by type, and a renderer that writes a document from it sorts the keys, so
two runs print the same rows in the same order.
"""
