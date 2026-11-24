"""The resolved configuration: one frozen pydantic tree over the 28 published keys.

AAP 0.4.5 fixes Foundry's configuration surface -- 28 leaf keys, each with a type, a default and an
accepted range -- and this module is that surface as a value: :class:`FoundryConfig` with ten
nested models beneath it, one per ``[section]`` of ``foundry.toml``. Adding, renaming or removing a
key breaks a published surface (AAP 0.4.9), so the field list below and the registry in
:mod:`foundry.config.keys` are the same 28 keys.

Resolution is *not* here: :mod:`foundry.config.merge` applies the layers of AAP 0.4.5, records a
:class:`foundry.config.sources.ConfigSource` per key, and this model validates the result. No
validator reads a file, an environment variable or a clock, so equal inputs stay equal.

A rejected value is pydantic's own ``ValidationError``: the wording that names the key and its range
belongs to :func:`foundry.config.validation.translate_validation_error`, which ends the session with
exit code 2 (assumption A4). The one ``ConfigError`` raised here is
:meth:`FoundryConfig.source_of`'s refusal of an unregistered key. Every credential key stores the
*name* of the environment variable holding a secret, never its value (requirement I9); the numeric
defaults and bounds come from :mod:`foundry.core.limits`, the ranges are ``Field(gt=..., le=...)``
rather than the deprecated ``confloat``/``conint`` as AAP 0.7.2 permits, and nothing on the plugin
startup path imports this module at module scope (requirement I5).
"""

from __future__ import annotations

import re
import warnings
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from foundry.config import errors, keys
from foundry.config.enums import (
    RETRY_PREDICATES,
    AuthStrategy,
    CassetteMode,
    LogLevel,
    ProfileName,
    QuarantineMode,
    RetryPredicate,
    accepted_values_text,
    parse_enum,
)
from foundry.config.layers import flatten
from foundry.config.sources import ConfigSource, ConfigSources
from foundry.core.exceptions import ConfigError
from foundry.core.limits import (
    BACKOFF_BASE_DEFAULT,
    BACKOFF_BASE_MAX,
    BACKOFF_BASE_MIN,
    BACKOFF_MAX_DEFAULT,
    BACKOFF_MAX_MAX,
    BACKOFF_MAX_MIN,
    CAPTURE_LINES_DEFAULT,
    CAPTURE_LINES_MAX,
    CAPTURE_LINES_MIN,
    CASSETTE_DIR_DEFAULT,
    MAX_ATTEMPTS_DEFAULT,
    MAX_ATTEMPTS_MAX,
    MAX_ATTEMPTS_MIN,
    TIMEOUT_MAX,
    TIMEOUT_MIN,
)

__all__: Final[tuple[str, ...]] = (
    "ArtifactsConfig",
    "CassetteConfig",
    "DataConfig",
    "ExecutionConfig",
    "FoundryConfig",
    "HttpAuthConfig",
    "HttpConfig",
    "HttpRetriesConfig",
    "LoggingConfig",
    "ReportingConfig",
    "SecretsConfig",
)

_HTTP_TIMEOUT_MAX: Final[float] = 300.0
"""Upper bound of ``http.timeout`` in seconds: one request waits at most five minutes."""

_EXECUTION_TIMEOUT_DEFAULT: Final[float] = 60.0
"""Default of ``execution.timeout`` in seconds: one minute for a single test."""

_EXECUTION_RETRIES_MIN: Final[int] = 0
"""Smallest accepted ``execution.retries``: the test runs once and is not repeated."""

_EXECUTION_RETRIES_MAX: Final[int] = 10
"""Largest accepted ``execution.retries``: ten repeats after the first attempt."""

_BASE_URL_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})
"""The two URL schemes ``http.base_url`` accepts; anything else is a configuration fault."""

_BASE_URL_EXPECTED: Final[str] = "http.base_url must be an absolute http or https URL"
"""The expectation ``http.base_url`` states, worded as the registry's constraint text is."""

_REDACT_KEYS_EXPECTED: Final[str] = "expected a list of header or JSON key names"
"""The expectation ``secrets.redact_keys`` states when given one bare name."""

_CONTROL_CHARACTER: Final[re.Pattern[str]] = re.compile(r"[\x00-\x1f\x7f]")
"""Matches a control character, which no HTTP header name may contain."""

_NESTED_DELIMITER: Final[str] = "__"
"""Separator between the segments of a nested environment variable (AAP 0.4.5)."""

