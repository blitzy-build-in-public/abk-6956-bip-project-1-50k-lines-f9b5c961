"""The element boundary of the report builders (AAP 0.14.3).
``build_context`` validates the element sequence once and raises ``ElementTypeError``, so a caller
defect never escapes as a builtin ``AttributeError``; ``build_panels`` reads only result records."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast

import pytest

from foundry.core.exceptions import ElementTypeError, FoundryError
from foundry.reporting import html, panels
from foundry.reporting.record import FailureRecord, Outcome, ResultElement, ResultRecord

_RUN_ID = "20250102T030405-abc123"
_STARTED_AT = datetime(2025, 1, 2, 3, 4, 5, 0, tzinfo=UTC)
_FINISHED_AT = datetime(2025, 1, 2, 3, 4, 5, 250_000, tzinfo=UTC)


def _record_for(name: str, **overrides: object) -> ResultRecord:
    fields: dict[str, object] = {
        "run_id": _RUN_ID,
        "started_at": _STARTED_AT,
        "finished_at": _FINISHED_AT,
        "profile": "ci",
        "nodeid": f"tests/test_cart.py::{name}",
        "outcome": Outcome.PASSED,
        "attempts": 1,
        "duration_ms": 12.5,
    }
    fields.update(overrides)
    return ResultRecord.model_validate(fields)


def _foreign_elements() -> list[ResultElement]:
    return cast("list[ResultElement]", [object()])


@pytest.mark.parametrize(
    "build", [html.render, html.build_context], ids=["render", "build-context"]
)
def test_a_foreign_element_is_refused_by_every_builder(build: Callable[..., object]) -> None:
    with pytest.raises(ElementTypeError, match="report.html") as excinfo:
        build(_foreign_elements())
    assert isinstance(excinfo.value, FoundryError) and excinfo.value.hint is not None


def test_the_panel_builder_skips_a_foreign_element() -> None:
    assert panels.build_panels(_foreign_elements()) == ()


def test_the_panel_builder_still_reads_the_records_it_can() -> None:
    failing = _record_for(
        "a", outcome=Outcome.FAILED, failures=(FailureRecord(phase="call", message="boom"),)
    )
    built = panels.build_panels([*_foreign_elements(), failing])
    assert [(panel.nodeid, panel.phase) for panel in built] == [(failing.nodeid, "call")]
