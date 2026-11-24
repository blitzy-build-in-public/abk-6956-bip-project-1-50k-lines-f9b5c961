"""Record/replay for Foundry: the cassette document, its store, the matcher, the modes, transports.

``foundry.cassette`` owns record/replay (AAP 0.3.2): :mod:`foundry.cassette.models` defines the one
JSON document a cassette is, :mod:`foundry.cassette.store` reads and writes it,
:mod:`foundry.cassette.matcher` decides whether an outgoing request matches a recorded one,
:mod:`foundry.cassette.modes` states the four ``cassette.mode`` rows of AAP 0.7.3 together with the
per-test session that applies one, :mod:`foundry.cassette.paths` names where a cassette file lives,
and :mod:`foundry.cassette.transport` supplies the two ``httpx`` transports a cassette run is built
from. This module is the barrel naming them, so :mod:`foundry.http` and :mod:`foundry.fixtures`
import one surface rather than reach into a module's internals (AAP 0.8.2).

Four rules hold in every module published here, each named with the AAP section that fixes it:

1. **Replay opens no socket, by construction** (AAP 0.2.1, AAP 0.3.3, acceptance criterion 8).
   :class:`ReplayTransport` is an ``httpx.BaseTransport`` handed a document and a path and never a
   second transport, so ``httpcore`` is not entered and no replay path reaches ``socket.connect``.
2. **Redaction happens at capture** (implicit requirement I4, AAP 0.3.2). :meth:`CassetteStore.save`
   scrubs a denied header name or JSON key through the one redaction engine before the document is
   serialised, because a cassette is a file that reaches a repository.
3. **Documents are deterministic** (AAP 0.13.1, assumption A7). Keys and ``redacted_keys`` are
   sorted, and a match carries neither host nor port, so one recording replays identically on any
   machine and behind any port.
4. **File writes have one funnel** (AAP 0.4.8). :mod:`foundry.cassette.store` is one of only three
   modules in the tree permitted to write a file, and this package imports :mod:`foundry.core` and
   :mod:`foundry.obs` and nothing else of Foundry.

One deliberate departure from the permitted-import column of AAP 0.4.8: that column also allows
:mod:`foundry.contract`, and no module here imports it, because no cassette value is validated
against a JSON Schema. The document shape is enforced by
:meth:`foundry.cassette.models.CassetteDocument.from_dict`, which raises
:class:`foundry.core.exceptions.FoundryError` naming the offending key.

Importing this module defines names only (AAP 0.4.8): it opens no file, reads no environment
variable, configures no logger, parses no argument and builds no module-level state. ``httpx`` is
loaded, through :mod:`foundry.cassette.transport` where it is imported by design, and nothing on
``foundry.plugin``'s import path reaches this package. Every name is re-exported explicitly and
:data:`__all__` states the surface, so a name that disappears shows up as a reviewable diff
(AAP 0.4.9). Nothing else lives here: ``foundry.cassette.matcher.describe`` and
``foundry.cassette.transport.fingerprint_from_request`` are used inside the package and stay
reachable from the module that owns them.
"""

# Every import below is written as ``X as X``, so the name is a re-export under ``mypy --strict``'s
# no-implicit-reexport and a used import under ruff's F401. Each carries the one fact a caller needs
# that the sibling module's own name does not give at a glance.

# A match is decided by this and nothing else: method, path, sorted query pairs, body hash (A7).
from .matcher import RequestFingerprint as RequestFingerprint

# Picks the recorded interaction a fingerprint matches, advancing the replay cursor as it goes.
from .matcher import match_interaction as match_interaction

# The one document a cassette file is: ``recorded_at``, ``redacted_keys``, ordered interactions.
from .models import CassetteDocument as CassetteDocument

# One exchange: the request as stored, paired with the reply it produced.
from .models import Interaction as Interaction

# What replay compares: no host, no port, no headers, and the body only as a SHA-256.
from .models import RecordedRequest as RecordedRequest

# The stored reply: status, headers and body, with every denied header value already replaced.
from .models import RecordedResponse as RecordedResponse

# AAP 0.7.3's mode table as data: one immutable row per ``cassette.mode`` value.
from .modes import BEHAVIOURS as BEHAVIOURS

# What one mode does, as the five flags the session builds its transport from.
from .modes import CassetteBehaviour as CassetteBehaviour

# One test's node id, mode and store: builds the transport, saves the capture at teardown.
from .modes import CassetteSession as CassetteSession

# Looks a mode name up in the table, raising FoundryError for a name outside the four.
from .modes import behaviour_for as behaviour_for

# The ``cassette.dir`` default, which exists before any configuration layer is resolved.
from .paths import DEFAULT_CASSETTE_DIR as DEFAULT_CASSETTE_DIR

# Where one node id's cassette lives: the sanitised file name under the cassette directory.
from .paths import cassette_path as cassette_path

# The only filesystem edge in the package: ``load`` and ``save`` one document per node id.
from .store import CassetteStore as CassetteStore

# The tag value for a reply the cassette answered, which is what a replay's ``source`` reads.
from .transport import SOURCE_CASSETTE as SOURCE_CASSETTE

# The ``httpx.Response.extensions`` key holding that tag, read by ``foundry.http.response``.
from .transport import SOURCE_EXTENSION_KEY as SOURCE_EXTENSION_KEY

# The tag value for a reply the peer answered, including one a ``passthrough`` replay delegated.
from .transport import SOURCE_LIVE as SOURCE_LIVE

# Wraps the live transport and captures what passed through it, tagging every reply it returns.
from .transport import RecordingTransport as RecordingTransport

# Serves one document's interactions and holds no live transport, so replay opens no socket.
from .transport import ReplayTransport as ReplayTransport

__all__ = (
    "BEHAVIOURS",
    "CassetteBehaviour",
    "CassetteDocument",
    "CassetteSession",
    "CassetteStore",
    "DEFAULT_CASSETTE_DIR",
    "Interaction",
    "RecordedRequest",
    "RecordedResponse",
    "RecordingTransport",
    "ReplayTransport",
    "RequestFingerprint",
    "SOURCE_CASSETTE",
    "SOURCE_EXTENSION_KEY",
    "SOURCE_LIVE",
    "behaviour_for",
    "cassette_path",
    "match_interaction",
)
