"""Profile selection and the merge of the selected ``[profiles.<name>]`` table.

:func:`select_profile` decides the name through the four-layer ladder, and :func:`apply_profile`
merges that table over the base values and reports the keys it contributed, so a value only
``[profiles.ci]`` supplies is attributed to ``profile:ci`` (AAP 0.4.5).
"""

from __future__ import annotations

import types
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from foundry.config import errors, validation
from foundry.config.cli_layer import CliInput
from foundry.config.enums import PROFILE_NAMES, ProfileName, accepted_values_text, parse_enum
from foundry.config.env import EnvPayload

__all__: Final[tuple[str, ...]] = (
    "DEFAULT_PROFILE",
    "PROFILES_KEY",
    "PROFILE_KEY",
    "ProfileApplication",
    "apply_profile",
    "profile_names",
    "select_profile",
)

PROFILE_KEY: Final[str] = "profile"

PROFILES_KEY: Final[str] = "profiles"

# "default" is a real profile: a [profiles.default] table is applied like any other.
DEFAULT_PROFILE: Final[ProfileName] = ProfileName.DEFAULT

_TABLE_EXPECTED: Final[str] = "a table of dotted configuration keys"

_KEY_EXPECTED: Final[str] = "a dotted key of the configuration surface"


def profile_names() -> tuple[str, ...]:
    """Return the profile names AAP 0.4.5 accepts, in declaration order.

    Derived from :class:`foundry.config.enums.ProfileName`, so a name and the text that spells it
    can never disagree.
    """
    return PROFILE_NAMES


def select_profile(*, cli: CliInput, env: EnvPayload, base: Mapping[str, object]) -> ProfileName:
    """Choose the active profile before the profile table is looked up.

    The four-layer ladder applied to the one key that has to be resolved ahead of the merge, highest
    priority first: ``--foundry-profile``, ``FOUNDRY_PROFILE``, the base file, then
    :data:`DEFAULT_PROFILE`, which is the whole of the zero-configuration state.

    The first layer that names a profile decides and the layers below it are not consulted: a
    ``profile`` in a profile table is rejected rather than read, so there is no fifth source. A file
    value is matched case-insensitively, so ``profile = "CI"`` selects the same profile ``ci`` does.

    Raises:
        foundry.core.exceptions.ConfigError: if the highest layer that names a profile names one
            outside the four, ``errors.profile_invalid`` reporting the ``profile`` key and the
            accepted values (exit code 2). A value of another type is reported the same way rather
            than with a ``TypeError``.
    """
    for value in (
        cli.overrides.get(PROFILE_KEY),
        env.values.get(PROFILE_KEY),
        base.get(PROFILE_KEY),
    ):
        if value is None:
            continue
        return _as_profile(value)
    return DEFAULT_PROFILE


def _as_profile(value: object) -> ProfileName:
    """Return the profile ``value`` names, or raise as if the layer had spelled it wrong.

    Nothing here coerces a number or a table into a profile.

    Raises:
        foundry.core.exceptions.ConfigError: if the value is not one of the four names.
    """
    if isinstance(value, ProfileName):
        return value
    if isinstance(value, str):
        member = parse_enum(ProfileName, value)
        if member is not None:
            return member
        raise errors.profile_invalid(value, accepted_values_text(ProfileName))
    raise errors.profile_invalid(repr(value), accepted_values_text(ProfileName))


def _sorted_values(values: Mapping[str, object]) -> Mapping[str, object]:
    """Return a read-only view of ``values`` whose keys iterate in sorted order.

    Ordered iteration, not immutability alone, is what the determinism rule needs: a merged mapping
    reaches ``foundry config show``, the debug log line and any test comparing two resolutions.
    Copying also keeps a caller's later edit of its own dict out of an application it already
    handed over.
    """
    return types.MappingProxyType({key: values[key] for key in sorted(values)})


