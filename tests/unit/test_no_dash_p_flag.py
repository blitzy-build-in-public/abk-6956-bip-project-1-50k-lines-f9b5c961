"""The tree-wide proof that nothing anywhere disables Foundry's own pytest plugin.

AAP 0.14.1 criterion 2 requires that no invocation anywhere passes pytest the option that selects
Foundry's plugin, because activation is the ``pytest11`` entry point alone (AAP 0.4.2): an
invocation naming the plugin is either redundant or -- in its negated form -- a suite that proves
nothing about the plugin it claims to exercise. AAP 0.14.3 makes that a mechanism. The one
sanctioned occurrence is the subtracted arm of the startup measurement in
``tests/integration/test_startup_cost.py``, which AAP 0.14.2's 150 ms budget requires.

Detection is textual, which is the right shape here: every route the option takes is a literal in a
first-party file -- an ``addopts`` string in a TOML or INI file, an argument list in a
``subprocess`` call, a shell script's command line, a documented command in Markdown. ``src/``,
``tests/``, ``examples/`` and ``scripts/`` are walked for the option's three spellings: the name in
its enable form, the negation joined to the name, and the negation as the option's own argument.
Every occurrence is reported with its path, line and spelling in one message, so a single run names
every offender rather than the first.

**Why this module spells no complete needle.** An allowlist holding one path cannot hold the scanner
too, so the scanner would otherwise report itself. Each spelling is assembled at import from the
fragments in :data:`_FLAG`, :data:`_NO` and :data:`_NAME`, and the first test asserts that this
file's own text holds none of them: the technique has to stay honest, because an edit that wrote a
whole spelling back would be an offender the allowlist cannot cover.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest

# --- The needles, assembled so that this file holds none of them -------------

_FLAG: Final[str] = "-p"
"""The pytest option that names a plugin on the command line, without its argument."""

_SPACE: Final[str] = " "
"""The separator pytest also accepts inside a single argv element: the third spelling."""

_NO: Final[str] = "no:"
"""The prefix that negates a plugin name given to :data:`_FLAG`."""

_NAME: Final[str] = "foundry"
"""The plugin name the ``pytest11`` entry point declares (AAP 0.4.2)."""

NEEDLES: Final[tuple[str, ...]] = (
    f"{_FLAG}{_SPACE}{_NAME}",
    f"{_FLAG}{_NO}{_NAME}",
    f"{_FLAG}{_SPACE}{_NO}{_NAME}",
)
"""The three spellings the scan refuses: enable form, joined negation, spaced negation.

Assembled from the fragments above rather than written out, so this module's own text holds no
complete occurrence of any of them and the tree-wide scan does not have to exempt its author.
"""

ALLOWLIST: Final[frozenset[str]] = frozenset({"tests/integration/test_startup_cost.py"})
"""The one location allowed to name the plugin-disable spelling, compared by exact path.

AAP 0.14.2 budgets plugin startup as the difference between two arms of one command, so that module
must run the plugin-disabled arm it subtracts; the folder brief makes it the single allowlisted
path. The entry is one, pinned by a test below, so widening the exemption is a visible diff in this
constant rather than a quiet inline exception -- and a second genuine need is a finding for the
folder owner, never an addition here.
"""

# --- What the walk reads ----------------------------------------------------

_SCAN_ROOTS: Final[tuple[str, ...]] = ("src", "tests", "examples", "scripts")
"""The four trees a first-party invocation can be written in: the package, the self-test suite, the
example project and the verification scripts."""

_SCANNED_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".py", ".sh", ".toml", ".cfg", ".ini", ".md", ".txt", ".yaml", ".yml", ".json"}
)
"""Every file type a command line is written down in: Python, shell, and the configuration and
document formats this repository keeps beside them."""

_SKIPPED_DIRECTORIES: Final[frozenset[str]] = frozenset(
    {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules", ".venv"}
)
"""Directories holding no first-party text, so the walk does not descend into them."""

_MINIMUM_INVENTORY: Final[int] = 100
"""A floor on the scanned file count, so an empty or broken walk cannot pass the suite.

The tree held 183 matching files when this scan was written; the floor sits well below that, so
ordinary growth of the suite needs no edit here while a walk that stops finding files fails.
"""

_ANCHOR_FILES: Final[tuple[str, ...]] = (
    "src/foundry/plugin.py",
    "tests/conftest.py",
    "tests/integration/test_startup_cost.py",
    "examples/checkout/conftest.py",
    "scripts/verify.sh",
)
"""One file per root the walk must reach: the plugin module, the suite's conftest, the allowlisted
consumer, the example project's conftest and a verification script.

