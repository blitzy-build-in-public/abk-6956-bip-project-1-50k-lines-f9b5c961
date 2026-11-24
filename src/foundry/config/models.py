"""The resolved configuration: one frozen pydantic tree over the 28 published keys.

AAP 0.4.5 fixes Foundry's configuration surface -- 28 leaf keys, each with a type, a default and an
accepted range -- and this module is that surface as a value: :class:`FoundryConfig` with ten nested
models beneath it, one per ``[section]`` of ``foundry.toml``. Adding, renaming or removing a key
breaks a published surface (AAP 0.4.9), so the field list below and the registry in
:mod:`foundry.config.keys` are the same 28 keys.

Resolution is *not* here: :mod:`foundry.config.merge` applies the layers of AAP 0.4.5, records a
:class:`foundry.config.sources.ConfigSource` per key, and this model validates the result. No
validator reads a file, an environment variable or a clock, so equal inputs stay equal. A rejected
value is pydantic's own ``ValidationError``, worded for the operator by
:func:`foundry.config.validation.translate_validation_error`, which exits 2 (assumption A4); the
only ``ConfigError`` raised here is :meth:`FoundryConfig.source_of`'s refusal of an unregistered
key. Every credential key stores the *name* of the variable holding a secret, never its value
(requirement I9); bounds come from :mod:`foundry.core.limits` (AAP 0.7.2).
"""

from __future__ import annotations

import re
import warnings
from pathlib import Path
from types import MappingProxyType
from typing import Final
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)
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
    EXECUTION_RETRIES_DEFAULT,
    EXECUTION_RETRIES_MAX,
    EXECUTION_RETRIES_MIN,
    HTTP_TIMEOUT_DEFAULT,
    HTTP_TIMEOUT_MAX,
    HTTP_TIMEOUT_MIN,
    MAX_ATTEMPTS_DEFAULT,
    MAX_ATTEMPTS_MAX,
    MAX_ATTEMPTS_MIN,
    TIMEOUT_DEFAULT,
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

_BASE_URL_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})

_BASE_URL_EXPECTED: Final[str] = "http.base_url must be an absolute http or https URL"

_REDACT_KEYS_EXPECTED: Final[str] = "expected a list of header or JSON key names"

_CONTROL_CHARACTER: Final[re.Pattern[str]] = re.compile(r"[\x00-\x1f\x7f]")

_NESTED_DELIMITER: Final[str] = "__"

_DEFAULT_REDACT_KEYS: Final[tuple[str, ...]] = (
    "authorization",
    "set-cookie",
    "cookie",
    "x-api-key",
)

_AUTH_STRATEGY_KEY: Final[str] = "http.auth.strategy"

_AUTH_BEARER_KEYS: Final[tuple[str, ...]] = ("http.auth.token_env",)

_AUTH_BASIC_KEYS: Final[tuple[str, ...]] = ("http.auth.user_env", "http.auth.password_env")

_AUTH_HEADER_KEYS: Final[tuple[str, ...]] = ("http.auth.header_name", "http.auth.header_value_env")


