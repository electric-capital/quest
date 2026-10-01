# Development Workflows

## Overview

Development workflow for the Quest project: branching and PRs, syntax checking, the pytest test suite, and the frontend unit tests.

## Branching & Pull Requests

All work lands on `main` through pull requests -- never commit directly to `main`:

1. Branch from up-to-date `origin/main` (e.g. `feat/<topic>` or `fix/<topic>`).
2. Develop and verify in local mode (`python3 run.py`; see [Run Modes](../architecture/run-modes.md)) and run the test suite before committing.
3. Push the branch and open a PR against `main` (`gh pr create`).
4. CI (`.github/workflows/ci.yml`) runs on every PR and push to `main`: the backend job runs `uv sync --locked` + `uv run pytest`, the frontend job runs `npm ci`, `npm run lint` (tsc), `npm test` (vitest) and `npm run build` on Node 24. Fix a red check before asking for review.
5. A human reviews and merges. Agents open PRs; they do not merge them.

## Key Files

- `tests/test_syntax.py` -- Parametrized pytest syntax check: walks the project tree and compiles every `.py` file with `py_compile`, one test per file
- `tests/` -- Core functional test modules, one per feature area (e.g. `test_edit_workspace_file.py` for the edit tool); browse the directory for the current set
- `plugins/<name>/tests/` -- Each plugin's own test suite (see "Plugin tests" below)
- `conftest.py` (repo root) -- Pytest hook that also collects out-of-tree plugin test suites from `QUEST_PLUGIN_PATH` roots
- `pyproject.toml` -- Dev dependency group includes `pytest>=8.0.0`

## Syntax Checking

```bash
uv run pytest tests/
```

`tests/test_syntax.py` walks the project tree starting from the repository root, compiles every `.py` file with `py_compile.compile(doraise=True)`, and skips the excluded directories defined in its `EXCLUDED_DIRS`: `.venv`, `venv`, `__pycache__`, `.git`, `node_modules`, `data`, `.pytest_cache`, `devplans`. Compiled `.pyc` output goes to a temporary directory that is cleaned up automatically.

Don't do you own py_compile checks. Those are not whitelisted commands and so will interrupt the user.

## Pytest Test Suite

Run all tests:

```bash
uv run pytest
```

The test suite requires `uv sync` to install dev dependencies. Tests use mock objects (no database or LLM API calls required).

Pytest's `testpaths` (pyproject.toml) covers both `tests/` and `plugins/`, so a bare `uv run pytest` runs the core suite plus every in-tree plugin's tests. Passing explicit paths runs exactly those (`uv run pytest tests/` no longer covers plugin tests).

### Plugin tests

Plugin-specific tests live in the plugin's own directory, not in `tests/`:

- `plugins/<name>/tests/` holds the suite, with an `__init__.py` (both the plugin directory and its `tests/` directory are packages, so test module names stay unique across plugins) and a `conftest.py` that builds the plugin's registration fixture from the shared factory:

  ```python
  from pathlib import Path

  from tests.plugin_support import plugin_fixture

  my_plugin = plugin_fixture(Path(__file__).resolve().parent.parent)
  ```

  The fixture imports `plugin.py` the way the loader does, registers the manifest into the live registries for the test, and unregisters it on teardown via `config.plugins.unregister_plugin()` (the maintained inverse of `register_plugin()`).
- Core cross-cutting tests that need a real plugin registered (connector rows, dispatch tables, the action-request schema) stay in `tests/` and use the same-named fixtures from `tests/conftest.py`.
- **Out-of-tree plugins**: when `QUEST_PLUGIN_PATH` names extra plugin roots (see [Plugins](../architecture/plugins.md)), a bare `uv run pytest` from the repo root also collects `<root>/<name>/tests/` for every plugin directory there that has one (repo-root `conftest.py`; explicit path arguments disable the auto-append). Out-of-tree suites follow the exact same layout and conftest recipe -- `pythonpath` in pyproject.toml keeps the repo root importable so they can import quest modules and `tests.plugin_support`.

