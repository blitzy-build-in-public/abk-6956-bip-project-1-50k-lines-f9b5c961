"""Exit-code vocabulary for the Foundry command line.

AAP 0.4.4 fixes what each code the command line publishes means and AAP 0.7.3 fixes
the mapping every CLI module applies:

- ``0`` (:data:`SUCCESS`) -- the command did what it was asked to do.
- ``1`` (:data:`FAILED`) -- tests failed, or ``foundry report compare`` found newly
  failing comparisons.
- ``2`` (:data:`USAGE_OR_CONFIG`) -- a usage or configuration error raised by Foundry
  itself, including one raised before pytest starts.
- ``3``, ``4`` and ``5`` (:data:`PYTEST_INTERNAL_ERROR`, :data:`PYTEST_USAGE_ERROR`,
  :data:`PYTEST_NO_TESTS`) -- pytest's own internal-error, usage-error and
  no-tests-collected codes, preserved exactly as ``foundry run`` received them.

Foundry maps its own failures to :data:`USAGE_OR_CONFIG` and never rewrites a code
pytest produced; :func:`passthrough` is where that intent is stated once for every
call site. The module holds constants and pure functions only: it imports nothing
from Foundry and nothing third-party, and performs no I/O, environment read, clock
read or logger configuration at import time, so any CLI module can import it freely.
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

    ``None`` -- a command that produced no status at all -- becomes
    :data:`SUCCESS`. An ``int`` subclass such as a ``bool`` or an ``IntEnum``
    (``pytest.ExitCode`` is one) is normalised to a plain :class:`int`. Every other
    value, an unknown or negative pytest code included, is returned unchanged rather
    than coerced: a code Foundry does not recognise is still pytest's, and it must
    reach the shell exactly as pytest produced it.

    Args:
        code: The exit code to publish, or ``None`` when none was produced.

    Returns:
        The exit code the process exits with.
    """
    if code is None:
        return SUCCESS
    return int(code)


def is_pytest_code(code: int) -> bool:
    """Report whether ``code`` is one of pytest's codes that Foundry preserves.

    Args:
        code: The exit code to classify.

    Returns:
        ``True`` for pytest's internal-error, usage-error and no-tests codes.
    """
    return code in PYTEST_PASSTHROUGH


def is_success(code: int) -> bool:
    """Report whether ``code`` means the command succeeded.

    Args:
        code: The exit code to classify.

    Returns:
        ``True`` only for :data:`SUCCESS`.
    """
    return code == SUCCESS


def describe(code: int) -> str:
    """Return the one-line meaning of ``code``.

    The wording is what a ``--help`` epilogue and a rendered CLI message print, so
    the caller never formats a bare number for a code the vocabulary documents. A
    code outside the vocabulary still gets usable text, ``"exit code <n>"``.

    Args:
        code: The exit code to describe.

    Returns:
        The documented meaning of a known code, otherwise ``"exit code <n>"``.
    """
    numeric = passthrough(code)
    return _DESCRIPTIONS.get(numeric, f"exit code {numeric}")
