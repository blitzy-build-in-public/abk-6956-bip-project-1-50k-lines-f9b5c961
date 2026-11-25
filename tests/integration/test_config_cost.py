"""Timing instrument for configuration resolution: twenty resolutions inside the AAP 0.14.2 budget.

AAP 0.14.2 budgets configuration resolution at "<= 50 ms, median of 20 resolutions". What is timed
is :func:`foundry.config.merge.resolve`, the one entry point a resolution has (AAP 0.7.1): a session
boot calls it once and every CLI invocation calls it once, so the figure is what a user pays, and
the four-layer precedence plus per-key source attribution (implicit requirement I2) exist only
behind that call. The document measured is one a real team writes -- every ``[section]`` of the
AAP 0.4.5 surface plus the ``[profiles.ci]`` table it selects -- and ``[http.auth]`` carries
variable *names* and never a value (implicit requirement I9).

The figure is the median, so one scheduling outlier cannot move it; one untimed resolution precedes
the loop, so it is steady-state cost rather than first-call imports; the correctness guard runs
before the budget assertion, because a resolution that fell back to the field defaults would be fast
and meaningless; and the twenty resolutions are asserted equivalent afterwards, so a loop that
stopped resolving cannot look cheap. No subprocess, server, socket or write outside ``tmp_path`` is
involved (AAP 0.14.3).

**A resolution the host starved is retaken, not measured.** A resolution reads a file and builds a
model, so at full speed its wall clock matches its own CPU time to a fraction of a millisecond; a
resolution the host kept off a core shows wall clock it never spent working, which on a 2-vCPU host
shared with a batch of agents arrives in whole CPU-quota periods -- measured here, a sample of about
1 ms reappeared at 27 ms and at 46 ms. The proof that a sample was starved is read from that sample
itself (``_host_contention.was_preempted``), never from the host's load, so a resolution that took
its CPU time at full speed and still came out slow is measured, never retaken. Up to
:data:`SAMPLE_RETRY_BUDGET` extra resolutions replace the starved ones; a host that keeps starving
every sample spends that budget and then has the samples it starved the least measured in their
place, so a real regression still fails the ceiling. The samples set aside, the ones measured in
their place and the cgroup's own throttling count are printed beside the figures.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from _host_contention import (
    QuotaThrottling,
    describe_quota_throttling,
    read_quota_throttling,
    was_preempted,
)

from foundry.config import (
    CassetteMode,
    ConfigSource,
    FoundryConfig,
    LogLevel,
    ProfileName,
    QuarantineMode,
    resolve,
)

__all__: Final[tuple[str, ...]] = ("measure_resolutions",)

# --- The budget and the sample this module measures -------------------------

RESOLUTION_BUDGET_MS: Final[float] = 50.0
"""The configuration-resolution budget AAP 0.14.2 sets, in milliseconds."""

SAMPLE_COUNT: Final[int] = 20
"""How many resolutions one measurement times: the count AAP 0.14.2 names."""

SAMPLE_RETRY_BUDGET: Final[int] = 20
"""How many extra resolutions may replace samples the host starved, so a saturated host cannot loop.

One retry costs one resolution of about a millisecond, so this budget costs a saturated host a few
milliseconds of extra wait and a quiet host nothing at all, while a host that starves a few samples
out of twenty still yields the twenty honest samples the budget is stated over.
"""

PREEMPTION_FLOOR_MS: Final[float] = 5.0
"""How much wall clock one resolution may spend beyond its own CPU time before the host starved it.

A resolution of a millisecond that ran at full speed agrees with its own CPU time to a fraction of a
millisecond, while one the host descheduled loses whole CPU-quota periods: measured on this 2-vCPU
host under a parallel batch of agents, starved samples appeared at 27 ms and at 46 ms. The floor
sits far above the first and far below the second, and ``PREEMPTION_RATIO`` in ``_host_contention``
widens it for a longer sample.
"""

PUBLISHED_KEY_COUNT: Final[int] = 28
"""Leaf keys the resolved configuration carries, as AAP 0.4.5 publishes them.

Asserted so the measured document is known to exercise the whole surface: a document that resolved
three keys and defaulted the other twenty-five would be fast for the wrong reason.
"""

CONFIG_FILE_NAME: Final[str] = "foundry.toml"
"""The name discovery reads out of the invocation directory (AAP 0.4.5, third step)."""

CONFIGURATION_DOCUMENT: Final[str] = """\
# The configuration a team ships once and every run resolves (AAP 0.4.5).
profile = "ci"

