"""The tree-wide proof that nothing anywhere disables Foundry's own pytest plugin.

AAP 0.14.1 criterion 2 requires that no invocation anywhere passes pytest the option that selects
Foundry's plugin, because activation is the ``pytest11`` entry point alone (AAP 0.4.2): an
invocation naming the plugin is either redundant or -- in its negated form -- a suite that proves
nothing about the plugin it claims to exercise. AAP 0.14.3 makes that a mechanism. The one
sanctioned occurrence is the subtracted arm of the startup measurement in
``tests/integration/test_startup_cost.py``, which AAP 0.14.2's 150 ms budget requires.

Detection is textual, which is the right shape here: every route the option takes is a literal in a
first-party file -- an ``addopts`` string in a TOML or INI file, an argument list in a
``subprocess`` call, a shell script's command line, a documented command in Markdown. Two passes
cover those routes. The first walks ``src/``, ``tests/``, ``examples/`` and ``scripts/`` for the
option's three spellings -- the name in its enable form, the negation joined to the name, and the
negation as the option's own argument -- and reads every file as plain text. The second reads
Markdown alone: the ``*.md`` files at the repository root and the tree under ``docs/``, the two
places a maintainer copies a command from. There a spelling counts only when a fenced code block
encloses its line, because a documented *command* is an invocation whereas the sentence that names
the spelling in order to state the rule -- which ``README.md`` carries, and which AAP 0.4.2 makes
worth documenting because the negation is the supported way to turn the plugin off -- is
documentation and not an invocation. Every occurrence is reported with its path, line and spelling
in one message, so a single run names every offender rather than the first.

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

_MARKDOWN_TREES: Final[tuple[str, ...]] = ("docs",)
"""The documentation tree the second pass walks, beside the root-level files it reads directly.

Listed apart from :data:`_SCAN_ROOTS` because it is read under a different rule -- only a spelling
inside a fenced code block counts there -- and because the documentation a maintainer copies a
command from is not a fifth source tree.
"""

_MARKDOWN_GLOB: Final[str] = "*.md"
"""The one file type the second pass reads: a documented command lives in a Markdown document.

Markdown alone, rather than every file at the root, because the pass counts a spelling only inside a
fenced code block -- the rule that lets a document name the spelling in prose -- and a root
configuration file offers no fence. ``pyproject.toml`` and the other root files therefore stay out
of this pass, and the four source trees keep their plain-text rule unchanged.
"""

_FENCE_CHARACTERS: Final[tuple[str, ...]] = ("`", "~")
"""The two characters Markdown opens and closes a code fence with."""

_FENCE_MINIMUM_LENGTH: Final[int] = 3
"""The shortest fence delimiter Markdown recognises, so a stray character is not an opening."""

_MINIMUM_INVENTORY: Final[int] = 100
"""A floor on the scanned file count, so an empty or broken walk cannot pass the suite.

The tree holds well over two hundred files -- more than two hundred under the four source trees
once this scan was written, plus the three documents the Markdown pass adds -- so the floor sits far
below a real inventory and ordinary growth of the suite needs no edit here, while a walk that stops
finding files fails.
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

_DOCUMENT_ANCHOR_FILES: Final[tuple[str, ...]] = ("README.md", "docs/verification.md")
"""One document per location the Markdown pass must reach: the root and the ``docs`` tree.

Named for the same reason :data:`_ANCHOR_FILES` is: a spelling written into the README or the
verification notes is the one a maintainer is most likely to copy, so the pass that reads them has
to be proven to reach both rather than trusted to.
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


def documentation_files(root: Path) -> tuple[Path, ...]:
    """Return every Markdown document the second pass reads, sorted.

    The root's own ``*.md`` files are taken without descending into it, and each tree in
    :data:`_MARKDOWN_TREES` is walked with the usual skip list. A directory that is missing is
    skipped rather than fatal, so the function is usable on a synthetic tree in ``tmp_path`` as well
    as on this repository.
    """
    files: list[Path] = [path for path in root.glob(_MARKDOWN_GLOB) if path.is_file()]
    for relative in _MARKDOWN_TREES:
        base = root / relative
        if not base.is_dir():
            continue
        files.extend(
            path
            for path in base.rglob(_MARKDOWN_GLOB)
            if path.is_file() and not _is_skipped(path, root)
        )
    return tuple(sorted(files))


def scanned_inventory(root: Path) -> tuple[Path, ...]:
    """Return every file the scan reads under ``root``: the four trees plus the documents."""
    return tuple(sorted({*scanned_files(root), *documentation_files(root)}))


def _hits_in_text(text: str, display: str, lines: frozenset[int] | None = None) -> tuple[Hit, ...]:
    """Return every needle in ``text`` on the 1-based line numbers ``lines`` names, sorted.

    One hit is recorded per line and spelling, so a line naming the option twice is one finding --
    the report counts offending lines, not characters. ``lines`` is ``None`` when the whole text
    counts, which is how the plain-text pass reads a file and how the Markdown pass narrows itself
    to fenced content.
    """
    return tuple(
        sorted(
            {
                Hit(display, number, needle)
                for number, line in enumerate(text.splitlines(), start=1)
                for needle in NEEDLES
                if needle in line and (lines is None or number in lines)
            }
        )
    )


def scan_file(path: Path, display: str) -> tuple[Hit, ...]:
    """Return every occurrence of a needle in ``path``, reported under ``display``, sorted.

    Read as UTF-8 with replacement, so a file holding undecodable bytes is scanned rather than
    crashing the walk.
    """
    return _hits_in_text(path.read_text(encoding="utf-8", errors="replace"), display)


def _fence_delimiter(line: str) -> tuple[str, int, str] | None:
    """Return the fence a line opens or closes with: its character, run length and what follows.

    Leading indentation is tolerated, so a fence nested in a list or inside a block quote still
    counts. ``None`` means the line is not a fence delimiter at all.
    """
    stripped = line.strip()
    if not stripped or stripped[0] not in _FENCE_CHARACTERS:
        return None
    character = stripped[0]
    length = len(stripped) - len(stripped.lstrip(character))
    if length < _FENCE_MINIMUM_LENGTH:
        return None
    return character, length, stripped[length:]


def _fenced_line_numbers(text: str) -> frozenset[int]:
    """Return the 1-based line numbers of ``text`` that a fenced code block encloses.

    A fence opens on a line carrying three or more backticks or tildes -- with or without an info
    string after them -- and closes on the next line that carries the same character at least as
    many times and holds nothing after it, which is Markdown's own rule. The delimiter lines count
    with the content between them, and a fence left open runs to the end of the document, so no line
    a reader would see rendered as code escapes the scan.
    """
    fenced: set[int] = set()
    opened: tuple[str, int] | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        delimiter = _fence_delimiter(line)
        if opened is None:
            if delimiter is not None:
                opened = (delimiter[0], delimiter[1])
                fenced.add(number)
            continue
        fenced.add(number)
        if (
            delimiter is not None
            and delimiter[0] == opened[0]
            and delimiter[1] >= opened[1]
            and delimiter[2] == ""
        ):
            opened = None
    return frozenset(fenced)


def scan_markdown_file(path: Path, display: str) -> tuple[Hit, ...]:
    """Return every occurrence of a needle inside a fenced code block in ``path``, sorted.

    A documented command is an invocation; a sentence that names the spelling in order to say which
    form is sanctioned is documentation of the rule, and ``README.md`` carries exactly that
    sentence. So a line counts only when a fence encloses it, and the rest of the document is read
    as prose. Read as UTF-8 with replacement, like :func:`scan_file`.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    return _hits_in_text(text, display, _fenced_line_numbers(text))


