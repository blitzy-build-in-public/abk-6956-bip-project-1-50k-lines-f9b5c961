#!/usr/bin/env bash
#
# count_lines.sh - the code-size budget gate for the Foundry tree.
#
# Purpose
#   Measure every source tree with wc -l and print one row per budget. Both
#   directions are enforced: a row below its minimum fails exactly like a row
#   above its maximum, which is how a missing deliverable is caught.
#
# Usage
#   ./scripts/count_lines.sh         measure the tree and report every row
#   ./scripts/count_lines.sh --help  print the usage block and stop
#   The script takes no other argument, and an unrecognised one is rejected
#   before anything is measured. It can be run from any directory: the root is
#   derived from this script's own location, so the caller's working directory
#   does not matter.
#
# Exit codes
#   0  every row is inside its band
#   1  at least one row is outside its band, in either direction
#   2  usage error, or a measurement fault: wc, find and sort are required
#      before the first row is counted, and a wc answer that is not a plain
#      number ends the run, so a fault is never reported as a budget verdict
#
# Binding standards (Agent Action Plan)
#   0.14.2  the budgets this script is the fail instrument for: src/foundry
#           28000..32000 lines, tests 12000..15000 lines, examples plus
#           scripts at most 2000 lines, total at most 50000 lines, and no
#           counted file above 700 lines.
#   0.14.1  criterion "Code-size budget" - the measurement: wc -l over
#           src/foundry/**/*.py, tests/**/*.py, examples/**/*.py plus every
#           file under scripts/**.
#   0.8.1, 0.8.2, 0.10.1  this script is a scripts/ deliverable.
#   0.1.1 and implicit requirement I12  the 700-line ceiling is why the package
#           is split at all.
#   0.14.3  constraints kept here: no network access, no package installation,
#           no unsafe parsing, no writes, deterministic output.
#   0.11    no user rules document exists for this project.
#
# Conventions: bash with set -euo pipefail and file mode 100755, no downloads,
# no installs. bash 3.2 (macOS) is supported, so bash-4-only features (bulk line
# reads, associative arrays, case-modification expansions) are not used. Output
# is English only, uncoloured and deterministic.

set -euo pipefail

# Budgets. Each number appears exactly once.
readonly SRC_MIN=28000
readonly SRC_MAX=32000
readonly TESTS_MIN=12000
readonly TESTS_MAX=15000
readonly EXAMPLES_SCRIPTS_MAX=2000
readonly TOTAL_MAX=50000
readonly MAX_FILE_LINES=700
# Floor shared by the rows whose only lower bound is "something was measured".
readonly NONEMPTY_MIN=1

# usage - the whole contract in one block: what is measured, that the script
# takes no argument, and what each exit status means. printf is a shell
# builtin, so --help needs nothing on PATH.
usage() {
  printf '%s\n' \
    'count_lines.sh - Foundry code-size budgets, measured with wc -l' \
    'Usage: count_lines.sh [--help]' \
    'Takes no other argument.' \
    'Budgets, physical lines measured with wc -l:' \
    '  src/foundry         28000..32000' \
    '  tests               12000..15000' \
    '  examples+scripts    1..2000' \
    '  total               1..50000' \
    '  largest file        1..700' \
    'Exit codes:' \
    '  0  every row is inside its band' \
    '  1  at least one row is outside its band, in either direction' \
    '  2  usage error, or a measurement fault: a required tool is missing, or' \
    '     wc did not report a line count'
}

# die <message> - the one diagnostic a fault prints, then the fault status.
# Nothing is measured after it, so a fault can never be read as a 0-line row.
die() {
  printf 'count_lines: %s\n' "$1" >&2
  exit 2
}

# Arguments are settled first so that --help still answers on a host whose
# tools are missing, and a mistyped flag is never taken for a measurement
# request.
case "${1:-}" in
  '') ;;
  -h | --help)
    usage
    exit 0
    ;;
  *)
    printf 'count_lines: unexpected argument: %s\n' "$1" >&2
    usage >&2
    exit 2
    ;;
esac

# require_tools - wc, find and sort are the entire measurement instrument, so
# they are proven present once, before the first row. Without this check a
# missing wc answers nothing per file and every row would report 0 lines,
# turning a measurement fault into five budget violations.
require_tools() {
  local tool
  for tool in wc find sort; do
    if ! command -v "${tool}" >/dev/null 2>&1; then
      die "required tool not found: ${tool}"
    fi
  done
}

require_tools

# Resolve the repository root from this script's own location so the rows stay
# root-relative no matter which directory the caller runs from.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd -- "${REPO_ROOT}"

# Rows that ended outside their band; summarised on stderr at the end.
FAILED_ROWS=0

# strip_space <value> - drop the blanks that BSD/macOS wc pads its count with,
# so the value is read as a plain number by shell arithmetic.
strip_space() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "${value}"
}

# line_count <file> - physical line count of one regular file. Fails closed: a
# wc answer that is not a plain number ends the run naming the file, so an
# empty string or a word can never reach the arithmetic below as a count.
line_count() {
  local path="$1"
  local value
  value="$(wc -l <"${path}")" || die "cannot measure ${path}: wc failed"
  value="$(strip_space "${value}")"
  case "${value}" in
    '' | *[!0-9]*)
      die "cannot measure ${path}: wc reported no line count"
      ;;
  esac
  printf '%s' "${value}"
}

