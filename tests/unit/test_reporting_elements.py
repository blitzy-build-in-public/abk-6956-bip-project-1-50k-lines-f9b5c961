"""The element boundary of the report builders (AAP 0.14.3).

Finding cr1-f01: ``foundry.reporting.panels.build_panels`` dereferenced ``element.failures`` on
every element that was not a run error, and ``foundry.reporting.html.build_context`` handed it the
unfiltered sequence, so a caller that passed anything else got a builtin ``AttributeError`` out of
the report writer -- an exception of no Foundry type crossing a module boundary. The sequence is now
validated once in ``build_context``, which raises
:class:`foundry.core.exceptions.ElementTypeError` as ``store.dumps`` and ``junit`` already do for
their own documents, and ``build_panels`` reads only a
:class:`foundry.reporting.record.ResultRecord`, so an element of any other kind is skipped there
rather than dereferenced.

Determinism and isolation: records are built from fixed timestamps, nothing sleeps, nothing touches
the network, and no assertion reads the host's clock or its working directory.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest

from foundry.core.exceptions import ElementTypeError, FoundryError
from foundry.reporting import html, panels
from foundry.reporting.record import FailureRecord, Outcome, ResultElement, ResultRecord

# Fixed run id, profile, module path and instants, so no assertion depends on the host's clock.

_RUN_ID = "20250102T030405-abc123"
_PROFILE = "ci"
_STARTED_AT = datetime(2025, 1, 2, 3, 4, 5, 0, tzinfo=UTC)
_FINISHED_AT = datetime(2025, 1, 2, 3, 4, 5, 250_000, tzinfo=UTC)
_MODULE = "tests/test_cart.py"


def _record_for(name: str, **overrides: object) -> ResultRecord:
    """Return a record whose node id is the module path plus ``name``."""
    fields: dict[str, object] = {
        "run_id": _RUN_ID,
        "started_at": _STARTED_AT,
        "finished_at": _FINISHED_AT,
        "profile": _PROFILE,
        "nodeid": f"{_MODULE}::{name}",
        "outcome": Outcome.PASSED,
        "attempts": 1,
        "duration_ms": 12.5,
    }
    fields.update(overrides)
    return ResultRecord.model_validate(fields)


def _foreign_elements() -> list[ResultElement]:
    """Return a one-element sequence holding a builtin object: the caller defect under test.

    It is cast because the builders' contract is typed for the two foundry element kinds; a caller
    that hands them anything else is the state AAP 0.14.3 requires them to report rather than to
    crash on.
    """
    return cast("list[ResultElement]", [object()])


def test_the_renderer_refuses_a_foreign_element() -> None:
    """``render`` raises the published type, so no builtin escapes into the session."""
    with pytest.raises(ElementTypeError) as excinfo:
        html.render(_foreign_elements())
    assert isinstance(excinfo.value, FoundryError)
    assert "report.html" in str(excinfo.value)


def test_the_context_builder_refuses_a_foreign_element() -> None:
    """``build_context`` validates the sequence once, before a single panel is built."""
    with pytest.raises(ElementTypeError) as excinfo:
        html.build_context(_foreign_elements())
    assert isinstance(excinfo.value, FoundryError)
    assert excinfo.value.hint is not None


def test_the_panel_builder_skips_a_foreign_element() -> None:
    """``build_panels`` reads no element it cannot place, so it returns and raises nothing."""
    assert panels.build_panels(_foreign_elements()) == ()


def test_the_panel_builder_still_reads_the_records_it_can() -> None:
    """A foreign element beside a failing record costs that record no panel.

    The guard is a skip and not an abort: the report's own records are still rendered when one
    element of the sequence is not one of them.
    """
    failing = _record_for(
        "a", outcome=Outcome.FAILED, failures=(FailureRecord(phase="call", message="boom"),)
    )
    built = panels.build_panels([*_foreign_elements(), failing])
    assert [panel.nodeid for panel in built] == [failing.nodeid]
    assert [panel.phase for panel in built] == ["call"]