A count alone does not catch a walk that misses a whole root, which is why the anchors are named.
"""


@dataclass(frozen=True, order=True)
class Hit:
    """One occurrence of a needle: the file (relative, POSIX), its 1-based line and the spelling.

    Ordered by ``path``, then ``line``, then ``needle``, so two runs of the scan produce the same
    report and the sorted set of hits compares exactly in the control tests.
    """

    path: str
    line: int
    needle: str


# --- The scanner ------------------------------------------------------------


def _display(path: Path, root: Path) -> str:
    """Return ``path`` relative to ``root`` as a POSIX string, the form every hit is reported in."""
    return path.relative_to(root).as_posix()


def _is_skipped(path: Path, root: Path) -> bool:
    """Return True when a directory between ``root`` and ``path`` is one the walk refuses."""
    return bool(_SKIPPED_DIRECTORIES.intersection(path.relative_to(root).parts[:-1]))


def scanned_files(root: Path) -> tuple[Path, ...]:
    """Return every file under the four roots that the scan reads, sorted.

    A root that is missing is skipped rather than fatal, so the function is usable on a synthetic
    tree in ``tmp_path`` as well as on this repository.
    """
    files: list[Path] = []
    for relative in _SCAN_ROOTS:
        base = root / relative
        if not base.is_dir():
            continue
        files.extend(
            path
            for path in base.rglob("*")
            if path.is_file() and path.suffix in _SCANNED_SUFFIXES and not _is_skipped(path, root)
        )
    return tuple(sorted(files))


def scan_file(path: Path, display: str) -> tuple[Hit, ...]:
    """Return every occurrence of a needle in ``path``, reported under ``display``, sorted.

    Read as UTF-8 with replacement, so a file holding undecodable bytes is scanned rather than
    crashing the walk. One hit is recorded per line and spelling, so a line naming the option twice
    is one finding -- the report counts offending lines, not characters.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    return tuple(
        sorted(
            {
                Hit(display, number, needle)
                for number, line in enumerate(text.splitlines(), start=1)
                for needle in NEEDLES
                if needle in line
            }
        )
    )


def scan_tree(root: Path) -> tuple[Hit, ...]:
    """Return every occurrence under ``root``, sorted and de-duplicated, allowlist not applied.

    The allowlist is deliberately absent here: exemption is a property of the repository scan, and
    keeping it out of the walk is what lets the control tests assert on a synthetic tree exactly.
    """
    hits: set[Hit] = set()
    for path in scanned_files(root):
        hits.update(scan_file(path, _display(path, root)))
    return tuple(sorted(hits))


def offending(root: Path) -> tuple[Hit, ...]:
    """Return the hits no allowlisted path accounts for, sorted."""
    return tuple(hit for hit in scan_tree(root) if hit.path not in ALLOWLIST)


def _format_hits(hits: tuple[Hit, ...]) -> str:
    """Render ``hits`` as the failure message: a count, then one ``path:line: spelling`` a line."""
    lines = [f"  {hit.path}:{hit.line}: {hit.needle}" for hit in hits]
    return "\n".join([f"{len(hits)} disallowed plugin-option occurrence(s):", *lines])


