"""AAP 0.14.1 criterion 13: no public surface entry rests on a unit test alone.

Reachability is the promise that every name AAP 0.4.3, 0.4.4, 0.7.1 and 0.4.6 publish is exercised
through the entry point a user actually runs -- an inner ``pytester`` session, a ``CliRunner``
invocation or a subprocess -- and never only by calling the implementation in process.
``scripts/verify.sh`` proves the weaker half of that, that no ``src/foundry`` module sits at 0 %
coverage; coverage cannot tell a fixture a real session requested from a function a test called by
hand, which is why this ledger exists beside it.

**The convention.** A module in ``tests/plugin/`` or ``tests/e2e/`` states what it reaches in one
module-level declaration::

    REACHES: tuple[str, ...] = ("fixture:foundry_config", "format:results.json")

The vocabulary is closed: ``fixture:<name>``, ``command:<name>``, ``loader:<name>`` and
``format:<name>``. An entry is a claim that a test *in that module* drives the named item through a
real entry point. The declaration is optional -- a module that reaches nothing declares an empty
tuple, or declares nothing at all -- but an entry outside a ``REACHES`` tuple never counts: prose, a
comment and an ordinary string are all invisible to this scan, which is the property
:func:`test_scanner_self_test` pins.

**What backs an entry.** An entry is a claim that a test in the declaring module drives the name
through a real entry point, so the claim is read against that module's own code rather than
accepted on its word: a fixture as the parameter a ``test_`` function or a fixture declares (or a
name handed to ``usefixtures``, ``getfixturevalue`` or ``indirect``), a subcommand as a string
handed to a call, a data loader as a data file the module names, and a report format as the
document the module names for it. ``tests/plugin/_reachability_evidence.py`` holds that scan and
:func:`test_every_declaration_is_backed_by_its_own_module` reports every entry it cannot trace, so
a name listed beside a test that never uses it fails here instead of passing the ledger silently
(AAP 0.14.1, 0.14.3). A module that drives pytest by writing a project backs its entries from the
child source it hands to ``makepyfile``, which is its code as much as the string around it; a
docstring is prose and backs nothing.

**Why the modules are parsed and never imported.** Every candidate module is read as text and
resolved through :mod:`ast`, so a scanned module is not executed inside this test and a sibling's
import cost, side effects or failures cannot change the verdict. Only the string literals of a
module-level assignment to ``REACHES`` are collected, and a declaration the scan cannot read as
literal text -- a bare name, a value computed at run time -- raises instead of being skipped,
because a declaration that silently contributes nothing shrinks the coverage claim without a word.

**Self-exclusion.** This file's own tables hold every required name, so scanning itself would
satisfy the check trivially: :data:`SELF_MODULE` is skipped by name and
:func:`test_this_module_is_excluded_from_its_own_scan` proves both that it is skipped and that it
declares nothing of its own. This module is the verifier, never a provider.
"""

from __future__ import annotations

import ast
import pathlib
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

import pytest
from _reachability_evidence import Evidence

# --- The declared surface (AAP 0.4.3, 0.4.4, 0.7.1, 0.4.6) -------------------

REQUIRED_FIXTURES: Final[tuple[str, ...]] = (
    "foundry_config",
    "foundry_profile",
    "foundry_run_id",
    "foundry_artifacts",
    "foundry_http",
    "foundry_api",
    "foundry_data",
    "foundry_db",
    "foundry_factory",
    "foundry_clock",
    "foundry_log_capture",
    "foundry_poll",
)
"""AAP 0.4.3's twelve fixtures, in the order its table publishes them."""

REQUIRED_COMMANDS: Final[tuple[str, ...]] = ("run", "init", "config", "report", "doctor")
"""AAP 0.4.4's five subcommands, in the order the group's help lists them."""

REQUIRED_LOADERS: Final[tuple[str, ...]] = ("csv", "json", "yaml", "jsonl")
"""The four data-file extensions AAP 0.7.1 registers a loader for."""