[http]
base_url = "http://127.0.0.1:8080/api"
timeout = 2.5

[http.headers]
accept = "application/json"
x-suite = "checkout"

[http.retries]
max_attempts = 4
backoff_base = 0.05
backoff_max = 1.0
retry_on = ["timeout", "connect", "status_5xx", "status_429"]

[http.auth]
strategy = "header"
header_name = "x-api-key"
header_value_env = "ACME_API_KEY"

[artifacts]
dir = "artifacts"
keep_passed = true

[reporting]
html = true
json = true

[data]
dir = "testdata"
schema_strict = false

[cassette]
dir = "cassettes"
mode = "replay"

[execution]
retries = 2
timeout = 45.0
quarantine_mode = "strict"

[secrets]
redact_keys = ["authorization", "cookie", "x-api-key", "x-trace-token"]

[logging]
level = "DEBUG"
capture_lines = 500

[profiles.ci.http]
timeout = 3.0

[profiles.ci.execution]
retries = 3
"""
"""The realistic ``foundry.toml`` written into ``tmp_path`` before every measurement.

Every key is one AAP 0.4.5 publishes, and the two ``[profiles.ci]`` tables override the base file's
``http.timeout`` and ``execution.retries``, which is what puts the profile layer inside the measured
work. The constants below state what the document declares, so text and expectation cannot drift.
"""

# --- What the document declares, and the layer that supplies it -------------

DECLARED_PROFILE: Final[ProfileName] = ProfileName.CI
"""The profile the base file selects (``profile = "ci"``)."""

FILE_HTTP_TIMEOUT: Final[float] = 2.5
"""``http.timeout`` as the base file states it, before the profile table overrides it."""

PROFILE_HTTP_TIMEOUT: Final[float] = 3.0
"""``http.timeout`` as ``[profiles.ci.http]`` states it: the value the resolution must hold."""

FILE_EXECUTION_RETRIES: Final[int] = 2
"""``execution.retries`` as the base file states it, before the profile table overrides it."""

PROFILE_EXECUTION_RETRIES: Final[int] = 3
"""``execution.retries`` as ``[profiles.ci.execution]`` states it: the resolved value."""

DECLARED_QUARANTINE_MODE: Final[QuarantineMode] = QuarantineMode.STRICT
"""``execution.quarantine_mode`` as the file states it: not the ``report`` default."""

DECLARED_CASSETTE_MODE: Final[CassetteMode] = CassetteMode.REPLAY
"""``cassette.mode`` as the file states it: not the ``off`` default."""

DECLARED_BASE_URL: Final[str] = "http://127.0.0.1:8080/api"
"""``http.base_url`` as the file states it: a loopback URL, never contacted by a resolution."""

DECLARED_HEADER_NAME: Final[str] = "x-api-key"
"""``http.auth.header_name``: the header the ``header`` strategy sets (AAP 0.4.5)."""

DECLARED_HEADER_VALUE_ENV: Final[str] = "ACME_API_KEY"
"""``http.auth.header_value_env``: the *name* of the variable holding the header's value.

The name is what the resolved configuration carries and what the guard asserts; the value stays in
the environment (implicit requirement I9).
"""

DECLARED_CAPTURE_LINES: Final[int] = 500
"""``logging.capture_lines`` as the file states it: not the ``200`` default."""

FILE_LOGGING_LEVEL: Final[LogLevel] = LogLevel.DEBUG
"""``logging.level`` as the file states it, before the environment layer overrides it."""

ENV_LOGGING_VARIABLE: Final[str] = "FOUNDRY_LOGGING__LEVEL"
"""The ``FOUNDRY_*`` variable the explicit environment supplies, spelling ``logging.level``.

AAP 0.4.5 fixes the encoding: the ``FOUNDRY_`` prefix and ``__`` between the segments of the dotted
key. Setting it is what puts the environment layer inside the measured resolution.
"""

ENVIRONMENT_LOGGING_LEVEL: Final[str] = "WARNING"
"""The value :data:`ENV_LOGGING_VARIABLE` carries: it must win over the file's ``DEBUG``."""

PLACEHOLDER_CREDENTIAL: Final[str] = "placeholder-supplied-by-the-environment"
"""The value the named auth variable carries, so its presence check passes.

A resolution membership-tests ``http.auth.header_value_env`` and never reads the value (implicit
requirement I9), so the text cannot reach a configuration, a report or a message; it is deliberately
shaped like no credential.
"""

