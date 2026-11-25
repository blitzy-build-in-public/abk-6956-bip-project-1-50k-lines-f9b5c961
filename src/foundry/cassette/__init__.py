"""Record/replay for Foundry: the cassette document, store, matcher, modes and transports.

The barrel of AAP 0.3.2 and AAP 0.8.2: replay opens no socket, redaction happens at capture.
"""

# Every import below is written as ``X as X``, so the name is a re-export under ``mypy --strict``'s
# no-implicit-reexport and a used import under ruff's F401. Each carries the one fact a caller needs
# that the sibling module's own name does not give at a glance.

# A match is decided by this and nothing else: method, path, sorted query pairs, body hash (A7).
from .matcher import RequestFingerprint as RequestFingerprint

# Picks the recorded interaction a fingerprint matches, advancing the replay cursor as it goes; the
# second name is the spelling earlier releases shipped, kept so an existing import keeps working.
from .matcher import match as match
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
    "match",
    "match_interaction",
)