REQUIRED_FORMATS: Final[tuple[str, ...]] = ("results.json", "junit.xml", "report.html")
"""AAP 0.4.6's three report documents, under the fixed names it writes them as."""

_VOCABULARY: Final[tuple[str, ...]] = ("fixture", "command", "loader", "format")
"""The closed set of kinds an entry may name, in the order a failure report groups them."""

_REQUIRED_BY_KIND: Final[Mapping[str, tuple[str, ...]]] = {
    "fixture": REQUIRED_FIXTURES,
    "command": REQUIRED_COMMANDS,
    "loader": REQUIRED_LOADERS,
    "format": REQUIRED_FORMATS,
}
"""The four tables above, addressable by the word an entry is prefixed with."""

_KIND_ALTERNATION: Final[str] = "|".join(_VOCABULARY)
_ENTRY_PATTERN: Final[re.Pattern[str]] = re.compile(rf"^(?:{_KIND_ALTERNATION}):[a-z0-9_.]+$")
"""``<kind>:<name>``: a vocabulary word, a colon, then a name of lowercase letters, digits, ``_``,
``.`` -- the alphabet every fixture, subcommand, extension and report file name is spelled in."""

_PROVIDER_HINT: Final[Mapping[str, str]] = {
    "fixture": "tests/plugin/test_public_contract.py, or the module that requests it",
    "command": "tests/plugin/test_cli_surface.py",
    "loader": "tests/plugin/test_data_collection_errors.py",
    "format": "tests/plugin/test_zero_config.py",
}
"""Where a missing entry belongs, so a failure names a file instead of a rule."""

# --- The scan ---------------------------------------------------------------

_DECLARATION_NAME: Final[str] = "REACHES"
"""The one name whose module-level assignment this scan reads: the convention's fixed spelling."""

SELF_MODULE: Final[str] = pathlib.Path(__file__).name
"""This module's file name, which the plugin scan skips so it cannot satisfy itself."""

_PLUGIN_FOLDER: Final[pathlib.Path] = pathlib.Path("tests") / "plugin"
_E2E_FOLDER: Final[pathlib.Path] = pathlib.Path("tests") / "e2e"
"""The two folders whose modules are scanned, relative to the repository root."""

_MINIMUM_PLUGIN_MODULES: Final[int] = 9
_MINIMUM_E2E_MODULES: Final[int] = 7
"""Floors on what each folder must yield, so a scan that finds nothing cannot pass."""

_EXPECTED_SIBLINGS: Final[frozenset[str]] = frozenset(
    {
        "test_cli_surface.py",
        "test_config_errors.py",
        "test_data_collection_errors.py",
        "test_isolation.py",
        "test_public_contract.py",
        "test_quarantine.py",
        "test_report_compare.py",
        "test_retries.py",
        "test_zero_config.py",
    }
)
"""The plugin modules the plan names, each of which must still be scanned by name.

The folder also holds modules the plan did not name -- ``test_fixture_activation.py`` and
``test_option_selection.py`` -- so membership is checked as a required subset rather than as an
exact inventory: a module added later must not fail this test, while a declared sibling that is
deleted or renamed must.
"""

_HELP_COMMAND: Final[re.Pattern[str]] = re.compile(
    r"^ {2}(?P<name>[a-z][a-z0-9-]*) {2,}\S", re.MULTILINE
)
"""One ``name  description`` row of a click help page, which is indented exactly two spaces."""


@dataclass(frozen=True, order=True, slots=True)
class Declaration:
    """One scanned ``REACHES`` entry together with the module that declared it.

    Ordered by ``module`` and then ``entry``, so a sorted collection of declarations -- and every
    message built from one -- reads identically on two runs (AAP 0.14.3, determinism).
    """

    module: str
    entry: str

    @property
    def kind(self) -> str:
        """The vocabulary word the entry is prefixed with: the text before the colon."""
        return self.entry.partition(":")[0]

    @property
    def name(self) -> str:
        """The surface name the entry refers to: the text after the colon."""
        return self.entry.partition(":")[2]


