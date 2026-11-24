"""The class contract one outcome badge shares across the report's two badge sites (AAP 0.4.6).

Finding cr2-f04: the Quarantined section built its modifier class inside the template
(``outcome-{{ q.outcome }}``) while the results table read a class the Python side precomputed, so
one badge contract had two sources and a change to ``Outcome``'s string form would have unstyled
the Quarantined badges alone. :class:`foundry.reporting.html.QuarantinedRow` now carries
``css_class``, filled from the row's own class -- which ``_outcome_css_class`` derives once for the
nine published outcome values (AAP 0.4.6, AAP 0.4.9) -- and the template reads it, so both badges
for one record render the same class.

Determinism and isolation (AAP 0.14.3): a fixed run identity, fixed instants, no sleep, no network,
and every assertion made against the rendered document rather than the host.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from foundry.reporting import html
from foundry.reporting.record import Outcome, QuarantineRecord, ResultRecord

# Fixed run id, profile and instant, so no assertion depends on the host's clock.

_RUN_ID = "20250102T030405-abc123"
_PROFILE = "ci"
_FINISHED_AT = datetime(2025, 1, 2, 3, 4, 5, 250_000, tzinfo=UTC)
_NODEID = "tests/test_cart.py::test_flaky"


def _record(outcome: Outcome, quarantine: QuarantineRecord) -> ResultRecord:
    """Return a quarantined result record whose every other field is fixed."""
    return ResultRecord(
        run_id=_RUN_ID,
        started_at=_FINISHED_AT,
        finished_at=_FINISHED_AT,
        profile=_PROFILE,
        nodeid=_NODEID,
        outcome=outcome,
        attempts=1,
        duration_ms=12.5,
        markers=("foundry_quarantine",),
        quarantine=quarantine,
    )


@pytest.mark.parametrize(
    ("outcome", "quarantine"),
    [
        (Outcome.QUARANTINED_FAILED, QuarantineRecord(state="failed")),
        (Outcome.QUARANTINE_PASSED, QuarantineRecord(state="passed")),
    ],
    ids=["quarantined-failed", "quarantine-passed"],
)
def test_a_quarantined_badge_takes_its_class_from_the_context(
    outcome: Outcome, quarantine: QuarantineRecord
) -> None:
    record = _record(outcome, quarantine)
    css_class = f"outcome-{outcome.value}"
    assert html.build_context([record]).quarantined[0].css_class == css_class
    # Both badges -- the table row's and the Quarantined entry's -- render that one class.
    document = html.render([record])
    assert document is not None
    assert document.count(f'class="outcome {css_class}"') == 2
    # The template reads the class rather than composing one from the enum, so the two sites cannot
    # drift even where the rendered markup would look the same either way.
    assert 'class="outcome {{ q.css_class }}"' in html.template_path().read_text(encoding="utf-8")
