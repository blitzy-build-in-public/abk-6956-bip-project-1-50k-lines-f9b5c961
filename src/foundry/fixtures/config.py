"""The ``foundry_config`` and ``foundry_profile`` fixtures: the resolved configuration of a run.

Both are session scoped, because a session has exactly one configuration and one profile:
``foundry_config`` returns a :class:`foundry.config.models.FoundryConfig` -- the settings resolved
through the four layers, carrying one :class:`foundry.config.sources.ConfigSource` per key -- and
``foundry_profile`` returns the active profile name as a plain ``str``: ``default``, ``local``,
``ci`` or ``staging``.

**The fixture resolves the configuration itself.** ``foundry.fixtures`` may not import the plugin's
session stash, so the fixture calls :func:`foundry.config.merge.resolve` with the two inputs the
plugin's own bootstrap passes: the ``--foundry-*`` overrides, read back through
:func:`foundry.config.cli_layer.collect_from_pytest`, and the invocation directory. Resolution being
the one place the four layers are applied, the fixture and the plugin cannot disagree about a value.
``environ`` is deliberately not passed: the environment layer belongs to :mod:`foundry.config.env`.

**Overridable by name.** ``foundry_config`` is a plain session-scoped fixture returning the plain
resolved model, so a project replaces it with pytest's documented same-name override and shadows the
value through ``model_copy(update=...)``, which is how ``examples/checkout/conftest.py`` supplies an
ephemeral ``http.base_url`` and points ``data.dir``/``cassette.dir`` at the example's directories;
``foundry_profile`` follows a shadowed value because it reads the configuration. Module scope
imports ``pytest``, :mod:`pathlib` and :mod:`typing` only, :mod:`foundry.config` coming in inside
the fixture body. A configuration holds the *names* of the variables that carry credentials, never
their values, and nothing here reads a clock.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from foundry.config.models import FoundryConfig

__all__ = ["foundry_config", "foundry_profile"]
"""The two public names this module contributes to the twelve-fixture set (AAP 0.4.3)."""


def _invocation_dir(config: pytest.Config) -> Path:
    """Return the directory a run was invoked from, which file discovery resolves against.

    Two of the five discovery steps are anchored to the invocation directory, so the value handed to
    :func:`foundry.config.merge.resolve` as ``cwd`` is the directory the operator typed the command
    in, not the root directory pytest computed from the arguments. The record is read as text before
    it becomes a path because ``Path("")`` is ``Path(".")``: an empty record would normalise itself
    into a usable "current directory" and hide the case this fallback exists for, so a blank record
    falls back to ``rootpath``.

    Returns:
        The invocation directory, or ``config.rootpath`` when the recorded directory is blank.
    """
    recorded = str(config.invocation_params.dir).strip()
    if not recorded:
        return Path(config.rootpath)
    return Path(recorded)


@pytest.fixture(scope="session")
def foundry_config(request: pytest.FixtureRequest) -> FoundryConfig:
    """Return the configuration this session resolved, with the source of every value.

    Session scoped: one resolution serves the whole run, and every test, every fixture that depends
    on it and the plugin's own report read the same immutable instance. The four layers are applied
    in their fixed order -- the settings field defaults, then the configuration file with the
    selected ``[profiles.<name>]`` table over it, then the ``FOUNDRY_*`` environment, then the
    ``--foundry-*`` command line -- so a value stated in more than one place resolves to the highest
    layer that stated it. Per-key provenance is available through
    :meth:`foundry.config.models.FoundryConfig.source_of`, which names the layer (``default``,
    ``file``, ``profile:<name>``, ``env`` or ``cli``) and what inside it supplied the value. A
    configuration fault is not absorbed here: :class:`foundry.core.exceptions.ConfigError`
    propagates unchanged, so the test that requested the fixture reports why the run cannot be
    configured, just as the plugin terminates a session it cannot configure before collection.

    Returns:
        The resolved, frozen :class:`foundry.config.models.FoundryConfig`.

    Raises:
        foundry.core.exceptions.ConfigError: For the first fatal fault, raised by
            :func:`foundry.config.merge.resolve` and propagated unchanged: an explicitly named
            configuration file that is absent, an unknown key in any layer, an unparseable
            environment value, a value outside its accepted range, a profile outside the four names,
            or a configured auth variable the environment does not set.
    """
    # Imported inside the fixture rather than at module scope: pytest imports this module at every
    # start, and `foundry.config` pulls in pydantic and pydantic-settings, which the 150 ms
    # plugin-startup budget of AAP 0.14.2 cannot pay for (implicit requirement I5).
    from foundry.config import resolve
    from foundry.config.cli_layer import collect_from_pytest

    return resolve(
        cli_overrides=collect_from_pytest(request.config),
        cwd=_invocation_dir(request.config),
    )


@pytest.fixture(scope="session")
def foundry_profile(foundry_config: FoundryConfig) -> str:
    """Return the name of the active profile as a plain string.

    Session scoped, and always one of ``default``, ``local``, ``ci`` or ``staging``: the value is
    the ``profile`` key of the resolved configuration, which the file layer's ``[profiles.<name>]``
    selection, a ``FOUNDRY_PROFILE`` variable or ``--foundry-profile`` may have set, with the
    environment and the command line outranking the file. A string rather than the
    :class:`foundry.config.enums.ProfileName` member, because this is the fixture a suite asserts
    and interpolates with: it is the name the terminal summary prints as ``foundry: profile=<name>``
    and the name recorded in every result record and manifest.

    Returns:
        The active profile name, for example ``"ci"``.
    """
    return str(foundry_config.profile)