def _declared_values(tree: ast.Module, *, module: str) -> list[ast.expr]:
    """Return the value expression of every module-level assignment binding ``REACHES``.

    ``ast.Module.body`` holds only module-level statements, so a function-local name that happens to
    be spelled ``REACHES`` is out of reach by construction: it lives in its own function's body.

    Raises:
        ValueError: the declaration is annotated but never assigned, which the scan cannot read.
    """
    values: list[ast.expr] = []
    for node in tree.body:
        if isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == _DECLARATION_NAME:
                if node.value is None:
                    raise ValueError(
                        f"{module}: {_DECLARATION_NAME} is annotated but never assigned, so the "
                        "scan cannot tell an empty declaration from an unfinished one"
                    )
                values.append(node.value)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == _DECLARATION_NAME
            for target in node.targets
        ):
            values.append(node.value)
    return values


def _literal_entries(value: ast.expr, *, module: str) -> tuple[str, ...]:
    """Return the entry strings ``value`` spells out, refusing anything that is not literal text.

    Raises:
        ValueError: the value is not a tuple, or an element of it is not a string constant. Both are
            reported with the module and, for an element, its line, because the alternative -- a
            declaration quietly contributing nothing -- is a coverage claim that is false.
    """
    if not isinstance(value, ast.Tuple):
        raise ValueError(
            f"{module}: {_DECLARATION_NAME} must be a tuple of string literals, "
            f"not {type(value).__name__}"
        )
    entries: list[str] = []
    for element in value.elts:
        if not (isinstance(element, ast.Constant) and isinstance(element.value, str)):
            raise ValueError(
                f"{module}: line {element.lineno}: {_DECLARATION_NAME} holds a non-literal element "
                f"{ast.unparse(element)!r}; every entry must be a string literal, because the scan "
                "reads declarations without executing the module that holds them"
            )
        entries.append(element.value)
    return tuple(entries)


def parse_declarations(source: str, *, module: str) -> tuple[Declaration, ...]:
    """Return the entries ``source`` declares in its single module-level ``REACHES`` assignment.

    Only that assignment counts. A docstring, a comment, a string bound to another name and a local
    variable are all prose as far as this function is concerned, which is what stops the surface
    check from being satisfiable by a module that merely talks about the surface.

    Args:
        source: the module's text, never its imported object.
        module: the file name to report the declaration under and to name in a failure.

    Returns:
        The declared entries, deduplicated and sorted by entry so two runs read identically. A
        module that declares nothing returns an empty tuple.

    Raises:
        ValueError: the assignment is missing a value, appears more than once, is not a tuple, or
            holds an element that is not a string literal.
    """
    tree = ast.parse(source, filename=module)
    values = _declared_values(tree, module=module)
    if not values:
        return ()
    if len(values) > 1:
        raise ValueError(
            f"{module}: {_DECLARATION_NAME} is assigned {len(values)} times at module level; one "
            "declaration per module keeps the ledger readable and the claim unambiguous"
        )
    entries = _literal_entries(values[0], module=module)
    return tuple(sorted({Declaration(module=module, entry=entry) for entry in entries}))


def scan_module(path: pathlib.Path) -> tuple[Declaration, ...]:
    """Return the declarations of one module, read from its text and never imported."""
    return parse_declarations(path.read_text(encoding="utf-8"), module=path.name)


def scan_folder(
    folder: pathlib.Path, *, exclude: frozenset[str]
) -> dict[str, tuple[Declaration, ...]]:
    """Return each ``test_*.py`` module of ``folder`` with the entries it declares, keyed by name.

    Modules are visited in sorted order, so the mapping -- and every message built from it -- reads
    the same on two runs.

    Args:
        folder: the directory to scan.
        exclude: file names to skip, which is how a scan keeps out of its own way.

    Returns:
        One entry per scanned module, in sorted file-name order.

    Raises:
        FileNotFoundError: ``folder`` is not a directory. A scan that walks nothing must not look
            like a scan that found nothing wrong.
    """
    if not folder.is_dir():
        raise FileNotFoundError(f"scan root does not exist: {folder}")
    return {
        path.name: scan_module(path)
        for path in sorted(folder.glob("test_*.py"))
        if path.name not in exclude
    }


