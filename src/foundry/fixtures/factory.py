"""The ``foundry_factory`` fixture: a Faker instance seeded per test from run id and node id.

``foundry_factory`` is one of Foundry's twelve public fixtures (AAP 0.4.3). It is function scoped
and returns a :class:`FoundryFactory` whose attribute access proxies the Faker providers, so
``factory.name()``, ``factory.email()`` and ``factory.uuid4()`` are the underlying instance's own
methods. The class also publishes ``faker`` -- the instance, for a caller that wants a typed
reference -- and ``seed``, the value it was seeded with.

**Seeded per instance, never globally.** The fixture seeds the instance it builds with
``faker.seed_instance(seed)`` and never with ``Faker.seed``. ``Faker.seed()`` reseeds the
module-wide shared :class:`random.Random` that every Faker instance draws from, so one test's draws
would shift every later test's values in the same worker and two workers would interleave from one
sequence (AAP 0.2.1). ``seed_instance`` gives the instance a generator of its own, which is what
makes a test's values depend on nothing another test does, in this process or in another xdist
worker.

**What reproducibility depends on.** The seed comes from :func:`_seed_for`: the first eight bytes of
SHA-256 over ``"<run_id>:<nodeid>"`` read as a big-endian unsigned integer. It is therefore stable
across processes, Python versions and ``PYTHONHASHSEED`` values, and identical on a serial run and
on any worker (implicit requirements I3, I6): every test has its own node id, so no two tests share
a sequence. The *values* a seed produces additionally require the Faker release pinned as a runtime
dependency (25.9.2, AAP 0.5.1) and the same call order, which is why that release is pinned and why
the fixture body draws nothing itself: the test's own calls are the whole order.

**No clock, no locale, no environment.** Nothing here reads the wall clock, the locale or
``os.environ`` (AAP 0.14.3), so a factory never makes ``results.json`` or ``report.html`` differ
between two otherwise identical runs. Faker's default locale is used and is not chosen here.

**Imported lazily.** ``faker`` is imported inside the fixture body and nowhere at module scope: the
plugin imports this module at every pytest start inside the 150 ms budget (AAP 0.14.2, implicit
requirement I5), while only a minority of suites request a factory, and importing Faker costs far
more than the budget allows.

Importing this module defines names only -- the seed helper, the class and the fixture. It holds no
module-level state, performs no I/O, reads no environment variable and configures no logger
(AAP 0.4.8); it imports the standard library and :mod:`pytest`, plus :mod:`faker` under
``TYPE_CHECKING`` for the annotation alone, and no other Foundry package (AAP 0.4.8).
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any, Final

import pytest

if TYPE_CHECKING:
    from faker import Faker

__all__ = ["FoundryFactory", "foundry_factory"]

_SEED_BYTES: Final[int] = 8
"""How many leading bytes of the digest become the seed: 64 bits, the width ``random.seed`` accepts
without truncation and wide enough that two tests sharing a seed is not a practical concern.
"""


def _seed_for(run_id: str, nodeid: str) -> int:
    """Return the Faker seed of one test, derived from the run id and that test's node id only.

    The derivation takes no worker id, host name, clock reading or random source, so a test draws
    the same values serially and under ``-n`` (implicit requirements I3, I6), and two runs of the
    same suite within one session cannot collide because node ids differ.

    Args:
        run_id: The session's run id, ``YYYYmmddTHHMMSS-<6 hex>``.
        nodeid: The requesting item's pytest node id, unsanitised.

    Returns:
        Eight bytes of SHA-256 over ``"<run_id>:<nodeid>"`` read as a big-endian unsigned integer,
        so the result lies in ``[0, 2**64)``.
    """
    # A digest rather than hash(): the builtin is salted per process, so the same test would draw
    # different values in a second process and on a different xdist worker.
    digest = hashlib.sha256(f"{run_id}:{nodeid}".encode()).digest()
    return int.from_bytes(digest[:_SEED_BYTES], "big")


class FoundryFactory:
    """A Faker instance seeded for one test, with its providers reachable by attribute.

    ``foundry_factory`` returns one of these per test (AAP 0.4.3). ``faker`` exposes the underlying
    instance for a caller that wants a typed reference, ``seed`` the value the instance was seeded
    with, and every other attribute is proxied to the instance, so ``factory.name()``,
    ``factory.email()`` and ``factory.uuid4()`` resolve to Faker's own provider methods.

    The class seeds nothing itself: :func:`foundry_factory` calls ``seed_instance`` on the instance
    before wrapping it, so constructing a factory never touches Faker's module-wide random state
    (AAP 0.2.1). The representation names the seed alone, never a generated value, so logging a
    factory cannot put generated data -- or a value derived from a secret -- into a log, a report or
    an artifact.
    """

    faker: Faker
    seed: int

    def __init__(self, faker: Faker, *, seed: int) -> None:
        """Wrap an instance the caller has already seeded.

        Args:
            faker: The instance to proxy. It is stored as given: nothing is seeded here, so a caller
                that wants reproducible values seeds it through ``seed_instance`` first.
            seed: The seed it was given, kept for :attr:`seed` and :meth:`__repr__`.
        """
        self.faker = faker
        self.seed = seed

    def __getattr__(self, name: str) -> Any:
        """Proxy an attribute of the underlying Faker instance.

        Faker builds its providers at instance construction from the enabled locales, so what a name
        resolves to is known only at run time; this is the file's one deliberate ``Any``, since
        ``factory.name()`` answers a ``str``, ``factory.pyint()`` an ``int`` and
        ``factory.profile()`` a mapping, and no annotation in this module can say which.

        Args:
            name: The attribute or provider name the caller asked for.

        Returns:
            Whatever the instance holds under ``name`` -- normally a bound provider method, which
            the caller then calls.

        Raises:
            AttributeError: The instance has no such attribute. An unknown provider is an ordinary
                ``AttributeError`` rather than a Foundry error, so ``hasattr()`` reports ``False``
                and ``dir()``, ``getattr()`` with a default and every other introspection protocol
                behave as they do for any Python object.
        """
        # Read from __dict__, not through self.faker: a factory whose __init__ never ran would
        # otherwise re-enter this method for "faker" itself, recursing until the stack ends.
        proxy: object = self.__dict__.get("faker")
        if proxy is None:
            raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")
        try:
            return getattr(proxy, name)
        except AttributeError:
            raise AttributeError(
                f"{type(self).__name__!r} object has no attribute {name!r}",
            ) from None

    def __repr__(self) -> str:
        """Return ``FoundryFactory(seed=<seed>)``, which names no generated value."""
        return f"{type(self).__name__}(seed={self.seed})"


@pytest.fixture
def foundry_factory(request: pytest.FixtureRequest, foundry_run_id: str) -> FoundryFactory:
    """Return this test's seeded factory.

    Function scoped (AAP 0.4.3): one instance per test, a fresh Faker behind it and a seed derived
    from the session's run id and this item's node id through :func:`_seed_for`, so neither a test's
    draws nor its seeding reach any other test.

    The instance is seeded with ``seed_instance`` and never with ``Faker.seed``, which would reseed
    the module-wide shared generator and shift every other test's values in this worker (AAP 0.2.1).
    Two runs of the same test with the same run id therefore draw the same sequence in the same call
    order, while two tests always differ; keeping those values identical across Faker releases
    depends on the pinned version (AAP 0.5.1) and on the test making its calls in the same order.

    Args:
        request: The requesting item, whose node id is half of the seed.
        foundry_run_id: The session's run id, the other half.

    Returns:
        A :class:`FoundryFactory` wrapping a Faker instance already seeded for this test.
    """
    from faker import Faker

    faker = Faker()
    seed = _seed_for(foundry_run_id, request.node.nodeid)
    faker.seed_instance(seed)
    return FoundryFactory(faker, seed=seed)
