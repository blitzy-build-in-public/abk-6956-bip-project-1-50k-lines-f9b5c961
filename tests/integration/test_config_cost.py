"""Configuration resolution: 50 ms median of 20 resolutions, the budget AAP 0.14.2 sets.

Each sample is one ``resolve()`` call in a ``tmp_path`` project; a starved sample is retaken, never
excused; the correctness guard runs before the budget so a defaulted resolution cannot look cheap.
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
    ConfigSource,
    FoundryConfig,
    LogLevel,
    ProfileName,
    QuarantineMode,
    resolve,
)

RESOLUTION_BUDGET_MS: Final[float] = 50.0
SAMPLE_COUNT: Final[int] = 20
SAMPLE_RETRY_BUDGET: Final[int] = 20
PREEMPTION_FLOOR_MS: Final[float] = 5.0
PUBLISHED_KEY_COUNT: Final[int] = 28
CONFIG_FILE_NAME: Final[str] = "foundry.toml"

# A document a team ships once and every run resolves: the AAP 0.4.5 sections a project sets, with
# [profiles.ci] overriding one value and [http.auth] carrying a variable name, never a secret.
CONFIGURATION_DOCUMENT: Final[str] = """\
profile = "ci"

[http]
base_url = "http://127.0.0.1:8080/api"
timeout = 2.5

[http.retries]
max_attempts = 4

[http.auth]
strategy = "header"
header_name = "x-api-key"
header_value_env = "ACME_API_KEY"

[execution]
retries = 2
quarantine_mode = "strict"

[logging]
level = "DEBUG"
capture_lines = 500