# --- The folder's declarations, read once per session ------------------------


@pytest.fixture(scope="module")
def plugin_scan(repo_root: pathlib.Path) -> dict[str, tuple[Declaration, ...]]:
    """Module scope: every sibling of ``tests/plugin`` except this module, with its declarations."""
    return scan_folder(repo_root / _PLUGIN_FOLDER, exclude=frozenset({SELF_MODULE}))


@pytest.fixture(scope="module")
def e2e_scan(repo_root: pathlib.Path) -> dict[str, tuple[Declaration, ...]]:
    """Module scope: every module of ``tests/e2e``, with its declarations."""
    return scan_folder(repo_root / _E2E_FOLDER, exclude=frozenset())


@pytest.fixture(scope="module")
def declarations(
    plugin_scan: Mapping[str, tuple[Declaration, ...]],
    e2e_scan: Mapping[str, tuple[Declaration, ...]],
) -> tuple[Declaration, ...]:
    """Module scope: every declaration of both folders, sorted so two runs compare exactly."""
    return tuple(
        sorted(
            declaration
            for scan in (plugin_scan, e2e_scan)
            for suite in scan.values()
            for declaration in suite
        )
    )


@pytest.fixture(scope="module")
def evidence(repo_root: pathlib.Path) -> dict[str, tuple[Evidence, ...]]:
    """Module scope: what each scanned module's own code traces, keyed by file name.

    A tuple per name, because the same file name occurs in both folders and an entry has to be
    backed by the module of that name in either one. This file is left out for the same reason the
    declaration scan leaves it out: the verifier is not a provider.
    """
    sources: dict[str, list[Evidence]] = defaultdict(list)
    for folder in (_PLUGIN_FOLDER, _E2E_FOLDER):
        for path in sorted((repo_root / folder).glob("test_*.py")):
            if path.name == SELF_MODULE:
                continue
            sources[path.name].append(Evidence.of(path.read_text(encoding="utf-8")))
    return {name: tuple(items) for name, items in sorted(sources.items())}


# --- The documented surface, transcribed a second time -----------------------

_DOCUMENTED_SURFACE: Final[Mapping[str, tuple[str, ...]]] = {
    "fixture": (
        "foundry_config",
        "foundry_profile",
        "foundry_run_id",
        "foundry_artifacts",
        "foundry_http",
        "foundry_api",
        "foundry_data",
        "foundry_db",
        "foundry_factory",
        "foundry_clock",
        "foundry_log_capture",
        "foundry_poll",
    ),
    "command": ("run", "init", "config", "report", "doctor"),
    "loader": ("csv", "json", "yaml", "jsonl"),
    "format": ("results.json", "junit.xml", "report.html"),
}
"""The surface read off AAP 0.4.3, 0.4.4, 0.7.1 and 0.4.6 independently of the constants above.

The duplication is the mechanism: two transcriptions that have to agree make an edit on either side
a failure rather than a quiet redefinition of the public surface, so a drift in what Foundry
publishes fails here before any scan runs.
"""


def _required_entries() -> frozenset[str]:
    """Return the required surface as prefixed entries, which is the spelling a declaration uses."""
    return frozenset(
        f"{kind}:{name}" for kind, names in _REQUIRED_BY_KIND.items() for name in names
    )


def _missing_report(missing: Sequence[str]) -> str:
    """Return the message for ``missing``, grouped by kind and naming the provider module."""
    grouped: dict[str, list[str]] = defaultdict(list)
    for entry in missing:
        grouped[entry.partition(":")[0]].append(entry)
    lines = [f"{len(missing)} public surface entries have no real-entry-point test behind them:"]
    for kind in _VOCABULARY:
        if kind not in grouped:
            continue
        lines.append(f"  {kind} (declare in {_PROVIDER_HINT[kind]}):")
        lines.extend(f"    {entry}" for entry in grouped[kind])
    lines.append(
        "Drive each entry through an inner pytest session, a CliRunner invocation or a subprocess, "
        f"then list it in the declaring module's {_DECLARATION_NAME} tuple (AAP 0.14.1, 0.14.3)."
    )
    return "\n".join(lines)