PROFILE_LAYER: Final[str] = "profile:ci"
"""How :class:`~foundry.config.ConfigSource` labels a value a profile table supplied."""

FILE_LAYER: Final[str] = "file"
"""How a source record labels a value the configuration file supplied."""

ENV_LAYER: Final[str] = "env"
"""How a source record labels a value a ``FOUNDRY_*`` variable supplied."""

PROFILE_DETAIL: Final[str] = "ci"
"""The detail a profile-sourced record repeats: the profile's own name."""


@dataclass(frozen=True, slots=True)
class Measurement:
    """One measured run: the elapsed milliseconds per resolution and what each resolution returned.

    Keeping the twenty configurations beside the twenty samples is what lets the caller prove the
    loop did the work it timed instead of trusting the clock. ``retakes`` counts the samples set
    aside because the host starved them, ``substituted`` how many of the least starved of those were
    measured once the retry budget ran out, and ``throttled`` is the cgroup's CPU-quota throttling
    over the whole loop, or ``None`` on a host that keeps no such counters.
    """

    samples: tuple[float, ...]
    configurations: tuple[FoundryConfig, ...]
    retakes: int
    substituted: int
    throttled: QuotaThrottling | None

    @property
    def median_ms(self) -> float:
        """Return the median of :attr:`samples`, which is the figure AAP 0.14.2 budgets.

        The median rather than the mean: one descheduled sample would drag a mean over the ceiling.
        """
        return float(statistics.median(self.samples))


def write_configuration(directory: Path) -> Path:
    """Write :data:`CONFIGURATION_DOCUMENT` into ``directory`` and return its path.

    The file is named ``foundry.toml`` because that is the discovery step AAP 0.4.5 places third
    and the one a project actually writes; it is written with an explicit encoding and LF endings,
    so the bytes parsed are the bytes this module states on any host.
    """
    path = directory / CONFIG_FILE_NAME
    path.write_text(CONFIGURATION_DOCUMENT, encoding="utf-8", newline="\n")
    written = path.read_text(encoding="utf-8")
    assert written == CONFIGURATION_DOCUMENT, f"{path} does not hold the document it was given"
    assert written.endswith("\n"), f"{path} is not LF-terminated"
    return path


def measurement_environment() -> dict[str, str]:
    """Return the whole environment the measured resolution reads, built once by the caller.

    A supplied ``environ`` is the *only* environment for that resolution, so the developer's own
    cannot reach it, and ``clean_foundry_env`` purges the ambient ``FOUNDRY_*`` variables too. It
    holds two entries: the variable the configuration names for its auth header, present so the
    presence check passes with a value no path reads, and the ``FOUNDRY_LOGGING__LEVEL`` override
    that puts the environment layer inside the measured work.
    """
    return {
        DECLARED_HEADER_VALUE_ENV: PLACEHOLDER_CREDENTIAL,
        ENV_LOGGING_VARIABLE: ENVIRONMENT_LOGGING_LEVEL,
    }


