"""The badge class one quarantined record shares across the report's two badge sites (AAP 0.4.6).

Keep both badges reading ``QuarantinedRow.css_class``: a modifier composed inside the template would
let a change to ``Outcome``'s string form unstyle the Quarantined section alone.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from foundry.reporting import html
from foundry.reporting.record import Outcome, QuarantineRecord, ResultRecord

_RUN_ID = "20250102T030405-abc123"
_FINISHED_AT = datetime(2025, 1, 2, 3, 4, 5, 250_000, tzinfo=UTC)


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
    record = ResultRecord(
        run_id=_RUN_ID,
        started_at=_FINISHED_AT,
        finished_at=_FINISHED_AT,
        profile="ci",
        nodeid="tests/test_cart.py::test_flaky",
        outcome=outcome,
        attempts=1,
        duration_ms=12.5,
        markers=("foundry_quarantine",),
        quarantine=quarantine,
    )
    assert html.build_context([record]).quarantined[0].css_class == f"outcome-{outcome.value}"
    document = html.render([record])
    assert document is not None
    assert document.count(f'class="outcome outcome-{outcome.value}"') == 2
    assert 'class="outcome {{ q.css_class }}"' in html.template_path().read_text(encoding="utf-8")