def _write(root: Path, relative: str, *lines: str) -> Path:
    """Write ``lines`` to ``root/relative``, creating parent directories, and return the path."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    return path


# --- The module does not exempt itself --------------------------------------


def test_this_module_spells_no_complete_needle(repo_root: Path) -> None:
    """The scanner's own source holds none of the three spellings, and the scan does read it.

    First, because it is what licenses the fragment technique: an edit that wrote a whole spelling
    back into this file would make the module an offender of itself, and the allowlist below cannot
    cover it. The second assertion keeps the check from being vacuous -- the file has to be inside
    the scanned inventory for a textual self-check to mean anything.
    """
    own_text = Path(__file__).read_text(encoding="utf-8")
    found = [needle for needle in NEEDLES if needle in own_text]
    assert found == [], f"{__file__} spells {found} literally: build it from the fragments instead"

    relative = Path(__file__).resolve().relative_to(repo_root).as_posix()
    assert relative in {_display(path, repo_root) for path in scanned_files(repo_root)}, (
        f"{relative} is not scanned, so the tree-wide scan could pass without reading it"
    )


def test_the_allowlist_is_the_single_documented_location(repo_root: Path) -> None:
    """The constant holds exactly the startup-cost module, so a second entry cannot slip in.

    The scanner is not exempt from its own rule: neither this module nor any other unit module may
    appear, because an allowlist that grows to cover the tests it polices polices nothing.
    """
    assert sorted(ALLOWLIST) == ["tests/integration/test_startup_cost.py"], sorted(ALLOWLIST)
    relative = Path(__file__).resolve().relative_to(repo_root).as_posix()
    assert relative not in ALLOWLIST, f"{relative} exempts itself"
    unit_modules = sorted(name for name in ALLOWLIST if name.startswith("tests/unit/"))
    assert unit_modules == [], f"the allowlist exempts unit modules: {unit_modules}"


def test_the_allowlist_entry_is_not_stale(repo_root: Path) -> None:
    """The allowlisted module exists, is scanned, and really names the option: it masks a hit.

    Specifically the spaced-negation spelling, which is the form that module's docstring documents;
    an exemption nobody uses is a licence granted for nothing, so a stale entry fails loudly here
    instead of quietly widening the allowlist's reach for a future edit.
    """
    (relative,) = tuple(ALLOWLIST)
    assert (repo_root / relative).is_file(), f"{relative} is allowlisted but absent: stale entry"
    assert (repo_root / relative) in set(scanned_files(repo_root)), f"{relative} is not scanned"
    needles = {hit.needle for hit in scan_tree(repo_root) if hit.path == relative}
    assert NEEDLES[2] in needles, f"{relative} names no {NEEDLES[2]!r}: the exemption masks nothing"


# --- The tree --------------------------------------------------------------


def test_no_invocation_disables_the_plugin(repo_root: Path) -> None:
    """A hit outside the allowlist is a finding (AAP 0.14.1 criterion 2, AAP 0.14.3)."""
    hits = offending(repo_root)
    assert hits == (), _format_hits(hits)


def test_the_scan_reaches_all_four_roots(repo_root: Path) -> None:
    """The walk reaches every root with a real inventory, and stops at the four directories."""
    scanned = {_display(path, repo_root) for path in scanned_files(repo_root)}
    assert len(scanned) > _MINIMUM_INVENTORY, f"the walk found only {len(scanned)} files"
    missing = [anchor for anchor in _ANCHOR_FILES if anchor not in scanned]
    assert missing == [], f"the walk misses {missing}"
    assert "pyproject.toml" not in scanned, "the roots are the four directories, not the root"


# --- Positive controls: the detector detects -------------------------------


@pytest.mark.parametrize("needle", NEEDLES)
def test_every_needle_is_detected(tmp_path: Path, needle: str) -> None:
    """Each of the three spellings is found on its own, with its path and line."""
    _write(tmp_path, "scripts/offending.sh", "set -eu", f"pytest {needle} tests")
    hits = scan_tree(tmp_path)
    assert hits == (Hit("scripts/offending.sh", 2, needle),), _format_hits(hits)


def test_a_clean_tree_reports_nothing(tmp_path: Path) -> None:
    """A tree that never names the option passes, so the scan is not a permanent failure.

    The fragments this module is built from are ordinary text and are not findings: the scan looks
    for the assembled spelling, not for the pieces it is made of.
    """
    _write(tmp_path, "scripts/clean.sh", "set -eu", "pytest -q tests")
    _write(tmp_path, "src/foundry/clean.py", '_FLAG = "-p"', '_NAME = "foundry"')
    hits = scan_tree(tmp_path)
    assert hits == (), _format_hits(hits)


def test_a_repeated_needle_is_reported_once_per_line(tmp_path: Path) -> None:
    """Two occurrences on one line are one hit: the report counts offending lines."""
    _write(tmp_path, "tests/twice.py", f'ARGS = ("{NEEDLES[0]}", "{NEEDLES[0]}")')
    hits = scan_tree(tmp_path)
    assert hits == (Hit("tests/twice.py", 1, NEEDLES[0]),), _format_hits(hits)


def test_every_offender_is_named_in_one_message(tmp_path: Path) -> None:
    """Three offenders yield one message naming all three, never only the first."""
    _write(tmp_path, "scripts/one.sh", f"pytest {NEEDLES[1]}")
    _write(tmp_path, "examples/two.py", f'ADDOPTS = "{NEEDLES[2]}"')
    _write(tmp_path, "tests/unit/three.toml", f'addopts = "{NEEDLES[2]}"')

    message = _format_hits(scan_tree(tmp_path))
    for path in ("scripts/one.sh", "examples/two.py", "tests/unit/three.toml"):
        assert path in message, message
    assert message.startswith("3 disallowed plugin-option occurrence(s)"), message


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        ("src/foundry/carrier.py", True),
        ("tests/carrier.sh", True),
        ("examples/carrier.toml", True),
        ("scripts/carrier.yaml", True),
        ("scripts/carrier.yml", True),
        ("scripts/carrier.json", True),
        ("scripts/carrier.ini", True),
        ("scripts/carrier.cfg", True),
        ("scripts/carrier.md", True),
        ("scripts/carrier.txt", True),
        ("scripts/carrier.png", False),
        ("scripts/carrier.pyc", False),
        ("scripts/carrier.log", False),
        ("scripts/__pycache__/carrier.py", False),
        ("src/foundry/.venv/carrier.py", False),
        ("tests/.pytest_cache/carrier.py", False),
        ("tests/.mypy_cache/carrier.py", False),
        ("docs/carrier.py", False),
        ("carrier.py", False),
    ],
)
def test_the_walk_reads_first_party_text_only(
    tmp_path: Path, relative: str, expected: bool
) -> None:
    """Every suffix in the set is read; a binary, a cache directory or a foreign root is not.

    That is what bounds the walk's claim to the four trees the scan names.
    """
    _write(tmp_path, relative, f"pytest {NEEDLES[2]}")
    assert (scan_tree(tmp_path) != ()) is expected


def test_undecodable_bytes_do_not_stop_the_scan(tmp_path: Path) -> None:
    """A file that is not valid UTF-8 is read with replacement, and its spelling is still found."""
    path = tmp_path / "scripts" / "binary_adjacent.txt"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe" + f"pytest {NEEDLES[2]}".encode())

    hits = scan_tree(tmp_path)
    assert hits == (Hit("scripts/binary_adjacent.txt", 1, NEEDLES[2]),), _format_hits(hits)
