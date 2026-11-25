"""The HTML writer's hardening of the path from a record to a file (AAP 0.4.7).
A document that reaches outside itself and a rendering fault outside ``jinja2.TemplateError`` both
take the documented fallback -- no file, one ``WARNING`` on ``foundry`` -- while a renderable page
inlines its two asset constants raw, escapes a record value and imports nothing undeclared."""

import ast
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from foundry.reporting import assets, html
from foundry.reporting.record import FailureRecord, Outcome, ResultRecord

_QUOTED_URL = 'AssertionError: see href="https://example.invalid/x" for details'
_RECORD = ResultRecord(
    run_id="20250102T030405-abc123",
    started_at=datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC),
    finished_at=datetime(2025, 1, 2, 3, 4, 5, 250_000, tzinfo=UTC),
    profile="ci",
    nodeid="tests/test_cart.py::test_total",
    outcome=Outcome.PASSED,
    attempts=1,
    duration_ms=12.5,
)
_UNRENDERABLE = (
    (
        '<link rel="stylesheet" href="https://cdn.example.invalid/report.css">',
        "cdn.example.invalid/report.css",
    ),
    ("<script src='//cdn.example.invalid/x.js'></script>", "cdn.example.invalid/x.js"),
    ("{{ ctx.run_id + 1 }}", "failed: TypeError:"),
    ("{{ ctx.run_id.index('zzz') }}", "failed: ValueError:"),
    ("{% macro loop() %}{{ loop() }}{% endmacro %}{{ loop() }}", "failed: RecursionError:"),
)
_IDS = ["absolute-href", "protocol-relative-src", "type-error", "value-error", "recursion-error"]


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("foundry") and record.levelno >= logging.WARNING
    ]


def _imports(nodes: list[ast.AST]) -> set[str]:
    return {
        (node.module if isinstance(node, ast.ImportFrom) else node.names[0].name).split(".")[0]
        for node in nodes
        if isinstance(node, (ast.Import, ast.ImportFrom)) and node.names
    }


@pytest.mark.parametrize(("body", "named"), _UNRENDERABLE, ids=_IDS)
def test_an_unrenderable_document_takes_the_fallback(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, body: str, named: str
) -> None:
    (tmp_path / "foreign.j2").write_text(body, encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="foundry"):
        document = html.render([_RECORD], tmp_path / "foreign.j2")
    warnings = _warnings(caplog)
    assert document is None and len(warnings) == 1
    assert "report.html not written" in warnings[0] and named in warnings[0]


def test_a_renderable_document_inlines_its_assets_and_escapes_a_record_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    failures = (FailureRecord(phase="call", message=_QUOTED_URL),)
    record = _RECORD.model_copy(update={"outcome": Outcome.FAILED, "failures": failures})
    with caplog.at_level(logging.WARNING, logger="foundry"):
        document = html.render([record])
    assert document is not None and _warnings(caplog) == []
    assert document.split("<style>", 1)[1].split("</style>", 1)[0] == assets.CSS
    assert document.split("<script>", 1)[1].split("</script>", 1)[0] == assets.JS
    assert "href=&#34;https://example.invalid/x&#34;" in document


def test_the_renderer_imports_no_undeclared_dependency(repo_root: Path) -> None:
    tree = ast.parse((repo_root / "src/foundry/reporting/html.py").read_text(encoding="utf-8"))
    assert "markupsafe" not in _imports(list(ast.walk(tree)))
    assert _imports(list(ast.walk(tree))) - set(sys.stdlib_module_names) - {"foundry"} == {"jinja2"}
    assert "jinja2" not in _imports(tree.body)
