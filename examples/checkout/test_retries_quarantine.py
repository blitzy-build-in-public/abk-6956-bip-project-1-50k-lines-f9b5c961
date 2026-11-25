"""The retry driver and the quarantine marker, observed through the documented example run.

AAP 0.8.1 gives this module two of the suite's 24 collected tests: one that fails its first attempt
and passes on the repeat, and one carrying ``@pytest.mark.foundry_quarantine`` that passes.

**The outcome conflict this module resolves.** AAP 0.14.1's criterion 3 asserts that
``pytest examples/checkout`` reports exactly ``24 passed`` and exits 0, while AAP 0.4.7 defines
``quarantined_failed`` for a *failing* marked test. Both hold only while no example test ends
failed, so the marked example test passes: it records ``quarantine_passed`` with exit 0 and the
marker-removal note AAP 0.4.6 and assumption A11 specify. The retried test likewise ends
``passed``, with ``attempts: 2``, because only its first attempt fails and the driver discards that
attempt. The failing-quarantine and ``strict``-mode matrix belongs to
``tests/plugin/test_quarantine.py`` (AAP 0.10.1), not to this tree.

**Why the counter is module level.** The driver re-runs the whole runtest protocol per attempt, so a
function-scoped fixture is rebuilt between attempts and a counter held in one would reset, making
every attempt look like the first (AAP 0.7.1); a mapping keyed by node id does not reset.

Both tests spend one call of the example's deterministic work budget, the source of the suite's 12 s
of aggregate CPU work (AAP 0.14.2); the retried test spends it on both attempts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import _work
import pytest

if TYPE_CHECKING:
    from foundry.config.models import FoundryConfig

_RETRIES_NEEDED: int = 1
"""Repeats the retried test needs: one repeat after the first attempt is two attempts (A2)."""

_PASSING_ATTEMPT: int = 2
"""The attempt number that passes, so the assertions state the driver's arithmetic explicitly."""

_ATTEMPTS: dict[str, int] = {}
"""Attempts already made, keyed by node id, surviving the driver's re-runs of this item."""


def test_retry_until_pass(request: pytest.FixtureRequest, foundry_config: FoundryConfig) -> None:
    """Fail the first attempt and pass the second, which only a re-run by the driver produces.

    The budget is asserted first, and it has to come from the project's own resolution rather than a
    ``--foundry-retries`` flag: the documented invocation of this suite carries no options at all
    (AAP 0.14.1).

    The counter is incremented and the work spent before the assertion, so the failing attempt costs
    what every other example test costs. Attempt one fails by design and the driver repeats the
    protocol; attempt two passes. There is no branch, no ``try`` and no ``xfail``: the first attempt
    must genuinely fail, or the retry path this module exists to prove was never taken.
    """
    assert foundry_config.execution.retries >= _RETRIES_NEEDED, (
        "this test needs at least one repeat after its first attempt: raise execution.retries "
        "to 1 in this example's configuration, not through a --foundry-retries flag; the "
        f"resolved value is {foundry_config.execution.retries}"
    )

    nodeid = request.node.nodeid
    attempt = _ATTEMPTS.get(nodeid, 0) + 1
    _ATTEMPTS[nodeid] = attempt
    _work.spend()

    assert attempt >= _PASSING_ATTEMPT, (
        "first attempt fails by design; the retry driver must re-run this test"
    )
    assert _ATTEMPTS[nodeid] == _PASSING_ATTEMPT, (
        f"the driver re-ran {nodeid!r} up to attempt {_ATTEMPTS[nodeid]}: the same node id "
        f"accounted for both attempts, of which {_PASSING_ATTEMPT} passes"
    )


@pytest.mark.foundry_quarantine
def test_quarantined_pass_is_reported(request: pytest.FixtureRequest) -> None:
    """Pass while carrying the quarantine marker, so the report lists it as a passing quarantine.

    The recorded outcome is ``quarantine_passed``: the run exits 0, and the report's Quarantined
    section lists the test with the recommendation to remove the marker, because a quarantined test
    that passes no longer needs quarantining (AAP 0.4.6, assumption A11).

    The marker is read back off the item rather than trusted from the decorator, so the assertion
    covers the item the framework sees; ``--strict-markers`` would already have refused an
    unregistered name.
    """
    assert request.node.get_closest_marker("foundry_quarantine") is not None, (
        "the foundry_quarantine marker is not attached to this item"
    )
    assert _work.spend() >= 0
