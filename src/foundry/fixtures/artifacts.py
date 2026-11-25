"""The ``foundry_run_id`` and ``foundry_artifacts`` fixtures: one identity and one artifact tree.

Both are session scoped, because a run has exactly one identity and one artifact tree:
``foundry_run_id`` returns the ``YYYYmmddTHHMMSS-<6 hex>`` identifier of the session, and
``foundry_artifacts`` returns that run's :class:`FoundryArtifacts` writer, whose ``dir_for``,
``write`` and ``manifest`` members are the published contract.

**The layout.** One directory per collected item, named by its percent-encoded node id, so parallel
workers never contend for a file::

    artifacts/<run_id>/<sanitized-nodeid>/request.json, response.json, stdout.log, stderr.log,
                                          traceback.txt, log_tail.log, manifest.json

``artifacts.dir`` roots the tree and ``artifacts.keep_passed`` decides whether a passing test keeps
its files or only ``manifest.json``. The run id is generated here, through
:func:`foundry.core.ids.run_id`, because ``foundry.fixtures`` may not import the plugin's session
stash, and the plugin's own bootstrap uses the same function.

**Failure isolation.** An artifacts directory that cannot be written disables capture for the run:
the writer reports the fault once at ``WARNING`` on the ``foundry`` logger, every later ``write``
returns ``None``, and no test outcome or exit code changes. A path escaping the run directory still
surfaces as :class:`foundry.core.exceptions.ArtifactError`, and no directory is created eagerly, so
an unwritable directory stays observable.

**Lazily imported.** ``foundry.plugin`` imports this module during ``pytest_configure`` at every
pytest start, inside the 150 ms budget, so module scope imports ``pytest``, the standard library and
nothing else of Foundry: the Foundry imports sit inside the fixture that first needs them, and
annotation names are imported under ``TYPE_CHECKING``.
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
    per-test half, addressing the test the writer is bound to, so a session-scoped writer that has
    not been bound raises rather than guessing.
    """

    def dir_for(self, nodeid: str) -> Path:
        """Return the artifact directory of one test, creating it on first use.

        Args:
            nodeid: The pytest node id, unsanitised; the writer scrubs and percent-encodes it, so
                the directory is one component inside the run directory and never a path the node
                id could steer elsewhere.

        Returns:
            The resolved absolute directory, ``artifacts/<run_id>/<sanitized-nodeid>/``.

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
        """Redact one payload and write it as an artifact of the bound test.

        Args:
            kind: Which artifact this is, one of the seven values of
                :data:`foundry.core.types.ArtifactKind`; it selects the file name ``name`` does not.
            name: An extra artifact's relative path inside the test's directory, or ``None`` to use
                the fixed file name the kind has. A blank, absolute or ``..`` name is rejected.
            data: The payload, as text or as bytes decoded as UTF-8 with replacement.

        Returns:
            The reference to the file written -- its kind, run-relative ``path``, ``bytes`` and
            ``sha256`` -- or ``None`` when capture is disabled or the write failed, which keeps a
            capture fault out of the test's outcome.

        Raises:
            foundry.core.exceptions.ArtifactError: The writer is not bound to a test, the name is
                not a file inside the test's directory, or the target path escapes the run
                directory.
            foundry.core.exceptions.FoundryError: ``kind`` has no file name and ``name`` is
                ``None``, so there is no file to write.
        """
        ...

    @property
    def manifest(self) -> tuple[ArtifactRef, ...]:
        """Return the artifacts written for the bound test, sorted by kind and path.

        Returns:
            One reference per file this writer wrote for its test, in artifact order, or ``()``
            before anything was written and for a writer bound to no test. The ordering is what
            keeps two runs byte-identical.
        """
        ...


@pytest.fixture(scope="session")
def foundry_run_id() -> str:
    """Return this session's run id, ``YYYYmmddTHHMMSS-<6 hex>``.

    Session scoped, so every artifact of a session lands under the one ``artifacts/<run_id>/``
    directory and the id a test reads is the id that names that directory. The value comes from
    :func:`foundry.core.ids.run_id` and is checked against the published pattern before it is
    returned; the six random hex digits and the UTC timestamp make it the only non-deterministic
    value this module produces.

    Returns:
        The run id, for example ``20250102T030405-9f3c1a``.

    Raises:
        foundry.core.exceptions.ArtifactError: The generated value does not match the published
            pattern, which is a defect in Foundry rather than in the suite under test.
    """
    from foundry.core.exceptions import ArtifactError
    from foundry.core.ids import is_valid_run_id, run_id

    value = run_id()
    if not is_valid_run_id(value):
        # No run directory exists yet, so both path attributes carry the offending id: it is the
        # value a reader has to inspect, and inventing a directory would name a place never used.
        raise ArtifactError(
            f"foundry_run_id generated a value that is not a run id: {value!r}",
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
    """Return the artifact writer for this session's ``artifacts/<run_id>/`` tree.

    Session scoped: one writer per run, holding the root the run's per-test directories hang from.
    It is built from the resolved configuration and the run id alone -- no clock reading, no
    filesystem access -- so constructing it is free and no directory exists until a test asks for
    one, which keeps the unwritable-directory state observable rather than raised.

    Redaction is wired in twice on purpose: ``secrets.redact_keys`` widens the header and query
    deny-lists :class:`foundry.obs.redaction.Redactor` always applies, and the writer scrubs every
    payload, file name and node id it writes through that engine, so a configured secret has no
    route onto the disk, into ``manifest.json`` or into a report.

    Returns:
        The run's writer, typed as :class:`FoundryArtifacts`.

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