## Frontend Tests

```bash
cd frontend && npm test
```

Frontend unit tests run with [Vitest](https://vitest.dev/) under jsdom (`frontend/vitest.config.ts`, kept separate from `vite.config.ts` so the build-only plugins stay out of the test pipeline), with React Testing Library for hooks and providers. Test files live beside the code they cover as `*.test.ts` / `*.test.tsx` under `frontend/src/` and are type-checked by the app tsconfig. Mock the API client (`src/api/client`) and the realtime singletons (`src/services/*`) with `vi.mock`; the existing hook tests (`src/hooks/*.test.tsx`) and the context boundary test (`src/contexts/AppProviders.test.tsx`) show the pattern. `npm test` runs once and exits (`vitest run`); use `npx vitest` for watch mode.

## Releases

Releases are marked by annotated git tags named `v<MAJOR>.<MINOR>.<PATCH>` (pre-releases: `v1.4.0-rc1`). The tag is the single source of truth: nothing in the tree is bumped, and the running server derives its version from the nearest reachable tag at startup.

The `v*` tag namespace is reserved for releases and protected on GitHub by the "release tags" ruleset (`.github/rulesets/release-tags.json`, importable via the repo's Rules settings; applied to the public repo): creating, moving, deleting and force-pushing any `v*` tag is restricted to the maintainers on the ruleset's bypass list. Consequences:

- Never name a non-release tag `v...`; the resolver only honours `v<semver>` but the ruleset blocks every `v*` push.
- A pushed release tag is immutable. A bad release gets a new patch release (`v1.4.1`), never a re-pointed `v1.4.0`.
- Only a maintainer on the bypass list can push the tag. Anyone else runs the script with `--no-push` (or lets the push fail -- the tag stays local and the script explains why) and hands the tag to a maintainer.

- Cut a release with `scripts/tag_release.py` from a clean, up-to-date `main` checkout:

  ```
  uv run python scripts/tag_release.py 1.4.0            # explicit version
  uv run python scripts/tag_release.py --next minor     # bump the latest tag
  uv run python scripts/tag_release.py 1.4.0 --no-push  # tag locally, push later
  ```

  The script refuses to tag a dirty tree, a branch other than `main`, a HEAD that differs from `origin/main`, or a version that is not greater than the latest existing release tag (`--force` overrides all four). It creates the annotated tag and pushes it to `origin`.
- Pushing the tag triggers `.github/workflows/release.yml`, which publishes a GitHub Release with notes generated from the merged PRs since the previous release tag (pre-release tags are marked as such).
- `config/version.py` resolves the release at process startup: `git describe --tags --match 'v[0-9]*'` for the nearest tag, the commit distance to it, the tag's date, and the HEAD hash. The result is exposed on the unauthenticated `GET /app/api/version` (`version`, `tag`, `released`, `commits_since_tag`, `git_hash`), printed by `run.py` in the startup banner (`Release: v1.4.0 (1a2b3c4)`), and shown in Settings > About. A checkout at the tag reports `1.4.0`; one that has moved past it reports `1.4.0+3.g1a2b3c4` and the About section adds a "N commits past release" note; an untagged checkout (or one without `.git`) reports `version: null` and About falls back to "unreleased (<short hash>)".
- Deployments must have the tags available: `git fetch` brings in tags reachable from the fetched commits, but a clone made with `--no-tags` or `--depth` will show as unreleased until `git fetch --tags` runs.

## Constraints

- Syntax checking validates compilation only, not runtime correctness (missing modules, undefined names pass)
- Only `.py` files are checked; frontend TypeScript/React files are not covered

## Design Decisions

**Why py_compile instead of a linter?**
Fast, zero-dependency syntax gate catching parse errors before production. Linting is a separate concern. Standard library only means it runs anywhere Python is installed.

**Why exclude `data/` and `devplans/`?**
`data/` contains runtime artifacts and may contain Python files written by Gemini into conversation workspaces. `devplans/` contains planning documents. Neither is project source code.