def _unbacked_report(unbacked: Sequence[Declaration]) -> str:
    """Return the message for ``unbacked``, one ``<module>: <entry>`` a line."""
    lines = [f"{len(unbacked)} declarations no scanned module backs with its own code:"]
    lines.extend(f"  {declaration.module}: {declaration.entry}" for declaration in unbacked)
    lines.append(
        "Give each entry a test in its own module that requests the fixture as a parameter, hands "
        "the subcommand to the entry point, names the data file the loader reads or names the "
        "report document the run writes, then keep the entry (AAP 0.14.1, 0.14.3)."
    )
    return "\n".join(lines)


def _commands_in_help(text: str) -> tuple[str, ...]:
    """Return the command names listed in a click help page's ``Commands:`` block, sorted."""
    return tuple(
        sorted({match["name"] for match in _HELP_COMMAND.finditer(text.partition("Commands:")[2])})
    )


# --- Synthetic sources for the scanner self-test -----------------------------
# The first sample is deliberately fooled twice over: its module docstring and its declaration's
# docstring both name entries, and a helper holds a local tuple of them.

_DECLARING_SAMPLE: Final[str] = '''"""A sample whose prose names fixture:foundry_config."""

REACHES: tuple[str, ...] = (
    "fixture:foundry_data",
    "format:junit.xml",
)
"""The declaration's own docstring names loader:csv, which is prose and not an entry."""

_UNRELATED: tuple[str, ...] = ("fixture:foundry_http",)


def _helper() -> tuple[str, ...]:
    """Return a local tuple: a name inside a function is not a module-level declaration."""
    reaches = ("command:doctor",)
    return reaches
'''

_MENTION_ONLY_SAMPLE: Final[str] = '''"""A sample that declares none of the surface.

fixture:foundry_api, command:report, loader:json and format:report.html are all prose here.
"""

_MENTION: Final[str] = "fixture:foundry_db"
'''

_NON_LITERAL_SAMPLE: Final[str] = '''"""A sample holding a name, not a literal."""

_EXTRA: Final[str] = "fixture:foundry_db"

REACHES: tuple[str, ...] = (
    "fixture:foundry_clock",
    _EXTRA,
)
'''


# --- The surface this module enforces ---------------------------------------


@pytest.mark.parametrize(
    ("kind", "count"), [("fixture", 12), ("command", 5), ("loader", 4), ("format", 3)]
)
def test_required_surface_counts(kind: str, count: int) -> None:
    """Each required table holds the documented names and exactly the documented number of them.

    ``count`` is written out here rather than taken from the table, so a name added to or removed
    from the constraint fails on the count as well as on the contents.
    """
    declared = _REQUIRED_BY_KIND[kind]
    documented = _DOCUMENTED_SURFACE[kind]
    assert len(declared) == count, (
        f"this module enforces {len(declared)} {kind} entries ({list(declared)}) where AAP "
        f"documents {count}; a change to the public surface belongs in this table and in the scan "
        "that proves it reachable"
    )
    assert sorted(declared) == sorted(documented), (
        f"the enforced {kind} table and the documented one disagree: "
        f"{sorted(set(declared) - set(documented))} are enforced but undocumented, "
        f"{sorted(set(documented) - set(declared))} are documented but unenforced"
    )


def test_every_surface_entry_has_a_real_entry_point_test(
    declarations: Sequence[Declaration],
) -> None:
    """No public surface entry exists without a real entry point driving it somewhere in the suite.

    This is AAP 0.14.3's "no capability that exists only in unit tests" applied to the published
    surface: the ledger is the union of every scanned module's declaration, and the assertion is
    that the required set is a subset of it.
    """
    provided = {declaration.entry for declaration in declarations}
    missing = sorted(_required_entries() - provided)
    assert not missing, _missing_report(missing)


