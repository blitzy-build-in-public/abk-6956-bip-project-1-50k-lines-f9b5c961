"""The evidence scan behind the reachability ledger, held to both directions of its rule: every
sample below is synthetic text, and a child source is scanned like the module that writes it."""

from __future__ import annotations

import ast
from typing import Final

import pytest
from _reachability_evidence import Evidence, parsed_sources

_ENTRIES: Final[tuple[tuple[str, str], ...]] = (
    ("command", "config"),
    ("fixture", "foundry_config"),
    ("format", "results.json"),
    ("loader", "csv"),
)

_BACKED_SAMPLE: Final[str] = """import pathlib
def test_the_session_is_configured(foundry_config) -> None:
    pathlib.Path("cases.csv").read_text()
    pathlib.Path("results.json").write_text(run_foundry("config", "show").stdout)
"""

_MENTION_ONLY_SAMPLE: Final[str] = '''"""Only prose: foundry_config, config and cases.csv."""
_FIXTURES = ("foundry_config",)
_COMMANDS = ("run", "init", "config", "report", "doctor")
_REPORT = "results.txt"
def test_nothing_here_reaches_the_surface() -> None:
    assert _FIXTURES == ("foundry_config",)
'''

_USEFIXTURES_SAMPLE: Final[str] = """import pytest
@pytest.mark.usefixtures("foundry_config")
def test_the_configuration_is_available() -> None:
    raise AssertionError
"""

_CHILD_SOURCE_SAMPLE: Final[str] = '''_CHILD = """
def test_the_fixture_is_requested(foundry_data) -> None:
    assert foundry_data.cases("cases.csv")
"""


def test_the_written_project_requests_it(pytester) -> None:
    pytester.makepyfile(_CHILD)
'''

_FSTRING_CHILD_SAMPLE: Final[str] = '''_RECORD = "cases.csv"
_CHILD = f"""
def test_the_fixture_reads_the_record(foundry_data) -> None:
    cases = foundry_data.cases("{_RECORD}")
    assert len(cases) == 12
"""


def test_the_written_project_requests_it(pytester) -> None:
    pytester.makepyfile(_CHILD)
'''

_PROSE_SAMPLE: Final[str] = '''"""A docstring holding a fragment that names a fixture:
def test_the_fixture_is_requested(foundry_data) -> None:
    assert foundry_data.cases("cases.csv")
"""
def test_the_docstring_is_not_a_request() -> None:
    raise AssertionError
'''

_PREFIXED_DOCUMENT_SAMPLE: Final[str] = '''"""Naming the documents it builds."""
_NEW: Final[str] = "results_new.json"
'''

_FRAGMENT_SAMPLE: Final[str] = """_BROKEN: Final[str] = "def ("
_NULL: Final[str] = "a\\x00b"
"""

_UNKNOWN_KINDS: Final[tuple[str, ...]] = ("", "Fixture", "fixtures", "measurement", "format ")


def _backed(evidence: Evidence) -> list[str]:
    return sorted(f"{kind}:{name}" for kind, name in _ENTRIES if evidence.backs(kind, name))


def test_every_kind_is_backed_by_the_code_that_names_it() -> None:
    expected = sorted(f"{kind}:{name}" for kind, name in _ENTRIES)
    assert _backed(Evidence.of(_BACKED_SAMPLE)) == expected, "the sample traces all four kinds"


def test_prose_a_constant_table_and_a_foreign_suffix_back_nothing() -> None:
    assert _backed(Evidence.of(_MENTION_ONLY_SAMPLE)) == [], (
        "a docstring, a table and a document in another format back nothing"
    )


def test_a_fixture_named_by_the_usefixtures_marker_is_backed() -> None:
    assert Evidence.of(_USEFIXTURES_SAMPLE).backs("fixture", "foundry_config"), "usefixtures"


@pytest.mark.parametrize("sample", [_CHILD_SOURCE_SAMPLE, _FSTRING_CHILD_SAMPLE])
def test_a_fixture_reached_only_from_an_embedded_child_source_is_backed(sample: str) -> None:
    # Each sample's own test function takes pytester alone, so only the child literal backs this.
    assert Evidence.of(sample).backs("fixture", "foundry_data"), "a child source is its own code"


def test_a_docstring_holding_parseable_python_stays_prose() -> None:
    assert _backed(Evidence.of(_PROSE_SAMPLE)) == [], "a docstring is prose, parsed or not"


def test_a_prefixed_document_backs_the_format_it_names() -> None:
    evidence = Evidence.of(_PREFIXED_DOCUMENT_SAMPLE)
    assert evidence.backs("format", "results.json"), "results_new.json is that report"
    assert not evidence.backs("format", "junit.xml"), "the suffix has to match"


def test_a_literal_that_is_not_python_is_passed_over_without_a_word() -> None:
    sources = parsed_sources(_FRAGMENT_SAMPLE)
    assert len(sources) == 1, "neither literal is Python, so the module's own tree is alone"
    assert isinstance(sources[0], ast.Module)


def test_an_unknown_kind_is_never_backed() -> None:
    evidence = Evidence.of(_BACKED_SAMPLE)
    assert [kind for kind in _UNKNOWN_KINDS if evidence.backs(kind, "foundry_config")] == [], (
        "a kind this scan cannot read must fail closed rather than back a claim"
    )
