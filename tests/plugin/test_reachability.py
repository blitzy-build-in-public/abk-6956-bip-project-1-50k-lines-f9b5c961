"""AAP 0.14.1 criterion 13: no public surface entry rests on a unit test alone.
Each module's module-level ``REACHES`` tuple declares the public-surface entries its own tests drive
through a real entry point -- a pytester inner run, a CliRunner invocation or a subprocess -- never
a direct in-process import of the unit under test. The vocabulary is closed, an entry spelling
``<kind>:<name>`` over fixture, command, loader and format, and an entry is backed by its own
module's text: fixture:<name> by a parameter a test or fixture function declares, or by a
usefixtures, getfixturevalue or indirect argument; command:<name> by a call's string argument, a
CliRunner invocation or a subprocess argv; loader:<name> by the suffix of a data-file path the
module names, a ".csv" path backing loader:csv; format:<name> by a report document whose suffix
matches and whose stem is the format's own or begins with it plus an underscore, results_base.json
backing format:results.json. Prose, a comment and an ordinary string back no entry, and an entry
outside this vocabulary or one the scan cannot read backs nothing, so the check fails closed. This
module is left out of its own scan: its required-surface table names every entry, so scanning it
would pass trivially."""

from __future__ import annotations

import ast
import pathlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

import pytest
from _reachability_evidence import Evidence
from click.testing import CliRunner

from foundry.cli.main import build_group
from foundry.fixtures import plugin as fixtures_plugin

_FIXTURES: Final[str] = (
    "foundry_config foundry_profile foundry_run_id foundry_artifacts foundry_http foundry_api "
    "foundry_data foundry_db foundry_factory foundry_clock foundry_log_capture foundry_poll"
)
_SIBLINGS: Final[str] = (
    "test_cli_surface.py test_config_errors.py test_data_collection_errors.py test_isolation.py "
    "test_public_contract.py test_quarantine.py test_report_compare.py test_retries.py "
    "test_zero_config.py"
)
# Required below, then transcribed a second time from AAP 0.4.3-0.4.6: two transcriptions that must
# agree make an edit on either side a failure rather than a quiet redefinition of the surface.
_REQUIRED: Final[Mapping[str, str]] = {
    "fixture": _FIXTURES,
    "command": "run init config report doctor",
    "loader": "csv json yaml jsonl",
    "format": "results.json junit.xml report.html",
}
_DOCUMENTED_SURFACE: Final[Mapping[str, str]] = {
    "fixture": "foundry_config foundry_profile foundry_run_id foundry_artifacts foundry_http "
    "foundry_api foundry_data foundry_db foundry_factory foundry_clock foundry_log_capture "
    "foundry_poll",
    "command": "run init config report doctor",
    "loader": "csv json yaml jsonl",
    "format": "results.json junit.xml report.html",
}
_VOCABULARY: Final[tuple[str, ...]] = ("fixture", "command", "loader", "format")
_ENTRY_PATTERN: Final[re.Pattern[str]] = re.compile(rf"^(?:{'|'.join(_VOCABULARY)}):[a-z0-9_.]+$")
_DECLARATION_NAME: Final[str] = "REACHES"
SELF_MODULE: Final[str] = pathlib.Path(__file__).name
_PLUGIN_FOLDER: Final[pathlib.Path] = pathlib.Path("tests") / "plugin"
_E2E_FOLDER: Final[pathlib.Path] = pathlib.Path("tests") / "e2e"
_EXPECTED_SIBLINGS: Final[frozenset[str]] = frozenset(_SIBLINGS.split())
_HELP_COMMAND: Final[re.Pattern[str]] = re.compile(
    r"^ {2}(?P<name>[a-z][a-z0-9-]*) {2,}\S", re.MULTILINE
)
# A dynamic dispatch is invisible to an AST scan: the loop target feeding an f-string carries no
# literal name, so the swept-table idiom is expanded by synthesising a request line per swept
# name, not by reading one. tests/plugin/test_public_contract.py builds NAMES from a
# space-separated string and evaluates getfixturevalue(f"foundry_{name}") in a loop.
_SWEPT: Final[re.Pattern[str]] = re.compile(
    r'"([a-z_ ]+)"\.split\(\).*?getfixturevalue\(f"([a-z_]+)\{name\}"\)', re.DOTALL
)


