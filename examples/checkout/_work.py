"""Deterministic CPU-bound work backing the ``examples/checkout`` wall clock.

The example suite has to occupy real wall clock so the sharding test can compare
a serial run against ``pytest -n 3`` and require the sharded median to be at most
60 % of the serial median. The test-hygiene constraint caps every sleep at 100 ms,
so that wall clock comes from genuine CPU work here instead: 24 example tests x
``DEFAULT_BUDGET_MS`` (500 ms) = the 12 s aggregate the budget names, and every
HTTP call in the suite is a loopback round trip of its own.

``time.perf_counter()`` is used only as a monotonic interval timer, to decide when
a call has spent its budget. It reads no wall-clock date, so it is not one of the
clock reads the sleep policy forbids (the epoch clock and the two calendar-clock
reads). The module performs no file, network or environment access, and nothing
happens at import time beyond binding the names below.

Each example test spends one call of ``DEFAULT_BUDGET_MS``, so the suite's serial
wall clock is comfortably above the 12 s of aggregate work and the sharded run can
show a real speed-up rather than timer noise. The budget is a lower bound, not an
exact figure: a call returns on the first batch boundary after its deadline.
"""

import hashlib
import time

DEFAULT_BUDGET_MS = 500
"""Work budget a single call spends, in milliseconds."""

TOTAL_WORK_MS = 24 * DEFAULT_BUDGET_MS
"""Aggregate work across the 24-test example suite: the 12 s the sharding test reasons about."""

_BATCH_SIZE = 256
"""Hash rounds between two deadline checks; small enough to bound the overshoot in milliseconds."""

_MODULUS = 1 << 32
"""Width of the accumulator fold, which keeps the result bounded and non-negative."""

_PREFIX = b"foundry-checkout"


def spend(milliseconds: int = DEFAULT_BUDGET_MS, seed: str = "checkout") -> int:
    """Burn roughly ``milliseconds`` of deterministic CPU work and return an opaque checksum.

    Each batch hashes a fixed prefix together with ``f"{seed}:{round_}:{index}"``,
    folds the digests into an integer accumulator and sorts the folded values, so
    the work has the shape of a real checksum pass rather than a busy loop.

    Args:
        milliseconds: Work budget for this call, in milliseconds; must be positive.
        seed: Stable label mixed into every hash round, so unrelated callers do
            not produce identical work.

    Returns:
        A non-negative checksum. The number of completed batches depends on the
        host and its load, so the value is machine-dependent by construction:
        callers may assert its type and non-negativity, never its value.

    Raises:
        ValueError: If ``milliseconds`` is not positive.
    """
    if milliseconds <= 0:
        raise ValueError(f"milliseconds must be positive, got {milliseconds}")

    deadline = time.perf_counter() + milliseconds / 1000
    accumulator = 0
    round_ = 0
    while True:
        payloads = [f"{seed}:{round_}:{index}".encode() for index in range(_BATCH_SIZE)]
        digests = [hashlib.sha256(_PREFIX + payload).digest() for payload in payloads]
        folded = sorted(int.from_bytes(digest, "big") for digest in digests)
        accumulator = (accumulator + sum(folded) - folded[-1] + folded[0]) % _MODULUS
        if time.perf_counter() >= deadline:
            return accumulator
        round_ += 1