_DEFAULT_REDACT_KEYS: Final[tuple[str, ...]] = (
    "authorization",
    "set-cookie",
    "cookie",
    "x-api-key",
)
"""Default of ``secrets.redact_keys``: the header names always scrubbed from a written artefact."""

_AUTH_STRATEGY_KEY: Final[str] = "http.auth.strategy"
"""The key naming the credential strategy, quoted by the cross-field rules' messages."""

_AUTH_BEARER_KEYS: Final[tuple[str, ...]] = ("http.auth.token_env",)
"""The key ``http.auth.strategy = "bearer"`` requires beside it."""

_AUTH_BASIC_KEYS: Final[tuple[str, ...]] = ("http.auth.user_env", "http.auth.password_env")
"""The keys ``http.auth.strategy = "basic"`` requires beside it."""

_AUTH_HEADER_KEYS: Final[tuple[str, ...]] = ("http.auth.header_name", "http.auth.header_value_env")
"""The keys ``http.auth.strategy = "header"`` requires beside it."""


class HttpRetriesConfig(BaseModel):
    """The ``http.retries.*`` table: attempts per request and the wait between them.

    Read by :mod:`foundry.http.retries` for every request a
    :class:`foundry.http.session.FoundrySession` issues. Frozen and ``extra="forbid"``, so a
    misspelt ``http.retries.backoff_maxx`` is a fault, not a silently ignored line (AAP 0.4.5).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_attempts: int = Field(
        default=MAX_ATTEMPTS_DEFAULT, ge=MAX_ATTEMPTS_MIN, le=MAX_ATTEMPTS_MAX
    )
    """Total attempts per request, the first included.

    Not to be confused with :attr:`ExecutionConfig.retries`, which counts repeats (assumption A2).
    """

    backoff_base: float = Field(
        default=BACKOFF_BASE_DEFAULT, ge=BACKOFF_BASE_MIN, le=BACKOFF_BASE_MAX
    )
    """Wait before the second attempt in seconds, doubled for each further attempt."""

    backoff_max: float = Field(default=BACKOFF_MAX_DEFAULT, ge=BACKOFF_MAX_MIN, le=BACKOFF_MAX_MAX)
    """Cap on any single wait in seconds, so an exponential backoff cannot run away."""

    retry_on: tuple[RetryPredicate, ...] = RETRY_PREDICATES[:3]
    """Predicates that make a request retryable, in authored order.

    The default is the first three of :data:`foundry.config.enums.RETRY_PREDICATES` --
    ``timeout``, ``connect``, ``status_5xx`` -- leaving ``status_429`` opt-in.
    """

    @field_validator("retry_on", mode="before")
    @classmethod
    def _parse_retry_on(cls, value: object) -> object:
        """Turn the key's authored form into a tuple of retry predicates.

        A file supplies a list and the environment layer its comma-separated elements (assumption
        A3); authored order is kept. An unordered collection is sorted, so the result cannot depend
        on set iteration order (AAP 0.13.1).

        Args:
            value: The raw value a layer supplied.

        Returns:
            A tuple of :class:`foundry.config.enums.RetryPredicate` members.

        Raises:
            ValueError: If an element names no predicate. The message names the accepted ones.
        """
        if isinstance(value, (set, frozenset)):
            value = sorted(value, key=str)
        authored = list(value) if isinstance(value, (list, tuple)) else [value]
        parsed = tuple(_retry_predicate(element) for element in authored)
        if any(predicate is None for predicate in parsed):
            raise ValueError(_retry_on_expectation())
        return tuple(predicate for predicate in parsed if predicate is not None)


class HttpAuthConfig(BaseModel):
    """The ``http.auth.*`` table: the credential strategy and the names holding its secret.

    No value lives here. Each strategy names the environment variable holding its secret, and
    :mod:`foundry.http.auth` resolves that name once at session start (implicit requirement I9),
    so a configuration object, ``results.json`` and a report print without exposing a credential.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy: AuthStrategy = AuthStrategy.NONE

    token_env: str | None = None

    user_env: str | None = None

    password_env: str | None = None

    header_name: str | None = None

    header_value_env: str | None = None

    @field_validator(
        "token_env", "user_env", "password_env", "header_name", "header_value_env", mode="after"
    )
    @classmethod
    def _blank_name_is_no_name(cls, value: str | None) -> str | None:
        """Normalise a name that is empty or holds only whitespace to ``None``.

        A key set to ``""`` names no variable and no header, so treating it as unset keeps the
        cross-field rule honest: such a strategy is a fault, not a session that resolves an empty
        variable name.
        """
        if value is None:
            return None
        return value.strip() or None

    @model_validator(mode="after")
    def _require_companion_keys(self) -> HttpAuthConfig:
        """Reject a strategy that is missing the key naming its credential.

        The message names the missing key and the strategy key, with the selected strategy in single
        quotes, because ``translate_validation_error`` reads them back out of it to build the
        ``ConfigError`` naming that key.

        Returns:
            The validated model, unchanged.

        Raises:
            ValueError: If the selected strategy is missing one of its companion keys.
        """
        if self.strategy is AuthStrategy.BEARER:
            _require_present(self, AuthStrategy.BEARER, _AUTH_BEARER_KEYS)
        elif self.strategy is AuthStrategy.BASIC:
            _require_present(self, AuthStrategy.BASIC, _AUTH_BASIC_KEYS)
        elif self.strategy is AuthStrategy.HEADER:
            _require_present(self, AuthStrategy.HEADER, _AUTH_HEADER_KEYS)
        return self