# sum_lines - stdin paths -> "<lines> <files>". Line-delimited input is safe
# here: no path in this repository contains a newline. The line_count status is
# checked explicitly because a command-substitution subshell does not inherit
# set -e, so without the check a fault would be summed as zero lines.
sum_lines() {
  local total=0
  local files=0
  local path
  local lines
  while IFS= read -r path; do
    [ -f "${path}" ] || continue
    lines="$(line_count "${path}")" || exit 2
    total=$((total + lines))
    files=$((files + 1))
  done
  printf '%s %s\n' "${total}" "${files}"
}

# list_files <root> [glob] - sorted files under root. An absent root yields
# nothing, so a tree that has not been written yet becomes a 0-line row rather
# than a find error that truncates the report.
list_files() {
  local root="$1"
  [ -d "${root}" ] || return 0
  if [ -n "${2:-}" ]; then
    find "${root}" -type f -name "$2" -print | LC_ALL=C sort
  else
    find "${root}" -type f -print | LC_ALL=C sort
  fi
}

# list_counted - every file the budgets measure, one path per line.
list_counted() {
  list_files src/foundry '*.py'
  list_files tests '*.py'
  list_files examples '*.py'
  list_files scripts
}

# largest_of - stdin paths -> "<lines> <path>". The first maximum of a stream
# sorted with LC_ALL=C wins, so a tie reports the smallest path every run.
largest_of() {
  local best_lines=0
  local best_path=""
  local path
  local lines
  while IFS= read -r path; do
    [ -f "${path}" ] || continue
    lines="$(line_count "${path}")" || exit 2
    if [ "${lines}" -gt "${best_lines}" ]; then
      best_lines="${lines}"
      best_path="${path}"
    fi
  done
  printf '%s %s\n' "${best_lines}" "${best_path}"
}

# over_limit - stdin paths -> one line per file above the ceiling, so a breach
# names the files that have to be split.
over_limit() {
  local path
  local lines
  while IFS= read -r path; do
    [ -f "${path}" ] || continue
    lines="$(line_count "${path}")" || exit 2
    if [ "${lines}" -gt "${MAX_FILE_LINES}" ]; then
      printf '       above the %s-line ceiling: %s (%s lines)\n' \
        "${MAX_FILE_LINES}" "${path}" "${lines}"
    fi
  done
}

# check_band <label> <measured> <min> <max> [detail] - print one row and count
# the failure. Every row is printed, passing or not, so one run shows them all.
check_band() {
  local label="$1"
  local measured="$2"
  local min="$3"
  local max="$4"
  local detail="${5:-}"
  local status="ok"
  if [ "${measured}" -lt "${min}" ] || [ "${measured}" -gt "${max}" ]; then
    status="FAIL"
    FAILED_ROWS=$((FAILED_ROWS + 1))
  fi
  printf '%-4s %-18s %8s lines   (expected %s..%s)' \
    "${status}" "${label}" "${measured}" "${min}" "${max}"
  if [ -n "${detail}" ]; then
    printf '  %s' "${detail}"
  fi
  printf '\n'
}

# --- Measure ---------------------------------------------------------------

# Each row is captured in the current shell before it is split: the assignment
# carries the pipeline's status, so a line_count fault ends the run with status
# 2 instead of leaving an empty value that would print as a 0-line row.
SRC_ROW="$(list_files src/foundry '*.py' | sum_lines)" || exit 2
TESTS_ROW="$(list_files tests '*.py' | sum_lines)" || exit 2
EXAMPLES_ROW="$(list_files examples '*.py' | sum_lines)" || exit 2
SCRIPTS_ROW="$(list_files scripts | sum_lines)" || exit 2
LARGEST_ROW="$(list_counted | LC_ALL=C sort | largest_of)" || exit 2
read -r SRC_LINES _ <<<"${SRC_ROW}"
read -r TESTS_LINES _ <<<"${TESTS_ROW}"
read -r EXAMPLES_LINES _ <<<"${EXAMPLES_ROW}"
read -r SCRIPTS_LINES _ <<<"${SCRIPTS_ROW}"
read -r LARGEST_LINES LARGEST_PATH <<<"${LARGEST_ROW}"
EXAMPLES_SCRIPTS_LINES=$((EXAMPLES_LINES + SCRIPTS_LINES))
TOTAL_LINES=$((SRC_LINES + TESTS_LINES + EXAMPLES_SCRIPTS_LINES))

# --- Evaluate every row ----------------------------------------------------

# Printed only after every row has been counted, so a fault during the counting
# passes leaves stdout empty instead of a report built from values that were
# never measured. The over-limit re-measure below runs only once a row printed
# a breach, so a fault there is reported beside rows that were all measured and
# still ends the run with status 2.
printf 'Foundry code-size budgets, physical lines measured with wc -l\n'

check_band "src/foundry" "${SRC_LINES}" "${SRC_MIN}" "${SRC_MAX}"
check_band "tests" "${TESTS_LINES}" "${TESTS_MIN}" "${TESTS_MAX}"
check_band "examples+scripts" "${EXAMPLES_SCRIPTS_LINES}" \
  "${NONEMPTY_MIN}" "${EXAMPLES_SCRIPTS_MAX}"
check_band "total" "${TOTAL_LINES}" "${NONEMPTY_MIN}" "${TOTAL_MAX}"
check_band "largest file" "${LARGEST_LINES}" "${NONEMPTY_MIN}" \
  "${MAX_FILE_LINES}" "${LARGEST_PATH:-(none)}"
if [ "${LARGEST_LINES}" -gt "${MAX_FILE_LINES}" ]; then
  list_counted | LC_ALL=C sort | over_limit
fi

if [ "${FAILED_ROWS}" -ne 0 ]; then
  printf 'count_lines: %s of 5 rows outside budget\n' "${FAILED_ROWS}" >&2
  exit 1
fi
printf 'count_lines: all 5 rows within budget\n'
