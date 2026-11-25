# Dependency provenance

This file records the provenance of every dependency `pytest-foundry` 0.1.0 declares: the registry it
comes from, the version that resolves, the licence it carries, the confirmation that the licence is
permissive, and the log a new dependency must enter before it may join the manifest. It is generated
from `pyproject.toml` and subordinate to it — the manifest is what the build installs, and this page
documents that manifest rather than defining it. Versions and licences were read from the installed
distributions' own metadata (`Version`, `License-Expression` and the `License ::` classifiers), not
inferred from package names.

## Source of truth

`[project] dependencies` and `[project.optional-dependencies] dev` in the root `pyproject.toml` are
authoritative. This document mirrors them, one row per declared package, with each constraint string
quoted verbatim. Where the two disagree, the disagreement is corrected here, never in the manifest on
this document's authority: a row the manifest does not declare is deleted, and a package the manifest
declares gains a row.

`uv` is the environment and build tool rather than a declared dependency, so it has no row. The
committed lockfile records the full resolved closure, transitive packages included; it declares
nothing, and nothing in it is a dependency of Foundry until it appears in `pyproject.toml`.

## Runtime dependencies

Declared in `[project] dependencies`; installed by every consumer of `pytest-foundry`.

| Package | Registry | Declared constraint | Resolved version | Licence | Permissive confirmation | Purpose |
|---|---|---|---|---|---|---|
| pytest | PyPI | `>=8.2,<9` | 8.4.2 | MIT | Permissive (MIT); no obligation beyond retaining the notice. | Plugin host; the `pytester` fixture for plugin self-tests |
| httpx | PyPI | `>=0.27,<0.28` | 0.27.2 | BSD-3-Clause | Permissive (BSD-3-Clause); notice retention only. | Sync HTTP client; redirects off, timeouts from configuration, custom transport as the replay seam |
| pydantic | PyPI | `>=2.7,<3` | 2.14.4 | MIT | Permissive (MIT); no obligation beyond retaining the notice. | Configuration models and frozen result records with `extra="forbid"` |
| pydantic-settings | PyPI | `>=2.2,<3` | 2.16.3 | MIT | Permissive (MIT); no obligation beyond retaining the notice. | `FOUNDRY_` environment source with `__` nesting |
| click | PyPI | `>=8.1,<9` | 8.5.1 | BSD-3-Clause | Permissive (BSD-3-Clause); notice retention only. | CLI; `CliRunner` used by tests |
| rich | PyPI | `>=13.7,<14` | 13.9.4 | MIT | Permissive (MIT); no obligation beyond retaining the notice. | CLI tables and `doctor` output |
| Jinja2 | PyPI | `>=3.1,<4` | 3.1.6 | BSD-3-Clause | Permissive (BSD-3-Clause); notice retention only. | HTML report template with `autoescape=True` |
| PyYAML | PyPI | `>=6.0.1,<7` | 6.0.4 | MIT | Permissive (MIT); no obligation beyond retaining the notice. | YAML data files and cassette YAML content; `safe_load` only |
| jsonschema | PyPI | `>=4.22,<5` | 4.26.0 | MIT | Permissive (MIT); no obligation beyond retaining the notice. | Response-body and data-file contract validation |
| referencing | PyPI | `>=0.28.4,<0.38` | 0.37.0 | MIT | Permissive (MIT); no obligation beyond retaining the notice. | The `referencing.exceptions.Unresolvable` that `foundry.contract.validate` catches; its bounds pin the copy `jsonschema` already requires |
| Faker | PyPI | `>=25,<26` | 25.9.2 | MIT | Permissive (MIT); no obligation beyond retaining the notice. | Seeded factories through `Faker.seed_instance` |
| SQLAlchemy | PyPI | `>=2.0.30,<2.1` | 2.0.58 | MIT | Permissive (MIT); no obligation beyond retaining the notice. | sqlite fixtures; in-memory engine per test; transactional rollback |
| pytest-xdist | PyPI | `>=3.6,<4` | 3.8.0 | MIT | Permissive (MIT); no obligation beyond retaining the notice. | Sharding; `workerinput` detection in the plugin |
| pytest-timeout | PyPI | `>=2.3,<3` | 2.4.0 | MIT | Permissive (MIT); no obligation beyond retaining the notice. | Declared per-item timeout enforcement; the newest installable release in range, because 2.5.0 is yanked |
| tomli-w | PyPI | `>=1.0,<2` | 1.2.0 | MIT | Permissive (MIT); no obligation beyond retaining the notice. | Writing `foundry.toml` in `foundry init`; TOML reads use stdlib `tomllib` |

The declared-constraint column is quoted from `pyproject.toml`, and the resolved version is the
release the offline wheel cache is populated with, the release the committed lockfile records, and a
release that satisfies its constraint. `referencing` is the one row added after the initial
inventory, and its reason is in the added-dependency log below.

## Development dependencies

Declared in `[project.optional-dependencies] dev`; installed only for Foundry's own build, lint,
type-check and test gates.

