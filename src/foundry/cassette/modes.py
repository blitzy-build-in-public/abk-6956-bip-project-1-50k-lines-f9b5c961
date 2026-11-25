"""The four record/replay modes, and the per-test session that applies one of them.

:data:`BEHAVIOURS` states AAP 0.7.3's mode table as data, one immutable row per ``cassette.mode``
value, and :class:`CassetteSession` binds a node id, a mode and a store per test (AAP 0.4.6).
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

    One boolean per column of AAP 0.7.3's table, so the session decides what to build from data
    rather than from a chain of mode comparisons. Each row is built once, at import, and is
    immutable.

    Attributes:
        name: The mode's published name, spelled as ``cassette.mode`` spells it.
        reads: Whether the test's cassette is loaded at session start.
        writes: Whether the capture is saved at teardown, overwriting the file.
        network: Whether a live transport exists for the mode at all.
        miss_raises: Whether a request with no recorded counterpart raises ``CassetteMissError``.
        falls_through: Whether such a request is delegated to the live transport instead.
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

| mode | Read | Write | Network | Miss behaviour |
|---|---|---|---|---|
| `off` | no | no | live transport | not applicable |
| `record` | no | yes, overwriting at teardown | live transport | not applicable |
| `replay` | yes | no | none (never `httpcore`) | `CassetteMissError` naming method and path |
| `passthrough` | yes | no | unmatched requests only | falls through to the live transport |

A :class:`~types.MappingProxyType` over a dictionary built at import, so every caller reads the same
table and none can extend or edit it.
"""


def behaviour_for(mode: str) -> CassetteBehaviour:
    """Return the row of the mode table for a ``cassette.mode`` value, however it is spelled.

    Case and surrounding whitespace are not part of a mode's identity, so a value from
    ``FOUNDRY_CASSETTE__MODE=REPLAY`` names the same mode as ``replay``. The configuration layer
    validates the enum before this module sees it.

    Raises:
        FoundryError: ``mode`` names none of the four modes. The message quotes the value and lists
        the
            accepted four, so it is safe to print.
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

    One wording serves both the constructor that validates the caller's argument and the builder
    that uses it. ``replay`` never reaches here: it builds its transport from the document alone
    (AAP 0.2.1).

    Raises:
        FoundryError: No transport was offered. The message names the mode that required one.
    """
    if live_transport is None:
        raise FoundryError(
            f"cassette mode {behaviour.name!r} needs a live transport", hint=_NO_LIVE_HINT
        )
    return live_transport


# --- The per-test session --------------------------------------------------


class CassetteSession:
    """One test's cassette lifecycle: the transport its mode calls for, and the file it saves.

    It binds the node id, the mode and the store, and answers which transport to send on, what was
    captured, whether to write it and how to let go of it. ``replay`` holds no live transport
    whatever the caller passes, so offline replay is a property of this object, and the transport is
    built once, on first use.
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

        A blank node id, a mode the table does not name and a mode with a network given no transport
        each raise ``FoundryError``.
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

        The build happens once, so a replay reads its document one time per test and the cursor it
        holds advances across that test's requests. A mode that needs the caller's transport is
        handed the very object the caller offered, wrapped only by ``record``.

        Returns:
            The transport to send requests on: the caller's own in ``off``, a ``RecordingTransport``
            around it in ``record``, and a ``ReplayTransport`` in ``replay`` and ``passthrough``.

        Raises:
            FoundryError: The cassette cannot be read, or is not a valid recording.
            ArtifactError: The resolved cassette path escapes the cassette directory.
        """
        if self._transport is None:
            self._transport = self._build_transport()
        return self._transport

    def _build_transport(self) -> httpx.BaseTransport:
        """Build the transport the mode calls for, once, as :meth:`transport` caches it.

        The run's redaction engine travels with the store into both transports, so the query value a
        ``record`` run captures and the one a ``replay`` or ``passthrough`` run looks up are masked
        by the same engine and compare equal (AAP 0.2.1, AAP 0.3.2).
        """
        name = self._behaviour.name
        if name == "off":
            return _live_transport_for(self._behaviour, self._live_transport)
        if name == "record":
            return RecordingTransport(
                _live_transport_for(self._behaviour, self._live_transport),
                self._record,
                redactor=self._store.redactor,
            )
        document = self._store.load(self._nodeid)
        path = cassette_path(self._nodeid, cassette_dir=self._store.cassette_dir)
        if name == "replay":
            # No fallback: a reply this document does not hold raises the typed miss, so nothing in
            # the replay path can reach httpcore or a socket (AAP 0.2.1, acceptance criterion 8).
            return ReplayTransport(document, path, redactor=self._store.redactor)
        return ReplayTransport(
            document,
            path,
            fallback=_live_transport_for(self._behaviour, self._live_transport),
            redactor=self._store.redactor,
        )

    def _record(self, interaction: Interaction) -> None:
        """Keep one captured exchange for the save at teardown, in arrival order.

        The list belongs to this instance and never to the module, so two tests cannot see each
        other's exchanges (AAP 0.4.8). Redaction happens in the store, on the way to disk.
        """
        self._interactions.append(interaction)

    def session_requests(self) -> tuple[Interaction, ...]:
        """Return the exchanges captured so far, in the order the test issued them.

        An immutable snapshot, so a caller may report what a ``record`` run collected without being
        able to alter what :meth:`finish` will write.
        """
        return tuple(self._interactions)

    def finish(self) -> Path | None:
        """Save the capture as this test's cassette, at most once, and return the file written.

        Only a writing mode that recorded something saves, so a ``record`` run whose test issued no
        request leaves any previous document in place. A capture fault does not raise (AAP 0.1.2,
        AAP 0.14.3): the store reports it as one ``WARNING`` and answers ``None``.
        """
        if self._saved or not self._behaviour.writes or not self._interactions:
            return None
        self._saved = True
        return self._store.save(self._nodeid, tuple(self._interactions))

    def close(self) -> None:
        """Close the transport this session built, once, and never one it did not build.

        A network mode's transport closes the live transport it was handed, so a fixture hands
        ownership of its transport to the session it creates. A session never used, and one already
        closed, both do nothing, which is what lets a teardown run this unconditionally.
        """
        if self._closed:
            return
        self._closed = True
        if self._transport is not None:
            self._transport.close()
