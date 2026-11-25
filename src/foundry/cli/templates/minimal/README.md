# Test project

A pytest project with Foundry installed. Foundry adds layered configuration, HTTP
helpers, data-driven cases, record and replay, in-framework retries, artifact capture
and an HTML report to a plain pytest suite. This scaffold starts you with a passing
test, a commented configuration and one example data file.

## Run the suite

1. `foundry run` — runs the suite and writes this run's outputs.
2. `pytest` — runs the same suite directly; Foundry activates with no flag and no
   configuration file.
3. Open `report.html` in the project directory to read the run. `results.json` and
   `junit.xml` sit beside it; `--foundry-json-report PATH` moves `results.json` to
   `PATH` and writes the other two beside it.

Each run collects per-test artifacts (request and response dumps, stdout, stderr,
traceback, the log tail and a manifest) under `artifacts/<run_id>/<test id>/`, inside
the directory the terminal summary's run line names, and the ignore file keeps every
run output out of version control.

## Configuration

`foundry.toml` lists the settings a starting project needs as commented defaults, so the
minimal project resolves the configuration Foundry uses with no file at all. Precedence,
lowest to highest, is defaults, the file, `FOUNDRY_*` environment variables, then
`--foundry-<dotted.key>` options. `foundry config show --resolved` prints each resolved
setting with the layer it came from.

Four profiles are available: `default`, `local`, `ci` and `staging`.

## Files

- `foundry.toml` — the settings a starting project needs, as commented defaults.
- `.gitignore` — keeps the run outputs (`artifacts/`, `results.json`, `junit.xml`,
  `report.html`) and the tool caches out of version control.
- `artifacts/` — created empty; each run fills `artifacts/<run_id>/`.
- `testdata/orders.csv` and `testdata/orders.schema.json` — one example data file and
  the JSON Schema that describes a row of it.
- `tests/test_smoke.py` — the passing test this scaffold starts with.

`--template api` adds `tests/test_api.py`, a `cassettes/` directory holding the recording
that test replays, and a `foundry.toml` of its own that sets `http.base_url` and
`cassette.mode = "replay"` as active values rather than commented defaults.
