"""The four record/replay modes, and the per-test session that applies one of them.

``foundry.cassette`` owns record/replay (AAP 0.3.2); this module holds the policy. AAP 0.7.3 fixes
the mode table and :data:`BEHAVIOURS` states it once, as data, so no caller re-derives it from
scattered flags:

* ``off`` -- no cassette is read, none is written, and every request goes to the live transport
  exactly as it would with no Foundry in the path. The default, and where a suite that has not
  adopted record/replay runs.
* ``record`` -- no cassette is read, the live transport answers every request, and the exchanges
  that passed through it are written as one cassette document for the test at teardown. Recording
  overwrites that document, so a cassette always describes one run.
* ``replay`` -- the cassette is read, nothing is written, and no live transport exists at all: the
  session hands :class:`~foundry.cassette.transport.ReplayTransport` a document and a path and never
  a second transport, so no request can reach ``httpcore`` or ``socket.connect`` (AAP 0.2.1,
  acceptance criterion 8). A request the cassette does not hold raises
  :class:`~foundry.core.exceptions.CassetteMissError` naming its method, its path and the document.
* ``passthrough`` -- the cassette is read and nothing is ever written (assumption A8). A request the
  cassette holds is served from it; one it does not hold is delegated to the live transport, which
  makes this the mode for keeping a few exchanges recorded while still talking to the real peer.

``off`` and ``passthrough`` are not the same mode: each has a live transport, and they differ in
whether the cassette is read at all. ``passthrough`` never opens a socket for a request the
recording can answer; ``off`` never consults a recording even when one sits on disk.

One session per test, one cassette per session
    A cassette belongs to one test (AAP 0.4.6), so :class:`CassetteSession` binds a node id, a mode
    and a store per test, builds the transport that mode calls for on first use, accumulates what
    was captured and saves it at teardown. That unit is what lets parallel workers each write their
    own document, and it is what the ``record`` row of the table cannot do without.

Import direction and import-time discipline (AAP 0.4.8)
    :mod:`foundry.cassette.paths`, :mod:`foundry.cassette.store`, :mod:`foundry.cassette.transport`
    and :mod:`foundry.core.exceptions`; never :mod:`foundry.config`, :mod:`foundry.http`,
    :mod:`foundry.reporting` or :mod:`foundry.execution`. The mode arrives as a plain string and the
    cassette directory as the store's own attribute, as :class:`~foundry.obs.redaction.Redactor`
    receives ``secrets.redact_keys`` as a plain iterable of names. ``httpx`` and
    :class:`~foundry.cassette.models.Interaction` are imported under :data:`typing.TYPE_CHECKING`,
    so the annotations name no runtime dependency past the transports composed here. Importing the
    module does no I/O, reads no environment variable, configures no logger and builds nothing
    mutable: :data:`BEHAVIOURS` is a read-only view created once.

Failure isolation (AAP 0.1.2, AAP 0.14.3)
    :meth:`CassetteSession.finish` never raises for a capture fault: it hands the interactions to
    :meth:`~foundry.cassette.store.CassetteStore.save`, which reports a redaction or write fault as
    one ``WARNING`` on the ``foundry`` logger and answers ``None``. The errors this module raises of
    its own are :class:`~foundry.core.exceptions.FoundryError` for a mode name or a transport the
    caller got wrong -- programming errors, not capture faults.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal

from foundry.cassette.paths import cassette_path
from foundry.cassette.store import CassetteStore
from foundry.cassette.transport import RecordingTransport, ReplayTransport
from foundry.core.exceptions import FoundryError

if TYPE_CHECKING:
    import httpx

    from foundry.cassette.models import Interaction

__all__: Final[tuple[str, ...]] = (
    "BEHAVIOURS",
    "CassetteBehaviour",
    "CassetteModeName",
    "CassetteSession",
    "behaviour_for",
)

CassetteModeName = Literal["off", "record", "replay", "passthrough"]
"""The four values ``cassette.mode`` accepts, as :class:`foundry.config.enums.CassetteMode` spells
them. Declared here as the key type of :data:`BEHAVIOURS`, so a row and a mode name cannot drift."""

_ACCEPTED_MODES: Final[str] = "off, record, replay, passthrough"
"""The accepted values, in the order the mode table lists them, for the unknown-mode message."""

_BLANK_NODEID_HINT: Final[str] = "pass item.nodeid, which names the cassette and is never blank"
"""The next step a blank-node-id error offers, as the store's path resolution offers it."""