| Package | Registry | Declared constraint | Resolved version | Licence | Permissive confirmation | Purpose |
|---|---|---|---|---|---|---|
| ruff | PyPI | `>=0.5,<1` | 0.17.6 | MIT | Permissive (MIT); no obligation beyond retaining the notice. | Linting and formatting gate |
| mypy | PyPI | `>=1.10,<2` | 1.20.2 | MIT | Permissive (MIT); no obligation beyond retaining the notice. | `--strict` type gate |
| pytest-cov | PyPI | `>=5,<6` | 5.0.0 | MIT | Permissive (MIT); no obligation beyond retaining the notice. | Coverage measurement and the `--cov-fail-under=85` gate |
| types-PyYAML | PyPI | none declared (no version specifier) | 6.0.12.20261122 | Apache-2.0 | Permissive (Apache-2.0); notice retention only. | Stubs for `mypy --strict` |
| types-jsonschema | PyPI | none declared (no version specifier) | 4.26.0.20261006 | Apache-2.0 | Permissive (Apache-2.0); notice retention only. | Stubs for `mypy --strict` |
| types-toml | PyPI | none declared (no version specifier) | 0.10.8.20260518 | Apache-2.0 | Permissive (Apache-2.0); notice retention only. | Stubs for `mypy --strict` |
| build | PyPI | `>=1.2` | 1.6.1 | MIT | Permissive (MIT); no obligation beyond retaining the notice. | Sdist and wheel inspection in the install test |

`types-PyYAML`, `types-jsonschema` and `types-toml` are declared without a version specifier; each was
recorded at its latest release at resolution time, and all three exist solely to satisfy
`mypy --strict`.

## Licence posture

All 22 declared dependencies carry a permissive licence, and only three families appear: MIT across
16 packages, BSD-3-Clause across `httpx`, `click` and `Jinja2`, and Apache-2.0 across the three
`types-*` stub packages. Consequently:

- There is no copyleft dependency: no GPL, LGPL, AGPL, MPL or EPL package is declared or reached.
- There is no source-available or business-source licence among them.
- No licence imposes an attribution, advertising or share-alike obligation beyond retaining the
  copyright notice and licence text.
- No dependency's terms reach Foundry's own MIT-licensed code.

These three licence families are the only ones allowed without an entry in the added-dependency log.
A dependency under any other licence is refused rather than logged.

## Dependency closure and offline installation

Transitive packages are deliberately not listed as rows, because the manifest does not declare them
and this file records declarations. The committed lockfile pins the whole closure, and every
verification run installs it offline from a pre-populated cache, so that cache must carry wheels for
CPython 3.11, 3.12 and 3.13 on Linux and macOS — including the native builds of `pydantic-core` (via
`pydantic`), `rpds-py` (via `jsonschema` and `referencing`), `greenlet` (via `SQLAlchemy` on
x86_64), `PyYAML` and `SQLAlchemy` themselves, and `ruff`'s per-platform binaries. The notable
transitive pulls are `httpcore`, `anyio`, `certifi`, `idna` and `sniffio` (via `httpx`), `execnet`
(via `pytest-xdist`), `python-dotenv` (via `pydantic-settings`) and `python-dateutil` (via `Faker`).

No third-party source is copied into this project. Every package named here arrives as an installed
distribution: the wheel's file list holds `foundry/**` and the distribution metadata directory and
nothing else, and the install test fails when the fresh environment holds a distribution that no
declared requirement reaches.

## Deliberately excluded packages

| Package or tool | Why it is not declared |
|---|---|
| `pytest-rerunfailures` | Retries are implemented in-framework, because retry must interoperate with quarantine state and artifact capture |
| `pytest-socket` | The offline guard is installed by the example project's `conftest.py`, not shipped as a runtime dependency |
| `vcrpy`, `betamax` | Cassettes are implemented in-house so that redaction happens at capture time |
| `pytest-html` | The HTML report is self-contained by design |
| `requests`, `aiohttp` | `httpx` is the only HTTP client, and HTTP is sync-only |
| Any async database driver (`aiosqlite`, `asyncpg`) | sqlite access is synchronous |
| Browser automation (`playwright`, `selenium`) | Out of scope for this release |
| Load and performance testing tools | Out of scope for this release |
| Coverage-merge and mutation-testing tools | Out of scope for this release |

This list is the starting state of the exclusion record: adding any of these requires an entry in the
next section first.

## Added-dependency log

No package may be added to `pyproject.toml` until an entry here records its registry, its resolved
version, its licence, the confirmation that the licence is permissive, the layer it joins (runtime or
development) and the reason the existing packages cannot serve. The entry lands before the manifest
change, and the package then also gains a row in the runtime or development table above. A dependency
whose licence is not permissive is refused, not logged.

