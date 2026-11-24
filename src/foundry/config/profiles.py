"""Profile selection and the merge of the selected ``[profiles.<name>]`` table.

A profile is the one configuration value that decides which other values apply, so Foundry
resolves it in two steps that this module owns and no other module repeats.

**Selection.** :func:`select_profile` reads the ``profile`` key from the command line, then
the environment, then the base file, then the field default, and answers with one
:class:`foundry.config.enums.ProfileName`. The step stands on its own because the table to
merge cannot be chosen before the name is known: the name is decided by the same four-layer
ladder every other key follows, and deciding it first is what makes the merge well defined.

**Application.** :func:`apply_profile` merges the selected table -- ``[profiles.<name>]`` in
``foundry.toml`` or ``[tool.foundry.profiles.<name>]`` in ``pyproject.toml`` -- over the base
file's values. Every key the table carries wins, every key only the base carries survives,
and a table for the selected name that does not exist leaves the values untouched instead of
raising, which is the zero-configuration path of AAP 0.4.7. A profile may not reselect the
profile, an unknown key inside a table is reported against that profile rather than against
the base file, and an entry that is not a table of dotted keys is refused the same way.

AAP 0.4.5 places a profile above the base file and below the environment and the command
line. :func:`apply_profile` accepts neither an ``environ`` nor a
:class:`foundry.config.cli_layer.CliInput`, so this module cannot consult the two higher
layers even by mistake: only :func:`select_profile` reads them, and only to choose the name.
The ordering itself belongs to :data:`foundry.config.layers.LAYER_ORDER` and is applied by
:mod:`foundry.config.merge`; :class:`ProfileApplication` reports which keys the selected
table contributed, so a value only ``[profiles.ci]`` supplies is attributed to
``profile:ci`` rather than to the file it sits beside (AAP 0.4.5, AAP 0.7.4, criterion 4).

Nothing here reads a value. A profile table carries the same key surface as the base file,
so ``secrets.redact_keys`` inside one is configuration like any other and never a credential
(implicit requirement I9). Nothing here builds a
:class:`foundry.config.sources.ConfigSource` either: ``ConfigSource.from_profile`` is the one
constructor of the ``profile:<name>`` record, and a second one would let the label drift.

The module performs no I/O, reads no environment variable, configures no logger and holds no
mutable module state -- immutable string constants, the derived default profile and the
accepted names the enum already owns are all it keeps. Every fault leaves as a
:class:`foundry.core.exceptions.ConfigError` built by :mod:`foundry.config.errors`, which the
command line reports as exit code 2 and the plugin turns into ``pytest.exit(returncode=2)``
before collection (assumption A4).
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
"""The dotted key naming the active profile (AAP 0.4.5).

It is a registry key like any other -- :data:`foundry.config.keys.KEY_SPECS` declares it
``enum``-kind with the ``--foundry-profile`` alias -- and it is named here as well because
this module reads it out of three payload shapes by name.
"""

PROFILES_KEY: Final[str] = "profiles"
"""The table holding one entry per profile, keyed by name.

``[profiles.<name>]`` in ``foundry.toml`` and ``[tool.foundry.profiles.<name>]`` in
``pyproject.toml`` (AAP 0.4.5). The file layer strips it from the base values and hands the
tables over unmerged; nothing merges it into the configuration surface, because a profile
name is not a configuration key.
"""

DEFAULT_PROFILE: Final[ProfileName] = ProfileName.DEFAULT
"""The profile a run uses when no layer selects one (AAP 0.4.7, AAP 0.4.5).