class HttpConfig(BaseModel):
    """The ``http.*`` table: base URL, timeout, static headers and the retry and auth tables.

    Consumed by :class:`foundry.http.session.FoundrySession` through the ``foundry_http`` and
    ``foundry_api`` fixtures. ``base_url`` may be unset -- ``foundry_http`` issues absolute
    requests -- but ``foundry_api`` raises
    :class:`foundry.core.exceptions.ConfigError` naming ``http.base_url`` when it is still unset
    as that fixture is requested (AAP 0.4.7).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    base_url: str | None = None
    """Absolute base URL the ``foundry_api`` fixture joins request paths against, or ``None``."""

    timeout: float = Field(default=10.0, gt=0.0, le=_HTTP_TIMEOUT_MAX)
    """Per-request timeout in seconds; a request argument overrides it, never the reverse."""

    headers: dict[str, str] = Field(default_factory=dict)

    retries: HttpRetriesConfig = Field(default_factory=HttpRetriesConfig)

    auth: HttpAuthConfig = Field(default_factory=HttpAuthConfig)

    @field_validator("base_url", mode="before")
    @classmethod
    def _check_base_url(cls, value: object) -> str | None:
        """Validate that ``base_url`` is an absolute ``http`` or ``https`` URL.

        The authored string is kept verbatim: no trailing-slash normalisation, no lower-casing of
        the host. ``config show`` echoes what the operator wrote and :mod:`foundry.http.url` joins
        against it as written, while ``HttpUrl`` would rewrite the value.

        Args:
            value: The raw value a layer supplied.

        Returns:
            The URL as authored, without surrounding whitespace, or ``None``.

        Raises:
            ValueError: If the value is not text, is empty, or has no host or another scheme.
        """
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(_BASE_URL_EXPECTED)
        candidate = value.strip()
        if not candidate:
            raise ValueError(_BASE_URL_EXPECTED)
        split = urlsplit(candidate)
        if split.scheme.lower() not in _BASE_URL_SCHEMES or not split.netloc:
            raise ValueError(_BASE_URL_EXPECTED)
        return candidate

    @field_validator("headers", mode="after")
    @classmethod
    def _check_header_names(cls, value: dict[str, str]) -> dict[str, str]:
        """Reject an empty header name and a name holding a control character.

        Only names are checked: a value may be a credential, and redaction -- not validation --
        keeps it out of every artefact (AAP 0.3.2). A faulty name is not echoed, being a control
        sequence.

        Args:
            value: The header table as written.

        Returns:
            The same table, unchanged.

        Raises:
            ValueError: If a header name is empty or contains a control character.
        """
        for name in value:
            if not name:
                raise ValueError("http.headers rejects an empty header name")
            if _CONTROL_CHARACTER.search(name) is not None:
                raise ValueError("http.headers rejects a header name with a control character")
        return value


class ArtifactsConfig(BaseModel):
    """The ``artifacts.*`` table: where per-test artefact directories live and what a pass keeps.

    ``dir`` roots the ``<dir>/<run_id>/`` tree AAP 0.4.6 fixes; the run id inside it is chosen by
    the session, so two runs never overwrite each other's files (assumption A5).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    dir: Path = Path("artifacts")
    """Root directory of the artefact tree, relative to the invocation directory by default."""

    keep_passed: bool = False