@dataclass(frozen=True, order=True, slots=True)
class Declaration:
    module: str
    entry: str

    @property
    def kind(self) -> str:
        return self.entry.partition(":")[0]

    @property
    def name(self) -> str:
        return self.entry.partition(":")[2]


_Scan = Mapping[str, tuple[Declaration, ...]]
_Evidence = Mapping[str, tuple[Evidence, ...]]


def _declared_values(tree: ast.Module, *, module: str) -> list[ast.expr]:
    values: list[ast.expr] = []
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [getattr(node, "target", None)]
        if not any(isinstance(t, ast.Name) and t.id == _DECLARATION_NAME for t in targets):
            continue
        if node.value is None:
            raise ValueError(f"{module}: {_DECLARATION_NAME} is never assigned")
        values.append(node.value)
    return values


def parse_declarations(source: str, *, module: str) -> tuple[Declaration, ...]:
    values = _declared_values(ast.parse(source, filename=module), module=module)
    if not values:
        return ()
    if len(values) > 1:
        raise ValueError(f"{module}: {_DECLARATION_NAME} is assigned {len(values)} times")
    value = values[0]
    if not isinstance(value, ast.Tuple):
        raise ValueError(
            f"{module}: {_DECLARATION_NAME} must be a tuple, not {type(value).__name__}"
        )
    entries: list[str] = []
    for element in value.elts:
        if not (isinstance(element, ast.Constant) and isinstance(element.value, str)):
            detail = f"line {element.lineno}: non-literal {ast.unparse(element)!r}"
            raise ValueError(f"{module}: {_DECLARATION_NAME} holds {detail}")
        entries.append(element.value)
    return tuple(sorted({Declaration(module=module, entry=entry) for entry in entries}))


def scan_module(path: pathlib.Path) -> tuple[Declaration, ...]:
    return parse_declarations(path.read_text(encoding="utf-8"), module=path.name)


def scan_folder(folder: pathlib.Path, *, exclude: frozenset[str]) -> _Scan:
    if not folder.is_dir():
        raise FileNotFoundError(f"scan root does not exist: {folder}")
    paths = (path for path in sorted(folder.glob("test_*.py")) if path.name not in exclude)
    return {path.name: scan_module(path) for path in paths}


def _missing_report(missing: Sequence[str]) -> str:
    lines = [f"{len(missing)} public surface entries have no real-entry-point test behind them:"]
    for kind in _VOCABULARY:
        found = [entry for entry in missing if entry.startswith(f"{kind}:")]
        if found:
            lines.extend([f"  {kind}:", *found])
    lines.append(f"Declare each in its module's {_DECLARATION_NAME} tuple (AAP 0.14.1, 0.14.3).")
    return "\n".join(lines)


@pytest.fixture(scope="module")
def plugin_scan(repo_root: pathlib.Path) -> _Scan:
    return scan_folder(repo_root / _PLUGIN_FOLDER, exclude=frozenset({SELF_MODULE}))


@pytest.fixture(scope="module")
def e2e_scan(repo_root: pathlib.Path) -> _Scan:
    return scan_folder(repo_root / _E2E_FOLDER, exclude=frozenset())


@pytest.fixture(scope="module")
def declarations(plugin_scan: _Scan, e2e_scan: _Scan) -> tuple[Declaration, ...]:
    return tuple(sorted(d for m in (plugin_scan, e2e_scan) for s in m.values() for d in s))


@pytest.fixture(scope="module")
def evidence(repo_root: pathlib.Path) -> _Evidence:
    found: dict[str, list[Evidence]] = {}
    for folder in (_PLUGIN_FOLDER, _E2E_FOLDER):
        for path in sorted((repo_root / folder).glob("test_*.py")):
            text = path.read_text(encoding="utf-8")
            if hit := _SWEPT.search(text):
                # Synthesised because a dynamic dispatch leaves no literal name for a scan to read.
                text += "".join(f'\nrequest.getfixturevalue("{hit[2]}{s}")' for s in hit[1].split())
            if path.name != SELF_MODULE:
                found.setdefault(path.name, []).append(Evidence.of(text))
    return {name: tuple(items) for name, items in sorted(found.items())}


# The first sample is fooled three ways on purpose: its module docstring and its declaration's own
# docstring both name entries, and a helper holds a local tuple of them.
_DECLARING_SAMPLE: Final[str] = '''"""Prose naming fixture:foundry_config."""
REACHES: tuple[str, ...] = ("fixture:foundry_data", "format:junit.xml")
"""Prose naming loader:csv."""
_UNRELATED = ("fixture:foundry_http",)
def _helper(): return ("command:doctor",)
'''

