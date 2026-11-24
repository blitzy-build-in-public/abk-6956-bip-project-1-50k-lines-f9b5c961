#!/usr/bin/env bash
#
# count_lines.sh - the code-size budget gate for the Foundry tree.
#
# Purpose
#   Measure every source tree with wc -l and print one row per budget. Both
#   directions are enforced: a row below its minimum fails exactly like a row
#   above its maximum, which is how a missing deliverable is caught. Exit 0
#   when all five rows are inside their band, 1 otherwise. Run from the
#   repository root as ./scripts/count_lines.sh (or scripts/count_lines.sh);
#   the root is derived from this script's own location, so the caller's
#   working directory does not matter.
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

# line_count <file> - physical line count of one regular file.
line_count() {
  strip_space "$(wc -l <"$1")"
}

# sum_lines - stdin paths -> "<lines> <files>". Line-delimited input is safe
# here: no path in this repository contains a newline.
sum_lines() {
  local total=0
  local files=0
  local path
  local lines
  while IFS= read -r path; do
    [ -f "${path}" ] || continue
    lines="$(line_count "${path}")"
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
    lines="$(line_count "${path}")"
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
    lines="$(line_count "${path}")"
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

printf 'Foundry code-size budgets, physical lines measured with wc -l\n'

read -r SRC_LINES _ <<<"$(list_files src/foundry '*.py' | sum_lines)"
read -r TESTS_LINES _ <<<"$(list_files tests '*.py' | sum_lines)"
read -r EXAMPLES_LINES _ <<<"$(list_files examples '*.py' | sum_lines)"
read -r SCRIPTS_LINES _ <<<"$(list_files scripts | sum_lines)"
EXAMPLES_SCRIPTS_LINES=$((EXAMPLES_LINES + SCRIPTS_LINES))
TOTAL_LINES=$((SRC_LINES + TESTS_LINES + EXAMPLES_SCRIPTS_LINES))
read -r LARGEST_LINES LARGEST_PATH <<<"$(list_counted | LC_ALL=C sort | largest_of)"

# --- Evaluate every row ----------------------------------------------------

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