def test_scan_roots_exist_and_are_non_trivial(
    repo_root: pathlib.Path,
    plugin_scan: Mapping[str, tuple[Declaration, ...]],
    e2e_scan: Mapping[str, tuple[Declaration, ...]],
) -> None:
    """Both scan roots yield at least the modules the plan counts on.

    A root that is missing rather than empty needs no case of its own: :func:`scan_folder` names a
    directory that does not exist in a ``FileNotFoundError``, so its absence errors rather than
    passing. What this case rules out is the other silent failure, a root that exists but whose glob
    matches almost nothing.
    """
    cases = (
        (_PLUGIN_FOLDER, plugin_scan, _MINIMUM_PLUGIN_MODULES),
        (_E2E_FOLDER, e2e_scan, _MINIMUM_E2E_MODULES),
    )
    for folder, scan, minimum in cases:
        assert len(scan) >= minimum, (
            f"{folder.as_posix()} under {repo_root} yielded {len(scan)} modules ({sorted(scan)}) "
            f"where at least {minimum} are expected: a scan that walks nothing must not look like "
            "a clean one"
        )


def test_provided_entries_are_well_formed(declarations: Sequence[Declaration]) -> None:
    """Every declared entry is spelled ``<kind>:<name>`` and names something Foundry publishes.

    An entry outside the documented surface is a finding rather than a harmless extra: it would let
    a module claim reachability for a name no consumer can use, and it would hide the fact that the
    real entry has no test.
    """
    problems: list[str] = []
    for declaration in declarations:
        if not _ENTRY_PATTERN.fullmatch(declaration.entry):
            problems.append(
                f"{declaration.module}: {declaration.entry!r} is not '<kind>:<name>' with kind in "
                f"{list(_VOCABULARY)} and a name of lowercase letters, digits, '_' and '.'"
            )
        elif declaration.name not in _REQUIRED_BY_KIND.get(declaration.kind, ()):
            documented = sorted(_REQUIRED_BY_KIND.get(declaration.kind, ()))
            problems.append(
                f"{declaration.module}: {declaration.entry!r} names no documented "
                f"{declaration.kind} (documented: {documented})"
            )
    assert not problems, "malformed or undocumented reachability declarations:\n" + "\n".join(
        f"  {problem}" for problem in problems
    )


def test_scanner_self_test() -> None:
    """The scan reads literal declarations and nothing else, including a non-literal it must refuse.

    Four properties in one case, because together they are the difference between a scanner and a
    silent pass: the literal tuple is returned, a docstring naming an entry contributes nothing, a
    name bound elsewhere or locally contributes nothing, and an element the scan cannot read as text
    raises instead of being dropped.
    """
    declared = parse_declarations(_DECLARING_SAMPLE, module="declaring_sample.py")
    assert tuple(declaration.entry for declaration in declared) == (
        "fixture:foundry_data",
        "format:junit.xml",
    ), "only the REACHES literals may be returned: prose and similarly named locals are not entries"
    assert {declaration.module for declaration in declared} == {"declaring_sample.py"}, (
        "every declaration must carry the module it came from, so a failure can name that module"
    )
    assert parse_declarations(_MENTION_ONLY_SAMPLE, module="mention_only.py") == (), (
        "a module that only talks about the surface declares nothing, so it must contribute nothing"
    )
    with pytest.raises(ValueError, match="non-literal"):
        parse_declarations(_NON_LITERAL_SAMPLE, module="non_literal_sample.py")


