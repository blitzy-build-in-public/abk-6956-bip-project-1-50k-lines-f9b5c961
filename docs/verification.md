# Dependency verification

Foundry's dependency list is closed. Every package below is declared in `pyproject.toml` and no
third-party code is vendored: the wheel ships `foundry/**` and the root metadata files only.

The registry is PyPI for every package. The resolved version is the release the offline build
installs from the pre-populated `uv` cache, and it is the release recorded in the committed
`uv.lock`. Versions and licences were read from the installed distributions' own metadata
(`importlib.metadata`: `Version`, `License-Expression` and the `License ::` classifiers), not
inferred from package names.

Every licence in this file is permissive — MIT, BSD-3-Clause or Apache-2.0. None is copyleft, none
is source-available, and none requires attribution beyond a notice.

## Runtime dependencies

Declared in `[project] dependencies`; installed by every consumer of `pytest-foundry`.

| Package | Declared constraint | Registry | Resolved version | Licence | Permissive |
|---|---|---|---|---|---|
| pytest | `>=8.2,<9` | PyPI | 8.4.2 | MIT | Yes — MIT is permissive |
| httpx | `>=0.27,<0.28` | PyPI | 0.27.2 | BSD-3-Clause | Yes — BSD-3-Clause is permissive |
| pydantic | `>=2.7,<3` | PyPI | 2.14.4 | MIT | Yes — MIT is permissive |
| pydantic-settings | `>=2.2,<3` | PyPI | 2.16.3 | MIT | Yes — MIT is permissive |
| click | `>=8.1,<9` | PyPI | 8.5.1 | BSD-3-Clause | Yes — BSD-3-Clause is permissive |
| rich | `>=13.7,<14` | PyPI | 13.9.4 | MIT | Yes — MIT is permissive |
| Jinja2 | `>=3.1,<4` | PyPI | 3.1.6 | BSD-3-Clause | Yes — BSD-3-Clause is permissive |
| PyYAML | `>=6.0.1,<7` | PyPI | 6.0.4 | MIT | Yes — MIT is permissive |
| jsonschema | `>=4.22,<5` | PyPI | 4.26.0 | MIT | Yes — MIT is permissive |
| referencing | `>=0.28.4,<0.38` | PyPI | 0.37.0 | MIT | Yes — MIT is permissive |
| Faker | `>=25,<26` | PyPI | 25.9.2 | MIT | Yes — MIT is permissive |
| SQLAlchemy | `>=2.0.30,<2.1` | PyPI | 2.0.58 | MIT | Yes — MIT is permissive |
| pytest-xdist | `>=3.6,<4` | PyPI | 3.8.0 | MIT | Yes — MIT is permissive |
| pytest-timeout | `>=2.3,<3` | PyPI | 2.4.0 | MIT | Yes — MIT is permissive |
| tomli-w | `>=1.0,<2` | PyPI | 1.2.0 | MIT | Yes — MIT is permissive |

`referencing` is the one entry this inventory gained after the initial dependency set; its reason is
in the justification log below.

## Development dependencies

Declared in `[project.optional-dependencies] dev`; installed only for Foundry's own build, lint,
type-check and test gates.

| Package | Declared constraint | Registry | Resolved version | Licence | Permissive |
|---|---|---|---|---|---|
| ruff | `>=0.5,<1` | PyPI | 0.17.6 | MIT | Yes — MIT is permissive |
| mypy | `>=1.10,<2` | PyPI | 1.20.2 | MIT | Yes — MIT is permissive |
| pytest-cov | `>=5,<6` | PyPI | 5.0.0 | MIT | Yes — MIT is permissive |
| types-PyYAML | none (stub package) | PyPI | 6.0.12.20261122 | Apache-2.0 | Yes — Apache-2.0 is permissive |
| types-jsonschema | none (stub package) | PyPI | 4.26.0.20261006 | Apache-2.0 | Yes — Apache-2.0 is permissive |
| types-toml | none (stub package) | PyPI | 0.10.8.20260518 | Apache-2.0 | Yes — Apache-2.0 is permissive |
| build | `>=1.2` | PyPI | 1.6.1 | MIT | Yes — MIT is permissive |

`uv` is the environment and build tool rather than a declared dependency. Transitive packages are
not listed here because the manifest does not declare them; the offline cache must still contain
them, including the native wheels for `pydantic-core`, `rpds-py`, `greenlet`, `PyYAML`,
`SQLAlchemy` and `ruff` on CPython 3.11, 3.12 and 3.13.

## Added-dependency justification log

One entry per dependency added to `pyproject.toml` after the initial set. An addition is recorded
here before it reaches the manifest, because the list is closed.

### `referencing>=0.28.4,<0.38` — `referencing` 0.37.0

- **What is declared.** `referencing`, resolved from the offline cache at 0.37.0 (MIT), constrained
  `>=0.28.4,<0.38`.
- **Why it is declared.** `foundry.contract.validate` catches `referencing.exceptions.Unresolvable`
  to report a schema whose `$ref` names a subschema that does not exist as a `ContractError`. That
  is the type a caller must catch: `jsonschema` 4.18 and later raises
  `jsonschema.exceptions._WrappedReferencingError`, which derives from both
  `referencing.exceptions.Unresolvable` and the retired `_RefResolutionError`, and reading
  `jsonschema.exceptions.RefResolutionError` emits `DeprecationWarning: ... is deprecated as of
  version 4.18.0. If you wish to catch potential reference resolution errors, directly catch
  referencing.exceptions.Unresolvable.` The module therefore imports a package it names directly,
  so that package is declared rather than relied on as a transitive requires-dist of `jsonschema`.
- **Constraint bounds.** The floor is `jsonschema` 4.26.0's own requirement, `referencing>=0.28.4`.
  The ceiling is the next minor after the resolved 0.37.0, following the 0.x upper-bound convention
  the other runtime constraints use.
- **No new package in the offline closure.** Declaring it changes nothing that gets installed:
  `jsonschema` 4.26.0 and `jsonschema-specifications` 2025.9.1 already require `referencing`, so
  `uv lock` resolves the same 54 packages, and `referencing`'s own requirements (`attrs>=22.2.0`,
  `rpds-py>=0.7.0` and, below CPython 3.13, `typing-extensions>=4.4.0`) are already present
  transitively. The install test's closure assertion therefore sees no new distribution.
- **Import cost.** The package is imported inside `foundry.contract.validate.iter_violations` and
  under `typing.TYPE_CHECKING`, never at module scope, so plugin startup cost is unchanged.
