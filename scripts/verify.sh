#!/usr/bin/env bash
#
# verify.sh - the single verification entry point for the Foundry tree: the six
# gates of AAP 0.14.4, in that order, stopping at the first failure with that
# gate's own status. Each step prints its name, then either "ok" or its failing
# output.
#   1. ruff check
#   2. ruff format --check
#   3. mypy --strict src/foundry
#   4. scripts/count_lines.sh
#   5. the two-module smoke coverage run under -n 2, proving pytest, pytest-cov
#      and pytest-xdist load together before the long run
#   6. the coverage suite under -n auto: its 360 s ceiling, then the coverage.json
#      zero-module assertion
#
# Usage: ./scripts/verify.sh, from any directory; the root comes from this
# script's location, so coverage.json lands at the repository root and every
# root-relative path below resolves the same way. It takes no argument. The
# environment must already be synced (uv sync --extra dev), and this script puts
# ./.venv/bin in front of PATH itself, so no activation step is required. It
# installs nothing, contacts no network and writes only gitignored paths:
# coverage.json, .coverage*, the tool caches and the run outputs Foundry writes.
#
# Exit codes: 0 every gate passed; <n> the first failing gate's own status,
# re-exited verbatim (1 for a code-size breach, the wall-clock ceiling, a failing
# test, a coverage shortfall or a module at 0 % coverage); 2 a required tool is
# missing, or an unexpected argument was given.
#
# Binding standards (Agent Action Plan): 0.14.4 gate order, per-step output and
# exit behaviour, "the only command a reviewer needs"; 0.14.2 the 360 s ceiling
# and the 85 % coverage floor; 0.14.1 the "Quality gates" and "Reachability"
# criteria this script produces; 0.14.3 no installation, no network, no unsafe
# parsing, deterministic output; 0.10.1/0.8.1/0.8.2/0.9.1 this script is a
# scripts/ deliverable; 0.11 no user rules document exists for this project, so
# the AAP is binding.
#
# bash, set -euo pipefail, mode 100755, bash 3.2 (macOS) compatible, no
# bash-4-only expansion, English-only uncoloured output.

set -euo pipefail

# Gate 6's paths and ceilings; COVERAGE_JSON is root-relative because the script
# cd's to the root. Gate 5's two modules are held in an array so the paths reach
# pytest unsplit.
readonly COVERAGE_JSON="coverage.json"
readonly SUITE_CEILING_SECONDS=360
readonly COVERAGE_FLOOR=85
readonly SMOKE_MODULES=(tests/unit/test_core.py tests/unit/test_obs.py)

# The repository root comes from this script's own location, so every relative
# path below resolves identically from any caller's directory; the synced
# environment beside it holds the lockfile's ruff, mypy, pytest and python3.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
VENV_BIN="${REPO_ROOT}/.venv/bin"
cd -- "${REPO_ROOT}"

# use_project_venv - put the project environment in front of PATH, so the tools
# the gates run are the lockfile's pins rather than whatever the caller's PATH
# holds. AAP 0.14.4 calls this script "the only command a reviewer needs", so it
# must not also demand an activation step, and gates 5 and 6 need the pinned
# pytest together with pytest-cov and pytest-xdist, which only .venv carries. It
# prepends an existing directory once and does nothing else: it installs
# nothing, creates nothing and contacts no network (AAP 0.14.4), and a tree with
# no .venv is left as found for require_tools to report.
use_project_venv() {
  case ":${PATH}:" in
    *":${VENV_BIN}:"*) return 0 ;;
  esac
  if [ -d "${VENV_BIN}" ]; then
    PATH="${VENV_BIN}${PATH:+:${PATH}}"
    export PATH
  fi
}

# require_tools - name the first missing tool and stop before any gate runs, so
# an unsynced environment is one line rather than a cascade of command-not-found
# and a gate blamed for the wrong reason. Not a gate: it installs nothing and
# exits 2, which AAP 0.4.4 reads as a usage or configuration error.
require_tools() {
  local tool
  for tool in ruff mypy pytest python3; do
    if ! command -v "${tool}" >/dev/null 2>&1; then
      printf 'verify: required tool not found: %s\n' "${tool}" >&2
      printf 'verify: sync the environment: uv sync --extra dev\n' >&2
      printf 'verify: then re-run this script, or activate it: source %s/activate\n' \
        "${VENV_BIN}" >&2
      exit 2
    fi
  done
}

# No argument is expected, so a mistyped flag never runs the whole gate. This
# runs before the tool check, so --help answers on a bare environment too.
case "${1:-}" in
  '') ;;
  -h | --help)
    printf '%s\n' 'Usage: scripts/verify.sh' \
      'Takes no argument; runs the six gates of AAP 0.14.4 in order.'
    exit 0
    ;;
  *)
    printf 'verify: unexpected argument: %s\n' "$1" >&2
    exit 2
    ;;
