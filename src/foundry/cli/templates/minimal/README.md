# Test project

A pytest project with Foundry installed. Foundry adds layered configuration, HTTP
helpers, data-driven cases, record and replay, in-framework retries, artifact capture
and an HTML report to a plain pytest suite. This scaffold starts you with a passing
test, a commented configuration and one example data file.

## Run the suite

1. `foundry run` — runs the suite and writes this run's outputs.
2. `pytest` — runs the same suite directly; Foundry activates with no flag and no
   configuration file.
3. Open `artifacts/<run_id>/report.html` to read the run.

Each run collects per-test artifacts (request and response dumps, stdout, stderr,
traceback, the log tail and a manifest) under `artifacts/<run_id>/<test id>/`, and the
ignore file keeps every run output out of version control.

## Configuration

`foundry.toml` lists every supported setting as a commented default, so nothing changes
behaviour until you uncomment a line. Precedence, lowest to highest, is defaults, the
file, `FOUNDRY_*` environment variables, then `--foundry-<dotted.key>` options.
`foundry config show --resolved` prints each resolved setting with the layer it came
from.

Four profiles are available: `default`, `local`, `ci` and `staging`.

## Files

- `foundry.toml` — every setting as a commented default.
- `testdata/orders.csv` and `testdata/orders.schema.json` — one example data file and
  the JSON Schema that describes a row of it.
- `tests/test_smoke.py` — the passing test this scaffold starts with.