| Date | Package | Registry | Resolved version | Licence | Permissive | Layer | Justification |
|---|---|---|---|---|---|---|---|
| 2026-11-24 | `referencing` | PyPI | 0.37.0 | MIT | Yes — MIT is permissive | runtime | `foundry.contract.validate` catches `referencing.exceptions.Unresolvable`, so the module names the package it imports instead of borrowing it from `jsonschema`. Reasoning below. |

**Why it is declared, and what it costs.** `foundry.contract.validate` reports a schema whose `$ref`
names a missing subschema as a `ContractError`, which means catching
`referencing.exceptions.Unresolvable` — the public type that `jsonschema` 4.18 and later raises
through a private wrapper, and the one its own deprecation notice directs callers to catch. A module
that imports a package names it, so the dependency is declared rather than relied on as a transitive
requirement of `jsonschema`. The floor is `jsonschema` 4.26.0's own requirement, `referencing>=0.28.4`,
and the ceiling is the next minor after the resolved 0.37.0, following the upper-bound convention the
other runtime constraints use. Declaring it adds no package to the offline closure, because
`jsonschema` already requires `referencing` and everything `referencing` itself requires is already
installed transitively, so the install test's closure assertion sees nothing new. The import sits
inside the function that needs it and never at module scope, so plugin startup cost is unchanged.

## Known advisories and their mitigations

One declared dependency carries an open advisory at the version this release resolves. It is recorded
here rather than closed in the manifest, because the fixed release falls outside the constraint the
specification fixes, and the suite is instead configured so that the advisory's vector is not reachable
from this repository's own runs.

**`pytest` 8.4.2, GHSA-6w46-j5rx-g56g / CVE-2025-71176 / PYSEC-2026-1845.** CWE-379 (generation of a
predictable temporary file name), CVSS 3.1 score 6.8, affected releases below 9.0.3, fixed in 9.0.3.
pytest's default base temporary directory is the predictable `{tempfile.gettempdir()}/pytest-of-{user}`,
which it creates with `exist_ok=True` and then validates through a symlink-following `stat()`, so on a
host where other users can write the shared temporary root an attacker who pre-creates that name
redirects the session's temporary tree. This suite requests `tmp_path` and `tmpdir` throughout.

**Why the constraint stands.** `pyproject.toml` declares `pytest>=8.2,<9` because the specification
fixes that range in its dependency table (AAP 0.5.1) and keeps pytest at 8.4.2 with `pytest-cov` 5.0.0
(AAP 0.14.6 A10). The fixed 9.0.3 release is outside that range, and changing a declared constraint
means revising the specification, which a fix may not do.

**What the tree does instead.** `tests/conftest.py` creates a private `tempfile.mkdtemp` root of
unpredictable name and mode `0700`, exports it as `PYTEST_DEBUG_TEMPROOT` — so every child process,
`pytester` run and worker inherits it — and as `TMPDIR` when the caller set none, and points the session's
`basetemp` inside it.
pytest uses a given base directory directly, so its `pytest-of-{user}` branch is not entered from this
repository's runs; the root is deleted after a clean run and kept, with its path printed, after a failing
one. `tests/unit/test_private_temproot.py` asserts the pin through a real session.

**Residual risk.** The mitigation covers this repository's own `tests/` runs only. A bare `pytest` over
another tree, whether `pytest examples/checkout` or a consumer's own project, still uses pytest 8.4.2's
default base directory unless `TMPDIR` or `PYTEST_DEBUG_TEMPROOT` names a private root, and every project
that installs `pytest-foundry` inherits this constraint and therefore that default. Upstream's fix leaves
the predictable name in place, so a local user who pre-creates `pytest-of-{user}` can still make a run
fail, a denial of service the advisory itself records as unaddressed.

**When the constraint is next revised.** Raise the floor to `>=9.0.3`, re-verify `pytest-cov` and
`pytest-xdist` against it, then delete the pin in `tests/conftest.py`, its guard test, and this section's
mitigation paragraph.

## Version-resolution notes

- `pytest-timeout` 2.5.0 is yanked on the registry, so the newest non-yanked release inside
  `>=2.3,<3` is 2.4.0, which declares `pytest>=7.0.0` and therefore accepts the 8.4.2 pin. A range
  constraint must never let an installer select the yanked release.
- `pytest` 8.4.2 carries one open advisory, GHSA-6w46-j5rx-g56g / CVE-2025-71176; the constraint that
  keeps it cannot reach the 9.0.3 fix, and "Known advisories and their mitigations" above records the
  private temporary root the suite pins instead.
- `pytest-cov` 5.0.0 runs against pytest 8.4.2 because the project's pytest configuration sets no
  `filterwarnings=error` entry and therefore stays outside the documented `INTERNALERROR` trigger for
  that plugin combination. The recorded fallback, if such a setting is ever adopted, is pytest 8.3.5
  with pytest-cov 5.0.0, both inside their declared ranges, so no constraint changes.
- The three `types-*` stub packages carry no version specifier in the manifest and were recorded at
  their latest releases at resolution time; they exist solely to satisfy `mypy --strict`.
