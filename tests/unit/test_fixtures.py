"""Unit tests for the ten public fixtures this tree delivers, and the scopes they are cached under.

AAP 0.4.3 publishes twelve fixtures with a scope and a return type each. This module covers the ten
the tree delivers -- ``foundry_clock``, ``foundry_poll``, ``foundry_config``, ``foundry_profile``,
``foundry_run_id``, ``foundry_artifacts``, ``foundry_data``, ``foundry_db``, ``foundry_factory`` and
``foundry_log_capture`` -- because ``foundry_http`` and ``foundry_api`` land with their sibling
modules.

The fixtures are exercised where a suite meets them, not by calling their functions: the
``pytest_plugins`` list below loads the eight fixture modules into this module's namespace, so every
test that names a fixture is a real resolution through pytest's own machinery, with the caching and
scope ordering that implies.

A published scope is asserted worker-independently, one fixture per test: the test resolves the
fixture, then reads the scope its own module declares and the scope pytest built for this node id,
and requires both to be the published one. Nothing here compares a value with one a *different* item
received, because xdist schedules two items of this module independently (AAP 0.2.2); that a
consuming suite receives one object for the four session-scoped fixtures and a fresh one for the
other six is proved by ``tests/plugin/test_fixture_activation.py``.

Beyond name, scope and type, each fixture's own promise is pinned: the run id's shape, the artifact
writer's ``dir_for``/``write``/``manifest``, the configuration's per-key provenance, the frozen
clock, the data loader, the sqlite session's isolation, the Faker seed derived from run id and node
id, the bounded log ring, and ``eventually`` both returning and timing out with an injected sleeper.

The artifact and data directories are redirected into the session's temporary directory by the
``foundry_config`` override below, and every other path is under ``tmp_path``, so nothing is written
inside the repository. No test sleeps: the poll tests inject their own ``sleep``, and the timeout
case advances a frozen clock (AAP 0.14.2).
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from faker import Faker
from sqlalchemy import String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from foundry.config import FoundryConfig
from foundry.data.cases import Case
from foundry.obs.artifacts import ArtifactWriter

if TYPE_CHECKING:
    from foundry.fixtures.artifacts import FoundryArtifacts
    from foundry.fixtures.clock import FoundryClock
    from foundry.fixtures.data import FoundryDataLoader
    from foundry.fixtures.factory import FoundryFactory
    from foundry.fixtures.logs import FoundryLogCapture
    from foundry.fixtures.polling import FoundryPoll

# Declared here because pytest reads this module's own plugin list at collection: it puts the ten
# fixtures in scope for the tests below, and it is the list a project's conftest writes verbatim.
pytest_plugins = [
    "foundry.fixtures.artifacts",
    "foundry.fixtures.clock",
    "foundry.fixtures.config",
    "foundry.fixtures.data",
    "foundry.fixtures.db",
    "foundry.fixtures.factory",
    "foundry.fixtures.logs",
    "foundry.fixtures.polling",
]

_RUN_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"\d{8}T\d{6}-[0-9a-f]{6}")
"""``YYYYmmddTHHMMSS-<6 hex>``: the shape AAP 0.4.3 publishes for ``foundry_run_id``."""

_EXPECTED: Final[Mapping[str, tuple[str, str]]] = {
    "foundry_config": ("foundry.fixtures.config", "session"),
    "foundry_profile": ("foundry.fixtures.config", "session"),
    "foundry_run_id": ("foundry.fixtures.artifacts", "session"),
    "foundry_artifacts": ("foundry.fixtures.artifacts", "session"),
    "foundry_data": ("foundry.fixtures.data", "function"),
    "foundry_db": ("foundry.fixtures.db", "function"),
    "foundry_factory": ("foundry.fixtures.factory", "function"),
    "foundry_clock": ("foundry.fixtures.clock", "function"),
    "foundry_log_capture": ("foundry.fixtures.logs", "function"),
    "foundry_poll": ("foundry.fixtures.polling", "function"),
}
"""Every fixture this tree delivers: the module AAP 0.10.1 places it in, and its AAP 0.4.3 scope.

