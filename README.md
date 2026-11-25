# Foundry

Foundry is a test-automation framework for Python services that already run pytest. A team adds one
distribution, `pytest-foundry`, to a repository, and that repository's existing suite gains layered
configuration, HTTP and API helpers, contract validation, data-driven case generation, record and
replay, in-framework retries with quarantine, parallel sharding, artifact capture and a
human-readable report — without writing any of that plumbing itself.

It is one distribution with four surfaces: a **pytest plugin** that activates through the `pytest11`
entry point with no flag and no configuration file; a **library** whose public surface is twelve
fixtures plus a small typed API; a **console application**, `foundry`, with the `run`, `init`,
`config`, `report` and `doctor` subcommands; and a **scaffolder** whose output runs green offline.

Distribution name `pytest-foundry`, import package `foundry`, version `0.1.0`, MIT licensed.

## Requirements and platforms

| Requirement | Value |
|---|---|
| Interpreter | CPython 3.11, 3.12 or 3.13 — `requires-python = ">=3.11,<3.14"` |
| Linux | supported; this release is developed and exercised on Linux |
| macOS | supported target platform; the repository ships no CI matrix, so macOS is declared rather than exercised |
| Windows | **not supported**; the classifiers declare POSIX platforms only |
| HTTP | synchronous only, through `httpx` |
| Test functions | synchronous only; a coroutine test is outside this release |
| Database | sqlite, in memory, one engine per test |
| Network | Foundry opens no non-loopback socket of its own at runtime |

## Installation

Version `0.1.0` is not published on a package index yet, so install it from the wheel `uv build`
writes, or from a local wheelhouse holding that wheel:

```bash
pip install dist/pytest_foundry-0.1.0-py3-none-any.whl                  # built by `uv build`
pip install --no-index --find-links /path/to/wheelhouse pytest-foundry   # offline, no index
```

Once the release is published, `uv add --dev pytest-foundry` and `pip install pytest-foundry`
install the same version from an index. Every runtime dependency is declared with an upper bound in
`pyproject.toml` and carries a permissive licence; [docs/verification.md](docs/verification.md) records a
provenance row per package.

## Activation: nothing to configure

The distribution declares exactly two entry points, and installing it is the whole setup:

| Group | Name | Target |
|---|---|---|
| `pytest11` | `foundry` | `foundry.plugin` |
| `console_scripts` | `foundry` | `foundry.cli.main:main` |

pytest imports `foundry.plugin` while it prepares the run, so the plugin is active in a directory
that holds no `foundry.toml`, no `conftest.py` entry and no ini setting. No invocation anywhere
passes `-p foundry`. `-p no:foundry` turns it off, and `pytest --trace-config` lists it.

What activates with nothing configured:

- the `--foundry-*` option group;
- the owned `foundry_*` marker namespace, whose documented member is `foundry_quarantine`;
- the twelve fixtures, registered before collection;
- a run id, the `artifacts/<run_id>/` directory, and a terminal summary whose first line is the
  activation proof `foundry: profile=default`, followed by a line naming the run id and the
  artifacts root;
- `results.json`, `junit.xml` and `report.html` in the invocation directory.

## Five-minute walkthrough

This is the whole path from source to an open report. It needs no network, no running service and no
environment variable.

**1. Build the distribution.**

```bash
uv build
```

That writes `dist/pytest_foundry-0.1.0-py3-none-any.whl` and `dist/pytest_foundry-0.1.0.tar.gz`.
The wheel is named for the normalised distribution name `pytest-foundry`, as PEP 427 requires.

**2. Install it into a virtual environment.**

```bash
python3 -m venv .venv-demo
source .venv-demo/bin/activate
pip install dist/pytest_foundry-0.1.0-py3-none-any.whl
foundry --version
```

The last command prints `foundry, version 0.1.0` and exits `0`.

**3. Scaffold a project and run it.**

```bash
foundry init demo
cd demo
pytest
```

The run exits `0`, reports one passed test, and prints `foundry: profile=default` in its summary.