def test_declared_fixture_and_command_names_exist_in_reality(
    declarations: Sequence[Declaration],
) -> None:
    """Every declared fixture is a real attribute, every declared command a real subcommand.

    The ledger is worth only as much as the names in it, so this closes the loop from the
    declaration to the implementation: a stale entry, or a surface renamed without the ledger
    following, fails here. Both checks are subset tests rather than equalities, because adding a
    fixture or a subcommand is a permitted additive change (AAP 0.4.9).
    """
    fixture_names = sorted({item.name for item in declarations if item.kind == "fixture"})
    command_names = sorted({item.name for item in declarations if item.kind == "command"})

    from foundry.fixtures import plugin as fixtures_plugin

    absent = [name for name in fixture_names if not hasattr(fixtures_plugin, name)]
    assert not absent, (
        f"fixtures declared but not defined by foundry.fixtures.plugin: {absent}; the ledger "
        "names a surface no test can request"
    )
    unregistered = [
        name for name in fixture_names if name not in getattr(fixtures_plugin, "FIXTURE_NAMES", ())
    ]
    assert not unregistered, (
        f"declared fixtures missing from foundry.fixtures.plugin.FIXTURE_NAMES: {unregistered}; a "
        "definition the plugin never registers cannot be requested by a real session"
    )

    from click.testing import CliRunner

    from foundry.cli.main import build_group

    result = CliRunner().invoke(build_group(), ["--help"])
    assert result.exit_code == 0, (
        f"foundry --help exited {result.exit_code}, so the subcommand names cannot be compared: "
        f"{result.output}"
    )
    listed = _commands_in_help(result.output)
    missing = [name for name in command_names if name not in listed]
    assert not missing, (
        f"declared subcommands foundry --help does not list: {missing}; listed: {list(listed)}"
    )


def test_every_declaration_is_backed_by_its_own_module(
    declarations: Sequence[Declaration],
    evidence: Mapping[str, tuple[Evidence, ...]],
) -> None:
    """Every declared entry is traced by the declaring module's own code, not only spelled in it.

    The union check proves the surface is covered somewhere and the reality check proves a name
    exists; neither proves that the module claiming an entry has a test touching it. Here the claim
    is held against that module's own text -- and, for a module that drives pytest by writing a
    project, against the child source it embeds -- so a name listed beside a test that never uses it
    fails instead of satisfying AAP 0.14.1 criterion 13 in silence.
    """
    unbacked = [
        declaration
        for declaration in declarations
        if not any(
            item.backs(declaration.kind, declaration.name)
            for item in evidence.get(declaration.module, ())
        )
    ]
    assert not unbacked, _unbacked_report(unbacked)


def test_the_ten_modules_of_this_folder_are_scanned(
    plugin_scan: Mapping[str, tuple[Declaration, ...]],
) -> None:
    """Every sibling the plan names is still scanned by name, so a rename cannot shrink the ledger.

    The folder held ten modules -- these nine and this file -- when the plan was written; it has
    since grown, so membership is asserted as a required subset and an added module is not a
    failure. A deleted or renamed one is: it would drop its declarations out of the union without
    any other case noticing.
    """
    found = frozenset(plugin_scan)
    missing = sorted(_EXPECTED_SIBLINGS - found)
    assert not missing, (
        f"{list(missing)} are missing from the scan of tests/plugin, which found {sorted(found)}; "
        "renaming or deleting a sibling must be a visible edit to this module, not a quiet loss of "
        "the entries it declares"
    )


def test_this_module_is_excluded_from_its_own_scan(
    repo_root: pathlib.Path, plugin_scan: Mapping[str, tuple[Declaration, ...]]
) -> None:
    """The scan skips this file, and this file declares nothing a scan could have counted anyway.

    Self-exclusion is the line that keeps the check honest: the tables above hold every required
    name, so a scan that read its own module would satisfy the surface from the verifier instead of
    from a provider. The second assertion states the other half -- this module is not a provider, so
    excluding it costs the ledger nothing.
    """
    assert SELF_MODULE not in plugin_scan, (
        f"{SELF_MODULE} scanned itself, so the required surface could be satisfied by the tables "
        "that define it rather than by a module that exercises it"
    )
    own = scan_module(repo_root / _PLUGIN_FOLDER / SELF_MODULE)
    assert own == (), f"this module must declare no {_DECLARATION_NAME} of its own, found {own}"