``default`` is a real profile and not merely the absence of one: a ``[profiles.default]``
table, when a file declares it, is applied like any other.
"""

_TABLE_EXPECTED: Final[str] = "a table of dotted configuration keys"
"""What an entry of the ``profiles`` table has to be, as ``errors.invalid_shape`` quotes it."""

_KEY_EXPECTED: Final[str] = "a dotted key of the configuration surface"
"""What a key inside a profile table has to be: text the registry can be asked about."""


def profile_names() -> tuple[str, ...]:
    """Return the profile names AAP 0.4.5 accepts, in declaration order.

    The tuple is :data:`foundry.config.enums.PROFILE_NAMES`, derived from
    :class:`foundry.config.enums.ProfileName` so a name and the text that spells it can never
    disagree. It is what a message lists as the accepted values and what ``foundry doctor``
    reports when it names the available profiles.

    Returns:
        ``("default", "local", "ci", "staging")`` as an immutable tuple, so a caller cannot
        edit what the next caller reads.
    """
    return PROFILE_NAMES


def select_profile(*, cli: CliInput, env: EnvPayload, base: Mapping[str, object]) -> ProfileName:
    """Choose the active profile before the profile table is looked up.

    The precedence is AAP 0.4.5's four-layer ladder applied to the one key that has to be
    resolved ahead of the merge, highest priority first:

    1. the command line -- :attr:`foundry.config.cli_layer.CliInput.overrides` entry for
       ``profile``, which ``--foundry-profile <name>`` supplies;
    2. the environment -- :attr:`foundry.config.env.EnvPayload.values` entry for ``profile``,
       which ``FOUNDRY_PROFILE`` supplies;
    3. the base file -- the ``profile`` key of the flattened file payload;
    4. :data:`DEFAULT_PROFILE`, which is the answer whenever no layer names one, and the whole
       of the zero-configuration state AAP 0.4.7 describes.

    The first layer that names a profile decides, and the layers below it are not consulted:
    a ``profile`` in a profile table is rejected rather than read, so there is no fifth
    source. A command line or environment value arrives already coerced to a member by
    :mod:`foundry.config.validation`; a value read from the file is plain TOML and is matched
    case-insensitively by :func:`foundry.config.enums.parse_enum`, so ``profile = "CI"``
    selects the same profile ``ci`` does.

    Args:
        cli: The command line's input for this invocation, or
            :data:`foundry.config.cli_layer.EMPTY_CLI_INPUT` when it supplied nothing.
        env: The environment layer's payload, or an empty payload when no ``FOUNDRY_*``
            variable was set.
        base: The file layer's flattened ``dotted key -> value`` payload, before any profile
            table is applied. An empty mapping is the no-file case.

    Returns:
        The selected profile, never ``None``: a run with nothing configured resolves to
        :data:`DEFAULT_PROFILE`.

    Raises:
        foundry.core.exceptions.ConfigError: If the highest layer that names a profile names
            one outside the four, ``errors.profile_invalid`` reporting the ``profile`` key and
            the accepted values (AAP 0.4.5, exit code 2). A value of another type -- a number
            or a table where a name belongs -- is reported the same way rather than with a
            ``TypeError``, because it reaches an operator as a message either way.
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

    Args:
        value: A profile read from one of the layers: a member when the layer coerced it
            already, the name as text when it came from the file, or whatever TOML held when
            the value is of another type.

    Returns:
        The matching member.

    Raises:
        foundry.core.exceptions.ConfigError: If the value is not one of the four names. The
            rendering of a non-string value is for a reader, never for a parser: nothing here
            coerces a number or a table into a profile.
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

    Ordered iteration, not immutability alone, is what the determinism rule needs: a merged
    mapping reaches ``foundry config show``, the debug log line and any test comparing two
    resolutions, so two applications of the same inputs must iterate alike whatever order the
    caller built its mapping in (AAP 0.13.1). Copying is also what keeps a caller's later edit
    of its own dict out of an application it already handed over.

    Args:
        values: The flat ``dotted key -> value`` mapping to copy.

    Returns:
        A :class:`types.MappingProxyType` over a fresh dict holding the same entries in sorted
        key order.
    """
    return types.MappingProxyType({key: values[key] for key in sorted(values)})


@dataclass(frozen=True, slots=True)
class ProfileApplication:
    """The base values with the selected profile table applied over them.

    The result of :func:`apply_profile`, and the whole of what
    :mod:`foundry.config.merge` needs from the profile step: the values to merge into the
    configuration, whether a table was actually applied, and which keys that table supplied so
    each one is attributed to ``profile:<name>`` rather than to the file it sits beside
    (AAP 0.4.5, AAP 0.7.4, criterion 4).

    :attr:`contributed` is the membership set merge tests a key against -- ``key in
    application.contributed`` -- because a source record is built per key and the lookup is the
    only question asked of it. Its iteration order is *not* meaningful: a set of strings is
    ordered by hash, which differs between processes. A reader that needs the keys in an order
    something can print calls :meth:`contributed_keys`, which sorts them.

    The instance is frozen and slotted, and :attr:`values` is a key-sorted read-only view, so a
    resolved configuration cannot be edited after the fact and two applications of the same
    inputs compare equal field by field. A changed application is a new application.

    Attributes:
        name: The profile whose table was applied, whether or not it existed.
        values: The base payload with the table merged over it, flat and ordered by dotted key.
        applied: Whether a table for :attr:`name` existed. ``False`` covers the no-file case
            and the case of a file with no table for the selected profile, both of which leave
            the configuration untouched and raise nothing (AAP 0.4.7).
        contributed: The keys the table supplied, each of which won its value from the base
            payload. Empty when :attr:`applied` is ``False``, and also when the table exists
            but sets no key.
    """

    name: ProfileName
    values: Mapping[str, object]
    applied: bool
    contributed: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        """Freeze the contributed keys and put the values in sorted key order.

        The dataclass is already frozen, but a ``dict`` handed to it would stay mutable and a
        :class:`frozenset` built from a caller's set would keep that set's identity. Both
        fields are rebuilt here instead, which is what makes the documented ordering and
        value-equality hold for an instance a caller constructs directly as well as for one
        :func:`apply_profile` returns.
        """
        object.__setattr__(self, "values", _sorted_values(self.values))
        object.__setattr__(self, "contributed", frozenset(self.contributed))

    def dotted_keys(self) -> tuple[str, ...]:
        """Return the resolved keys in sorted order.

        Sorted rather than insertion-ordered because the order reaches the debug log line, the
        source table and any list a report prints, and two runs over the same inputs must
        produce the same text (AAP 0.13.1).

        Returns:
            One dotted key per resolved value, sorted.
        """
        return tuple(sorted(self.values))

    def contributed_keys(self) -> tuple[str, ...]:
        """Return the keys the profile table supplied, in sorted order.

        The deterministic form of :attr:`contributed`, for a caller that prints the keys or
        lists them in a document. Sorting here rather than at each call site is what keeps a
        hash-ordered set from reaching output.

        Returns:
            One dotted key per contributed value, sorted; empty when the table existed but set
            nothing, and when no table was applied at all.
        """
        return tuple(sorted(self.contributed))


def apply_profile(
    base: Mapping[str, object],
    profile_tables: Mapping[str, Mapping[str, object]],
    name: ProfileName,
) -> ProfileApplication:
    """Merge the ``[profiles.<name>]`` table over the base file's values.

    The second half of AAP 0.4.5's profile rule, and the only place the two are combined. The
    table's values win over the base file's for every key the table carries; every key only the
    base carries survives untouched; keys the table sets are reported through
    :attr:`ProfileApplication.contributed` so they can be attributed to ``profile:<name>``. The
    environment and the command line are not arguments and are never consulted: they sit above
    the file plus profile layer in :data:`foundry.config.layers.LAYER_ORDER`, which is what
    makes "a profile never overrides an operator" structurally true rather than a convention.

    A profile for the selected name that the tables do not contain -- no configuration file at
    all, or a file without that profile -- returns ``base`` unchanged with ``applied`` false,
    which is the zero-configuration state AAP 0.4.7 requires to proceed rather than fail. An
    empty table is different: it is applied, contributes nothing and changes nothing.

    Two faults are refused here. A ``profile`` key inside the table would reselect the profile
    while its own table is being applied, and an unknown key inside the table is reported
    against ``profile:<name>`` rather than against the file, so an operator is sent to the
    table at fault. :mod:`foundry.config.files` rejects the first when it reads the file; the
    check is repeated here because :mod:`foundry.config.merge` can also be handed a file
    payload a caller built itself. An entry that is not a table, and a table keyed by something
    other than text, are refused the same way rather than left to fail deeper in the registry
    lookup, which is what keeps a hand-built payload from surfacing an ``AttributeError``.

    Args:
        base: The file layer's flattened ``dotted key -> value`` payload, before the profile
            table. An empty mapping is the no-file case.
        profile_tables: The unmerged ``profile name -> flattened table`` mapping the file layer
            extracted. Keys may be spelled as :class:`str` or as
            :class:`foundry.config.enums.ProfileName` members: a ``StrEnum`` member compares
            and hashes as its value, so both spellings address the same table.
        name: The profile :func:`select_profile` chose.

    Returns:
        The merged payload with the table's contribution recorded; ``base`` itself, unchanged
        and ``applied=False``, when no table matches ``name``.

    Raises:
        foundry.core.exceptions.ConfigError: ``errors.profile_override_attempt`` when the
            table sets ``profile``, ``errors.unknown_key`` with the closest valid spelling when
            it sets a key the registry does not declare, and ``errors.invalid_shape`` when the
            entry is not a table or not keyed by text. Each names ``profile:<name>`` as the
            layer and ends a run with exit code 2 (AAP 0.4.5).
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

    Every question asked of a table before it is merged is asked here, in the order that
    produces the most useful message: the entry must be a table, its keys must be text, it may
    not reselect the profile, and every key it sets must be one the registry declares. The
    caller that reaches this function has already established that a table exists for ``name``.

    The first two checks exist for a payload built in code rather than read from a file --
    :mod:`foundry.config.merge` accepts a :class:`foundry.config.layers.LayerPayload` from any
    caller. Left out, a hand-written ``profiles.ci = "fast"`` would be matched on a substring
    by the reselection test below, and a table keyed by a number would fail inside the
    registry lookup with an ``AttributeError`` instead of the configuration fault an operator
    can act on.

    Args:
        table: The entry the tables mapping held for ``name``, typed loosely because a payload
            a caller built is not guaranteed to hold a table.
        name: The profile whose table this is, which every message names.

    Returns:
        The same table, proved to be a mapping keyed by dotted text.

    Raises:
        foundry.core.exceptions.ConfigError: As :func:`apply_profile` documents.
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