def assert_declared_values(config: FoundryConfig) -> None:
    """Assert the resolved configuration holds what :data:`CONFIGURATION_DOCUMENT` declares.

    The guard that keeps the budget honest: every asserted value differs from its field default, so
    a resolution that had fallen back to the defaults -- or skipped the profile and environment
    layers -- fails here instead of being timed as a fast no-op.
    """
    assert config.http.timeout == PROFILE_HTTP_TIMEOUT, (
        f"http.timeout resolved to {config.http.timeout!r}, not the profile's "
        f"{PROFILE_HTTP_TIMEOUT!r} (the base file states {FILE_HTTP_TIMEOUT!r})"
    )
    assert config.profile is DECLARED_PROFILE, (
        f"profile resolved to {config.profile!r}, not {DECLARED_PROFILE!r}"
    )
    assert config.execution.retries == PROFILE_EXECUTION_RETRIES, (
        f"execution.retries resolved to {config.execution.retries!r}, not the profile's "
        f"{PROFILE_EXECUTION_RETRIES!r} (the base file states {FILE_EXECUTION_RETRIES!r})"
    )
    assert config.cassette.mode is DECLARED_CASSETTE_MODE, (
        f"cassette.mode resolved to {config.cassette.mode!r}, not {DECLARED_CASSETTE_MODE!r}"
    )
    assert config.http.auth.header_value_env == DECLARED_HEADER_VALUE_ENV, (
        "http.auth.header_value_env resolved to "
        f"{config.http.auth.header_value_env!r}, not the variable name "
        f"{DECLARED_HEADER_VALUE_ENV!r}"
    )
    assert config.http.auth.header_name == DECLARED_HEADER_NAME, (
        f"http.auth.header_name resolved to {config.http.auth.header_name!r}"
    )
    assert config.http.base_url == DECLARED_BASE_URL, (
        f"http.base_url resolved to {config.http.base_url!r}, not {DECLARED_BASE_URL!r}"
    )
    assert config.execution.quarantine_mode is DECLARED_QUARANTINE_MODE, (
        f"execution.quarantine_mode resolved to {config.execution.quarantine_mode!r}"
    )
    assert config.logging.capture_lines == DECLARED_CAPTURE_LINES, (
        f"logging.capture_lines resolved to {config.logging.capture_lines!r}"
    )
    assert config.logging.level is LogLevel.WARNING, (
        f"logging.level resolved to {config.logging.level!r}, not the environment's "
        f"{ENVIRONMENT_LOGGING_LEVEL!r} over the file's {FILE_LOGGING_LEVEL!r}"
    )
    assert len(config.leaf_values()) == PUBLISHED_KEY_COUNT, (
        f"the resolved configuration carries {len(config.leaf_values())} leaf keys, not the "
        f"{PUBLISHED_KEY_COUNT} AAP 0.4.5 publishes"
    )


def measure_resolutions(environ: Mapping[str, str], cwd: Path) -> Measurement:
    """Time :data:`SAMPLE_COUNT` resolutions and return the samples with what each returned.

    Each iteration is exactly one :func:`foundry.config.merge.resolve` call, with the environment
    and invocation directory passed in, so nothing composed or copied sits inside a timed window.
    One untimed resolution runs first, because the first call of a process also imports ``pydantic``
    and builds the field validators, which no later invocation pays again.

    Every iteration reads two clocks across its resolution: the wall clock the budget is stated in,
    and the CPU time this thread spent, so a sample the host starved can be told from a slow one. A
    starved sample is set aside and retaken, up to :data:`SAMPLE_RETRY_BUDGET` extra resolutions. A
    sample that spent its CPU time at full speed and still came out slow is never set aside. A host
    that starves every sample spends the budget, and the samples it starved the least are then
    measured in place of nothing at all, so a real regression still reaches the ceiling.

    The cgroup's throttling counters are read once around the loop rather than around each sample:
    the samples are a millisecond long, and a file read between two of them would add work to a
    window this tight. The delta names the contention the whole measurement ran under.
    """
    resolve(environ=environ, cwd=cwd)

    honest: list[tuple[float, float, FoundryConfig]] = []
    starved: list[tuple[float, float, FoundryConfig]] = []
    throttled_before = read_quota_throttling()
    for _ in range(SAMPLE_COUNT + SAMPLE_RETRY_BUDGET):
        cpu_before = time.thread_time()
        started = time.perf_counter()
        config = resolve(environ=environ, cwd=cwd)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        cpu_ms = (time.thread_time() - cpu_before) * 1000.0
        sample = (elapsed_ms, elapsed_ms - cpu_ms, config)
        if was_preempted(elapsed_ms=elapsed_ms, cpu_ms=cpu_ms, floor_ms=PREEMPTION_FLOOR_MS):
            starved.append(sample)
        else:
            honest.append(sample)
        if len(honest) == SAMPLE_COUNT:
            break
    throttled_after = read_quota_throttling()

    least_starved = sorted(starved, key=lambda sample: sample[1])
    kept = (*honest, *least_starved[: SAMPLE_COUNT - len(honest)])
    return Measurement(
        samples=tuple(elapsed_ms for elapsed_ms, _, _ in kept),
        configurations=tuple(config for _, _, config in kept),
        retakes=len(starved),
        substituted=len(kept) - len(honest),
        throttled=(
            throttled_after.since(throttled_before)
            if throttled_before is not None and throttled_after is not None
            else None
        ),
    )


