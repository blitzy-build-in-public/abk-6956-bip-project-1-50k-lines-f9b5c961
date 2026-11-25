"""The two determinism fixtures: a per-test seeded Faker instance and a frozen clock.

AAP 0.8.1 places this module in the example project as 2 of the suite's 24 collected tests.
``foundry_factory`` is proved to draw from a stream seeded by ``run_id + nodeid`` through
``seed_instance`` alone, and ``foundry_clock`` to be a frozen, injectable time source, so no test
here reads the machine's clock (AAP 0.4.3). No generated value is asserted literally -- the seed
carries the run id -- so reproducibility is proved seed-to-seed. Each test spends one call of the
example's deterministic work budget (AAP 0.14.2).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Final

import _work

from foundry.fixtures.clock import DEFAULT_INSTANT, FoundryClock
from foundry.fixtures.factory import FoundryFactory

_START: Final[datetime] = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
"""The explicit instant the clock test starts from, so this tree reads no wall clock."""

_LATER: Final[datetime] = datetime(2030, 6, 1, tzinfo=UTC)
"""A second explicit instant, which ``set`` must adopt rather than add to the first."""


def _twin_of(factory: FoundryFactory) -> FoundryFactory:
    """Return a second, independent factory seeded exactly as the fixture seeded ``factory``.

    ``seed`` is the handle :class:`FoundryFactory` publishes, so a fresh instance put through the
    same ``seed_instance`` call the fixture makes compares seeds rather than literals.
    """
    # Imported here, mirroring the fixture: Faker is only needed once a draw is compared.
    from faker import Faker

    twin = Faker()
    twin.seed_instance(factory.seed)
    return FoundryFactory(twin, seed=factory.seed)


def test_factory_is_reproducible_within_a_run(foundry_factory: FoundryFactory) -> None:
    """Check the factory proxies its providers and that its seed alone decides what it draws."""
    # The instance's first draw, kept so the twin is compared against a draw and not a literal, as
    # the seed carries the run id: a literal value would change on every run.
    first_name = foundry_factory.name()
    assert first_name and isinstance(first_name, str)

    email = foundry_factory.email()
    assert isinstance(email, str)
    assert "@" in email

    assert uuid.UUID(foundry_factory.uuid4()).version == 4
    assert foundry_factory.name() != first_name, "a generator, not a constant"

    # Why seed_instance rather than Faker.seed: the module-level seed reseeds the shared
    # random.Random every instance draws from, so two xdist workers would interleave their draws and
    # one test's values would depend on the order the tests ran in. Seeding each instance from
    # run_id + nodeid keeps every test's sequence its own and stable on any worker (AAP 0.2.1).
    twin = _twin_of(foundry_factory)
    assert twin.seed == foundry_factory.seed
    assert twin.name() == first_name, "an equal seed must reproduce the first drawn value"

    assert _work.spend() >= 0


def test_frozen_clock_advances_deterministically(foundry_clock: FoundryClock) -> None:
    """Check the clock is frozen, accumulates deltas and lets ``set`` replace the instant."""
    assert foundry_clock.now() == DEFAULT_INSTANT, "a fresh clock starts at the documented instant"

    foundry_clock.set(_START)
    assert foundry_clock.now() == _START
    assert foundry_clock.now() == _START, "frozen: the same instant, not a later one"

    assert foundry_clock.advance(minutes=5) == _START + timedelta(minutes=5)
    assert foundry_clock.now() == _START + timedelta(minutes=5)

    # The second delta builds on the first: one clock carries a test's whole timeline.
    foundry_clock.advance(seconds=30)
    assert foundry_clock.now() == _START + timedelta(minutes=5, seconds=30)

    foundry_clock.set(_LATER)
    assert foundry_clock.now() == _LATER, "set() replaces the instant rather than adding to it"

    instant = foundry_clock.now()
    assert instant.tzinfo is not None, "an aware instant compares the same on every host"
    assert instant.utcoffset() == timedelta(0)

    assert _work.spend() >= 0
