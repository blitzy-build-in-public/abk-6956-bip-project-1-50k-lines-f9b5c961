"""The evidence scan behind the reachability ledger, held to both directions of its rule.

``tests/plugin/_reachability_evidence.py`` exists because a ``REACHES`` tuple is a claim: the
ledger in ``tests/plugin/test_reachability.py`` can prove the tuples cover the published surface
and that every name in them exists in Foundry (AAP 0.14.1 criterion 13, AAP 0.14.3), but neither
check can tell a module that drives a name from one that merely lists it. The scan reads the
declaring module's own code and answers whether it traces what it claims, so the case that consumes
it fails on a false declaration instead of passing it silently.

**Both directions, in synthetic text.** Every sample below is a string, never a file in this tree,
so a rule can be exercised on code built to violate it: :data:`_BACKED_SAMPLE` traces all four
kinds and is backed for each, while :data:`_MENTION_ONLY_SAMPLE` names the same surface only in
prose, in a constant table and behind a foreign suffix and is backed for none of it. That second
sample is the gap the ledger had -- a name written next to a test that never uses it -- and the
cases here pin that the scan closes it rather than trusting the tuple.

**The child-source cases are the pytester shape.** Most of ``tests/plugin/`` drives pytest by
writing a project, so a fixture request often lives in a string literal handed to ``makepyfile``
rather than in the module's own signature. :data:`_CHILD_SOURCE_SAMPLE` and
:data:`_FSTRING_CHILD_SAMPLE` are both shaped that way -- their own test functions take
``pytester`` alone -- so the fixture they claim is backed only by the child source, and the second
one only once its f-string has been rebuilt.

Nothing here writes a file, imports a scanned module or reads a clock (AAP 0.14.3).
"""

from __future__ import annotations

import ast
from typing import Final

from _reachability_evidence import Evidence, parsed_sources

_ENTRIES: Final[tuple[tuple[str, str], ...]] = (
    ("command", "config"),
    ("fixture", "foundry_config"),
    ("format", "results.json"),
    ("loader", "csv"),
)
"""One entry of each kind, in a fixed order, so every case below asks the same four questions."""

_BACKED_SAMPLE: Final[str] = '''"""A sample whose own code traces every kind of evidence."""

import pathlib


def test_the_session_is_configured(foundry_config) -> None:
    """Load a data file, run a subcommand and name the report the run writes."""
    data_file = pathlib.Path("cases.csv")
    document = pathlib.Path("results.json")
    assert data_file.name == "cases.csv"
    assert document.name == "results.json"
    assert run_foundry("config", "show").returncode == 0
'''

_MENTION_ONLY_SAMPLE: Final[str] = '''"""A sample that only talks about the surface.

foundry_config is named here, and so are config and cases.csv: all three are prose.
"""

_FIXTURES: Final[tuple[str, ...]] = ("foundry_config",)
"""A table of fixture names: a fixture is requested by a parameter, not by naming it."""

_COMMANDS: Final[tuple[str, ...]] = ("run", "init", "config", "report", "doctor")
"""The five subcommands of AAP 0.4.4 in a table: naming them invokes none of them."""

_REPORT: Final[str] = "results.txt"
"""A document with the report's stem and another format's suffix, which backs no format."""


def test_nothing_here_reaches_the_surface() -> None:
    """Touch none of it: no parameter, no call, no data file, no report document."""
    assert _FIXTURES == ("foundry_config",)
'''

_USEFIXTURES_SAMPLE: Final[str] = '''"""A sample that asks for its fixture through a marker."""

import pytest


@pytest.mark.usefixtures("foundry_config")
def test_the_configuration_is_available() -> None:
    """Ask for the fixture through the marker, which is a request with no parameter."""
    raise AssertionError
'''

_CHILD_SOURCE_SAMPLE: Final[str] = '''"""A sample that reaches a fixture from its project."""

_CHILD: Final[str] = """
def test_the_fixture_is_requested(foundry_data) -> None:
    assert foundry_data.cases("cases.csv")
"""


def test_the_written_project_requests_it(pytester) -> None:
    """Write the child module and run it, which is where the fixture is requested."""
    pytester.makepyfile(_CHILD)
    assert pytester.runpytest().ret == 0
'''

_FSTRING_CHILD_SAMPLE: Final[str] = '''"""A sample whose child source is an f-string."""

_RECORD: Final[str] = "cases.csv"

_CHILD: Final[str] = f"""
def test_the_fixture_reads_the_record(foundry_data) -> None:
    cases = foundry_data.cases("{_RECORD}")
    assert len(cases) == 12
"""


def test_the_written_project_requests_it(pytester) -> None:
    """Write the child module whose record name is interpolated, then run it."""
    pytester.makepyfile(_CHILD)
    assert pytester.runpytest().ret == 0
'''

_PROSE_SAMPLE: Final[str] = '''"""A sample whose docstring is a fragment naming a fixture.

def test_the_fixture_is_requested(foundry_data) -> None:
    assert foundry_data.cases("cases.csv")
"""


def test_the_docstring_is_not_a_request() -> None:
    """The fragment above parses as Python and still backs nothing."""
    raise AssertionError
'''