class HttpRetriesConfig(BaseModel):
    """The ``http.retries.*`` table: attempts per request and the wait between them.

    Read by :mod:`foundry.http.retries` for every session request, frozen and ``extra="forbid"``,
    so a misspelt key is a fault, not a silently ignored line (AAP 0.4.5).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_attempts: int = Field(
        default=MAX_ATTEMPTS_DEFAULT, ge=MAX_ATTEMPTS_MIN, le=MAX_ATTEMPTS_MAX
    )
    """Total attempts per request, the first included -- not :attr:`ExecutionConfig.retries`,
    which counts repeats (assumption A2)."""

    backoff_base: float = Field(
        default=BACKOFF_BASE_DEFAULT, ge=BACKOFF_BASE_MIN, le=BACKOFF_BASE_MAX
    )
    """Wait before the second attempt in seconds, doubled for each further attempt."""

    backoff_max: float = Field(default=BACKOFF_MAX_DEFAULT, ge=BACKOFF_MAX_MIN, le=BACKOFF_MAX_MAX)
    """Cap on any single wait in seconds, so an exponential backoff cannot run away."""

    retry_on: list[RetryPredicate] = Field(default_factory=lambda: list(RETRY_PREDICATES[:3]))
    """Predicates that make a request retryable, in authored order; ``status_429`` is opt-in."""

    @field_validator("retry_on", mode="before")
    @classmethod
    def _parse_retry_on(cls, value: object) -> list[RetryPredicate]:
        """Turn the key's authored form into a list of retry predicates.

        A file supplies a list and the environment its comma-separated elements (assumption A3);
        authored order is kept and an unordered collection is sorted, so nothing depends on set
        iteration order (AAP 0.13.1).

        Raises:
            ValueError: If an element names no predicate. The message names the accepted ones.
        """
        if isinstance(value, (set, frozenset)):
            value = sorted(value, key=str)
        authored = list(value) if isinstance(value, (list, tuple)) else [value]
        parsed = [_retry_predicate(element) for element in authored]
        if any(predicate is None for predicate in parsed):
            raise ValueError(_retry_on_expectation())
        return [predicate for predicate in parsed if predicate is not None]


class HttpAuthConfig(BaseModel):
    """The ``http.auth.*`` table: the credential strategy and the names holding its secret.

    No value lives here: each strategy names the environment variable holding its secret, which
    :mod:`foundry.http.auth` resolves once at session start (requirement I9).
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

        A key set to ``""`` names no variable and no header, so reading it as unset keeps the
        cross-field rule honest.
        """
        if value is None:
            return None
        return value.strip() or None

    @model_validator(mode="after")
    def _require_companion_keys(self) -> HttpAuthConfig:
        """Reject a strategy missing the key naming its credential, returning the model unchanged.

        The message names the missing key and the strategy key in single quotes, because
        ``translate_validation_error`` reads both back to build the ``ConfigError``.

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

    ``base_url`` may be unset, but the ``foundry_api`` fixture raises
    :class:`foundry.core.exceptions.ConfigError` naming it while it is (AAP 0.4.7).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    base_url: str | None = None
    """Absolute base URL the ``foundry_api`` fixture joins request paths against, or ``None``."""

    timeout: float = Field(default=HTTP_TIMEOUT_DEFAULT, gt=HTTP_TIMEOUT_MIN, le=HTTP_TIMEOUT_MAX)
    """Per-request timeout in seconds; a request argument overrides it, never the reverse."""

    headers: dict[str, str] = Field(default_factory=dict)

    retries: HttpRetriesConfig = Field(default_factory=HttpRetriesConfig)

    auth: HttpAuthConfig = Field(default_factory=HttpAuthConfig)

    @field_validator("base_url", mode="before")
    @classmethod
    def _check_base_url(cls, value: object) -> str | None:
        """Validate that ``base_url`` is an absolute ``http`` or ``https`` URL.

        The authored string is kept verbatim -- no trailing-slash normalisation, no lower-casing of
        the host, which ``HttpUrl`` would rewrite -- because ``config show`` echoes what the
        operator wrote; it comes back stripped of surrounding whitespace, or ``None`` when unset.

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
        keeps it out of every artefact, so a faulty name is not echoed either (AAP 0.3.2).

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

    ``dir`` roots the ``<dir>/<run_id>/`` tree of AAP 0.4.6, so two runs never overwrite each
    other's files (assumption A5).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    dir: Path = Path("artifacts")
    """Root directory of the artefact tree, relative to the invocation directory by default."""

    keep_passed: bool = False


# pydantic warns that the field name "json" shadows an attribute in parent "BaseModel" once the
# class body finishes, so the filter wraps the class statement itself; mypy reports the same
# shadowing against the deprecated ``json()`` method, hence the ignore below. AAP 0.4.5 fixes the
# key's name, and a renamed field would hand a reader that method instead of the boolean.
with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message=r'Field name "json" in "ReportingConfig" shadows an attribute in parent',
        category=UserWarning,
    )

    class ReportingConfig(BaseModel):
        """The ``reporting.*`` table: which of the two optional report files a run writes.

        The file *names* are fixed by AAP 0.4.6 -- ``results.json`` and ``report.html`` in the
        invocation directory, relocated together by ``--foundry-json-report`` (assumption A6).
        """

        model_config = ConfigDict(extra="forbid", frozen=True)

        html: bool = True
        """Whether the self-contained ``report.html`` is written."""

        json: bool = True  # type: ignore[assignment]
        """Whether the machine-readable ``results.json`` is written."""


class DataConfig(BaseModel):
    """The ``data.*`` table: where the loaders look for data files and how strictly they check.

    ``dir`` is the root a :class:`foundry.data.cases.Case` name resolves against, so a test writes
    ``foundry_data.cases("prices.csv")``, not a module-relative path; ``schema_strict`` makes an
    unknown column in a data row an error (AAP 0.4.5).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    dir: Path = Path("testdata")

    schema_strict: bool = True


