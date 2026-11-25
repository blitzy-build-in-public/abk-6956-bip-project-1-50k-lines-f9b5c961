"""``foundry init``: scaffold a project that runs green with no network.

The command exits ``0`` when the scaffold is written and ``2`` when the target or a destination is
refused. The layout is a ``foundry.toml`` holding the settings a starting project needs as
commented defaults, a ``tests/test_smoke.py`` that requests ``foundry_config`` and passes,
``testdata/`` with one CSV and its row's JSON Schema, an ``artifacts/`` directory with its ignore
entry, and a ``README.md``; ``--template api`` adds ``tests/test_api.py`` and a ``cassettes/``
directory, with a ``foundry.toml`` selecting ``replay`` so the first HTTP test opens no socket.
Every refusal leaves the filesystem as it was: the target is checked, and every destination
resolved through :func:`foundry.core.paths.resolve_under`, before the first byte is written, so a
path that is not a directory, a directory that is not empty without ``--force``, or a destination
that would escape the target creates nothing. A fault inside the write loop is not a refusal: it
ends the command with the same exit code ``2`` and leaves earlier files in place
(:func:`_write_file`), and ``init`` resolves no project configuration, taking none of the
``--foundry-*`` options. Nothing here reads the clock, sleeps, opens a socket or reads the
environment, and the created paths print through :func:`foundry.cli.render.render_lines` in plan
order, so two runs write the same lines.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from types import MappingProxyType
from typing import Final, NoReturn

import click

from foundry.cli import exit_codes
from foundry.cli import render as cli_render
from foundry.core.exceptions import ArtifactError, ConfigError
from foundry.core.paths import resolve_under, sanitize_nodeid

__all__: Final[tuple[str, ...]] = (
    "SCAFFOLD_DIRS",
    "SCAFFOLD_FILES",
    "SCAFFOLD_TEMPLATES",
    "ScaffoldDir",
    "ScaffoldEntry",
    "init",
    "init_command",
    "scaffold_dirs",
    "scaffold_plan",
)

_PACKAGE: Final[str] = "foundry.cli"
"""The package the scaffold sources ship in, read through :mod:`importlib.resources` (AAP 0.4.1)."""

_TEMPLATE_DIR: Final[str] = "templates"
"""The package-data folder under :data:`_PACKAGE` holding one sub-folder per template."""

_MINIMAL: Final[str] = "minimal"
"""The default template's name: configuration, one smoke test, one data file and its schema."""

_API: Final[str] = "api"
"""The template that adds an HTTP test and the recording that test replays."""

_CASSETTE_DIR: Final[str] = "cassettes"
"""The cassette directory the scaffolded project reads, the ``cassette.dir`` default of AAP 0.4.5.

``foundry.cli`` may not import :mod:`foundry.cassette` (AAP 0.4.8), so the directory name, the
document extension and the destination below restate what that package owns; they are the values the
store itself uses, which is what makes the shipped recording the one the scaffolded test replays.
"""

_CASSETTE_SUFFIX: Final[str] = ".json"
"""The extension of a cassette document (AAP 0.4.6), appended here to the sanitised node id."""

_CASSETTE_NODEID: Final[str] = "tests/test_api.py::test_health"
"""The node id of the test the api template ships, from which its cassette file name is derived.

The name is computed through :func:`foundry.core.paths.sanitize_nodeid` rather than typed: a literal
percent-encoded component would be a second sanitisation rule, and the store derives the file it
looks for from the node id alone, so the two must not be able to drift apart.
"""

_HINT_INDENT: Final[str] = "  "
"""The two spaces an error's hint line is indented by, the shape a Foundry error message prints."""

_PERMISSION_HINT: Final[str] = "check that the target directory is writable and not read-only"
"""The one hint a filesystem fault carries, shared by the read, write and create refusals."""

_API_CASSETTE: Final[str] = f"{_CASSETTE_DIR}/{sanitize_nodeid(_CASSETTE_NODEID)}{_CASSETTE_SUFFIX}"
"""The cassette's destination in the scaffolded project, derived from :data:`_CASSETTE_NODEID`."""


@dataclass(frozen=True, slots=True)
class ScaffoldEntry:
    """One file a scaffold writes: the package-data source and the destination inside the project.

    Attributes:
        template: The template contributing the file, a key of :data:`SCAFFOLD_TEMPLATES`.
        source: The file's name inside that template's package-data folder. A source that becomes
            a Python module carries a ``.tmpl`` suffix, so package data is never collected or
            linted as source; the destination below is the module's real name.
        destination: The file's POSIX-relative path inside the scaffolded project.
    """

    template: str
    source: str
    destination: str