The scope column is asserted against the declaration and against the definition pytest builds for
the requesting item (see :func:`test_each_fixture_is_cached_under_the_scope_the_table_publishes`),
and the published return type by :func:`_contract_types`, so a rename, a scope change or a
return-type change of any of the ten fails here rather than in a consumer.
"""

_SESSION_SCOPED: Final[tuple[str, ...]] = tuple(
    name for name, (_module, scope) in _EXPECTED.items() if scope == "session"
)
_FUNCTION_SCOPED: Final[tuple[str, ...]] = tuple(
    name for name, (_module, scope) in _EXPECTED.items() if scope == "function"
)


def _contract_types() -> Mapping[str, type[object]]:
    """Return the return type AAP 0.4.3 publishes for each of the ten fixtures.

    The fixture modules are imported here rather than at module scope on purpose: pytest loads them
    as plugins when it collects this module, and a module imported before that cannot have assertion
    rewriting applied to it, which pytest reports as a warning. Importing them from inside a test
    keeps the collection clean and still asserts the exact type each fixture hands a suite.
    """
    from foundry.fixtures.clock import FoundryClock
    from foundry.fixtures.data import FoundryDataLoader
    from foundry.fixtures.factory import FoundryFactory
    from foundry.fixtures.logs import FoundryLogCapture
    from foundry.fixtures.polling import FoundryPoll

    return {
        "foundry_config": FoundryConfig,
        "foundry_profile": str,
        "foundry_run_id": str,
        "foundry_artifacts": ArtifactWriter,
        "foundry_data": FoundryDataLoader,
        "foundry_db": Session,
        "foundry_factory": FoundryFactory,
        "foundry_clock": FoundryClock,
        "foundry_log_capture": FoundryLogCapture,
        "foundry_poll": FoundryPoll,
    }


_NODEID: Final[str] = "tests/test_api.py::test_checkout[order-1]"
"""A node id carrying ``/``, ``::`` and ``[``, so the artifact directory proves its sanitising."""

_OTHER_NODEID: Final[str] = "tests/test_api.py::test_checkout[order-2]"
"""A node id no test in this module carries: the seed a *different* item would be given."""

_LOG_MESSAGE: Final[str] = "foundry fixture probe: one captured line"


class _Base(DeclarativeBase):
    """The declarative base of the one model the database fixture's schema is built from."""


class _Row(_Base):
    """A two-column table, which is enough to prove a schema was created and a row was written."""

    __tablename__ = "widgets"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(20))


# --- Fixtures -------------------------------------------------------------


