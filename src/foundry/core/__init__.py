"""The foundation package: the vocabulary every other Foundry package speaks.

It defines the exception hierarchy a module raises across a boundary, the phase, artifact-kind
and quarantine vocabularies, node id encoding and path containment, and the id and timestamp
formats. It is the stable import root and the stdlib-only foundation rung, so importing it
defines names only: no I/O, no environment read, no logger configuration (AAP 0.4.8).
"""

from typing import Final

# The sole definition site of every exception type Foundry raises across a boundary.
from foundry.core.exceptions import ArtifactError as ArtifactError
from foundry.core.exceptions import CassetteMissError as CassetteMissError
from foundry.core.exceptions import ConfigError as ConfigError
from foundry.core.exceptions import ContractError as ContractError
from foundry.core.exceptions import DataError as DataError
from foundry.core.exceptions import FoundryError as FoundryError
from foundry.core.exceptions import RetryExhaustedError as RetryExhaustedError
from foundry.core.exceptions import TransportError as TransportError

# Run id, data-driven case ids and the ISO 8601 timestamp formats.
from foundry.core.ids import RUN_ID_FORMAT as RUN_ID_FORMAT
from foundry.core.ids import RUN_ID_PATTERN as RUN_ID_PATTERN
from foundry.core.ids import RUN_ID_TOKEN_HEX_DIGITS as RUN_ID_TOKEN_HEX_DIGITS
from foundry.core.ids import case_id_for_matrix as case_id_for_matrix
from foundry.core.ids import case_id_for_row as case_id_for_row
from foundry.core.ids import is_valid_run_id as is_valid_run_id
from foundry.core.ids import parse_iso8601 as parse_iso8601
from foundry.core.ids import run_id as run_id
from foundry.core.ids import to_iso8601 as to_iso8601
from foundry.core.ids import utc_now as utc_now

# Node id encoding, and the containment rule every per-test path is resolved through.
from foundry.core.paths import MAX_NODEID_NAME_LENGTH as MAX_NODEID_NAME_LENGTH
from foundry.core.paths import artifact_dir_for as artifact_dir_for
from foundry.core.paths import relative_artifact_path as relative_artifact_path
from foundry.core.paths import resolve_under as resolve_under
from foundry.core.paths import sanitize_nodeid as sanitize_nodeid

# Shared phase, artifact-kind and quarantine vocabulary, and the injectable protocols.
from foundry.core.types import ARTIFACT_KINDS as ARTIFACT_KINDS
from foundry.core.types import PHASES as PHASES
from foundry.core.types import QUARANTINE_STATES as QUARANTINE_STATES
from foundry.core.types import ArtifactKind as ArtifactKind
from foundry.core.types import AttemptRecord as AttemptRecord
from foundry.core.types import ClockLike as ClockLike
from foundry.core.types import HttpResponseLike as HttpResponseLike
from foundry.core.types import Phase as Phase
from foundry.core.types import QuarantineState as QuarantineState
from foundry.core.types import Sleep as Sleep

__all__: Final[tuple[str, ...]] = (
    "ARTIFACT_KINDS",
    "ArtifactError",
    "ArtifactKind",
    "AttemptRecord",
    "CassetteMissError",
    "ClockLike",
    "ConfigError",
    "ContractError",
    "DataError",
    "FoundryError",
    "HttpResponseLike",
    "MAX_NODEID_NAME_LENGTH",
    "PHASES",
    "Phase",
    "QUARANTINE_STATES",
    "QuarantineState",
    "RUN_ID_FORMAT",
    "RUN_ID_PATTERN",
    "RUN_ID_TOKEN_HEX_DIGITS",
    "RetryExhaustedError",
    "Sleep",
    "TransportError",
    "artifact_dir_for",
    "case_id_for_matrix",
    "case_id_for_row",
    "is_valid_run_id",
    "parse_iso8601",
    "relative_artifact_path",
    "resolve_under",
    "run_id",
    "sanitize_nodeid",
    "to_iso8601",
    "utc_now",
)
