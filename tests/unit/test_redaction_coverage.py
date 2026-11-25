"""Redaction's coverage floor: every credential shape loses its value wherever it stands, while a
location, a node id and a deny-list read as written under the pass that must keep them.
"""

from __future__ import annotations

from typing import Final

import pytest

from foundry.obs.redaction import REDACTED, default_redactor

_SECRET: Final[str] = "coverage-floor-3d17"

_MASKING: Final[tuple[str, ...]] = (
    "connecting with password: $s",
    "request failed: password: $s",
    "- password: $s",
    "E   AssertionError: token: $s",
    "[('token','$s')]",
    "('x-api-key','$s')",
    "dict_items([('password','$s')])",
    'http://h/x?token="$s"',
    "token=$s",
    "authorization: Bearer $s",
    "Cookie: sid=$s",
    '{"password": "$s"}',
    'name="token"\r\n\r\n$s\r\n--X--\r\n',
    "['token', '$s']",
    '{"query": [["token", "$s"]]}',
    '    token = os.environ.get("QA_BEARER_TOKEN", "$s")',
    'foo("QA_BEARER_TOKEN", "$s")',
    "redact_keys=['authorization', '$s']",
    'tokens=["$s"]',
)

_VERBATIM: Final[tuple[str, ...]] = (
    "test_auth_login.py:42: AssertionError: bad",
    "tests/test_token_refresh.py::test_x failed",
    "test_secret.py:16: AssertionError",
    "'set-cookie', 'cookie', 'x-api-key'",
    "['authorization', 'set-cookie', 'cookie', 'x-api-key']",
    "['authorization', 'cookie']",
    "redact_keys=['authorization', 'cookie']",
    "SecretsConfig(redact_keys=['authorization', 'set-cookie', 'cookie', 'x-api-key'])",
)

# AAP 0.4.5 diagnostics reach the operator, so the key, its closest match and its range survive.
_DIAGNOSTIC: Final[tuple[str, ...]] = (
    "unknown configuration key: zzzz_misspelled",
    "unknown configuration key: http.timeot",
    "invalid value for option --foundry-http.auth.strategy: expected none, bearer, basic, header",
    "invalid value for http.timeout: -1 (expected 0 < t <= 300)",
)

_MASKED_DIAGNOSTIC: Final[tuple[str, ...]] = (
    "token=$s",
    "authorization: Bearer $s",
    "http://u:$s@h/x",
    "http://h/x?token=$s",
)


@pytest.mark.parametrize("shape", _MASKING)
def test_every_credential_shape_loses_its_value(shape: str) -> None:
    text = shape.replace("$s", _SECRET)
    scrubbed = default_redactor().text(text)
    assert _SECRET not in scrubbed, f"the value survived: {scrubbed!r}"
    assert REDACTED in scrubbed
    assert default_redactor().text(scrubbed) == scrubbed, "a second pass must change nothing"


@pytest.mark.parametrize("shape", _VERBATIM)
def test_a_location_and_a_deny_list_read_as_written(shape: str) -> None:
    assert default_redactor().text(shape) == shape


@pytest.mark.parametrize("shape", _DIAGNOSTIC)
def test_a_configuration_diagnostic_reads_through_the_diagnostic_pass(shape: str) -> None:
    assert default_redactor().diagnostic(shape) == shape


@pytest.mark.parametrize("shape", _MASKED_DIAGNOSTIC)
def test_the_diagnostic_pass_still_masks_every_value(shape: str) -> None:
    text = shape.replace("$s", _SECRET)
    scrubbed = default_redactor().text(text)
    assert default_redactor().diagnostic(text) == scrubbed
    assert _SECRET not in scrubbed
