"""The two behaviours the ``foundry doctor`` configuration row and ``foundry_artifacts`` share.

Finding ``cr20-profile-value-attribute``: ``foundry.fixtures.config`` documents that a suite may
shadow ``foundry_config`` with a ``model_copy`` that installs a plain string for ``profile``, and
``model_copy`` bypasses validation, so ``config.profile`` is then a ``str`` rather than a
:class:`foundry.config.ProfileName` member. Reading the profile as text with :func:`str` keeps that
override working: the fixture built the artifact writer with ``.value`` and raised
``AttributeError: 'str' object has no attribute 'value'``, and the doctor probe turned the same
override into a ``FAIL`` row.

Finding ``cr06-doctor-valid-literal``: the doctor row used to re-state
``configuration valid (profile=<name>)`` instead of calling
:func:`foundry.config.errors.success_message`, so ``foundry doctor`` and ``foundry config validate``
held two copies of the one sentence AAP 0.4.4 publishes. The row now renders from that single
definition site, which the template test below observes by changing the template at run time: a
re-stated literal would stay behind.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from foundry.config import errors
from foundry.config.models import FoundryConfig
from foundry.diagnostics import checks
from foundry.diagnostics.report import ProbeStatus
from foundry.fixtures.artifacts import foundry_artifacts

_SHADOWED_PROFILE = "ci"
"""The plain-string profile the shadowing override installs, one of the four AAP 0.4.5 names."""

_RUN_ID = "20260101T000000-abcdef"
"""A run id in the shape AAP 0.4.3 fixes, which the artifact writer only checks for containment."""

_NODE_ID = "tests/test_checkout.py::test_total"
"""A node id whose sanitised directory the artifact writer has to resolve."""


def _shadowed(
    *, profile: str = _SHADOWED_PROFILE, artifacts_dir: Path | None = None
) -> FoundryConfig:
    """Return a resolved configuration whose ``profile`` is a plain string.

    ``model_copy(update=...)`` is the override :func:`foundry.fixtures.config.foundry_config`
    documents; it bypasses validation, so the value stays whatever was installed.

    Args:
        profile: the profile name to install as text.
        artifacts_dir: the artifact root to record, or ``None`` to keep the configured default.

    Returns:
        The shadowed configuration.
    """
    base = FoundryConfig()
    updates: dict[str, object] = {"profile": profile}
    if artifacts_dir is not None:
        updates["artifacts"] = base.artifacts.model_copy(update={"dir": str(artifacts_dir)})
    return base.model_copy(update=updates)


def test_shadowing_installs_a_plain_string_profile() -> None:
    """The reproduction holds: the shadowed value is a ``str`` and has no ``.value``."""
    shadowed = _shadowed()
    assert type(shadowed.profile) is str
    assert not hasattr(shadowed.profile, "value")


def test_configuration_probe_reports_a_plain_string_profile() -> None:
    """``check_configuration`` returns the ``PASS`` row instead of raising ``AttributeError``."""
    row = checks.check_configuration(_shadowed())
    assert row.status is ProbeStatus.PASS
    assert row.detail == errors.success_message(_SHADOWED_PROFILE)


def test_doctor_report_keeps_the_configuration_row_passing(tmp_path: Path) -> None:
    """``run_checks`` isolates no fault for the configuration row of a shadowed configuration."""
    report = checks.run_checks(config=_shadowed(), cwd=tmp_path)
    rows = {row.name: row for row in report.checks}
    configuration = rows[checks.PROBE_CONFIGURATION]
    assert configuration.status is ProbeStatus.PASS
    assert configuration.detail == errors.success_message(_SHADOWED_PROFILE)


@pytest.mark.parametrize("profile", ["default", "local", "ci", "staging"])
def test_configuration_row_renders_the_published_message(profile: str) -> None:
    """The row carries the AAP 0.4.4 message for every profile name, as text."""
    row = checks._configuration_row(str(profile))
    assert row.status is ProbeStatus.PASS
    assert row.detail == f"configuration valid (profile={profile})"
    assert row.detail == errors.success_message(profile)


def test_configuration_row_follows_the_one_definition_site(monkeypatch: pytest.MonkeyPatch) -> None:
    """A changed template moves the row, so the sentence is not a second copy of the text."""
    monkeypatch.setattr(errors, "VALIDATION_OK_TEMPLATE", "probe rerouted (profile={profile})")
    row = checks.check_configuration(_shadowed(profile="default"))
    assert row.detail == errors.success_message("default")
    assert row.detail == "probe rerouted (profile=default)"


def test_foundry_artifacts_builds_under_a_plain_string_profile(tmp_path: Path) -> None:
    """The fixture yields a writer that records the plain-string profile as text in its manifest."""
    config = _shadowed(artifacts_dir=tmp_path)
    writer = foundry_artifacts.__wrapped__(config, _RUN_ID)
    directory = writer.dir_for(_NODE_ID)
    # AAP 0.1.2 places one directory per test inside ``artifacts/<run_id>/``; the name is the
    # sanitised nodeid and is not this finding's subject.
    assert directory.parent == tmp_path / _RUN_ID
    assert writer.for_node(_NODE_ID).write_manifest() is not None
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["profile"] == _SHADOWED_PROFILE