@dataclass(frozen=True, slots=True)
class ScaffoldDir:
    """One directory a scaffold creates even when it holds no file.

    Attributes:
        template: The template the directory belongs to, a key of :data:`SCAFFOLD_TEMPLATES`.
        directory: The directory's POSIX-relative path inside the scaffolded project.
    """

    template: str
    directory: str


SCAFFOLD_TEMPLATES: Final[Mapping[str, str]] = MappingProxyType({_MINIMAL: _MINIMAL, _API: _API})
"""The templates ``--template`` accepts, mapped to their package-data sub-folder (AAP 0.4.4).

Read-only, so the set ``--help`` prints cannot be edited at run time, and stated as a mapping rather
than a tuple because the two names are independent facts: the word an operator types and the folder
the sources live in could differ without touching any caller.
"""

SCAFFOLD_FILES: Final[tuple[ScaffoldEntry, ...]] = (
    ScaffoldEntry(template=_MINIMAL, source="foundry.toml", destination="foundry.toml"),
    ScaffoldEntry(template=_MINIMAL, source="gitignore", destination=".gitignore"),
    ScaffoldEntry(template=_MINIMAL, source="README.md", destination="README.md"),
    ScaffoldEntry(
        template=_MINIMAL, source="test_smoke.py.tmpl", destination="tests/test_smoke.py"
    ),
    ScaffoldEntry(template=_MINIMAL, source="orders.csv", destination="testdata/orders.csv"),
    ScaffoldEntry(
        template=_MINIMAL, source="orders.schema.json", destination="testdata/orders.schema.json"
    ),
    ScaffoldEntry(template=_API, source="foundry.toml", destination="foundry.toml"),
    ScaffoldEntry(template=_API, source="test_api.py.tmpl", destination="tests/test_api.py"),
    ScaffoldEntry(template=_API, source="cassette_health.json", destination=_API_CASSETTE),
)
"""Every file both templates contribute, in the order the minimal template writes them.

The layout is AAP 0.7.4's: the commented configuration, the smoke test, the data file with its
schema, the ignore entry and the README. The api rows come last and are the only ones that are not
part of the minimal template; the ``foundry.toml`` row replaces the minimal one at its own position,
and the cassette row's destination is the node-id-derived path rather than a literal.
"""

SCAFFOLD_DIRS: Final[tuple[ScaffoldDir, ...]] = (
    ScaffoldDir(template=_MINIMAL, directory="tests"),
    ScaffoldDir(template=_MINIMAL, directory="testdata"),
    ScaffoldDir(template=_MINIMAL, directory="artifacts"),
    ScaffoldDir(template=_API, directory=_CASSETTE_DIR),
)
"""The directories a scaffold creates, the api template inheriting the minimal ones.

``tests/`` and ``testdata/`` hold files, and ``cassettes/`` does for the api template, but
``artifacts/`` is empty until the first run: it is created here so a freshly scaffolded project has
the directory its ignore entry names, and so ``foundry run`` needs no special case.
"""


def scaffold_plan(template: str) -> tuple[ScaffoldEntry, ...]:
    """Return the files ``template`` writes, in the order they are written.

    The plan is the minimal template's rows with the chosen template's rows layered over them: a
    destination the chosen template also names is taken from that template, in the position the
    minimal row held, and one only the chosen template names is appended in :data:`SCAFFOLD_FILES`
    order. The api template therefore takes ``foundry.toml`` from its own folder, keeps the smoke
    test and the data file, and gains the API test and its cassette. Nothing here reads the
    filesystem: the order comes from the constants above, so two runs print the same lines and two
    workers never disagree (AAP 0.13.1).

    Args:
        template: The template to plan, one of the keys of :data:`SCAFFOLD_TEMPLATES`.

    Returns:
        One entry per file to write, in write order; a name that is not a key of
        :data:`SCAFFOLD_TEMPLATES` raises ``foundry.core.exceptions.ConfigError``.
    """
    _require_template(template)
    overrides = {entry.destination: entry for entry in SCAFFOLD_FILES if entry.template == template}
    plan: list[ScaffoldEntry] = []
    for entry in SCAFFOLD_FILES:
        if entry.template != _MINIMAL:
            continue
        plan.append(overrides.pop(entry.destination, entry))
    plan.extend(overrides.values())
    return tuple(plan)


def scaffold_dirs(template: str) -> tuple[ScaffoldDir, ...]:
    """Return the directories ``template`` creates, in the order they are created.

    The chosen template inherits the minimal template's directories, so ``tests/``, ``testdata/``
    and ``artifacts/`` exist for both and the api template adds ``cassettes/``.

    Args:
        template: The template to list, one of the keys of :data:`SCAFFOLD_TEMPLATES`.

    Returns:
        One entry per directory to create, in creation order; a name that is not a key of
        :data:`SCAFFOLD_TEMPLATES` raises ``foundry.core.exceptions.ConfigError``.
    """
    _require_template(template)
    inherited = {_MINIMAL, template}
    return tuple(entry for entry in SCAFFOLD_DIRS if entry.template in inherited)