_NO_LIVE_HINT: Final[str] = (
    "pass the transport requests are sent on, or run with cassette.mode=replay"
)
"""The next step a missing-live-transport error offers: replay is the mode that needs none."""


# --- The mode table --------------------------------------------------------


@dataclass(frozen=True)
class CassetteBehaviour:
    """What one ``cassette.mode`` value does, as the five questions the session asks of it.

    The five flags are the whole table of AAP 0.7.3, one boolean per column, so the session decides
    what to build from data rather than from a chain of mode comparisons. A behaviour is immutable
    and each row is built once at import, so a caller holding one cannot change what the run does.

    Attributes:
        name: The mode's published name, spelled as ``cassette.mode`` spells it.
        reads: Whether the test's cassette is loaded at session start -- ``replay`` and
            ``passthrough``.
        writes: Whether the capture is saved at teardown -- ``record`` alone, overwriting the file.
        network: Whether a live transport exists for the mode at all -- false for ``replay``, which
            is what makes a replay's offline behaviour structural (AAP 0.2.1).
        miss_raises: Whether a request with no recorded counterpart raises
            :class:`~foundry.core.exceptions.CassetteMissError`.
        falls_through: Whether such a request is delegated to the live transport instead; never true
            together with :attr:`miss_raises`.
    """

    name: CassetteModeName
    reads: bool
    writes: bool
    network: bool
    miss_raises: bool
    falls_through: bool


BEHAVIOURS: Final[Mapping[CassetteModeName, CassetteBehaviour]] = MappingProxyType(
    {
        "off": CassetteBehaviour(
            name="off",
            reads=False,
            writes=False,
            network=True,
            miss_raises=False,
            falls_through=False,
        ),
        "record": CassetteBehaviour(
            name="record",
            reads=False,
            writes=True,
            network=True,
            miss_raises=False,
            falls_through=False,
        ),
        "replay": CassetteBehaviour(
            name="replay",
            reads=True,
            writes=False,
            network=False,
            miss_raises=True,
            falls_through=False,
        ),
        "passthrough": CassetteBehaviour(
            name="passthrough",
            reads=True,
            writes=False,
            network=True,
            miss_raises=False,
            falls_through=True,
        ),
    }
)
"""The four rows of the mode table (AAP 0.7.3), keyed by the name ``cassette.mode`` carries.

A :class:`~types.MappingProxyType` over a dictionary built at import, so every caller reads the same
table and none can extend or edit it. ``record`` is the only row that writes, ``replay`` the only
row with no live transport, and ``passthrough`` the only row that reads a cassette and still falls
through to the network -- which is exactly what separates it from ``off`` (assumption A8).
"""


def behaviour_for(mode: str) -> CassetteBehaviour:
    """Return the row of the mode table for a ``cassette.mode`` value, however it is spelled.

    Case and surrounding whitespace are not part of a mode's identity, so a value that arrived from
    ``FOUNDRY_CASSETTE__MODE=REPLAY`` or from a hand-edited ``foundry.toml`` names the same mode as
    ``replay``. The configuration layer validates the enum before this module sees it, so a value
    that names no row came from a caller that built the mode itself.

    Args:
        mode: The mode name, for example ``replay`` or ``" PASSTHROUGH"``.

    Returns:
        The immutable behaviour of that mode.

    Raises:
        FoundryError: ``mode`` names none of the four modes. The message quotes the value and lists
            the accepted four, so it is safe to print.
    """
    normalised = mode.strip().casefold()
    for name, behaviour in BEHAVIOURS.items():
        if name == normalised:
            return behaviour
    raise FoundryError(
        f"unknown cassette mode {mode.strip()!r}: expected one of {_ACCEPTED_MODES}",
        hint="check cassette.mode in foundry.toml, or the --foundry-record/--foundry-replay option",
    )


def _live_transport_for(
    behaviour: CassetteBehaviour, live_transport: httpx.BaseTransport | None
) -> httpx.BaseTransport:
    """Return ``live_transport``, or raise because ``behaviour`` cannot work without one.

    One wording serves both places a live transport is demanded -- the constructor that validates
    the caller's argument and the builder that uses it -- so a session is never told two different
    things about the same missing argument. ``replay`` never reaches here: it builds its transport
    from the document alone (AAP 0.2.1).

    Args:
        behaviour: The row of the mode table the session runs under.
        live_transport: The transport the caller offered, or ``None``.

    Returns:
        The same transport, so the check can stand where the value is consumed.

    Raises:
        FoundryError: No transport was offered. The message names the mode that required one, so a
            silent no-network session cannot become a confusing miss at the first request.
    """
    if live_transport is None:
        raise FoundryError(
            f"cassette mode {behaviour.name!r} needs a live transport", hint=_NO_LIVE_HINT
        )
    return live_transport