# pydantic raises ``UserWarning: Field name "json" in "ReportingConfig" shadows an attribute in
# parent "BaseModel"`` from its metaclass, once the class body has finished, so the filter has to
# wrap the class statement itself; mypy reports the same shadowing as an incompatible assignment
# against BaseModel's deprecated ``json()`` method, hence the ignore below. Both are kept because
# AAP 0.4.5 fixes the key's name and the attribute has to stay ``config.reporting.json``: a
# renamed field would silently hand a reader that deprecated method instead of the boolean.
with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message=r'Field name "json" in "ReportingConfig" shadows an attribute in parent',
        category=UserWarning,
    )

    class ReportingConfig(BaseModel):
        """The ``reporting.*`` table: which of the two optional report files a run writes.

        Two booleans only. The report file *names* are fixed by AAP 0.4.6 -- ``results.json`` and
        ``report.html`` in the invocation directory, relocated together by
        ``--foundry-json-report`` -- and are deliberately not configurable, so this table adds no
        key the published schema lacks (assumption A6).
        """

        model_config = ConfigDict(extra="forbid", frozen=True)

        html: bool = True
        """Whether the self-contained ``report.html`` is written."""

        json: bool = True  # type: ignore[assignment]
        """Whether the machine-readable ``results.json`` is written."""


class DataConfig(BaseModel):
    """The ``data.*`` table: where the loaders look for data files and how strictly they check.

    ``dir`` is the root a :class:`foundry.data.cases.Case` name resolves against, so a test writes
    ``foundry_data.cases("prices.csv")`` instead of a path relative to its own module.
    ``schema_strict`` decides whether an unknown column is a collection error or is ignored
    (AAP 0.4.5).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    dir: Path = Path("testdata")

    schema_strict: bool = True


class CassetteConfig(BaseModel):
    """The ``cassette.*`` table: where cassettes live and which record/replay mode the run uses.

    The directory holds one JSON document per test, named from the sanitised node id (AAP 0.4.6).
    ``mode`` selects what :mod:`foundry.cassette.modes` does: ``off`` bypasses cassettes,
    ``record`` writes them, ``replay`` serves them and opens no socket, and ``passthrough``
    serves a match and otherwise falls through to the live transport (assumption A8).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    dir: Path = Path(CASSETTE_DIR_DEFAULT)

    mode: CassetteMode = CassetteMode.OFF


