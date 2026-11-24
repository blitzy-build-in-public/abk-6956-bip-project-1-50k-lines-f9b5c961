"""The ``foundry_config`` and ``foundry_profile`` fixtures: the resolved configuration of a run.

Two of Foundry's twelve public fixtures (AAP 0.4.3), both session scoped, because a session has
exactly one configuration and one profile:

* ``foundry_config`` returns a :class:`foundry.config.models.FoundryConfig` -- the settings of
  AAP 0.4.5 resolved through the four layers, carrying one
  :class:`foundry.config.sources.ConfigSource` per key, which ``source_of(dotted_key)`` reports.
* ``foundry_profile`` returns the active profile name as a plain ``str``: ``default``, ``local``,
  ``ci`` or ``staging``.

**The fixture resolves the configuration itself.** AAP 0.4.8 puts a session's state behind the
typed ``pytest.StashKey`` values of :mod:`foundry.execution.state`, and ``foundry.fixtures`` may
not import :mod:`foundry.execution` (see "Layering" below). The fixture therefore calls
:func:`foundry.config.merge.resolve` with the two inputs the plugin's own bootstrap passes: the
``--foundry-*`` overrides, read back through
:func:`foundry.config.cli_layer.collect_from_pytest`, and the invocation directory that the file
discovery of AAP 0.4.5 is anchored to. Resolution is the one place the four layers are applied, so
the fixture and the plugin cannot disagree about a value, and pytest's session-scoped caching means
the work is paid for once, well inside the 50 ms per-resolution budget of AAP 0.14.2. ``environ``
is deliberately not passed: the environment layer belongs to :mod:`foundry.config.env`, which
AAP 0.4.8 makes -- together with :mod:`foundry.http.auth` -- the only reader of the process
environment in the product.

**Overridable by name.** ``foundry_config`` is a plain ``@pytest.fixture(scope="session")``
function returning the plain resolved model -- no decorator wrapper, no subclass, no cached private
copy -- so a project replaces it with pytest's documented same-name override and shadows the value
through ``model_copy(update=...)`` on the instance it requests from the parent scope. That is how
``examples/checkout/conftest.py`` supplies an ephemeral ``http.base_url`` and points
``data.dir``/``cassette.dir`` at the example's own directories (AAP 0.8.1, assumption A9), and
``foundry_profile`` follows a shadowed value because it reads the configuration rather than a
separate session record.

**Import discipline** (implicit requirement I5, AAP 0.14.2). ``foundry.plugin`` imports this module
during ``pytest_configure``, at every pytest start, inside the 150 ms budget, while only a minority
of suites read the configuration fixture. Module scope therefore imports ``pytest``,
:mod:`pathlib` and :mod:`typing` and nothing else: :mod:`foundry.config` -- which pulls in
``pydantic`` and ``pydantic-settings`` -- is imported inside the fixture body that first needs it,
and :class:`FoundryConfig` is imported under ``TYPE_CHECKING`` for the annotations alone.

**Layering** (AAP 0.4.8). This module uses the single permitted upward edge of the capability rung:
``foundry.fixtures`` may import ``foundry.config``, and no other package on that rung may. It
imports no ``foundry.execution`` -- the typed stash keys stay in the plugin -- no
``foundry.reporting``, no ``foundry.cli`` and no ``foundry.plugin``, at module scope or inside a
function body, which is what ``tests/unit/test_layering.py`` parses every import for.

**No secret and no clock here** (AAP 0.14.3, implicit requirement I9). The fixture returns the
configuration as resolved and adds no sink: it prints nothing, logs nothing and stores nothing, and
a configuration holds the *names* of the environment variables that carry credentials, never their
values. Nothing in this module reads a clock, so it contributes nothing to a difference between two
otherwise identical runs.

Importing this module defines names only: two fixtures and one private helper. It reads no
environment variable, performs no I/O, parses no argument, configures no logger and holds no
module-level state (AAP 0.4.8).

Example:
    A test that reads one resolved value and the layer that supplied it::

        from foundry.config import FoundryConfig

        def test_the_timeout_came_from_the_environment(foundry_config: FoundryConfig) -> None:
            assert foundry_config.http.timeout == 3.5
            assert foundry_config.source_of("http.timeout").layer == "env"
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

    AAP 0.4.5 anchors two of its five discovery steps to the invocation directory -- the
    ``foundry.toml`` found there and the ``[tool.foundry]`` table of the ``pyproject.toml`` found
    there -- so the value handed to :func:`foundry.config.merge.resolve` as ``cwd`` has to be the
    directory the operator typed the command in, not the root directory pytest computed from the
    arguments after scanning for a configuration file, and not a test file's directory.

    The recorded value is read as text before it becomes a path because ``Path("")`` is
    ``Path(".")``: an empty record normalises itself into a perfectly usable "current directory"
    and hides the case this fallback exists for. A record that is empty or whitespace-only
    therefore falls back to ``rootpath``, which pytest always populates, so a resolution never runs
    against a directory nobody named. A record already normalised to ``.`` is taken at its word --
    it means the process working directory, which at startup is the invocation directory itself --
    because nothing distinguishes it from a run that genuinely started there.

    Args:
        config: The pytest configuration of the running session, whose ``invocation_params``
            carries the directory the run was started from.

    Returns:
        The invocation directory, or ``config.rootpath`` when the recorded directory is blank.
        A relative record is returned as recorded; the configuration package resolves it against
        the process working directory, which at startup is the invocation directory itself.
    """
    recorded = str(config.invocation_params.dir).strip()
    if not recorded:
        return Path(config.rootpath)
    return Path(recorded)