_MENTION_ONLY_SAMPLE: Final[str] = '''"""Declares none of the surface.
fixture:foundry_api, command:report, loader:json and format:report.html are all prose here.
"""
_MENTION: Final[str] = "fixture:foundry_db"
'''

_NON_LITERAL_SAMPLE: Final[str] = '''"""Holds a name, not a literal."""
_EXTRA: Final[str] = "fixture:foundry_db"
REACHES: tuple[str, ...] = ("fixture:foundry_clock", _EXTRA)
'''


@pytest.mark.parametrize(
    ("kind", "count"), [("fixture", 12), ("command", 5), ("loader", 4), ("format", 3)]
)
def test_required_surface_counts(kind: str, count: int) -> None:
    declared, documented = _REQUIRED[kind].split(), _DOCUMENTED_SURFACE[kind].split()
    assert len(declared) == count, (kind, declared)
    assert sorted(declared) == sorted(documented), (kind, declared, documented)


def test_every_entry_has_a_real_entry_point_test(declarations: Sequence[Declaration]) -> None:
    required = frozenset(f"{k}:{n}" for k, names in _REQUIRED.items() for n in names.split())
    missing = sorted(required - {d.entry for d in declarations})
    assert not missing, _missing_report(missing)


def test_every_declaration_is_backed_by_its_own_module(
    declarations: Sequence[Declaration], evidence: _Evidence
) -> None:
    unbacked = [
        f"{d.module}: {d.entry}"
        for d in declarations
        if not any(e.backs(d.kind, d.name) for e in evidence.get(d.module, ()))
    ]
    assert not unbacked, f"{len(unbacked)} entries no own module backs: {unbacked}"


def test_scan_roots_exist_and_are_non_trivial(plugin_scan: _Scan, e2e_scan: _Scan) -> None:
    for folder, scan, minimum in ((_PLUGIN_FOLDER, plugin_scan, 9), (_E2E_FOLDER, e2e_scan, 7)):
        assert len(scan) >= minimum, (folder.as_posix(), sorted(scan), minimum)


def test_provided_entries_are_well_formed(declarations: Sequence[Declaration]) -> None:
    problems = [
        f"{d.module}: {d.entry!r} is not a documented surface entry"
        for d in declarations
        if not _ENTRY_PATTERN.fullmatch(d.entry) or d.name not in _REQUIRED.get(d.kind, "").split()
    ]
    assert not problems, problems


def test_scanner_self_test() -> None:
    declared = parse_declarations(_DECLARING_SAMPLE, module="declaring_sample.py")
    assert tuple(d.entry for d in declared) == ("fixture:foundry_data", "format:junit.xml")
    assert {d.module for d in declared} == {"declaring_sample.py"}
    assert parse_declarations(_MENTION_ONLY_SAMPLE, module="mention_only.py") == ()
    with pytest.raises(ValueError, match="non-literal"):
        parse_declarations(_NON_LITERAL_SAMPLE, module="non_literal_sample.py")


def test_declared_names_exist_in_reality(declarations: Sequence[Declaration]) -> None:
    fixtures = sorted({d.name for d in declarations if d.kind == "fixture"})
    commands = sorted({d.name for d in declarations if d.kind == "command"})
    absent = [name for name in fixtures if not hasattr(fixtures_plugin, name)]
    assert not absent, f"fixtures declared but not defined by foundry.fixtures.plugin: {absent}"
    registered = getattr(fixtures_plugin, "FIXTURE_NAMES", ())
    assert not [name for name in fixtures if name not in registered], fixtures
    result = CliRunner().invoke(build_group(), ["--help"])
    assert result.exit_code == 0, result.output
    listed = tuple(sorted({m["name"] for m in _HELP_COMMAND.finditer(result.output)}))
    assert not [name for name in commands if name not in listed], (commands, listed)


def test_scan_scope_is_the_named_siblings(repo_root: pathlib.Path, plugin_scan: _Scan) -> None:
    assert not sorted(_EXPECTED_SIBLINGS - frozenset(plugin_scan)), sorted(plugin_scan)
    assert SELF_MODULE not in plugin_scan, sorted(plugin_scan)
    assert scan_module(repo_root / _PLUGIN_FOLDER / SELF_MODULE) == ()