def scan_tree(root: Path) -> tuple[Hit, ...]:
    """Return every occurrence under ``root``, sorted and de-duplicated, allowlist not applied.

    Both passes run: the plain-text walk over the four source trees, then the Markdown walk over the
    document locations, which reports only fenced content.

    The allowlist is deliberately absent here: exemption is a property of the repository scan, and
    keeping it out of the walk is what lets the control tests assert on a synthetic tree exactly.
    """
    hits: set[Hit] = set()
    for path in scanned_files(root):
        hits.update(scan_file(path, _display(path, root)))
    for path in documentation_files(root):
        hits.update(scan_markdown_file(path, _display(path, root)))
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


def test_the_scan_reaches_every_root_and_every_document(repo_root: Path) -> None:
    """Both passes reach a real inventory: the four trees, then the root and ``docs`` documents.

    ``_ANCHOR_FILES`` proves the plain-text walk still reaches one file per source tree, and
    ``_DOCUMENT_ANCHOR_FILES`` does the same for the Markdown pass at the root and under ``docs`` --
    the two places a spelling could otherwise sit unseen in a documented command. ``pyproject.toml``
    stays out, because the root pass reads ``*.md`` and nothing else there.
    """
    scanned = {_display(path, repo_root) for path in scanned_inventory(repo_root)}
    assert len(scanned) > _MINIMUM_INVENTORY, f"the walk found only {len(scanned)} files"
    anchors = (*_ANCHOR_FILES, *_DOCUMENT_ANCHOR_FILES)
    missing = [anchor for anchor in anchors if anchor not in scanned]
    assert missing == [], f"the walk misses {missing}"
    assert "pyproject.toml" not in scanned, "the root pass reads Markdown, so no root config file"


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


# --- The documentation pass: a fenced command counts, prose does not --------


def test_a_fenced_command_in_the_root_readme_is_reported(tmp_path: Path) -> None:
    """A spelling inside a fenced command block at the root is a hit, with its path and line."""
    _write(
        tmp_path,
        "README.md",
        "# Foundry",
        "",
        "Install it, then run the suite:",
        "",
        "```console",
        f"$ pytest {NEEDLES[2]} -q tests",
        "```",
    )
    hits = scan_tree(tmp_path)
    assert hits == (Hit("README.md", 6, NEEDLES[2]),), _format_hits(hits)


def test_a_prose_mention_in_the_root_readme_is_not_a_command(tmp_path: Path) -> None:
    """Prose naming the spelling to state the rule is documentation, not an invocation, so no hit.

    ``README.md`` carries exactly such a sentence, which is why the pass has to tell the two apart.
    """
    _write(
        tmp_path,
        "README.md",
        "# Foundry",
        "",
        f"No invocation passes `{NEEDLES[0]}`; `{NEEDLES[1]}` turns the plugin off.",
    )
    hits = scan_tree(tmp_path)
    assert hits == (), _format_hits(hits)


def test_a_fenced_command_under_docs_is_reported(tmp_path: Path) -> None:
    """The ``docs`` tree is the second document location, and an indented fence still counts."""
    _write(
        tmp_path,
        "docs/verification.md",
        "# Verification",
        "",
        "1. Run the suite:",
        "",
        "   ~~~bash",
        f"   pytest {NEEDLES[1]} -q",
        "   ~~~",
    )
    hits = scan_tree(tmp_path)
    assert hits == (Hit("docs/verification.md", 6, NEEDLES[1]),), _format_hits(hits)


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