@pytest.fixture(scope="session")
def foundry_config(request: pytest.FixtureRequest) -> FoundryConfig:
    """Return the configuration this session resolved, with the source of every value.

    Session scoped (AAP 0.4.3): one resolution serves the whole run, and every test, every fixture
    that depends on it and the plugin's own report read the same immutable instance.

    The four layers of AAP 0.4.5 are applied in their fixed order -- the settings field defaults,
    then the configuration file with the selected ``[profiles.<name>]`` table over it, then the
    ``FOUNDRY_*`` environment, then the ``--foundry-*`` command line -- so a value stated in more
    than one place resolves to the highest layer that stated it, and a key no layer stated keeps
    its default. Per-key provenance is available on the result through
    :meth:`foundry.config.models.FoundryConfig.source_of`, which names the layer (``default``,
    ``file``, ``profile:<name>``, ``env`` or ``cli``) and what inside it supplied the value -- the
    file path, the environment variable's spelling or the option as it was typed. That is what
    makes the precedence inspectable from a test rather than merely asserted by the documentation.

    A configuration fault is not absorbed here. :class:`foundry.core.exceptions.ConfigError`
    propagates unchanged, so the test that requested the fixture reports why the run cannot be
    configured: the message names the key and its accepted range, or the unknown key with the
    closest valid spelling as the hint, and for an absent credential variable the variable's name
    and never its value. The plugin terminates a session it cannot configure before collection
    (assumption A4, exit code 2); this fixture raises at the point of use for the same inputs.

    The fixture is deliberately a plain session-scoped function returning the plain model, so a
    project overrides it by name -- pytest's documented same-name override, requesting the parent
    instance and returning ``model_copy(update=...)`` -- to inject project-specific values without
    editing a configuration file for the test run (AAP 0.8.1, assumption A9).

    Args:
        request: The fixture request, read for the running session's configuration: the
            ``--foundry-*`` options the command line supplied and the invocation directory that
            file discovery resolves against.

    Returns:
        The resolved, frozen :class:`foundry.config.models.FoundryConfig` holding all 28 published
        keys plus one :class:`foundry.config.sources.ConfigSource` per key.

    Raises:
        foundry.core.exceptions.ConfigError: For the first fault AAP 0.4.7 makes fatal, raised by
            :func:`foundry.config.merge.resolve` and propagated unchanged: an explicitly named
            configuration file that is absent (``config file not found: <path>``), an unknown key
            in any layer, an unparseable environment value, a value outside its accepted range, a
            profile outside the four names, or a configured auth variable the environment does not
            set.
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

    Session scoped (AAP 0.4.3), and always one of ``default``, ``local``, ``ci`` or ``staging``:
    the value is the ``profile`` key of the resolved configuration, which the file layer's
    ``[profiles.<name>]`` selection, a ``FOUNDRY_PROFILE`` variable or ``--foundry-profile`` may
    have set, and the environment and the command line outrank the file as they do for every other
    key (AAP 0.4.5).

    The value is a string rather than the :class:`foundry.config.enums.ProfileName` member because
    this is the fixture a suite asserts and interpolates with: it is the same name the terminal
    summary prints as ``foundry: profile=<name>`` (AAP 0.7.4), the name recorded in every result
    record and manifest, and the name a table-driven test compares against a fixture file. A
    ``StrEnum`` member formats as its value, so the conversion costs nothing and the type stays
    what a test would write in an assertion; it also keeps the fixture answering with a string
    after a project shadows ``foundry_config`` with a ``model_copy`` that installs a plain string,
    which ``model_copy`` does not revalidate.

    The profile is read from the configuration rather than from a session record of its own, so an
    overridden ``foundry_config`` is reflected here with no second place to keep in step.

    Args:
        foundry_config: The resolved configuration, read for its ``profile`` key alone.

    Returns:
        The active profile name, for example ``"ci"``.
    """
    return str(foundry_config.profile)