**4. Open the results.** `report.html`, `results.json` and `junit.xml` now sit in the project root,
and `artifacts/<run_id>/` holds the per-test artifact directories. Open `report.html` in any browser:
it is a single file with inline styling and scripting, so it renders with no network access.

`foundry init` writes a project a team can extend immediately:

- `foundry.toml`, with the settings a starting project needs present as commented defaults;
- `tests/test_smoke.py`, one passing test that asks for `foundry_config`;
- `testdata/`, one small CSV and the JSON Schema of one of its rows;
- `artifacts/`, plus its entry in `.gitignore`;
- `README.md`, naming the three commands to run.

`--template api` adds `tests/test_api.py` and a `cassettes/` directory holding the recording that
test replays, so the first HTTP test opens no socket. `foundry init` refuses a non-empty target
without `--force`, and a refusal creates nothing.

## Fixtures

| Fixture | Scope | Returns |
|---|---|---|
| `foundry_config` | session | `FoundryConfig`: the resolved configuration, with a recorded source per key readable through `source_of(dotted_key)` |
| `foundry_profile` | session | `str`: the active profile name, `default`, `local`, `ci` or `staging` |
| `foundry_run_id` | session | `str`: `YYYYmmddTHHMMSS-<6 hex>`, which is also the artifact directory name |
| `foundry_artifacts` | session | `FoundryArtifacts`: `dir_for(nodeid)`, `write(kind, name, data)` and `manifest` |
| `foundry_http` | function | `FoundrySession`: unauthenticated, no base URL, configured retry policy and timeouts |
| `foundry_api` | function | `FoundrySession`: configured base URL, auth strategy, static headers and cassette mode applied |
| `foundry_data` | function | `FoundryDataLoader`: `.cases(name)` and `.matrix(name)`, resolved against `data.dir` |
| `foundry_db` | function | `sqlalchemy.orm.Session`: transactional, on a fresh in-memory sqlite schema, rolled back at teardown |
| `foundry_factory` | function | `FoundryFactory`: Faker-backed, seeded from the run id and the node id |
| `foundry_clock` | function | `FoundryClock`: frozen `now()`, `advance(**delta)` and `set(datetime)` |
| `foundry_log_capture` | function | `FoundryLogCapture`: records from the `foundry` logger, with `.tail(n)` and `.records` |
| `foundry_poll` | function | `FoundryPoll`: `eventually(fn, timeout=..., interval=..., sleep=...)` with the sleep injectable |

Two contracts are worth spelling out. `foundry_api` raises `ConfigError` naming `http.base_url`
while that key is unset, and `foundry_http` sends unauthenticated calls so that a test can drive a
login flow itself. Every fixture is single-threaded and deterministic: no session fixture opens a
connection, and none leaves a thread running past the test that asked for it.

## Configuration

### Layers and precedence

Four layers resolve into one immutable `FoundryConfig`, and later layers win:

1. field defaults;
2. the TOML file, which itself applies the base table and then the selected profile's table;
3. the `FOUNDRY_*` environment;
4. the `--foundry-*` command line.

`foundry config show --resolved` prints which layer supplied each key. The worked example: with
defaults, a `foundry.toml` carrying `http.timeout = 2.0`, `FOUNDRY_HTTP__TIMEOUT=3.5` and
`--foundry-http.timeout 1.0`, the resolved value is `1.0`; dropping the command line yields `3.5`;
dropping the environment yields `2.0`; with no file at all the default `10.0` applies.

### File discovery

First match wins:

1. the path given to `--foundry-config`;
2. the path in `FOUNDRY_CONFIG`;
3. `foundry.toml` in the invocation directory;
4. `[tool.foundry]` in the invocation directory's `pyproject.toml`;
5. the field defaults.

A file named explicitly and then missing is a configuration error, not a silent fallback: the run
exits `2` with `config file not found: <path>`.

### Profiles

The profiles are `default`, `local`, `ci` and `staging`. A profile's table lives in
`[profiles.<name>]` of `foundry.toml`, or in `[tool.foundry.profiles.<name>]` of `pyproject.toml`,
and its values override the base file — never the environment and never the command line.

### Environment variables