def test_the_measured_configuration_resolves_to_the_values_it_declares(
    clean_foundry_env: None, tmp_path: Path
) -> None:
    """The document this module times resolves to what it declares, layer by layer."""
    config_path = write_configuration(tmp_path)
    config = resolve(environ=measurement_environment(), cwd=tmp_path)

    assert_declared_values(config)

    profiled = config.source_of("http.timeout")
    assert profiled == ConfigSource(layer=PROFILE_LAYER, detail=PROFILE_DETAIL), (
        f"http.timeout is attributed to {profiled!r}, not to the [profiles.ci.http] table"
    )
    from_environment = config.source_of("logging.level")
    assert from_environment == ConfigSource(layer=ENV_LAYER, detail=ENV_LOGGING_VARIABLE), (
        f"logging.level is attributed to {from_environment!r}, not to {ENV_LOGGING_VARIABLE}"
    )
    from_file = config.source_of("http.base_url")
    assert from_file.layer == FILE_LAYER, (
        f"http.base_url is attributed to {from_file.layer!r}, not to the file layer"
    )
    assert from_file.detail == str(config_path), (
        f"the file layer is attributed to {from_file.detail!r}, not to the written {config_path}"
    )


def test_twenty_resolutions_stay_inside_the_fifty_millisecond_budget(
    clean_foundry_env: None, tmp_path: Path
) -> None:
    """Twenty resolutions of the realistic configuration finish inside the AAP 0.14.2 budget."""
    write_configuration(tmp_path)
    environ = measurement_environment()
    measurement = measure_resolutions(environ, cwd=tmp_path)

    assert len(measurement.samples) == SAMPLE_COUNT, (
        f"the measurement holds {len(measurement.samples)} samples, not {SAMPLE_COUNT}"
    )
    assert_declared_values(measurement.configurations[0])
    assert_declared_values(measurement.configurations[-1])

    median = measurement.median_ms
    sorted_samples = [round(sample, 3) for sample in sorted(measurement.samples)]
    throttling = describe_quota_throttling(
        [measurement.throttled] if measurement.throttled is not None else []
    )
    print(
        f"configuration resolution: median {median:.2f} ms over {SAMPLE_COUNT} resolutions "
        f"(budget {RESOLUTION_BUDGET_MS:.0f} ms), samples: {sorted_samples}; starved samples set "
        f"aside {measurement.retakes}, {measurement.substituted} of the least starved measured "
        f"after {SAMPLE_COUNT + SAMPLE_RETRY_BUDGET} resolutions; {throttling}"
    )

    first = measurement.configurations[0].leaf_values()
    differing = [
        index
        for index, config in enumerate(measurement.configurations)
        if config.leaf_values() != first
    ]
    assert differing == [], (
        f"{len(differing)} of the {SAMPLE_COUNT} resolutions returned a configuration unlike the "
        f"first, at sample indices {differing[:5]}"
    )

    assert median <= RESOLUTION_BUDGET_MS, (
        f"configuration resolution median {median:.2f} ms exceeds the "
        f"{RESOLUTION_BUDGET_MS:.0f} ms budget (samples: {sorted_samples}; "
        f"{measurement.retakes} starved samples set aside, {measurement.substituted} of the least "
        f"starved measured; {throttling})"
    )


def test_a_changed_environment_reaches_the_next_resolution(
    clean_foundry_env: None, tmp_path: Path
) -> None:
    """A second resolution with a changed environment observes the change, so nothing is cached."""
    write_configuration(tmp_path)
    baseline = resolve(environ=measurement_environment(), cwd=tmp_path)
    changed = resolve(
        environ={**measurement_environment(), ENV_LOGGING_VARIABLE: LogLevel.ERROR.value},
        cwd=tmp_path,
    )

    assert baseline.logging.level is LogLevel.WARNING, (
        f"the baseline resolved logging.level to {baseline.logging.level!r}"
    )
    assert changed.logging.level is LogLevel.ERROR, (
        f"the changed environment resolved logging.level to {changed.logging.level!r}, so the "
        "second resolution reused the first one's result"
    )
    assert changed.source_of("logging.level") == ConfigSource(
        layer=ENV_LAYER, detail=ENV_LOGGING_VARIABLE
    ), "the changed value is not attributed to the variable that supplied it"

    baseline_values = baseline.leaf_values()
    changed_values = changed.leaf_values()
    differing = sorted(
        key for key in baseline_values if baseline_values[key] != changed_values[key]
    )
    assert differing == ["logging.level"], (
        f"the two resolutions differ in {differing}, and only logging.level was overridden"
    )
