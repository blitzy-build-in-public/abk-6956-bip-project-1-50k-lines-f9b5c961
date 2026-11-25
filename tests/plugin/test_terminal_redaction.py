"""The terminal as a redaction sink, driven through ``pytester``: a printed secret and a logged one.
AAP 0.14.3 makes the terminal a sink beside the files and the reports, and the framework reaches it
through the one ``TerminalWriter`` pytest prints every failure, capture section and live log record
with, so only a real run shows that the value never arrives."""

import json
from typing import Any, Final

import pytest

REACHES: tuple[str, ...] = ()

# A value no provider's credential pattern recognises, so only a name-keyed pass can mask it, and
# distinctive enough that any occurrence left in the terminal is a real leak.
_SENTINEL: Final[str] = "qa-terminal-9f3ab1c7d2e4"

_PRINTED_LEAK: Final[str] = (
    "def test_prints_a_secret() -> None:\n"
    f'    print("Authorization: Bearer {_SENTINEL}")\n'
    f'    assert False, "token={_SENTINEL} password={_SENTINEL}"\n'
)

_LOGGED_LEAK: Final[str] = (
    "import logging\n\n\n"
    "def test_logs_a_token() -> None:\n"
    f'    logging.getLogger("foundry").debug("about to call with token {_SENTINEL}")\n'
    "    assert 1 == 2\n"
)

_DEBUG_TOML: Final[str] = '[logging]\nlevel = "DEBUG"\n'


def _terminal(result: pytest.RunResult) -> str:
    """Return everything the run wrote to the terminal, both streams included."""
    return result.stdout.str() + result.stderr.str()


def _only_record(pytester: pytest.Pytester) -> dict[str, Any]:
    """Return the run's single result record, asserting that ``results.json`` carries one."""
    path = pytester.path / "results.json"
    assert path.is_file(), f"results.json was not written to {pytester.path}"
    document = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(document, list) and len(document) == 1, document
    record: dict[str, Any] = document[0]
    assert record["record_type"] == "result" and record["outcome"] == "failed", record
    return record


def test_a_printed_secret_reaches_neither_the_terminal_nor_the_report(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    pytester.makepyfile(test_leak=_PRINTED_LEAK)
    result = pytester.runpytest()
    assert result.ret == 1
    result.assert_outcomes(failed=1)
    terminal = _terminal(result)
    assert _SENTINEL not in terminal, terminal
    # The captured stdout line and the assertion message each lose their value, name kept.
    assert "Authorization: ***" in terminal and "token=***" in terminal
    [failure] = _only_record(pytester)["failures"]
    assert _SENTINEL not in failure["message"] and "token=***" in failure["message"]


def test_a_logged_secret_never_reaches_its_live_log_line(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    pytester.makefile(".toml", foundry=_DEBUG_TOML)
    pytester.makepyfile(test_logs=_LOGGED_LEAK)
    result = pytester.runpytest("--log-cli-level=DEBUG")
    assert result.ret == 1
    result.assert_outcomes(failed=1)
    terminal = _terminal(result)
    assert _SENTINEL not in terminal, terminal
    # A space separates the name from its token, so only the spaced pass can mask this one.
    assert "about to call with token ***" in terminal
