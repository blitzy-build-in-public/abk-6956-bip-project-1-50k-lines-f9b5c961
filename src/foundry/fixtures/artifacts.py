"""The ``foundry_run_id`` and ``foundry_artifacts`` fixtures: one identity and one artifact tree.

Both are session scoped, because a run has exactly one identity and one artifact tree:
``foundry_run_id`` returns the ``YYYYmmddTHHMMSS-<6 hex>`` identifier of the session, and
``foundry_artifacts`` returns that run's :class:`FoundryArtifacts` writer, whose ``dir_for``,
``write`` and ``manifest`` members are the published contract. One directory per collected item is
named by its percent-encoded node id, so parallel workers never contend for a file::

    artifacts/<run_id>/<sanitized-nodeid>/request.json, response.json, stdout.log, stderr.log,
                                          traceback.txt, log_tail.log, manifest.json

``artifacts.dir`` roots the tree and ``artifacts.keep_passed`` decides whether a passing test keeps
its files or only ``manifest.json``. The run id is the plugin's -- read out of ``config.stash``
through the typed key :data:`foundry.obs.artifacts.SESSION_RUN_ID` -- so the fixture, the terminal
banner and the directory name one value (AAP 0.4.3); one is minted through
:func:`foundry.core.ids.run_id` only where no session state exists, which keeps a suite registering
this module without the plugin working. That key lives in ``foundry.obs`` because
``foundry.fixtures`` may not import ``foundry.execution`` (AAP 0.4.8).

**Failure isolation.** An artifacts directory that cannot be written disables capture for the run:
the writer reports the fault once at ``WARNING`` on the ``foundry`` logger, its later writes return
``None``, and no test outcome or exit code changes. A path escaping the run directory still surfaces
as :class:`foundry.core.exceptions.ArtifactError`, and no directory is created eagerly. Module scope
imports only ``pytest`` and the standard library inside the 150 ms budget, Foundry imports sitting
inside the fixture that first needs them.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import pytest

if TYPE_CHECKING:
    from foundry.config.models import FoundryConfig
    from foundry.core.types import ArtifactKind
    from foundry.obs.artifacts import ArtifactRef

__all__ = ["FoundryArtifacts", "foundry_artifacts", "foundry_run_id"]


class FoundryArtifacts(Protocol):
    """The three published members of the artifact writer ``foundry_artifacts`` returns.

    :class:`foundry.obs.artifacts.ArtifactWriter` satisfies them structurally, so a test can
    annotate its parameter with this protocol without importing :mod:`foundry.obs`, exactly as
    :class:`foundry.core.types.ClockLike` does for the poller. ``dir_for`` is the session-scoped
    half: it answers with the directory of any collected item. ``write`` and ``manifest`` are the
    per-test half: they address the test that is running, so a test body writes its own evidence and
    reads back its own artifacts without naming a node id, and a call with no test running raises
    rather than guessing a directory.
    """

    def dir_for(self, nodeid: str) -> Path:
        """Return the resolved ``artifacts/<run_id>/<sanitized-nodeid>/`` of one test, creating it.

        Args:
            nodeid: The pytest node id, unsanitised; the writer percent-encodes it without redacting
                it, so the directory is one component inside the run directory and never a path the
                node id could steer elsewhere.

        Raises:
            foundry.core.exceptions.FoundryError: The node id is empty or whitespace-only.
            foundry.core.exceptions.ArtifactError: The derived path escapes the run directory, which
                a symlink inside it can cause.
        """
        ...

    def write(
        self,
        kind: ArtifactKind,
        name: str | None,
        data: str | bytes,
    ) -> ArtifactRef | None:
        """Redact one payload and write it as an artifact of the test that is running.

        Its reference comes back -- kind, run-relative ``path``, ``bytes`` and ``sha256`` -- or
        ``None`` when capture is disabled or the write failed, which keeps a capture fault out of
        the outcome.

        Args:
            kind: Which artifact this is, one of the seven :data:`foundry.core.types.ArtifactKind`
                values; it selects the file name when ``name`` is ``None``.
            name: An extra artifact's relative path inside the test's directory, or ``None`` for the
                fixed file name the kind has. A blank, absolute or ``..`` name is rejected.
            data: The payload, as text or as bytes decoded as UTF-8 with replacement.

        Raises:
            foundry.core.exceptions.ArtifactError: No test is capturing artifacts, so there is no
                per-test directory to write into; ``dir_for(nodeid)`` and ``for_node(nodeid)``
                address one test's artifacts from outside a test body. Also raised when the name is
                not a file inside the test's directory, or the target path escapes the run
                directory.
            foundry.core.exceptions.FoundryError: ``kind`` has no file name and ``name`` is
                ``None``, so there is no file to write.
        """
        ...

    @property
    def manifest(self) -> tuple[ArtifactRef, ...]:
        """Return the artifacts written for the test that is running, sorted by kind and path.

        Returns:
            One reference per file this run wrote for the test in flight, in artifact order, or
            ``()`` before anything was written and for a run whose capture is disabled. The ordering
            is what keeps two runs byte-identical.

        Raises:
            foundry.core.exceptions.ArtifactError: No test is capturing artifacts, so no test's
                manifest can be named.
        """
        ...


@pytest.fixture(scope="session")
def foundry_run_id(request: pytest.FixtureRequest) -> str:
    """Return this session's run id, for example ``20250102T030405-9f3c1a``.

    Session scoped, so every artifact of a session lands under the one ``artifacts/<run_id>/``
    directory and the id a test reads is the id that names that directory. The value is the id
    the plugin bootstrapped for the session -- the one its terminal banner prints, its records
    carry and its writer builds the run directory from -- so a suite that reads it names the run
    its documents describe; only a session that registered this module without that bootstrap
    mints one through :func:`foundry.core.ids.run_id`. Either way it is checked against the
    published pattern ``YYYYmmddTHHMMSS-<6 hex>`` before it is returned, the six random digits
    and the UTC timestamp making it the only non-deterministic value this module produces.

    Args:
        request: The session's fixture request; ``request.config.stash`` holds the run id the plugin
            stored under :data:`foundry.obs.artifacts.SESSION_RUN_ID` during ``pytest_configure``.

    Raises:
        foundry.core.exceptions.ArtifactError: The value does not match the published pattern, which
            is a defect in Foundry rather than in the suite under test.
    """
    from foundry.core.exceptions import ArtifactError
    from foundry.core.ids import is_valid_run_id, run_id
    from foundry.obs.artifacts import SESSION_RUN_ID

    value = request.config.stash.get(SESSION_RUN_ID, None)
    if value is None:
        value = run_id()
    if not is_valid_run_id(value):
        # No run directory exists yet, so both path attributes carry the offending id: it is the
        # value a reader has to inspect, and inventing a directory would name a place never used.
        raise ArtifactError(
            f"foundry_run_id resolved a value that is not a run id: {value!r}",
            attempted_path=Path(value),
            run_dir=Path(value),
            hint="a run id is a UTC stamp of the form YYYYmmddTHHMMSS and six lowercase hex digits",
        )
    return value


@pytest.fixture(scope="session")
def foundry_artifacts(
    foundry_config: FoundryConfig,
    foundry_run_id: str,
) -> FoundryArtifacts:
    """Return the run's writer, typed as :class:`FoundryArtifacts`, for its artifact tree.

    Session scoped: one writer per run, holding the root the run's per-test directories hang from.
    It is built from the resolved configuration and the run id alone -- no clock reading, no
    filesystem access -- so constructing it is free and no directory exists until a test asks for
    one, which keeps the unwritable-directory state observable rather than raised. ``dir_for``
    addresses any collected item by node id. ``write`` and ``manifest`` answer for the test that is
    running: called from a test body they write into that test's own directory, so the reference
    also appears in its ``manifest.json`` and in the record's ``artifacts[]``, and in a run whose
    capture is disabled they record nothing. Called with no test running at all they raise
    :class:`foundry.core.exceptions.ArtifactError` naming that, rather than guessing a directory.
    Redaction is wired in twice on purpose: ``secrets.redact_keys`` widens the header and query
    deny-lists :class:`foundry.obs.redaction.Redactor` always applies, and the writer scrubs the
    payload text of every artifact it writes, so a configured secret has no route onto the disk,
    into ``manifest.json`` or into a report; a node id is percent-encoded and never redacted.

    Raises:
        foundry.core.exceptions.ArtifactError: The run directory joined with the run id would escape
            ``artifacts.dir``, or the run id is blank. Deliberately not caught here: a path
            containment fault must surface.
    """
    from foundry.obs.artifacts import ArtifactWriter
    from foundry.obs.redaction import Redactor

    redactor = Redactor(foundry_config.secrets.redact_keys)
    return ArtifactWriter(
        foundry_config.artifacts.dir,
        run_id=foundry_run_id,
        profile=str(foundry_config.profile),
        redactor=redactor,
        keep_passed=foundry_config.artifacts.keep_passed,
    )
