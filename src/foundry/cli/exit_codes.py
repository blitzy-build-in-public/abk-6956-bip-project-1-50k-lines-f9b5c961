"""Exit-code vocabulary for the Foundry command line.

``0`` success, ``1`` failed tests or a newly failing comparison, ``2`` a usage or configuration
error raised by Foundry, and ``3``/``4``/``5`` pytest's own internal-error, usage-error and no-tests
codes, kept exactly as ``foundry run`` received them; Foundry never rewrites a code pytest produced.
Pure constants and functions only: no Foundry import, no I/O, no environment or clock read.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

__all__ = [
    "FAILED",
    "FOUNDRY_CODES",
    "INTERRUPT",
    "PYTEST_INTERNAL_ERROR",
    "PYTEST_NO_TESTS",
    "PYTEST_PASSTHROUGH",
    "PYTEST_USAGE_ERROR",
    "SUCCESS",
    "USAGE_OR_CONFIG",
    "describe",
    "is_pytest_code",
    "is_success",
    "passthrough",
]

# The command completed successfully.
SUCCESS: Final[int] = 0

# Tests failed, or ``foundry report compare`` reported newly failing comparisons.
FAILED: Final[int] = 1

# A usage or configuration error raised by Foundry itself.
USAGE_OR_CONFIG: Final[int] = 2

# pytest's own codes, which ``foundry run`` returns unchanged.
PYTEST_INTERNAL_ERROR: Final[int] = 3
PYTEST_USAGE_ERROR: Final[int] = 4
PYTEST_NO_TESTS: Final[int] = 5

# pytest's exit code for an interrupted run (``KeyboardInterrupt``). It coincides
# numerically with USAGE_OR_CONFIG, so no caller can tell an interrupt from a
# configuration fault by the number alone; Foundry keeps pytest's own code for an
# interrupt and never remaps it onto a code of its own (AAP 0.4.7, "KeyboardInterrupt"
# -- partial results are flushed and the report is written before the code is kept).
INTERRUPT: Final[int] = 2

# The pytest codes that pass through Foundry untouched.
PYTEST_PASSTHROUGH: Final[frozenset[int]] = frozenset(
    {PYTEST_INTERNAL_ERROR, PYTEST_USAGE_ERROR, PYTEST_NO_TESTS}
)

# The codes Foundry produces itself.
FOUNDRY_CODES: Final[frozenset[int]] = frozenset({SUCCESS, FAILED, USAGE_OR_CONFIG})

# Read-only so that the describe() vocabulary cannot be edited at runtime.
_DESCRIPTIONS: Final[Mapping[int, str]] = MappingProxyType(
    {
        SUCCESS: "success",
        FAILED: "failure",
        USAGE_OR_CONFIG: "usage or configuration error",
        PYTEST_INTERNAL_ERROR: "pytest internal error",
        PYTEST_USAGE_ERROR: "pytest usage error",
        PYTEST_NO_TESTS: "no tests collected",
    }
)


def passthrough(code: int | None) -> int:
    """Return ``code`` as the exit status this process publishes.

    ``None`` -- a command that produced no status at all -- becomes :data:`SUCCESS`, and an ``int``
    subclass such as a ``bool`` or a ``pytest.ExitCode`` is normalised to a plain :class:`int`.
    Every other value, an unknown or negative pytest code included, is returned unchanged rather
    than coerced: a code Foundry does not recognise is still pytest's, and it must reach the shell
    exactly as pytest produced it.
    """
    if code is None:
        return SUCCESS
    return int(code)


def is_pytest_code(code: int) -> bool:
    """Report whether ``code`` is one of pytest's internal-error, usage-error or no-tests codes."""
    return code in PYTEST_PASSTHROUGH


def is_success(code: int) -> bool:
    """Report whether ``code`` means the command succeeded; ``True`` only for :data:`SUCCESS`."""
    return code == SUCCESS


def describe(code: int) -> str:
    """Return the one-line meaning of ``code``.

    The wording is what a ``--help`` epilogue and a rendered CLI message print, so the caller never
    formats a bare number for a documented code. A code outside the vocabulary still gets usable
    text, ``"exit code <n>"``.
    """
    numeric = passthrough(code)
    return _DESCRIPTIONS.get(numeric, f"exit code {numeric}")
