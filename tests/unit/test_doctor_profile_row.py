"""The two behaviours the ``foundry doctor`` configuration row and ``foundry_artifacts`` share: a
``model_copy`` override may install a plain-string profile where ``.value`` would raise, and the row
renders from ``foundry.config.errors.success_message`` rather than a second copy of the sentence."""

import json
from pathlib import Path

import pytest

from foundry.config import errors
from foundry.config.models import FoundryConfig
from foundry.diagnostics import checks
from foundry.diagnostics.report import ProbeStatus
from foundry.fixtures.artifacts import foundry_artifacts

_PROFILE = "ci"
_RUN_ID = "20260101T000000-abcdef"
_NODE_ID = "tests/test_checkout.py::test_total"


def _shadowed(profile: str = _PROFILE, artifacts_dir: Path | None = None) -> FoundryConfig:
    base = FoundryConfig()
    updates: dict[str, object] = {"profile": profile}
    if artifacts_dir is not None:
        updates["artifacts"] = base.artifacts.model_copy(update={"dir": str(artifacts_dir)})
    return base.model_copy(update=updates)


def test_the_probe_and_the_report_pass_a_plain_string_profile(tmp_path: Path) -> None:
    shadowed = _shadowed()
    assert type(shadowed.profile) is str and not hasattr(shadowed.profile, "value")
    row = checks.check_configuration(shadowed)
    assert row.status is ProbeStatus.PASS and row.detail == errors.success_message(_PROFILE)
    reported = {row.name: row for row in checks.run_checks(config=shadowed, cwd=tmp_path).checks}
    assert reported[checks.PROBE_CONFIGURATION].status is ProbeStatus.PASS
    assert reported[checks.PROBE_CONFIGURATION].detail == errors.success_message(_PROFILE)


@pytest.mark.parametrize("profile", ["default", "local", "ci", "staging"])
def test_configuration_row_renders_the_published_message(profile: str) -> None:
    row = checks._configuration_row(profile)
    assert row.status is ProbeStatus.PASS
    assert row.detail == f"configuration valid (profile={profile})"
    assert row.detail == errors.success_message(profile)


def test_configuration_row_follows_the_one_definition_site(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(errors, "VALIDATION_OK_TEMPLATE", "probe rerouted (profile={profile})")
    row = checks.check_configuration(_shadowed("default"))
    assert row.detail == "probe rerouted (profile=default)"


def test_foundry_artifacts_builds_under_a_plain_string_profile(tmp_path: Path) -> None:
    writer = foundry_artifacts.__wrapped__(_shadowed(artifacts_dir=tmp_path), _RUN_ID)
    directory = writer.dir_for(_NODE_ID)
    # AAP 0.1.2 places one directory per test inside ``artifacts/<run_id>/``, named for the nodeid.
    assert directory.parent == tmp_path / _RUN_ID
    assert writer.for_node(_NODE_ID).write_manifest() is not None
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["profile"] == _PROFILE