class CassetteConfig(BaseModel):
    """The ``cassette.*`` table: where cassettes live and which record/replay mode the run uses.

    The directory holds one JSON document per test, named from the sanitised node id (AAP 0.4.6).
    ``mode`` is ``off``, ``record``, ``replay`` (which serves cassettes and opens no socket) or
    ``passthrough``, which falls through to the live transport on a miss (assumption A8).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    dir: Path = Path(CASSETTE_DIR_DEFAULT)

    mode: CassetteMode = CassetteMode.OFF


class ExecutionConfig(BaseModel):
    """The ``execution.*`` table: in-framework retries, the per-test timeout and quarantine.

    Read by :mod:`foundry.execution.coordinator`, :mod:`foundry.execution.timeout` and
    :mod:`foundry.execution.quarantine`; Foundry retries a test itself, because an attempt
    interacts with quarantine state and artefacts (AAP 0.5.1).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    retries: int = Field(
        default=EXECUTION_RETRIES_DEFAULT, ge=EXECUTION_RETRIES_MIN, le=EXECUTION_RETRIES_MAX
    )
    """Repeats **after** the first attempt, so ``2`` allows three attempts (assumption A2)."""

    timeout: float = Field(default=TIMEOUT_DEFAULT, gt=TIMEOUT_MIN, le=TIMEOUT_MAX)

    quarantine_mode: QuarantineMode = QuarantineMode.REPORT
    """Whether a quarantined failure fails the run (``strict``) or only the report (``report``)."""


class SecretsConfig(BaseModel):
    """The ``secrets.*`` table: the key names redacted from everything Foundry writes.

    :class:`foundry.obs.redaction.Redactor` is the only consumer; it matches these names, plus its
    own deny-lists, case-insensitively, before anything is serialised (AAP 0.3.2).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    redact_keys: list[str] = Field(default_factory=lambda: list(_DEFAULT_REDACT_KEYS))
    """Header and JSON key names to redact, deduplicated case-insensitively in authored order."""

    @field_validator("redact_keys", mode="before")
    @classmethod
    def _sequence_only(cls, value: object) -> object:
        """Reject one bare string and order an unordered collection deterministically.

        The key is a list of names, so a single name without list syntax is rejected rather than
        read as characters; a set is sorted, so the result cannot depend on set order (AAP 0.13.1).

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
    def _deduplicate(cls, value: list[str]) -> list[str]:
        """Drop repeated and blank names, comparing case-insensitively.

        A duplicate deny-list entry cannot change what redaction does, so the first spelling wins
        and authored order is kept.
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
        return unique


class LoggingConfig(BaseModel):
    """The ``logging.*`` table: the ``foundry`` logger's level and the capture ring size.

    ``level`` configures Foundry's own logger only -- never root logging (AAP 0.13.1) -- and
    ``capture_lines`` sizes :class:`foundry.obs.capture.LogRing` per test and the log tail a failed
    test's artefact and report panel carry (AAP 0.4.6).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    level: LogLevel = LogLevel.INFO

    capture_lines: int = Field(
        default=CAPTURE_LINES_DEFAULT, ge=CAPTURE_LINES_MIN, le=CAPTURE_LINES_MAX
    )
    """Records retained per test; ``0`` disables capture and the maximum is ten thousand."""