@pytest.fixture(scope="session")
def foundry_config(
    foundry_config: FoundryConfig,
    repo_root: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> FoundryConfig:
    """Override: put the artifact tree and the data directory inside the temporary directory.

    This is the override AAP 0.4.3 documents -- request the public fixture, return a ``model_copy``
    -- and it exists so that requesting ``foundry_artifacts`` cannot create an ``artifacts/`` tree
    in the repository, and so that ``foundry_data`` resolves the committed files under
    ``tests/fixtures`` instead of the default ``testdata``.
    """
    root = tmp_path_factory.mktemp("foundry-fixtures")
    return foundry_config.model_copy(
        update={
            "artifacts": foundry_config.artifacts.model_copy(update={"dir": root / "artifacts"}),
            "data": foundry_config.data.model_copy(
                update={"dir": repo_root / "tests" / "fixtures"}
            ),
        }
    )


# --- Name, type and scope (AAP 0.4.3) ------------------------------------


def test_the_delivered_fixtures_publish_the_names_the_contract_fixes() -> None:
    """Each published name is the name its module declares, in the module AAP 0.10.1 places it."""
    declared: dict[str, str] = {}
    for name, (module_name, _scope) in _EXPECTED.items():
        module = __import__(module_name, fromlist=[name])
        definition = getattr(module, name)
        assert definition.name == name
        declared[name] = module_name
    assert declared == {name: module for name, (module, _s) in _EXPECTED.items()}
    assert {name: scope for name, (_m, scope) in _EXPECTED.items()} == {
        "foundry_config": "session",
        "foundry_profile": "session",
        "foundry_run_id": "session",
        "foundry_artifacts": "session",
        "foundry_data": "function",
        "foundry_db": "function",
        "foundry_factory": "function",
        "foundry_clock": "function",
        "foundry_log_capture": "function",
        "foundry_poll": "function",
    }


def test_every_fixture_returns_the_type_the_table_publishes(
    foundry_config: FoundryConfig,
    foundry_profile: str,
    foundry_run_id: str,
    foundry_artifacts: FoundryArtifacts,
    foundry_data: FoundryDataLoader,
    foundry_db: Session,
    foundry_factory: FoundryFactory,
    foundry_clock: FoundryClock,
    foundry_log_capture: FoundryLogCapture,
    foundry_poll: FoundryPoll,
) -> None:
    """The ten fixtures resolve, and each one's exact type is the type AAP 0.4.3 names."""
    delivered: Mapping[str, object] = {
        "foundry_config": foundry_config,
        "foundry_profile": foundry_profile,
        "foundry_run_id": foundry_run_id,
        "foundry_artifacts": foundry_artifacts,
        "foundry_data": foundry_data,
        "foundry_db": foundry_db,
        "foundry_factory": foundry_factory,
        "foundry_clock": foundry_clock,
        "foundry_log_capture": foundry_log_capture,
        "foundry_poll": foundry_poll,
    }
    assert set(delivered) == set(_EXPECTED)
    assert {name: type(value) for name, value in delivered.items()} == dict(_contract_types())


@pytest.mark.parametrize("name", sorted(_EXPECTED))
def test_each_fixture_is_cached_under_the_scope_the_table_publishes(
    name: str, request: pytest.FixtureRequest
) -> None:
    """The fixture resolves; its declared and its effective scope are both the published one."""
    module_name, expected_scope = _EXPECTED[name]
    request.getfixturevalue(name)
    declared = getattr(__import__(module_name, fromlist=[name]), name)
    effective = request._fixturemanager.getfixturedefs(name, request.node)
    assert declared._fixture_function_marker.scope == expected_scope
    assert effective, f"{name} has no fixture definition for {request.node.nodeid}"
    assert effective[-1].scope == expected_scope
    assert sorted(_SESSION_SCOPED) == sorted(
        {"foundry_config", "foundry_profile", "foundry_run_id", "foundry_artifacts"}
    )
    assert sorted(_FUNCTION_SCOPED) == sorted(set(_EXPECTED) - set(_SESSION_SCOPED))


# --- foundry_run_id ------------------------------------------------------


def test_foundry_run_id_matches_the_published_pattern_and_names_the_run_directory(
    foundry_run_id: str, foundry_artifacts: ArtifactWriter, foundry_config: FoundryConfig
) -> None:
    """The id is ``YYYYmmddTHHMMSS-<6 hex>``, and it is the name of the run's artifact directory."""
    assert _RUN_ID_PATTERN.fullmatch(foundry_run_id) is not None
    assert foundry_artifacts.run_dir == (foundry_config.artifacts.dir / foundry_run_id).resolve()


# --- foundry_artifacts ---------------------------------------------------


def test_foundry_artifacts_writes_and_accounts_for_a_file_inside_the_run_directory(
    foundry_artifacts: ArtifactWriter, foundry_run_id: str
) -> None:
    """``dir_for`` creates the test's directory, ``write`` fills it and ``manifest`` lists it."""
    directory = foundry_artifacts.dir_for(_NODEID)
    assert directory.is_dir()
    assert directory.parent == foundry_artifacts.run_dir
    assert foundry_run_id in directory.parent.name
    writer = foundry_artifacts.for_node(_NODEID)
    reference = writer.write("request", None, "GET /echo")
    assert reference is not None
    written = foundry_artifacts.run_dir / reference.path
    assert written.is_file()
    assert written.read_text(encoding="utf-8") == "GET /echo"
    assert reference in writer.manifest
    assert len(reference.sha256) == 64


# --- foundry_config and foundry_profile ----------------------------------


def test_foundry_config_answers_for_a_key_and_the_profile_agrees(
    foundry_config: FoundryConfig, foundry_profile: str
) -> None:
    """A key's provenance is readable through ``source_of``; the profile is the resolved name."""
    source = foundry_config.source_of("http.timeout")
    assert source.label() == "default"
    assert foundry_config.http.timeout == 10.0
    assert foundry_profile == str(foundry_config.profile) == "default"


# --- foundry_clock ------------------------------------------------------


def test_foundry_clock_is_frozen_until_the_test_moves_it(foundry_clock: FoundryClock) -> None:
    """``now()`` repeats, ``advance()`` moves the instant and ``set()`` replaces it."""
    from foundry.fixtures.clock import DEFAULT_INSTANT

    assert foundry_clock.now() == DEFAULT_INSTANT
    assert foundry_clock.now() == foundry_clock.now()
    assert foundry_clock.advance(seconds=90) == DEFAULT_INSTANT.replace(minute=1, second=30)
    assert foundry_clock.now() == DEFAULT_INSTANT.replace(minute=1, second=30)
    replacement = datetime(2030, 6, 1, 12, 0, tzinfo=UTC)
    foundry_clock.set(replacement)
    assert foundry_clock.now() == replacement


# --- foundry_data -------------------------------------------------------


def test_foundry_data_reads_the_committed_cases_and_matrix(
    foundry_data: FoundryDataLoader,
) -> None:
    """``cases`` expands the tabular file; ``matrix`` the cross-product, both under ``data.dir``."""
    cases = foundry_data.cases("cases.csv")
    assert len(cases) == 12
    assert all(isinstance(case, Case) for case in cases)
    assert [case.id for case in cases][:2] == ["cases-0", "cases-1"]
    assert cases[0].values["case_id"] == "C-001"
    matrix = foundry_data.matrix("matrix.yaml")
    assert len(matrix) == 12
    assert len({case.id for case in matrix}) == 12


# --- foundry_db --------------------------------------------------------


@pytest.mark.parametrize("foundry_db", [_Row], indirect=True)
def test_foundry_db_serves_a_transactional_session_over_the_registered_model(
    foundry_db: Session,
) -> None:
    """The schema holds the requested table, and a committed row is visible to its own session."""
    foundry_db.add(_Row(id=1, name="first"))
    foundry_db.commit()
    assert [row.name for row in foundry_db.scalars(select(_Row))] == ["first"]


@pytest.mark.parametrize("foundry_db", [_Row], indirect=True)
def test_foundry_db_shows_none_of_the_previous_tests_rows(foundry_db: Session) -> None:
    """A second test's session is a fresh database, so nothing the first one wrote survives."""
    assert foundry_db.scalars(select(_Row)).all() == []


# --- foundry_factory ---------------------------------------------------


def test_foundry_factory_is_seeded_from_the_run_id_and_the_node_id(
    foundry_factory: FoundryFactory,
    foundry_run_id: str,
    request: pytest.FixtureRequest,
) -> None:
    """The seed is the published digest of ``run_id:nodeid``, and it reproduces the same draws."""
    expected = int.from_bytes(
        hashlib.sha256(f"{foundry_run_id}:{request.node.nodeid}".encode()).digest()[:8], "big"
    )
    assert foundry_factory.seed == expected
    drawn = foundry_factory.name()
    mirror = Faker()
    mirror.seed_instance(expected)
    assert mirror.name() == drawn
    assert repr(foundry_factory) == f"FoundryFactory(seed={expected})"


def test_foundry_factory_gives_a_different_test_a_different_value(
    foundry_factory: FoundryFactory,
    foundry_run_id: str,
    request: pytest.FixtureRequest,
) -> None:
    """Another node id seeds a different generator, so neither the seed nor the value repeats."""
    other = int.from_bytes(
        hashlib.sha256(f"{foundry_run_id}:{_OTHER_NODEID}".encode()).digest()[:8], "big"
    )
    assert request.node.nodeid != _OTHER_NODEID
    assert foundry_factory.seed != other
    mirror = Faker()
    mirror.seed_instance(other)
    assert foundry_factory.name() != mirror.name()


# --- foundry_log_capture -----------------------------------------------


def test_foundry_log_capture_records_what_the_foundry_logger_emitted(
    foundry_log_capture: FoundryLogCapture,
) -> None:
    """A record emitted on the ``foundry`` logger reaches both ``records`` and ``tail``."""
    foundry_log_capture.clear()
    logging.getLogger("foundry").warning(_LOG_MESSAGE)
    records = foundry_log_capture.records
    assert len(records) == 1
    assert records[0].getMessage() == _LOG_MESSAGE
    assert records[0].name == "foundry"
    tail = foundry_log_capture.tail(1)
    assert len(tail) == 1
    assert _LOG_MESSAGE in tail[0]
    assert foundry_log_capture.capacity > 0


# --- foundry_poll ------------------------------------------------------


def test_foundry_poll_returns_the_first_truthy_value(foundry_poll: FoundryPoll) -> None:
    """``eventually`` polls until the callable returns something truthy, and waits only as asked."""
    answers = iter([False, False, True])
    delays: list[float] = []
    ready = foundry_poll.eventually(
        lambda: next(answers), timeout=1.0, interval=0.1, sleep=delays.append
    )
    assert ready is True
    assert delays == [0.1, 0.1]


def test_foundry_poll_fails_once_the_deadline_passes(
    foundry_poll: FoundryPoll, foundry_clock: FoundryClock
) -> None:
    """A poller whose clock is driven by its sleeper expires without waiting for real time."""
    from foundry.fixtures.clock import DEFAULT_INSTANT

    def never() -> bool:
        """Report "not ready", which no number of polls can satisfy."""
        return False

    with pytest.raises(AssertionError) as raised:
        foundry_poll.eventually(
            never,
            timeout=0.5,
            interval=0.1,
            clock=foundry_clock,
            sleep=_advancing(foundry_clock),
        )
    assert "timed out after 0.5s" in str(raised.value)
    assert foundry_clock.now() > DEFAULT_INSTANT


def _advancing(clock: FoundryClock) -> Callable[[float], None]:
    """Return a sleeper that moves ``clock`` instead of spending the time it is asked to wait."""

    def sleep(seconds: float) -> None:
        """Advance the frozen clock by the interval the poller asked for."""
        clock.advance(seconds=seconds)

    return sleep
