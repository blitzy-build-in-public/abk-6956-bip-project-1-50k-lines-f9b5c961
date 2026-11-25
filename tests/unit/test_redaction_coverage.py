"""The free-text engine's coverage floor: every credential shape masks, every diagnostic reads.

AAP 0.14.3 makes "no secret reaches a file, the terminal or a report" a hard constraint, and AAP
0.4.5 makes an unknown key, its closest match and a key's accepted range part of a diagnostic, so
this module pins both halves of the line the engine walks: a ``name: value`` pair, a quoted pair
list entry, a tuple repr, a URL query value and a logged credential lose the value wherever they
stand, while a file location, a node id and a rendered deny-list read as written. The engine's own
rules are ``tests/unit/test_redaction_shapes.py``'s; this module holds the regression table a
narrowing has to keep passing.
"""

from __future__ import annotations

from typing import Final

import pytest

from foundry.obs.redaction import REDACTED, default_redactor

_SECRET: Final[str] = "coverage-floor-3d17"
"""A value no redacted form may contain, distinctive enough that a match is a real leak."""


def _masked(shape: str) -> tuple[str, str]:
    """Return ``shape`` with its sentinel spliced in, and its scrubbed form."""
    text = shape.replace("$s", _SECRET)
    return text, default_redactor().text(text)


@pytest.mark.parametrize(
    "shape",
    [
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
    ],
)
def test_every_credential_shape_loses_its_value(shape: str) -> None:
    """A pair, a quoted run, a tuple repr, a URL query value and a header line all mask."""
    text, scrubbed = _masked(shape)

    assert _SECRET not in scrubbed, f"the value survived: {scrubbed!r}"
    assert REDACTED in scrubbed
    assert default_redactor().text(scrubbed) == scrubbed, "a second pass must change nothing"


@pytest.mark.parametrize(
    "shape",
    [
        "test_auth_login.py:42: AssertionError: bad",
        "tests/test_token_refresh.py::test_x failed",
        "test_secret.py:16: AssertionError",
        "'set-cookie', 'cookie', 'x-api-key'",
        "['authorization', 'set-cookie', 'cookie', 'x-api-key']",
        "['authorization', 'cookie']",
        "redact_keys=['authorization', 'cookie']",
        "SecretsConfig(redact_keys=['authorization', 'set-cookie', 'cookie', 'x-api-key'])",
    ],
)
def test_a_location_and_a_published_deny_list_read_as_written(shape: str) -> None:
    """AAP 0.4.6 and 0.4.5: a file location and a rendered deny-list are not credentials."""
    assert default_redactor().text(shape) == shape


@pytest.mark.parametrize(
    "shape",
    [
        "unknown configuration key: zzzz_misspelled",
        "unknown configuration key: http.timeot",
        "invalid value for option --foundry-http.auth.strategy: "
        "expected none, bearer, basic, header",
        "invalid value for http.timeout: -1 (expected 0 < t <= 300)",
    ],
)
def test_a_configuration_diagnostic_reads_through_the_diagnostic_pass(shape: str) -> None:
    """AAP 0.4.5: the key, its closest match and its accepted range reach the operator."""
    assert default_redactor().diagnostic(shape) == shape


@pytest.mark.parametrize(
    "shape",
    ["token=$s", "authorization: Bearer $s", "http://u:$s@h/x", "http://h/x?token=$s"],
)
def test_the_diagnostic_pass_still_masks_every_value(shape: str) -> None:
    """A diagnostic is prose, not a licence: the credential shapes it can carry still mask."""
    text, scrubbed = _masked(shape)

    assert default_redactor().diagnostic(text) == scrubbed
    assert _SECRET not in scrubbed