class ExecutionConfig(BaseModel):
    """The ``execution.*`` table: in-framework retries, the per-test timeout and quarantine.

    Read by :mod:`foundry.execution.coordinator`, :mod:`foundry.execution.timeout` and
    :mod:`foundry.execution.quarantine`. Foundry retries a test itself rather than through a
    plugin, because an attempt interacts with quarantine state and artefact capture (AAP 0.5.1).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    retries: int = Field(default=0, ge=_EXECUTION_RETRIES_MIN, le=_EXECUTION_RETRIES_MAX)
    """Repeats **after** the first attempt, so ``2`` allows three attempts (assumption A2).

    Not to be confused with :attr:`HttpRetriesConfig.max_attempts`, which counts attempts.
    """

    timeout: float = Field(default=_EXECUTION_TIMEOUT_DEFAULT, gt=TIMEOUT_MIN, le=TIMEOUT_MAX)

    quarantine_mode: QuarantineMode = QuarantineMode.REPORT
    """Whether a quarantined failure fails the run (``strict``) or only the report (``report``).

    The exit-code consequence belongs to :mod:`foundry.execution.quarantine` (assumption A11).
    """


class SecretsConfig(BaseModel):
    """The ``secrets.*`` table: the key names redacted from everything Foundry writes.

    :class:`foundry.obs.redaction.Redactor` is the only consumer: it matches these names, plus its
    own header and URL-parameter deny-lists, case-insensitively, and runs before any artefact,
    cassette, result document or report is serialised (AAP 0.3.2).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    redact_keys: tuple[str, ...] = _DEFAULT_REDACT_KEYS
    """Header and JSON key names to redact, deduplicated case-insensitively in authored order."""

    @field_validator("redact_keys", mode="before")
    @classmethod
    def _sequence_only(cls, value: object) -> object:
        """Reject one bare string and order an unordered collection deterministically.

        The key is a list of names, so a single name without list syntax is rejected rather than
        read as characters. A set is sorted, so the result cannot depend on set order (AAP 0.13.1).

        Args:
            value: The raw value a layer supplied.

        Returns:
            The value as supplied, or the sorted form of an unordered collection.

        Raises:
            ValueError: If the value is a single string.
        """
        if isinstance(value, str):
            raise ValueError(_REDACT_KEYS_EXPECTED)
        if isinstance(value, (set, frozenset)):
            return sorted(value, key=str)
        return value

    @field_validator("redact_keys", mode="after")
    @classmethod
    def _deduplicate(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Drop repeated and blank names, comparing case-insensitively.

        A duplicate deny-list entry cannot change what redaction does, so it is removed; the first
        spelling wins. Matching stays case-insensitive in :mod:`foundry.obs.redaction`, and a blank
        name is dropped as a no-op.

        Args:
            value: The names as authored.

        Returns:
            The names in first-occurrence order, without duplicates or blanks.
        """
        seen: set[str] = set()
        unique: list[str] = []
        for name in value:
            stripped = name.strip()
            folded = stripped.casefold()
            if not stripped or folded in seen:
                continue
            seen.add(folded)
            unique.append(stripped)
        return tuple(unique)


class LoggingConfig(BaseModel):
    """The ``logging.*`` table: the ``foundry`` logger's level and the capture ring size.

    ``level`` configures Foundry's own logger only -- never root logging, which belongs to the
    project (AAP 0.13.1). ``capture_lines`` sizes both the bounded ring
    :class:`foundry.obs.capture.LogRing` keeps per test and the log tail a failed test's artefact
    directory and report panel carry (AAP 0.4.6).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    level: LogLevel = LogLevel.INFO

    capture_lines: int = Field(
        default=CAPTURE_LINES_DEFAULT, ge=CAPTURE_LINES_MIN, le=CAPTURE_LINES_MAX
    )
    """Records retained per test; ``0`` disables capture and the maximum is ten thousand."""


class FoundryConfig(BaseSettings):
    """One resolved, immutable configuration: the 28 published keys plus their provenance.

    A run builds exactly one of these, in :func:`foundry.config.merge.resolve`, from what the four
    layers of AAP 0.4.5 supplied, and every consumer -- the ``foundry_config`` fixture, the CLI,
    the HTTP session, the cassette store, the artefact writer, the report -- reads that instance.
    It is frozen, so no consumer alters what another sees, and provenance is attached afterwards
    with :meth:`with_sources` rather than by mutation.

    It subclasses :class:`pydantic_settings.BaseSettings` so the ``FOUNDRY_`` prefix and ``__``
    nesting of AAP 0.4.5 are declared where pydantic-settings reads them, while
    :meth:`settings_customise_sources` keeps the caller's mapping as the only source. An ambient
    read of ``os.environ`` here would add a fifth, unrecorded layer -- making the precedence test
    and ``foundry config show`` non-deterministic -- and would read a source AAP 0.4.8 reserves
    for :mod:`foundry.config.env` and :mod:`foundry.http.auth`.
    """

    model_config = SettingsConfigDict(
        extra="forbid",
        frozen=True,
        validate_default=True,
        env_prefix=keys.ENV_PREFIX,
        env_nested_delimiter=_NESTED_DELIMITER,
        case_sensitive=False,
        populate_by_name=True,
    )

    profile: ProfileName = ProfileName.DEFAULT
    """Active profile: ``default``, ``local``, ``ci`` or ``staging`` (AAP 0.4.5).

    Its ``[profiles.<name>]`` table overrides the base file, never the environment or the CLI.
    """

    http: HttpConfig = Field(default_factory=HttpConfig)
    artifacts: ArtifactsConfig = Field(default_factory=ArtifactsConfig)
    reporting: ReportingConfig = Field(default_factory=ReportingConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    cassette: CassetteConfig = Field(default_factory=CassetteConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    secrets: SecretsConfig = Field(default_factory=SecretsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    sources: dict[str, ConfigSource] = Field(default_factory=dict, repr=False)
    """Which layer supplied each key, as :mod:`foundry.config.merge` recorded it.

    Empty when a configuration is built without provenance, and then :meth:`source_of` reports every
    registered key as ``default``. Not a configuration key: no layer sets it, and a ``sources``
    table in a file is rejected as an unknown key. ``ConfigSource`` compares by value, so equal
    resolutions compare equal.
    """

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Return the init source alone, so the model never reads the ambient environment.

        Attribution and precedence are Foundry's own: :mod:`foundry.config.merge` applies the layers
        in the order AAP 0.4.5 fixes and records the winner (requirement I2).

        Args:
            settings_cls: The settings class pydantic-settings is building values for.
            init_settings: The mapping the caller passed; the only source kept.
            env_settings: The library's environment source, deliberately not used.
            dotenv_settings: The library's dotenv source, deliberately not used.
            file_secret_settings: The library's secrets-directory source, deliberately not used.

        Returns:
            A one-element tuple holding ``init_settings``.
        """
        return (init_settings,)

    def source_of(self, dotted: str) -> ConfigSource:
        """Return where the value of ``dotted`` came from.

        AAP 0.4.3 publishes this method, so a test can assert that an environment variable, and not
        the file, supplied a value. A registered key with no recorded entry came from a field
        default and reports as ``default``, not as an absence.

        Args:
            dotted: The dotted configuration key, for example ``http.retries.max_attempts``.

        Returns:
            The recorded source, or the ``default`` source for a registered key.

        Raises:
            foundry.core.exceptions.ConfigError: If ``dotted`` is not a registered key. Message
                and closest-spelling hint are :mod:`foundry.config.errors`' own wording; the layer
                note is left out because no layer supplied the key -- a caller asked about it.
        """
        recorded = self.sources.get(dotted)
        if recorded is not None:
            return recorded
        if keys.spec_for(dotted) is not None:
            return ConfigSource.default()
        closest = keys.closest_key(dotted)
        hint = (
            errors.UNKNOWN_KEY_HINT_TEMPLATE.format(closest=closest)
            if closest is not None
            else None
        )
        raise ConfigError(errors.UNKNOWN_KEY_TEMPLATE.format(key=dotted), key=dotted, hint=hint)

    def with_sources(self, sources: ConfigSources) -> FoundryConfig:
        """Return a copy of this configuration carrying ``sources`` as its provenance.

        How :func:`foundry.config.merge` attaches attribution once the values validate, and how a
        suite overriding the ``foundry_config`` fixture rebuilds a configuration with an injected
        ``http.base_url``. ``model_copy`` does not revalidate, so the records pass through.

        Args:
            sources: The per-key provenance to attach, keyed by dotted key.

        Returns:
            A new configuration equal to this one except in its provenance.
        """
        return self.model_copy(update={"sources": dict(sources)})

    def leaf_values(self) -> dict[str, object]:
        """Return the resolved values as the 28 flat dotted keys AAP 0.4.5 publishes.

        The dotted-key spelling exists once, in :func:`foundry.config.layers.flatten`, which the
        merge also applies a layer with, so ``config show`` and the merge agree. Values stay the
        objects the model holds; every renderer formats its own output.

        Returns:
            One entry per leaf key, ordered by key: ``http.retries.max_attempts``,
            ``http.timeout``, ``profile`` and the other 25.
        """
        return flatten(self.model_dump(exclude={"sources"}, mode="python"))


def _retry_predicate(element: object) -> RetryPredicate | None:
    """Return the retry predicate ``element`` names, or ``None`` when it names none.

    Args:
        element: One authored element of ``http.retries.retry_on`` -- a member, or the text
            naming one in any case.

    Returns:
        The matching member, or ``None`` so the caller raises one message naming every accepted
        predicate.
    """
    if isinstance(element, RetryPredicate):
        return element
    if isinstance(element, str):
        return parse_enum(RetryPredicate, element)
    return None


def _retry_on_expectation() -> str:
    """Return the message naming the predicates ``http.retries.retry_on`` accepts.

    Returns:
        The expected shape, with the accepted values taken from the enum, so a member added there
        cannot leave the message behind.
    """
    return f"expected a list of {accepted_values_text(RetryPredicate)}"


def _require_present(
    model: HttpAuthConfig, strategy: AuthStrategy, companions: tuple[str, ...]
) -> None:
    """Raise if one of ``companions`` is unset while ``strategy`` is the selected strategy.

    The message names the missing key and the strategy key with the selected strategy in single
    quotes, because :func:`foundry.config.validation.translate_validation_error` reads the two keys
    and the quoted value back out of it to build the ``ConfigError`` naming the missing key.

    Args:
        model: The auth table being validated.
        strategy: The strategy that requires the companion keys.
        companions: The dotted keys the strategy requires.

    Raises:
        ValueError: If a companion key holds no name.
    """
    for key in companions:
        if getattr(model, key.rsplit(".", 1)[-1]) is not None:
            continue
        raise ValueError(f"{key} is required when {_AUTH_STRATEGY_KEY} is '{strategy.value}'")