# --- The per-test session --------------------------------------------------


class CassetteSession:
    """One test's cassette lifecycle: the transport its mode calls for, and the file it saves.

    A cassette belongs to one test (AAP 0.4.6), so the object that owns it is created per test. The
    session binds the node id, the mode and the store, and answers what the record and replay paths
    ask: which transport to send on (:meth:`transport`), what was captured
    (:meth:`session_requests`), whether to write it (:meth:`finish`) and how to let go of it
    (:meth:`close`).

    Which transport a mode gets is the table of AAP 0.7.3
        ``off`` returns the live transport itself, unwrapped and with no cassette read. ``record``
        wraps that transport in :class:`~foundry.cassette.transport.RecordingTransport` and reads
        nothing, because a recording run replaces the document rather than extending it. ``replay``
        builds :class:`~foundry.cassette.transport.ReplayTransport` from the loaded document and the
        path its miss error names, with no fallback, so nothing in it can open a socket.
        ``passthrough`` builds the same transport with the live transport as its fallback, and never
        writes back (assumption A8).

    The transport is built once, on first use
        :meth:`transport` builds on the first call and returns the same object afterwards, so a
        replay loads its document once per test -- which is why its cursor advances across that
        test's requests. The build is lazy because a test that issues no request should not have a
        document read on its behalf.

    A replay session holds no live transport, whatever the caller passes
        Offline replay (AAP 0.2.1) is then a property of the object rather than of the argument: in
        ``replay`` the offered transport is not kept, so no later call can reach it, and a caller
        that still needs its own transport closes it itself. In a network mode the session takes
        ownership and :meth:`close` closes it.

    Attributes:
        _nodeid: The pytest node id of the test whose cassette this is.
        _behaviour: The row of :data:`BEHAVIOURS` the session runs under.
        _store: The store the document is loaded from and saved to.
        _live_transport: The transport the caller offered, kept only for a network mode.
        _transport: The transport :meth:`transport` built, or ``None`` before the first call.
        _interactions: The exchanges captured so far, in the order the test issued them.
        _saved: Whether :meth:`finish` has already attempted its one write.
        _closed: Whether :meth:`close` has already run, which is what makes it idempotent.
    """

    _nodeid: str
    _behaviour: CassetteBehaviour
    _store: CassetteStore
    _live_transport: httpx.BaseTransport | None
    _transport: httpx.BaseTransport | None
    _interactions: list[Interaction]
    _saved: bool
    _closed: bool

    def __init__(
        self,
        nodeid: str,
        *,
        mode: str,
        store: CassetteStore,
        live_transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Bind the session to a test, a mode and a store, and demand what the mode needs.

        A live transport is required by every mode whose row of the table has a network -- ``off``,
        ``record`` and ``passthrough`` -- and refused as absent here, because a session that quietly
        had none would fail at the first request with a miss that describes the cassette rather than
        the real fault. ``replay`` needs none and keeps none.

        Args:
            nodeid: The pytest node id of the test. It names the cassette document, so a blank one
                is refused here rather than at teardown, when the outcome is already decided.
            mode: The resolved ``cassette.mode``, as a plain string: the configuration layer owns
                the enum and this module never imports it (AAP 0.4.8).
            store: The store holding the cassette directory and the redaction engine.
            live_transport: The transport requests are sent on. Required by ``off``, ``record`` and
                ``passthrough``; ignored by ``replay``, which holds no live transport at all.

        Raises:
            FoundryError: ``nodeid`` is blank, ``mode`` names no mode of the table, or a mode with a
                network was given no transport. The message says which.
        """
        if not nodeid.strip():
            raise FoundryError(
                "cassette session needs the node id of the test it records",
                hint=_BLANK_NODEID_HINT,
            )
        self._behaviour = behaviour_for(mode)
        self._nodeid = nodeid
        self._store = store
        # A replay keeps no live transport even when one is offered, so "replay opens nothing" is a
        # property of this object rather than of the caller's argument (AAP 0.2.1).
        self._live_transport = (
            _live_transport_for(self._behaviour, live_transport)
            if self._behaviour.network
            else None
        )
        self._transport = None
        self._interactions = []
        self._saved = False
        self._closed = False

    @property
    def behaviour(self) -> CassetteBehaviour:
        """The row of the mode table this session runs under, for a caller that reports it."""
        return self._behaviour

    def transport(self) -> httpx.BaseTransport:
        """Return the transport this session's mode calls for, building it on first use.

        The build happens once: the object made here is kept and returned by every later call, so a
        replay reads its document one time per test and the cursor it holds advances across that
        test's requests. A mode that needs the caller's transport is handed the very object the
        caller offered, wrapped only by ``record``.

        Returns:
            The transport to send requests on: the caller's own in ``off``, a
            :class:`~foundry.cassette.transport.RecordingTransport` around it in ``record``, and a
            :class:`~foundry.cassette.transport.ReplayTransport` in ``replay`` and ``passthrough``.

        Raises:
            FoundryError: The cassette named by this session's node id cannot be read, or is not a
                valid recording, which the store reports for ``replay`` and ``passthrough``.
            ArtifactError: The resolved cassette path escapes the cassette directory.
        """
        if self._transport is None:
            self._transport = self._build_transport()
        return self._transport

    def _build_transport(self) -> httpx.BaseTransport:
        """Build the transport the mode calls for, once, as :meth:`transport` caches it."""
        name = self._behaviour.name
        if name == "off":
            return _live_transport_for(self._behaviour, self._live_transport)
        if name == "record":
            return RecordingTransport(
                _live_transport_for(self._behaviour, self._live_transport), self._record
            )
        document = self._store.load(self._nodeid)
        path = cassette_path(self._nodeid, cassette_dir=self._store.cassette_dir)
        if name == "replay":
            # No fallback: a reply this document does not hold raises the typed miss, so nothing in
            # the replay path can reach httpcore or a socket (AAP 0.2.1, acceptance criterion 8).
            return ReplayTransport(document, path)
        return ReplayTransport(
            document, path, fallback=_live_transport_for(self._behaviour, self._live_transport)
        )

    def _record(self, interaction: Interaction) -> None:
        """Keep one captured exchange for the save at teardown, in arrival order.

        The list belongs to this instance and never to the module, so two tests cannot see each
        other's exchanges (AAP 0.4.8). This is the sink
        :class:`~foundry.cassette.transport.RecordingTransport` calls, and it cannot fail: the
        capture is redacted by the store on the way to disk, not here.
        """
        self._interactions.append(interaction)

    def session_requests(self) -> tuple[Interaction, ...]:
        """Return the exchanges captured so far, in the order the test issued them.

        An immutable snapshot of the recording in progress, so a caller may report what a ``record``
        run has collected -- for an artifact, a log line, or a test asserting its own traffic --
        without being able to alter what :meth:`finish` will write.
        """
        return tuple(self._interactions)

    def finish(self) -> Path | None:
        """Save the capture as this test's cassette, at most once, and return the file written.

        A session writes only when its mode writes and something was recorded: an ``off``,
        ``replay`` or ``passthrough`` run saves nothing (assumption A8), and a ``record`` run whose
        test issued no request leaves any previous document in place rather than replacing it with
        an empty one.

        A capture fault does not raise (AAP 0.1.2, AAP 0.14.3):
        :meth:`~foundry.cassette.store.CassetteStore.save` reports a redaction failure or an
        unwritable directory as one ``WARNING`` on the ``foundry`` logger and answers ``None``, so a
        cassette that could not be recorded never changes a test's outcome or the run's exit code.
        The attempt is recorded before the save runs, so a teardown that calls this twice performs
        one write, however that write ended.

        Returns:
            The path the document was written to, or ``None`` when nothing was written.

        Raises:
            FoundryError: The session's node id is blank, which the store's path resolution refuses.
            ArtifactError: The resolved cassette path escapes the cassette directory.
        """
        if self._saved or not self._behaviour.writes or not self._interactions:
            return None
        self._saved = True
        return self._store.save(self._nodeid, tuple(self._interactions))

    def close(self) -> None:
        """Close the transport this session built, once, and never one it did not build.

        A network mode's transport closes the live transport it was handed -- that is what ``httpx``
        transports do -- so a fixture hands ownership of its transport to the session it creates. A
        session whose :meth:`transport` was never called, and a session already closed, both do
        nothing, which is what lets a teardown run this unconditionally.
        """
        if self._closed:
            return
        self._closed = True
        if self._transport is not None:
            self._transport.close()