[profiles.ci.http]
timeout = 3.0
"""

# Every expected value differs from its field default, so a resolution that fell back to the
# defaults, or skipped the profile or environment layer, fails the guard rather than timing cheap.
DECLARED_VALUES: Final[Mapping[str, object]] = {
    "profile": ProfileName.CI,
    "http.base_url": "http://127.0.0.1:8080/api",
    "http.timeout": 3.0,
    "http.retries.max_attempts": 4,
    "http.auth.header_name": "x-api-key",
    "http.auth.header_value_env": "ACME_API_KEY",
    "execution.retries": 2,
    "execution.quarantine_mode": QuarantineMode.STRICT,
    "logging.capture_lines": 500,
    "logging.level": LogLevel.WARNING,
}
LOGGING_VARIABLE: Final[str] = "FOUNDRY_LOGGING__LEVEL"
HEADER_VALUE_VARIABLE: Final[str] = "ACME_API_KEY"
PROFILE_LAYER: Final[str] = "profile:ci"


@dataclass(frozen=True, slots=True)
class Measurement:
    samples: tuple[float, ...]
    configurations: tuple[FoundryConfig, ...]
    retakes: int
    substituted: int
    throttled: QuotaThrottling | None

    @property
    def median_ms(self) -> float:
        return float(statistics.median(self.samples))


def write_configuration(directory: Path) -> Path:
    path = directory / CONFIG_FILE_NAME
    path.write_text(CONFIGURATION_DOCUMENT, encoding="utf-8", newline="\n")
    assert path.read_text(encoding="utf-8") == CONFIGURATION_DOCUMENT, f"{path} lost its document"
    return path


def measurement_environment() -> dict[str, str]:
    credential = "placeholder-supplied-by-the-environment"
    return {HEADER_VALUE_VARIABLE: credential, LOGGING_VARIABLE: "WARNING"}


def assert_declared_values(config: FoundryConfig) -> None:
    values = config.leaf_values()
    wrong = {key: values[key] for key, want in DECLARED_VALUES.items() if values[key] != want}
    assert wrong == {}, f"{len(wrong)} keys hold other than declared: {wrong}"
    assert len(values) == PUBLISHED_KEY_COUNT, f"{len(values)} leaf keys, not {PUBLISHED_KEY_COUNT}"


def measure_resolutions(environ: Mapping[str, str], cwd: Path) -> Measurement:
    resolve(environ=environ, cwd=cwd)
    honest: list[tuple[float, FoundryConfig]] = []
    starved: list[tuple[float, float, FoundryConfig]] = []
    throttled_before = read_quota_throttling()
    for _ in range(SAMPLE_COUNT + SAMPLE_RETRY_BUDGET):
        cpu_before = time.thread_time()
        started = time.perf_counter()
        config = resolve(environ=environ, cwd=cwd)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        cpu_ms = (time.thread_time() - cpu_before) * 1000.0
        if was_preempted(elapsed_ms=elapsed_ms, cpu_ms=cpu_ms, floor_ms=PREEMPTION_FLOOR_MS):
            starved.append((elapsed_ms - cpu_ms, elapsed_ms, config))
        else:
            honest.append((elapsed_ms, config))
        if len(honest) == SAMPLE_COUNT:
            break
    throttled_after = read_quota_throttling()
    least_starved = sorted(starved, key=lambda sample: sample[0])[: SAMPLE_COUNT - len(honest)]
    kept = [*honest, *((elapsed_ms, config) for _, elapsed_ms, config in least_starved)]
    return Measurement(
        samples=tuple(elapsed_ms for elapsed_ms, _ in kept),
        configurations=tuple(config for _, config in kept),
        retakes=len(starved),
        substituted=len(kept) - len(honest),
        throttled=(
            throttled_after.since(throttled_before)
            if throttled_before is not None and throttled_after is not None
            else None
        ),
    )


def test_twenty_resolutions_stay_inside_the_fifty_millisecond_budget(
    clean_foundry_env: None, tmp_path: Path
) -> None:
    config_path = write_configuration(tmp_path)
    measurement = measure_resolutions(measurement_environment(), cwd=tmp_path)
    assert len(measurement.samples) == SAMPLE_COUNT, f"{len(measurement.samples)} samples"
    first = measurement.configurations[0]
    assert_declared_values(first)
    file_source = ConfigSource(layer="file", detail=str(config_path))
    assert first.source_of("http.timeout") == ConfigSource(layer=PROFILE_LAYER, detail="ci")
    assert first.source_of("logging.level") == ConfigSource(layer="env", detail=LOGGING_VARIABLE)
    assert first.source_of("http.base_url") == file_source
    repeat = [config.leaf_values() == first.leaf_values() for config in measurement.configurations]
    assert all(repeat), f"{repeat.count(False)} of {SAMPLE_COUNT} resolutions differ from the first"

    median = measurement.median_ms
    samples = [round(sample, 3) for sample in sorted(measurement.samples)]
    throttled = [measurement.throttled] if measurement.throttled is not None else []
    throttling = describe_quota_throttling(throttled)
    print(
        f"configuration resolution: median {median:.2f} ms over {SAMPLE_COUNT} resolutions "
        f"(budget {RESOLUTION_BUDGET_MS:.0f} ms); {throttling}"
    )
    assert median <= RESOLUTION_BUDGET_MS, (
        f"resolution median {median:.2f} ms exceeds the {RESOLUTION_BUDGET_MS:.0f} ms budget "
        f"(samples {samples}; {measurement.retakes} starved set aside, "
        f"{measurement.substituted} measured in their place; {throttling})"
    )


def test_a_changed_environment_reaches_the_next_resolution(
    clean_foundry_env: None, tmp_path: Path
) -> None:
    write_configuration(tmp_path)
    baseline = resolve(environ=measurement_environment(), cwd=tmp_path)
    changed = resolve(
        environ={**measurement_environment(), LOGGING_VARIABLE: LogLevel.ERROR.value}, cwd=tmp_path
    )
    assert_declared_values(baseline)
    assert baseline.logging.level is LogLevel.WARNING, f"baseline {baseline.logging.level!r}"
    assert changed.logging.level is LogLevel.ERROR, "the second resolution reused the first result"
    assert changed.source_of("logging.level") == ConfigSource(
        layer="env", detail=LOGGING_VARIABLE
    ), "the changed value is not attributed to the variable that supplied it"
    before, after = baseline.leaf_values(), changed.leaf_values()
    differing = sorted(key for key in before if before[key] != after[key])
    assert differing == ["logging.level"], f"the two resolutions differ in {differing}"