_PREFIXED_DOCUMENT_SAMPLE: Final[str] = '''"""A sample naming the documents it builds."""

_NEW: Final[str] = "results_new.json"
"""The stem of that document begins with the report's own: how a test names the second one."""
'''

_FRAGMENT_SAMPLE: Final[str] = '''"""A sample holding text no Python parser accepts."""

_BROKEN: Final[str] = "def ("

_NULL: Final[str] = "a\\x00b"
'''

_UNKNOWN_KINDS: Final[tuple[str, ...]] = ("", "Fixture", "fixtures", "measurement", "format ")
"""Kinds no declaration may use: answering one would back a claim nobody could read."""


def _backed(evidence: Evidence) -> list[str]:
    """Return the ``<kind>:<name>`` entries of :data:`_ENTRIES` that ``evidence`` backs, sorted."""
    return sorted(f"{kind}:{name}" for kind, name in _ENTRIES if evidence.backs(kind, name))


def test_every_kind_is_backed_by_the_code_that_names_it() -> None:
    """A fixture parameter, a subcommand handed to a call, a data file and a report all count."""
    assert _backed(Evidence.of(_BACKED_SAMPLE)) == sorted(
        f"{kind}:{name}" for kind, name in _ENTRIES
    ), "the sample traces all four kinds in its own code, so all four entries must be backed"


def test_prose_a_constant_table_and_a_foreign_suffix_back_nothing() -> None:
    """The exact gap the ledger had: names written down beside tests that never use them.

    The module docstring names the fixture, a table names the subcommand and the fixture, and the
    one document carries the report's stem behind another format's suffix. None of it is code that
    touches the surface, so nothing here may be backed.
    """
    assert _backed(Evidence.of(_MENTION_ONLY_SAMPLE)) == [], (
        "a docstring, a module-level table and a document in another format are not requests, "
        "invocations or report formats, and must back nothing"
    )


def test_a_fixture_named_by_the_usefixtures_marker_is_backed() -> None:
    """``usefixtures`` names a fixture with no parameter, and is a real request all the same."""
    assert Evidence.of(_USEFIXTURES_SAMPLE).backs("fixture", "foundry_config"), (
        "usefixtures is how a fixture reaches a test whose signature does not list it"
    )


def test_a_fixture_requested_only_from_an_embedded_child_source_is_backed() -> None:
    """A pytester module's child source is its own code, so the request inside it counts.

    The sample's own test function takes ``pytester`` alone, so nothing but the child literal can
    back this entry: scanning only the module around it would report the request missing.
    """
    assert Evidence.of(_CHILD_SOURCE_SAMPLE).backs("fixture", "foundry_data")


def test_a_fixture_requested_only_from_an_embedded_f_string_is_backed() -> None:
    """A child source held as an f-string is rebuilt, so the request inside it is still read.

    ``tests/plugin/test_data_collection_errors.py`` holds its fixture child this way, which is why
    the joined string is unparsed back to the expression it interpolates rather than skipped.
    """
    assert Evidence.of(_FSTRING_CHILD_SAMPLE).backs("fixture", "foundry_data")


def test_a_docstring_holding_parseable_python_stays_prose() -> None:
    """The docstring above would parse as a test requesting a fixture, and backs nothing.

    Skipping docstring constants is what makes this hold: a module that documents the surface has
    not exercised it, and several modules in this tree document it in exactly this shape.
    """
    assert _backed(Evidence.of(_PROSE_SAMPLE)) == [], (
        "a docstring is prose even when Python parses it, so re-parsing it would back a claim "
        "with documentation"
    )


def test_a_prefixed_document_backs_the_format_it_names() -> None:
    """``results_new.json`` backs ``format:results.json``; a document of another format backs none.

    The prefix is how a module names the second of the several documents a comparison builds, so
    the stem is compared against the format's own stem and that stem followed by an underscore.
    """
    evidence = Evidence.of(_PREFIXED_DOCUMENT_SAMPLE)
    assert evidence.backs("format", "results.json"), (
        "a document whose stem begins with the report's stem is a spelling of that report"
    )
    assert not evidence.backs("format", "junit.xml"), (
        "the suffix has to match, so a JSON document never backs the JUnit report"
    )


def test_a_literal_that_is_not_python_is_passed_over_without_a_word() -> None:
    """A child source the parser rejects is data, and the module's own tree is still returned.

    A fragment and a string holding a null byte raise ``SyntaxError`` and ``ValueError``
    respectively; both are skipped, because a scan that failed on the data its subject happens to
    hold would be unusable on a module that embeds a TOML document.
    """
    sources = parsed_sources(_FRAGMENT_SAMPLE)
    assert len(sources) == 1, (
        "neither literal above is Python, so the module's own tree is the only source here"
    )
    assert isinstance(sources[0], ast.Module)


def test_an_unknown_kind_is_never_backed() -> None:
    """A declaration kind the scan does not read is answered ``False``, never ``True``."""
    evidence = Evidence.of(_BACKED_SAMPLE)
    assert [kind for kind in _UNKNOWN_KINDS if evidence.backs(kind, "foundry_config")] == [], (
        "a kind this scan cannot read has no evidence rule, so it must fail closed rather than "
        "back a claim nobody checked"
    )
