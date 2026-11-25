"""The 28 configuration keys of AAP 0.4.5 as an executable contract, compared through the CLI.
AAP 0.4.5 publishes every key with its type, default and accepted range, and AAP 0.4.9 fixes them --
addable, never renamed and never removed. AAP 0.13.2 requires each to be visible through ``foundry
config show``, which is the command this comparison runs. Both directions fail: a key that stops
resolving, and a key the command resolves that no AAP table declares.
The rest of the fixed surface -- fixtures, subcommands, record fields, outcomes, exceptions and the
client types -- is the sibling module ``tests.plugin.test_public_contract``. This group has its own
module because AAP 0.14.2 caps every counted file at 700 lines.
Nothing here sleeps, reads a clock or opens a socket (AAP 0.14.3): the command writes its document
to a buffer and the directory it runs in is ``tmp_path``."""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import pytest
from click.testing import CliRunner

import foundry.cli.main

CONFIG_KEYS: Final[tuple[str, ...]] = (
    "profile",
    "http.base_url",
    "http.timeout",
    "http.headers",
    "http.retries.max_attempts",
    "http.retries.backoff_base",
    "http.retries.backoff_max",
    "http.retries.retry_on",
    "http.auth.strategy",
    "http.auth.token_env",
    "http.auth.user_env",
    "http.auth.password_env",
    "http.auth.header_name",
    "http.auth.header_value_env",
    "artifacts.dir",
    "artifacts.keep_passed",
    "reporting.html",
    "reporting.json",
    "data.dir",
    "data.schema_strict",
    "cassette.dir",
    "cassette.mode",
    "execution.retries",
    "execution.timeout",
    "execution.quarantine_mode",
    "secrets.redact_keys",
    "logging.level",
    "logging.capture_lines",
)
"""The 28 dotted configuration keys of AAP 0.4.5, row for row and in that table's order."""


def _flatten(document: Mapping[str, Any], *, prefix: str = "") -> dict[str, Any]:
    """Return ``document`` as dotted leaves, treating a documented key's own mapping as one leaf.
    The key set says where a document stops nesting: the empty ``http.headers`` table is one leaf,
    and a group no key declares keeps nesting, so the comparison bites in both directions."""
    leaves: dict[str, Any] = {}
    for name, value in document.items():
        dotted = f"{prefix}{name}"
        if isinstance(value, Mapping) and dotted not in CONFIG_KEYS:
            leaves.update(_flatten(value, prefix=f"{dotted}."))
        else:
            leaves[dotted] = value
    return leaves


def test_configuration_key_set_matches_the_documented_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_foundry_env: None
) -> None:
    """AAP 0.4.5, AAP 0.13.2: the 28 keys, compared in both directions through the command line.
    The command runs in an empty directory with no ``FOUNDRY_*`` variable, so it prints the field
    defaults. A missing key and an undocumented key each fail, and the provenance document covers
    the same leaf set, because AAP 0.4.5 promises a source per key."""
    monkeypatch.chdir(tmp_path)
    cli = foundry.cli.main.build_group()
    result = CliRunner().invoke(cli, ["config", "show", "--format", "json"])
    assert result.exit_code == 0, (
        f"`foundry config show --format json` exited {result.exit_code}:\n{result.output}"
    )
    document = json.loads(result.output)
    assert isinstance(document, dict), type(document).__name__
    leaves = _flatten(document["resolved"])
    missing = sorted(set(CONFIG_KEYS) - set(leaves))
    undocumented = sorted(set(leaves) - set(CONFIG_KEYS))
    assert not missing, (
        f"`config show` does not resolve {missing}, which AAP 0.4.5 publishes as configuration keys"
    )
    assert not undocumented, (
        f"`config show` resolves {undocumented}, which no AAP 0.4.5 key declares: a key is part of "
        "the published surface, and an undocumented one is invisible to a consumer"
    )
    sources = document["sources"]
    assert sorted(sources) == sorted(leaves), (
        "the provenance document does not cover exactly the resolved keys (AAP 0.4.5 promises a "
        f"source for every key): missing={sorted(set(leaves) - set(sources))} "
        f"extra={sorted(set(sources) - set(leaves))}"
    )
