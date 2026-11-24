"""The public assertion helpers: status, JSON containment, JSON Schema and polling.

``foundry.assertions`` is the import surface of Foundry's assertion layer, and the four names in
:data:`__all__` are the whole of it (AAP 0.4.3):

* :func:`assert_status` compares a response's status code against the code or codes a test
  accepts and, on a miss, reports the received status, the expected set and a redacted,
  200-byte excerpt of the body.
* :func:`assert_json_subset` proves that a decoded JSON value contains the expected fragment,
  naming the first mismatching JSON path.
* :func:`assert_json_schema` validates a value against a JSON Schema and reports the first
  violation's ``json_path``.
* :func:`eventually` polls a callable until it returns something truthy, measuring the deadline
  on the monotonic clock -- or on a supplied clock -- and waiting through an injected ``sleep``.

Two failure types, and only those two, cross these functions (AAP 0.4.3, AAP 0.4.7):
``AssertionError`` from the three assertion helpers, from a misused argument and from an expired
``eventually`` deadline, and :class:`foundry.core.exceptions.ContractError` from a schema
violation. Their names, parameter shapes and behaviour are fixed for ``0.1.0`` and may only be
added to (AAP 0.4.9).

The package imports :mod:`foundry.core`, :mod:`foundry.obs` and :mod:`foundry.contract` and
nothing above them (AAP 0.4.8). Importing it binds names only: no I/O, no environment read, no
logger configuration, no argument parsing and no module-level mutable state. Every message a
helper raises is scrubbed through :mod:`foundry.obs.redaction` and bounded by
:data:`~foundry.obs.redaction.MAX_EXCERPT_BYTES`, so a value that reaches a terminal,
``results.json``, ``junit.xml`` or ``report.html`` cannot carry a configured secret.

Importing this package imports its four modules, so it must stay free of third-party imports:
:mod:`jsonschema`, :mod:`yaml`, ``httpx``, ``pydantic`` and ``Jinja2`` are imported lazily inside
the functions that need them, which is what keeps Foundry's plugin startup inside its 150 ms
budget (implicit requirement I5).
"""

from __future__ import annotations

from foundry.assertions.eventually import eventually as eventually
from foundry.assertions.json_schema import assert_json_schema as assert_json_schema
from foundry.assertions.json_subset import assert_json_subset as assert_json_subset
from foundry.assertions.status import assert_status as assert_status

__all__ = [
    "assert_json_schema",
    "assert_json_subset",
    "assert_status",
    "eventually",
]