class FoundryConfig(BaseSettings):
    """One resolved, immutable configuration: the 28 published keys plus their provenance.

    A run builds exactly one, in :func:`foundry.config.merge.resolve`, from what the four layers of
    AAP 0.4.5 supplied, and every consumer -- the fixture, the CLI, the HTTP session, the cassette
    store, the artefact writer, the report -- reads that instance; it is frozen, so no consumer
    alters what another sees, and provenance is attached with :meth:`with_sources`.

    It subclasses :class:`pydantic_settings.BaseSettings` so the ``FOUNDRY_`` prefix and ``__``
    nesting of AAP 0.4.5 are declared where pydantic-settings reads them, while
    :meth:`settings_customise_sources` keeps the caller's mapping the only source: an ambient
    ``os.environ`` read would add a fifth, unrecorded layer (AAP 0.4.8).
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
    """Active profile: ``default``, ``local``, ``ci`` or ``staging`` (AAP 0.4.5). Its
    ``[profiles.<name>]`` table overrides the base file, never the environment or the CLI."""

    http: HttpConfig = Field(default_factory=HttpConfig)
    artifacts: ArtifactsConfig = Field(default_factory=ArtifactsConfig)
    reporting: ReportingConfig = Field(default_factory=ReportingConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    cassette: CassetteConfig = Field(default_factory=CassetteConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    secrets: SecretsConfig = Field(default_factory=SecretsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    sources: ConfigSources = Field(default_factory=dict, repr=False)
    """Which layer supplied each key, as :mod:`foundry.config.merge` recorded it.

    A read-only mapping of the ``Mapping[str, ConfigSource]`` AAP 0.7.2 publishes, installed as a
    :class:`types.MappingProxyType` because ``frozen=True`` stops a field being rebound but not a
    ``dict`` inside it being edited. It drives ``config show``, ``doctor`` and the report; empty
    when a configuration is built without provenance, and then :meth:`source_of` reports every
    registered key as ``default``. No layer sets it, so a ``sources`` table in a file is an
    unknown key.
    """

    @model_validator(mode="after")
    def _read_only_sources(self) -> FoundryConfig:
        """Install the read-only view of the provenance the field declares.

        pydantic stores the mutable mapping it validated, so it is replaced by a
        :class:`types.MappingProxyType` over a private copy; an already read-only value is kept.
        """
        if not isinstance(self.sources, MappingProxyType):
            object.__setattr__(self, "sources", MappingProxyType(dict(self.sources)))
        return self

    @field_serializer("sources", mode="plain")
    def _sources_as_mapping(self, sources: ConfigSources) -> dict[str, ConfigSource]:
        """Serialise the provenance as a plain mapping of its records.

        ``model_dump`` cannot serialise a :class:`types.MappingProxyType` against the field's
        declared type, so the read-only view is unwrapped here and a dump carries ``sources`` as the
        object AAP 0.4.5 publishes -- what :func:`foundry.execution.session.worker_payload` ferries
        to the xdist workers, kept a JSON object rather than a string.
        """
        return dict(sources)

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
        in the order AAP 0.4.5 fixes and records the winner (requirement I2). The library's own
        environment, dotenv and secrets-directory sources are deliberately not consulted.

        Returns:
            A one-element tuple holding ``init_settings``.
        """
        return (init_settings,)

    def source_of(self, dotted: str) -> ConfigSource:
        """Return where the value of ``dotted`` came from.

        AAP 0.4.3 publishes this method, so a test can assert that an environment variable, and not
        the file, supplied a value. A registered key with no recorded entry came from a field
        default and reports as ``default``, not as an absence.

        Returns:
            The recorded source, or the ``default`` source for a registered key.

        Raises:
            foundry.core.exceptions.ConfigError: If ``dotted`` is not a registered key, with
                :mod:`foundry.config.errors`' own message and closest-spelling hint.
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
        ``http.base_url``. ``model_copy`` revalidates nothing, so the copy installs the read-only
        view itself, and the result's provenance cannot be rewritten either.

        Returns:
            A new configuration equal to this one except in its provenance.
        """
        return self.model_copy(update={"sources": MappingProxyType(dict(sources))})

    def leaf_values(self) -> dict[str, object]:
        """Return the resolved values as the 28 flat dotted keys AAP 0.4.5 publishes.

        The dotted-key spelling exists once, in :func:`foundry.config.layers.flatten`, which the
        merge also applies a layer with, so ``config show`` and the merge agree; values stay the
        objects the model holds.

        Returns:
            One entry per leaf key, ordered by key: ``http.timeout``, ``profile`` and 26 more.
        """
        return flatten(self.model_dump(exclude={"sources"}, mode="python"))


def _retry_predicate(element: object) -> RetryPredicate | None:
    """Return the retry predicate ``element`` names, or ``None`` when it names none.

    Args:
        element: One authored element of ``http.retries.retry_on`` -- a member, or the text naming
            one in any case. Nothing matched, the answer is ``None``, so the caller raises one
            message naming every accepted predicate.
    """
    if isinstance(element, RetryPredicate):
        return element
    if isinstance(element, str):
        return parse_enum(RetryPredicate, element)
    return None


def _retry_on_expectation() -> str:
    """Return the message naming the predicates ``http.retries.retry_on`` accepts.

    The accepted values come from the enum, so a member added there cannot leave the message behind.
    """
    return f"expected a list of {accepted_values_text(RetryPredicate)}"


def _require_present(
    model: HttpAuthConfig, strategy: AuthStrategy, companions: tuple[str, ...]
) -> None:
    """Raise if one of ``companions`` is unset while ``strategy`` is the selected strategy.

    The message names the missing key and the strategy key, quoted strategy included, because
    :func:`foundry.config.validation.translate_validation_error` reads both back for its error.

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