The prefix is `FOUNDRY_`, nesting is written with `__`, names are case-insensitive and a list is
comma-separated:

```bash
export FOUNDRY_HTTP__TIMEOUT=3.5
export FOUNDRY_HTTP__RETRIES__RETRY_ON=timeout,status_429
export FOUNDRY_CASSETTE__MODE=replay
```

`FOUNDRY_CONFIG` is the one exception: it selects a configuration file and is not a key. A value that
cannot be parsed is a configuration error naming the variable and the expected type.

### Accepted keys

Every model sets `extra="forbid"`, so a key outside this table is an error in every layer.

| Key | Type | Default | Accepted |
|---|---|---|---|
| `profile` | enum | `default` | `default`, `local`, `ci`, `staging` |
| `http.base_url` | URL or null | `null` | absolute `http`/`https` URL; required by `foundry_api`, unused by `foundry_http` |
| `http.timeout` | float | `10.0` | `0 < t <= 300` seconds |
| `http.headers` | map of str to str | `{}` | any header names and values; a name in `secrets.redact_keys` is redacted at every sink |
| `http.retries.max_attempts` | int | `3` | `1..10`, the first attempt included |
| `http.retries.backoff_base` | float | `0.2` | `0..10` seconds |
| `http.retries.backoff_max` | float | `2.0` | `0..60` seconds, a cap on any single backoff |
| `http.retries.retry_on` | list of enum | `["timeout","connect","status_5xx"]` | any of `timeout`, `connect`, `status_5xx`, `status_429` |
| `http.auth.strategy` | enum | `none` | `none`, `bearer`, `basic`, `header` |
| `artifacts.dir` | path | `artifacts` | any writable path; `<dir>/<run_id>/` is created per run |
| `artifacts.keep_passed` | bool | `false` | `true` keeps a passing test's artifacts; `false` keeps only its `manifest.json` |
| `reporting.html` | bool | `true` | writes `report.html` |
| `reporting.json` | bool | `true` | writes `results.json` |
| `data.dir` | path | `testdata` | directory the data loaders resolve names against |
| `data.schema_strict` | bool | `true` | `true` makes an unknown column in a data row an error |
| `cassette.dir` | path | `cassettes` | directory the cassette store reads and writes |
| `cassette.mode` | enum | `off` | `off`, `record`, `replay`, `passthrough` |
| `execution.retries` | int | `0` | `0..10`, counting repeats **after** the first attempt |
| `execution.timeout` | float | `60.0` | `0 < t <= 3600` seconds per test |
| `execution.quarantine_mode` | enum | `report` | `report` (a quarantined failure does not fail the run) or `strict` (it does) |
| `secrets.redact_keys` | list of str | `["authorization","set-cookie","cookie","x-api-key"]` | header names and JSON keys, matched case-insensitively |
| `logging.level` | enum | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `logging.capture_lines` | int | `200` | `0..10000`, the size of the per-test log ring and the report's log tail |

### Authentication

The `http.auth.*` keys store the **name** of the environment variable holding a credential, never
the value itself, so a secret cannot reach a config echo, `results.json` or a report:

| Key | Used by |
|---|---|
| `http.auth.token_env` | `bearer`: the variable holding the bearer token |
| `http.auth.user_env` | `basic`: the variable holding the user |
| `http.auth.password_env` | `basic`: the variable holding the password |
| `http.auth.header_name` | `header`: the header to set |
| `http.auth.header_value_env` | `header`: the variable holding that header's value |

A strategy is checked for its companion keys when the configuration validates, and an absent
credential variable is a `ConfigError` naming the variable only — the value is never printed.

### Data files

`foundry_data` loads CSV, JSON, YAML and JSONL from `data.dir`, expands matrix and cross-product
cases, and gives every case a stable id derived from the file content and the row index, so two runs
and two workers produce the same ids. A row missing a required column is a collection error naming
the file, the line and the column; an empty data file instead skips the generated item with the
reason `no cases in <file>` and the session still succeeds.

### Configuration errors

