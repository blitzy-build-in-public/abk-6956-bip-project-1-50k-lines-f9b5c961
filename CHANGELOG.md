# Changelog

Foundry follows semantic versioning, and each released version has its own section here, newest
first, stating what that release provides.

## [0.1.0] - 2026-11-25

### Added

- **Configuration layer** — typed, validated settings resolved through four layers in fixed
  precedence (field defaults, TOML file, `FOUNDRY_*` environment, `--foundry-*` command line) with a
  recorded source for every key, unknown-key rejection naming the closest valid key, a
  `secrets.redact_keys` deny list, and the `default`, `local`, `ci` and `staging` profiles.
- **pytest plugin core** — activation through the `pytest11` entry point with no plugin flag and
  no configuration file, the owned `foundry_*` marker namespace with `foundry_quarantine`, the
  declared option group including the generic `--foundry-<dotted.key>` override for every scalar
  configuration key, and session wiring for the run id, the artifacts root and the
  `foundry: profile=<name>` terminal banner.
- **Fixture library** — the 12 public fixtures with their documented scopes: session-scoped
  `foundry_config`, `foundry_profile`, `foundry_run_id` and `foundry_artifacts`, plus
  function-scoped `foundry_http`, `foundry_api`, `foundry_data`, `foundry_db`, `foundry_factory`,
  `foundry_clock`, `foundry_log_capture` and `foundry_poll`.
- **HTTP and API layer** — a sync `httpx` session that returns every response including non-2xx, a
  retry engine with the typed predicates `timeout`, `connect`, `status_5xx` and `status_429` and
  clamped exponential backoff, the `none`, `bearer`, `basic` and `header` auth strategies resolving
  credentials by environment-variable name, and redaction of configured secrets before any write.
- **Assertion helpers** — `assert_status`, `assert_json_subset`, `assert_json_schema` and the
  `eventually` poller, each reporting a redacted, path-annotated failure.
- **Contract validation** — JSON Schema validation of HTTP response bodies and data files,
  reported as the first violation with its JSON path, ordered deterministically.
- **Data-driven testing** — CSV, JSON, YAML (`safe_load` only) and JSONL loaders, matrix and
  cross-product expansion, stable sorted case ids, schema enforcement, and collection-time errors
  naming file, line and column; a header-only file skips with `no cases in <file>`.
- **Record and replay** — one JSON cassette per test under `cassettes/`, matching on method, path,
  order-insensitive query and body hash, a replay transport that opens no socket, redaction at
  capture time, and a typed `CassetteMissError` naming the method, the path and the cassette path.
- **Execution features** — in-framework retries with backoff (not `pytest-rerunfailures`),
  quarantine markers in `report` and `strict` modes, per-test timeouts enforced through the
  pytest-timeout per-item timer hook, and sharding that yields the same nodeid set under
  `pytest-xdist` as the serial run.
- **Reporting** — immutable per-test result records in `results.json`, an enriched `junit.xml`, a
  self-contained offline `report.html` with inline assets, artifact capture with a per-test
  `manifest.json` and captured log tail, and `foundry report compare`.
- **CLI and diagnostics** — the `foundry` console script with the `run`, `init`, `config`,
  `report` and `doctor` subcommands (`config show`, `config validate`, `report compare`), the
  documented exit codes (`0`, `1`, `2`, preserving pytest's `3`, `4` and `5`), and the `doctor`
  environment probes.
- **Scaffolder** — `foundry init PATH` with the `minimal` and `api` templates, whose output runs
  green with no network and no configuration.
- **Quality and verification deliverables** — the self-test suite (unit, integration, `pytester`
  and end-to-end), the 24-test `examples/checkout` project with committed cassettes,
  `scripts/verify.sh`, `scripts/count_lines.sh` and `docs/verification.md`.

The public surface — the two entry points, the 12 fixture names and scopes, the eight exception
classes, the five subcommands and their exit-code meanings, the configuration keys, the
result-record fields and the nine outcome values, the cassette document fields, the artifact file
names and the three report file names — is fixed for `0.1.0`: later releases may add to it, and may
never rename or remove any part of it.