@dataclass(frozen=True, slots=True)
class ProfileApplication:
    """The base values with the selected profile table applied over them.

    The whole of what :mod:`foundry.config.merge` needs from the profile step: the values to merge,
    whether a table was applied, and which keys it supplied so each one is attributed to
    ``profile:<name>`` rather than to the file it sits beside. :attr:`contributed`'s iteration
    order is *not* meaningful, so a reader that needs an order calls :meth:`contributed_keys`.

    Attributes:
        name: The profile whose table was applied, whether or not it existed.
        values: The base payload with the table merged over it, ordered by dotted key.
        applied: Whether a table for :attr:`name` existed; ``False`` changes nothing.
        contributed: The keys the table supplied; empty when :attr:`applied` is ``False``.
    """

    name: ProfileName
    values: Mapping[str, object]
    applied: bool
    contributed: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        """Freeze the contributed keys and put the values in sorted key order.

        A ``dict`` handed to the frozen dataclass would stay mutable, so both fields are rebuilt
        here, which is what makes the documented ordering and value-equality hold for an instance
        a caller constructs directly as well as for one :func:`apply_profile` returns.
        """
        object.__setattr__(self, "values", _sorted_values(self.values))
        object.__setattr__(self, "contributed", frozenset(self.contributed))

    def dotted_keys(self) -> tuple[str, ...]:
        """Return the resolved keys in sorted order.

        The order reaches the debug log line and the source table, and two runs over the same
        inputs must produce the same text.
        """
        return tuple(sorted(self.values))

    def contributed_keys(self) -> tuple[str, ...]:
        """Return the keys the profile table supplied, in sorted order.

        The deterministic form of :attr:`contributed`: sorting here rather than at each call site
        keeps a hash-ordered set from reaching output.
        """
        return tuple(sorted(self.contributed))


def apply_profile(
    base: Mapping[str, object],
    profile_tables: Mapping[str, Mapping[str, object]],
    name: ProfileName,
) -> ProfileApplication:
    """Merge the ``[profiles.<name>]`` table over the base file's values.

    The second half of the profile rule, and the only place the two are combined: the table's values
    win for every key it carries, every key only the base carries survives untouched, and the
    table's keys are reported through :attr:`ProfileApplication.contributed` so they can be
    attributed to ``profile:<name>``. The environment and the command line are never consulted: they
    sit above the file-plus-profile layer in :data:`foundry.config.layers.LAYER_ORDER`, which makes
    "a profile never overrides an operator" structurally true rather than a convention.

    A profile the tables do not contain returns ``base`` unchanged with ``applied`` false, which is
    the zero-configuration state, and an empty table is applied, contributes nothing and changes
    nothing. Two faults are refused here: a ``profile`` key inside the table would reselect the
    profile while its own table is being applied, and an unknown key inside the table is reported
    against ``profile:<name>`` rather than against the file. :mod:`foundry.config.files` rejects the
    first when it reads the file; the check is repeated here because :mod:`foundry.config.merge`
    can also be handed a payload a caller built itself.

    Raises:
        foundry.core.exceptions.ConfigError: ``errors.profile_override_attempt`` when the table sets
            ``profile``, ``errors.unknown_key`` with the closest valid spelling when it sets a key
            the registry does not declare, and ``errors.invalid_shape`` when the entry is not a
            table or not keyed by text.
    """
    table = profile_tables.get(name.value)
    if table is None:
        return ProfileApplication(name=name, values=base, applied=False)
    checked = _checked_table(table, name)
    merged = dict(base)
    merged.update(checked)
    return ProfileApplication(
        name=name,
        values=merged,
        applied=True,
        contributed=frozenset(checked),
    )


def _checked_table(table: object, name: ProfileName) -> Mapping[str, object]:
    """Return the selected profile table once it can be merged, or raise naming it.

    Every question asked of a table before it is merged is asked here, in the order that produces
    the most useful message: the entry must be a table, its keys must be text, it may not reselect
    the profile, and every key it sets must be one the registry declares. The first two checks
    exist for a payload built in code rather than read from a file.

    Raises:
        foundry.core.exceptions.ConfigError: as :func:`apply_profile` documents.
    """
    layer = errors.profile_layer(name.value)
    label = f"{PROFILES_KEY}.{name.value}"
    if not isinstance(table, Mapping):
        raise errors.invalid_shape(label, _TABLE_EXPECTED, layer=layer)
    for key in table:
        if not isinstance(key, str):
            raise errors.invalid_shape(label, _KEY_EXPECTED, layer=layer)
    checked: Mapping[str, object] = table
    if PROFILE_KEY in checked:
        raise errors.profile_override_attempt(name.value)
    validation.validate_unknown_keys(checked, layer=layer, detail=name.value)
    return checked