An unknown key exits `2` and names the key together with the closest valid key. A value outside its
range exits `2` and names the key and the accepted range — `http.timeout = -1` reports `http.timeout`
and `0 < t <= 300`. Both faults stop the run before collection, on a bare `pytest` invocation and
under `foundry run` alike.

## Command line

| Command | Options | Behaviour |
|---|---|---|
| `foundry run` | `[pytest args...]`, `--foundry-profile`, `--foundry-config`, `--foundry-retries`, `--foundry-timeout`, `--foundry-record`, `--foundry-replay`, `--foundry-json-report`, plus the generic overrides | wraps the pytest run and passes every argument it does not own through unchanged |
| `foundry init PATH` | `--template` (`minimal` or `api`), `--force` | scaffolds a project; takes no configuration override, because it resolves no project configuration |
| `foundry config show` | `--resolved`, `--format json` or `toml`, plus the overrides | without `--resolved` it reports defaults, file, profile and environment and lists this command's own overrides in a not-applied section; with it, they are applied and attributed to `cli:<option>` |
| `foundry config validate` | the overrides | validates the resolved configuration without running tests, printing `configuration valid (profile=<name>)` |
| `foundry report compare BASE.json NEW.json` | `--json` | prints the newly-failing, newly-passing and still-failing node id sets; takes no configuration override |
| `foundry doctor` | `--json`, plus the overrides | probes the interpreter, plugin load, configuration validity, artifact-directory writability, cassette-directory state and socket-guard state |
| `foundry --version` | none | prints `foundry, version 0.1.0` and exits `0` |
| `foundry --help` | none | lists `run`, `init`, `config`, `report` and `doctor` |

**The generic override.** Every scalar configuration key gains `--foundry-<dotted.key>` with the
key's own type, so `--foundry-http.timeout 1.0` and `--foundry-data.schema_strict false` are both
valid. The named options are aliases onto specific keys: `--foundry-profile` is `profile`,
`--foundry-timeout` is `execution.timeout`, `--foundry-retries` is `execution.retries`,
`--foundry-config` is the file selector and `--foundry-json-report` is the `results.json` path. The
override set is registered on `run`, `config show`, `config validate` and `doctor`. `--foundry-record`
sets `cassette.mode` to `record` and `--foundry-replay` sets it to `replay`; passing both is a usage
error that exits `2`.

**Exit codes.**

| Code | Meaning |
|---|---|
| `0` | success |
| `1` | test failures, or newly failing comparisons from `report compare` |
| `2` | a usage or configuration error that Foundry itself raised |
| `3`, `4`, `5` | pytest's internal error, usage error and no-tests-collected codes, preserved unchanged by `foundry run` |

`foundry report compare` exits `1` only when the newly-failing set is non-empty, and `foundry doctor`
exits `2` only when a required probe fails; a warning never changes the code.

## Record and replay

`--foundry-record` writes one cassette per test at `cassettes/<sanitized-nodeid>.json`, holding each
call the test made and each response it received. `--foundry-replay` serves those cassettes and opens
no socket at all, because replay is an `httpx` transport that never reaches the network layer. A call
with no recorded counterpart raises `CassetteMissError` naming the method, the path and the cassette
path. Recording redacts before writing, so `Authorization` and cookie values never reach a cassette.

| `cassette.mode` | Reads | Writes | Network | On a miss |
|---|---|---|---|---|
| `off` | no | no | the live transport | not applicable |
| `record` | no | yes, overwriting the cassette at teardown | the live transport | not applicable |
| `replay` | yes | no | none | `CassetteMissError` |
| `passthrough` | yes | no | the live transport | falls through to the live transport |

A cassette matches on method, path, order-insensitive query parameters and the body hash; headers are
not part of the match.

## Reporting and artifacts

Every run writes three files to the invocation directory under fixed names, and
`--foundry-json-report PATH` moves `results.json` with the other two beside it:

- `results.json` — one immutable record per test, sorted by node id, carrying the outcome, attempts,
  duration, markers, failures, artifacts and quarantine state; a run-level fault appears as a
  `run_error` element after the records;
- `junit.xml` — one testcase per record, with `foundry.*` properties, relative artifact references and
  a redacted failure message;
