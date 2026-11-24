"""Unit tests for the HTML writer's hardening of the path from a record to a file.

The finished document, not only the constant asset bundle, is what a reader opens offline, so:

* cr1-f02 -- a rendered ``href`` or ``src`` pointing outside the document takes the documented
  fallback: ``None``, one ``WARNING`` on the ``foundry`` logger, no ``report.html`` (AAP 0.4.6,
  AAP 0.4.7, implicit requirement I10). Only attribute values are read, so a failure message that
  quotes a URL is not a false positive.
* cr1-f07 -- no package ``pyproject.toml`` does not declare is imported (AAP 0.5.1), while the two
  asset constants still reach the page unescaped through the template's ``safe`` filter and a record
  value stays escaped (AAP 0.4.6).
* cr1-f08 -- a rendering fault outside :class:`jinja2.TemplateError` becomes one ``WARNING`` rather
  than an exception out of ``pytest_sessionfinish`` (AAP 0.1.2, AAP 0.14.3).

Determinism and isolation: fixed run id, node ids and instants; every foreign template under
``tmp_path``; nothing sleeps, nothing touches the network, no plugin is activated (AAP 0.14.3).
"""

from __future__ import annotations

import ast
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from foundry.reporting import assets, html
from foundry.reporting.record import FailureRecord, Outcome, ResultRecord

_LOGGER = "foundry"
"""The one logger Foundry logs on; the documented fallback is a ``WARNING`` record on it."""

_RUN_ID = "20250102T030405-abc123"
_PROFILE = "ci"
_STARTED_AT = datetime(2025, 1, 2, 3, 4, 5, 0, tzinfo=UTC)
_FINISHED_AT = datetime(2025, 1, 2, 3, 4, 5, 250_000, tzinfo=UTC)
_NODEID = "tests/test_cart.py::test_total"

_SCRIPT_NODEID = "tests/test_cart.py::test_<script>alert(1)</script>"
_QUOTED_URL_MESSAGE = 'AssertionError: see href="https://example.invalid/x" for details'
"""A failure message quoting an ``href``, the false positive an attribute-level scan avoids."""

_EXTERNAL_TEMPLATES: tuple[tuple[str, str, str], ...] = (
    (
        "href",
        '<link rel="stylesheet" href="https://cdn.example.invalid/report.css">',
        "https://cdn.example.invalid/report.css",
    ),
    (
        "protocol_relative_src",
        "<script src='//cdn.example.invalid/x.js'></script>",
        "//cdn.example.invalid/x.js",
    ),
)
"""One foreign template per external shape: an absolute href and a protocol-relative src."""

_RENDERING_FAULTS: tuple[tuple[str, str], ...] = (
    ("TypeError", "{{ ctx.run_id + 1 }}"),
    ("ValueError", "{{ ctx.run_id.index('zzz') }}"),
    ("RecursionError", "{% macro loop() %}{{ loop() }}{% endmacro %}{{ loop() }}"),
)
"""One template per family a filter or a value raises outside ``jinja2.TemplateError``."""


def _record(**overrides: object) -> ResultRecord:
    """Return a passing result record whose every field the caller did not override is fixed."""
    fields: dict[str, object] = {
        "run_id": _RUN_ID,
        "started_at": _STARTED_AT,
        "finished_at": _FINISHED_AT,
        "profile": _PROFILE,
        "nodeid": _NODEID,
        "outcome": Outcome.PASSED,
        "attempts": 1,
        "duration_ms": 12.5,
    }
    fields.update(overrides)
    return ResultRecord.model_validate(fields)


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return the ``WARNING`` lines the ``foundry`` logger emitted, and nothing else."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name.startswith(_LOGGER) and record.levelno >= logging.WARNING
    ]


def _template(tmp_path: Path, name: str, body: str) -> Path:
    """Write ``body`` as the report template ``name`` under ``tmp_path`` and return its path."""
    path = tmp_path / f"{name}.j2"
    path.write_text(body, encoding="utf-8")
    return path


def _element_text(document: str, tag: str) -> str:
    """Return the text between the first ``<tag>`` and the next ``</tag>``."""
    return document.split(f"<{tag}>", 1)[1].split(f"</{tag}>", 1)[0]


# --- cr1-f02: the writer rejects a document that reaches outside itself -----


@pytest.mark.parametrize(("name", "body", "reference"), _EXTERNAL_TEMPLATES)
def test_a_document_that_reaches_outside_itself_takes_the_fallback(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, name: str, body: str, reference: str
) -> None:
    """An external href or src costs ``report.html`` and logs exactly one warning naming it."""
    template = _template(tmp_path, name, body)
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        document = html.render([_record()], template)
    warnings = _warnings(caplog)
    assert document is None
    assert len(warnings) == 1
    assert "report.html not written" in warnings[0]
    assert reference in warnings[0]


def test_the_shipped_template_reaches_nothing_outside_itself(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The shipped template passes the same scan, so its render logs no warning at all."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        document = html.render([_record()])
    assert document is not None
    assert _warnings(caplog) == []


def test_a_failure_message_quoting_an_attribute_is_not_an_external_reference(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Autoescape escapes the message's quotes, so a quoted URL is text and not an attribute."""
    record = _record(
        outcome=Outcome.FAILED,
        failures=(FailureRecord(phase="call", message=_QUOTED_URL_MESSAGE),),
    )
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        document = html.render([record])
    assert document is not None
    assert _warnings(caplog) == []
    assert "href=&#34;https://example.invalid/x&#34;" in document


# --- cr1-f08: every rendering fault takes the same fallback ----------------


@pytest.mark.parametrize(("name", "body"), _RENDERING_FAULTS)
def test_a_rendering_fault_outside_the_template_family_is_isolated(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, name: str, body: str
) -> None:
    """A TypeError, ValueError or RecursionError is one warning and no document, never an escape."""
    template = _template(tmp_path, name, body)
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        document = html.render([_record()], template)
    warnings = _warnings(caplog)
    assert document is None
    assert len(warnings) == 1
    assert f"failed: {name}:" in warnings[0]


# --- cr1-f07: the closed dependency list, and what the template marks safe --


def _imported_modules(tree: ast.Module, *, module_scope_only: bool = False) -> set[str]:
    """Return the top-level module names ``tree`` imports, at module scope or anywhere in it."""
    nodes: list[ast.AST] = list(tree.body) if module_scope_only else list(ast.walk(tree))
    names: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_the_renderer_imports_no_undeclared_dependency(repo_root: Path) -> None:
    """MarkupSafe is gone, Jinja2 is the only third-party import, and it stays function-scoped."""
    module = repo_root / "src" / "foundry" / "reporting" / "html.py"
    tree = ast.parse(module.read_text(encoding="utf-8"))
    imported = _imported_modules(tree)
    third_party = {
        name for name in imported if name not in sys.stdlib_module_names and name != "foundry"
    }
    assert "markupsafe" not in imported
    assert third_party == {"jinja2"}
    assert "jinja2" not in _imported_modules(tree, module_scope_only=True)


def test_the_assets_inline_raw_while_a_record_value_stays_escaped() -> None:
    """The ``safe`` filter keeps the constants raw; a node id carrying markup is escaped."""
    document = html.render([_record(nodeid=_SCRIPT_NODEID)])
    assert document is not None
    assert _element_text(document, "style") == assets.CSS
    assert _element_text(document, "script") == assets.JS
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in document
    assert _SCRIPT_NODEID not in document