esac

use_project_venv
require_tools

# run_step <name> <command> [args...] - print the step, run it with its own
# stdout and stderr passed through untouched, then print "ok". On failure it
# prints the status and exits with it verbatim: AAP 0.14.4's "exits with the first
# failing step's status". The command is invoked as a positional parameter list,
# never through a shell string, so nothing is re-parsed or word-split.
run_step() {
  local name="$1"
  shift
  local status=0
  printf '==> %s\n' "${name}"
  "$@" || status=$?
  if [ "${status}" -ne 0 ]; then
    printf 'FAIL %s (exit %s)\n' "${name}" "${status}" >&2
    exit "${status}"
  fi
  printf 'ok   %s\n' "${name}"
}

# assert_no_zero_coverage - read the coverage.json gate 6 just wrote and fail,
# listing the offenders sorted, when a src/foundry module has statements but no
# covered line (AAP 0.14.1 "Reachability"). coverage.json is read rather than the
# terminal summary, because a mis-scoped --cov still prints a summary; a document
# that cannot be read, holds no src/foundry entry, or is not JSON fails too, so a
# broken scope cannot pass silently. Parsed with python3, never a shell
# evaluator (AAP 0.14.3).
assert_no_zero_coverage() {
  python3 - "${COVERAGE_JSON}" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        files = json.load(handle).get("files", {})
except (OSError, ValueError) as error:
    raise SystemExit(f"verify: cannot read {sys.argv[1]}: {error}") from error
measured = {
    name: entry.get("summary", {})
    for name, entry in files.items()
    if name.startswith("src/foundry")
}
if not measured:
    raise SystemExit(f"verify: no src/foundry module measured in {sys.argv[1]}")
empty = sorted(
    name
    for name, summary in measured.items()
    if summary.get("num_statements", 0) > 0 and summary.get("covered_lines", 0) == 0
)
if empty:
    print(f"verify: {len(empty)} module(s) at 0% coverage:", file=sys.stderr)
    print("\n".join(f"  {name}" for name in empty), file=sys.stderr)
    raise SystemExit(1)
print(f"{len(measured)} module(s) measured, none at 0% coverage")
PY
}

# run_coverage_gate <name> - gate 6. The suite is timed with bash's SECONDS
# rather than wrapped in GNU timeout, which macOS does not ship; AAP 0.14.2 is a
# ceiling, not a kill. The status is captured rather than aborting, so the wall
# clock is always printed. A failing suite exits with its own status; an
# over-budget run fails gate 6 with 1.
run_coverage_gate() {
  local name="$1"
  local status=0
  printf '==> %s\n' "${name}"
  local start="${SECONDS}"
  pytest --cov=foundry "--cov-fail-under=${COVERAGE_FLOOR}" \
    --cov-report=term-missing "--cov-report=json:${COVERAGE_JSON}" -n auto || status=$?
  local elapsed=$((SECONDS - start))
  printf 'suite wall clock: %s s (ceiling %s s)\n' "${elapsed}" "${SUITE_CEILING_SECONDS}"
  if [ "${status}" -ne 0 ]; then
    printf 'FAIL %s (exit %s)\n' "${name}" "${status}" >&2
    exit "${status}"
  fi
  if [ "${elapsed}" -gt "${SUITE_CEILING_SECONDS}" ]; then
    printf 'FAIL suite wall clock (%s s > %s s)\n' \
      "${elapsed}" "${SUITE_CEILING_SECONDS}" >&2
    exit 1
  fi
  printf 'ok   %s\n' "${name}"
}

# Gates 1-4. Settings come from pyproject.toml: [tool.ruff] for gates 1 and 2,
# [tool.mypy] for gate 3 with the path spelled out so the gate stays honest if
# `files` ever changes, and the five code-size budgets of gate 4 from the sibling
# script, invoked through bash so the gate does not depend on its file mode.
run_step 'ruff check' ruff check
run_step 'ruff format --check' ruff format --check
run_step 'mypy --strict src/foundry' mypy --strict src/foundry
run_step 'scripts/count_lines.sh' bash scripts/count_lines.sh

# Gate 5. The coverage flags are on the command line because
# [tool.pytest.ini_options] deliberately configures no coverage section;
# --cov-fail-under is absent here on purpose, since this is a liveness run.
run_step 'pytest --cov=foundry --cov-report=term -n 2 smoke modules' \
  pytest --cov=foundry --cov-report=term -n 2 "${SMOKE_MODULES[@]}"

# Gate 6, then the zero-module assertion over the document it wrote.
run_coverage_gate "pytest --cov=foundry --cov-fail-under=${COVERAGE_FLOOR} -n auto"
run_step 'coverage.json: no src/foundry module at 0%' assert_no_zero_coverage

printf 'ok   suite (coverage >= %s%%, no module at 0%%)\n' "${COVERAGE_FLOOR}"