- `report.html` — one self-contained file with inline styling and scripting, no external font and no
  CDN reference, so it opens offline; it carries a run summary, a table of every test, a Quarantined
  section and a failure panel per failure with the request dump, the response dump and the last
  `logging.capture_lines` captured log lines.

Per-test artifacts live at `artifacts/<run_id>/<sanitized-nodeid>/`: `request.json`, `response.json`,
`stdout.log`, `stderr.log`, `traceback.txt`, `log_tail.log` and `manifest.json`. Node ids are
percent-encoded, so a `/`, a `::` or a `[` cannot introduce a directory separator, and a path that
would resolve outside the run directory is refused.

## Retries, timeouts and quarantine

`execution.retries` repeats a failing test in-framework, so an attempt shares the artifact capture and
the quarantine state of the run: with `--foundry-retries 2`, a test that fails twice and then passes
ends as `passed` with `attempts: 3` recorded. The HTTP retry policy is separate, governed by
`http.retries.*` and clamped by `http.retries.backoff_max`, and `execution.timeout` bounds one test: an
overrun fails in the `call` phase with the partial stdout and the log tail attached.

Mark a known-flaky test with the quarantine marker:

```python
import pytest


@pytest.mark.foundry_quarantine
def test_legacy_export() -> None: ...
```

With `execution.quarantine_mode = "report"`, a quarantined failure is reported as
`quarantined_failed` and does not fail the run, which still exits `0`; with `strict` it does fail the
run. A quarantined test that passes is reported as `quarantine_passed`, with a note recommending the
marker's removal.

## Sharding

`pytest -n 3` shards the suite through `pytest-xdist` and produces the same node id set as the serial
run: case ids and collection order are sorted and worker-independent, and `results.json` is written
once, on the controller. A lost worker leaves its items reported as `not_run`, beside a `run_error`
entry naming the worker.

## Secret redaction

One redaction engine runs before anything is written, keyed by `secrets.redact_keys` plus its own
deny-list covering `authorization`, `proxy-authorization`, `cookie`, `set-cookie`, `x-api-key`, token,
secret and key query parameters, and URL userinfo; the artifact writer, the cassette store, the JSON
store, the JUnit writer, the HTML renderer and the console renderer all pass through it.

Foundry never reconfigures another package's logger, and it does not silence `httpx` for you. A
project that wants the `httpx` INFO-level request-URL lines suppressed adds one line of its own
logging configuration:

```python
import logging

logging.getLogger("httpx").setLevel(logging.WARNING)
```

That line belongs to the host project, not to Foundry's configuration.

## The shipped example

```bash
pytest examples/checkout -q
pytest examples/checkout -q --foundry-replay
```

The first command runs the 24-test example suite offline against an echo server bound to a loopback
ephemeral port; the second replays the committed cassettes with no server and no socket. The example
carries no `foundry.toml` on purpose: it is invoked from the repository root, so it overrides the
`foundry_config` fixture in its own `conftest.py` to inject that ephemeral base URL and to point
`data.dir` and `cassette.dir` at its own directories.

## Verification

`scripts/verify.sh` is the single gate entry point, and it stops at the first failure:

```bash
bash scripts/verify.sh
```

It runs `ruff check`, `ruff format --check`, `mypy --strict src/foundry`, `scripts/count_lines.sh`, a
two-module smoke coverage run, and finally the whole suite with
`pytest --cov=foundry --cov-fail-under=85 -n auto`, followed by an assertion that no `src/foundry`
module sits at 0 % coverage.

## Documentation and licence

- [CHANGELOG.md](CHANGELOG.md) — what each release provides, `0.1.0` first.
- [docs/verification.md](docs/verification.md) — dependency provenance, licences and the
  added-dependency log.
- [LICENSE](LICENSE) — MIT.

The public surface — both entry points, the twelve fixture names and scopes, the eight exception
classes, the five subcommands and their exit-code meanings, the configuration keys, the result-record
fields and the nine outcome values — is fixed for `0.1.0`: later releases may add to it, never rename
or remove any part of it.