def _require_template(template: str) -> None:
    """Reject a template name ``--template`` could not have produced.

    ``click.Choice`` filters the option before this module's command runs, so the guard exists for a
    caller reaching :func:`scaffold_plan` or :func:`scaffold_dirs` directly, and names the accepted
    templates rather than only the rejected one.

    Args:
        template: The template name to check; a name that is not a key of
            :data:`SCAFFOLD_TEMPLATES` raises ``foundry.core.exceptions.ConfigError``.
    """
    if template in SCAFFOLD_TEMPLATES:
        return
    accepted = ", ".join(sorted(SCAFFOLD_TEMPLATES))
    raise ConfigError(
        f"unknown scaffold template: {template}", hint=f"accepted templates: {accepted}"
    )


def _refuse(message: str, hint: str) -> NoReturn:
    """Stop the command with a usage error: the message on stderr, the hint indented beneath it.

    ``click.UsageError`` is the route that publishes exit code ``2`` for the installed script and
    for the command line :mod:`foundry.cli.main` builds alike (AAP 0.4.4), and it writes the message
    to stderr itself, so the fault is reported once rather than again by the error mapping. The hint
    is folded in as an indented second line, the shape a Foundry error's own hint prints (AAP
    0.7.4), and neither line can carry a secret because nothing here holds one (I9).

    Args:
        message: Why the command refused, naming the path or destination at fault.
        hint: What the operator can change to make the command succeed.

    Raises:
        click.UsageError: Always, carrying both lines and click's exit code ``2``.
    """
    raise click.UsageError(f"{message}\n{_HINT_INDENT}{hint}")


def _check_target(path: Path, *, force: bool) -> None:
    """Refuse a target the scaffold cannot be written into, before anything is created.

    Two states are refused (AAP 0.4.4): a path that exists and is not a directory, and a directory
    that already holds something unless ``--force`` was given. Both are decided from the path alone,
    so nothing has been created by the time either refusal fires.

    Args:
        path: The target directory as the operator spelled it; it may not exist yet.
        force: Whether ``--force`` was given, which admits a non-empty directory.

    Raises:
        click.UsageError: The path is not a directory, unreadable, or non-empty without ``force``.
    """
    if path.exists() and not path.is_dir():
        _refuse(
            f"cannot scaffold into {path}: the path exists and is not a directory",
            "pass a directory path, or a path that does not exist yet",
        )
    if force or not path.is_dir():
        return
    try:
        occupied = any(path.iterdir())
    except OSError:
        _refuse(f"cannot read {path}", _PERMISSION_HINT)
    if occupied:
        _refuse(
            f"cannot scaffold into {path}: the directory is not empty",
            "pass --force to write the scaffold files into it anyway",
        )


def _contained(root: Path, relative: str) -> Path:
    """Return ``root / relative`` resolved, or refuse when it does not stay inside ``root``.

    The containment rule belongs to :func:`foundry.core.paths.resolve_under` -- one implementation,
    shared with the artifact and cassette writers -- so this function only turns that typed refusal
    into the usage error this command publishes.

    Args:
        root: The target directory as the operator spelled it.
        relative: A POSIX-relative path from :data:`SCAFFOLD_FILES` or :data:`SCAFFOLD_DIRS`.

    Returns:
        The resolved absolute path to write; a path outside ``root`` raises ``click.UsageError``.
    """
    try:
        return resolve_under(root, relative)
    except ArtifactError:
        _refuse(
            f"refusing to write {relative} outside {root}",
            "the scaffold writes only inside the directory named on the command line",
        )


def _plan_targets(
    root: Path, plan: Sequence[ScaffoldEntry]
) -> tuple[tuple[ScaffoldEntry, Path], ...]:
    """Resolve every planned file under ``root``, refusing the first destination that escapes it.

    The whole plan is checked before the command's first write, so a destination such as
    ``../escape`` leaves the target untouched instead of writing the destinations that came before
    it (AAP 0.14.3, "No traversal escape").

    Args:
        root: The target directory as the operator spelled it.
        plan: The files to write, in write order.

    Returns:
        Each entry with the resolved absolute path it is written to, in plan order.

    Raises:
        click.UsageError: A destination resolves outside ``root``.
    """
    return tuple((entry, _contained(root, entry.destination)) for entry in plan)


def _directory_targets(root: Path, directories: Sequence[ScaffoldDir]) -> tuple[Path, ...]:
    """Resolve every planned directory under ``root``, with the same containment check as the files.

    Args:
        root: The target directory as the operator spelled it.
        directories: The directories to create, in creation order.

    Returns:
        The resolved absolute paths of the directories, in creation order.

    Raises:
        click.UsageError: A directory resolves outside ``root``.
    """
    return tuple(_contained(root, directory.directory) for directory in directories)


def _template_bytes(folder: str, source: str) -> bytes:
    """Return the bytes of one package-data scaffold source.

    The source is read through the :class:`importlib.resources.abc.Traversable` API rather than from
    a path relative to this module, so an installed wheel shipping the templates as package data
    serves them exactly as a source checkout does (AAP 0.4.1).

    Args:
        folder: The template's package-data sub-folder, a value of :data:`SCAFFOLD_TEMPLATES`.
        source: The file's name inside that folder.

    Returns:
        The file's bytes, copied to its destination unchanged; a missing or unreadable resource
        means an incomplete distribution and raises ``foundry.core.exceptions.ConfigError``
        naming the template folder and the file, and no path outside the package.
    """
    resource = resources.files(_PACKAGE).joinpath(_TEMPLATE_DIR, folder, source)
    try:
        return resource.read_bytes()
    except OSError as error:
        raise ConfigError(
            f"cannot read the {folder} scaffold source {source}: {error.strerror or error}",
            hint="reinstall Foundry: the scaffold templates ship as package data",
        ) from error


def _write_file(destination: Path, payload: bytes) -> None:
    """Write ``payload`` to ``destination``, creating its parents, or refuse.

    A target that cannot be written is the operator's to fix, so it ends the command with the clean
    usage error every other refusal produces rather than a traceback. Files written before the fault
    stay written: removing what a refusal did not create is not this command's decision.

    Args:
        destination: The resolved absolute path to write.
        payload: The bytes of the package-data source the destination was planned from.

    Raises:
        click.UsageError: The parent directory could not be created, or the file not written.
    """
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
    except OSError:
        _refuse(f"cannot write {destination}", _PERMISSION_HINT)


def _create_directory(directory: Path) -> None:
    """Create ``directory`` and its parents, or refuse.

    Args:
        directory: The resolved absolute path of the directory to create.

    Raises:
        click.UsageError: The directory could not be created.
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        _refuse(f"cannot create {directory}", _PERMISSION_HINT)


@click.command(name="init", help="scaffold a Foundry project")
@click.argument("path", type=click.Path(file_okay=False, dir_okay=True, path_type=Path))
@click.option(
    "--template",
    type=click.Choice(sorted(SCAFFOLD_TEMPLATES)),
    default=_MINIMAL,
    show_default=True,
    help="the scaffold to write",
)
@click.option("--force", is_flag=True, default=False, help="write into a non-empty directory")
def init(path: Path, *, template: str = _MINIMAL, force: bool = False) -> int:
    """Scaffold a Foundry project into ``path`` and publish the exit status of AAP 0.4.4.

    ``PATH`` is the directory to write into: created when it does not exist, and it must be empty
    unless ``--force`` says otherwise. Every destination is resolved under ``path`` before the
    first byte is written, so a pre-check refusal leaves the target exactly as it was; a fault
    while writing leaves earlier files in place, as :func:`_write_file` documents. The created
    files print one per line, in plan order and spelled with the directory the operator named (AAP
    0.7.4). Configuration is not resolved and no ``--foundry-*`` option is declared, because
    scaffolding reads no project setting (AAP 0.4.4, AAP 0.4.8).

    Args:
        path: The directory to scaffold, as the operator spelled it.
        template: The template to write, one of the keys of :data:`SCAFFOLD_TEMPLATES`.
        force: Whether to write into a directory that already holds something.

    Returns:
        :data:`foundry.cli.exit_codes.SUCCESS`, the status :func:`foundry.cli.main.main` publishes;
        a path that is not a directory, a directory not empty without ``force``, a destination
        escaping the target or an unwritable file raises ``click.UsageError``, whose exit code
        ``2`` is the only code other than success this command produces.
    """
    _check_target(path, force=force)
    plan = scaffold_plan(template)
    directories = scaffold_dirs(template)
    targets = _plan_targets(path, plan)
    directory_targets = _directory_targets(path, directories)

    for entry, destination in targets:
        _write_file(destination, _template_bytes(SCAFFOLD_TEMPLATES[entry.template], entry.source))
    for directory in directory_targets:
        _create_directory(directory)

    cli_render.render_lines(tuple((path / entry.destination).as_posix() for entry in plan))
    return exit_codes.SUCCESS


init_command: Final[click.Command] = init
"""The same command under the ``<verb>_command`` name its sibling modules publish.

:mod:`foundry.cli.main` may import either spelling; both name one :class:`click.Command`, so the
options and the callback exist once.
"""
