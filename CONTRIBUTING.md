# Contributing

Thanks for helping. Please read this page and the [Code of Conduct](CODE_OF_CONDUCT.md) first.

## Before you start

- For a bug or a small fix, open a pull request or an issue.
- For a new feature or a change to the public API, open an issue first so we can agree on the shape before you write
  code.
- Report security problems privately, see [SECURITY.md](SECURITY.md).
- Server behaviour and the HTTP API belong to [koldera-core](https://github.com/koldera/koldera-core). This repository
  only wraps them. Changes must not make the SDK claim more than a machine check: no claim that a document is "accessible"
  or conformant, no generated alternative text, no guessed structure.

## Licence of contributions

The project is licensed under Apache-2.0. By submitting a contribution you agree that it is licensed under the same
terms (section 5 of the licence). There is no CLA and no DCO sign-off.

## Setup

You need [uv](https://docs.astral.sh/uv/) and Node.js 20 or later (only for the hygiene script). uv installs the Python
version it needs; the package supports Python 3.10 to 3.14.

```bash
uv sync --frozen                      # creates .venv with the pinned development tools
uv run pytest                         # unit tests (warnings are errors), README examples included
uv run ruff check .                   # lint
uv run ruff format --check .          # formatting (uv run ruff format . fixes it)
uv run mypy                           # mypy --strict over src, tests and scripts
uv run python scripts/check_licences.py   # licences of the runtime dependencies
node scripts/check-source-hygiene.mjs     # must exit 0
uv run pdoc koldera -o site           # API reference into site/ (not committed)
uv build                              # sdist and wheel into dist/
```

To run the tests on another Python version: `uv run --python 3.10 pytest` (uv creates a separate environment for it
unless you set `UV_PROJECT_ENVIRONMENT`).

### Contracts

`contracts/` is a mirror of [sdk-contract-kit](https://github.com/koldera/sdk-contract-kit): `openapi.yaml` and the
schemas next to it come from koldera-core at the commit named in `contracts/CORE_VERSION`, the golden fixtures drive
the SDK tests, and `contracts/KIT_VERSION` names the kit commit. Do not edit `contracts/` here: change the kit and run
its `scripts/sync-to-sdk.mjs` against this repository. CI fetches the kit at `contracts/KIT_VERSION` and fails on any
difference in `contracts/` (it is skipped with a notice while sdk-contract-kit is private and not readable with the
workflow token).

### Source hygiene

The hygiene check scans for internal planning references, hosted-service hostnames, long comment blocks and wording that
claims more than a machine check can. Say what was checked and that human review is still required.

## Making a change

- **Test first.** Write a failing test for the behaviour, then the smallest change that passes it.
- **One implementation.** Behaviour lives in `_flows.py` and `_core.py`; `client.py` and `async_client.py` only run what
  a flow asks for. A test checks that both clients expose the same parameters; keep it that way.
- **Keep the public surface honest.** If you add or change an export, update the README example that shows it; the
  README test type-checks every Python example.
- **No real documents or credentials.** Fixtures are synthetic. Never commit tokens, keys or personal data.
- Pin development dependencies exactly and keep `uv.lock` in the same commit.

## Commit style

Small focused commits, imperative mood, plain messages that say what changed and why when it is not obvious:

```text
Hash large uploads in a worker thread in the async client
Keep the request on a redacted network error
Document the compatibility table
```

Avoid messages like "update" or "fix".

## Pull requests

Describe what changed and why, list the commands you ran with their results, and note anything you could not run. Keep
a pull request to one concern. The Koldera team reviews it; expect questions about tests and about what a change could
make the SDK claim. The repository is squash-merged.

## Releasing

Releases are made by the maintainers.

One-time setup by the owner:

1. Create an account on https://pypi.org and enable two-factor authentication.
2. Add a pending trusted publisher (PyPI: Your account, Publishing, "Add a new pending publisher", GitHub): project
   name `koldera`, owner `koldera`, repository `koldera-python`, workflow `release.yml`, environment `pypi`. The project
   is created by the first publish; no API token is ever stored.
3. In the GitHub repository settings, create the environment `pypi` (Settings, Environments). Optionally require a
   reviewer for it, so every publish waits for an approval.
4. In the repository settings, open Pages and set the source to "GitHub Actions". Pages on a private repository needs
   a paid GitHub plan; it works once the repository is public. The `github-pages` environment already allows
   deployments from tags `v*` (set up with the GitHub API); if it was recreated, add a deployment tag rule `v*` under
   Settings, Environments, github-pages.
5. Once the repository is public, enable private vulnerability reporting (Settings, Code security).
6. Optional: once the project exists on PyPI, keep trusted publishing as the only route and do not create API tokens.

To release:

1. Make sure `main` is green. Until sdk-contract-kit is public the CI contract check is skipped, so run it by hand:
   from a clean sdk-contract-kit checkout at the commit in `contracts/KIT_VERSION`, run
   `node scripts/sync-to-sdk.mjs <this repository>` and check that `git status` shows no change in `contracts/`. Set `version` in `pyproject.toml` (the package version is read from the installed metadata) and move the changelog entry from "Unreleased" to the version and date.
2. Commit and merge that change through a pull request.
3. Tag the merged commit and push the tag:

   ```bash
   git tag v0.1.0 <commit-on-main>
   git push origin v0.1.0
   ```

4. `release.yml` verifies that the tag matches the package version and sits on `main`, runs the whole `ci.yml` workflow
   (hygiene, secret scan, licences, contracts, docs build, package checks) and the compatibility matrix (`matrix.yml`)
   for that commit, builds the sdist and wheel with `uv build`, publishes them to PyPI with attestations, creates the
   GitHub Release with generated notes and deploys the pdoc site to GitHub Pages.

The pending trusted publisher is first exercised by the real publish, so the first release can fail at that step
(check the four values in step 2); nothing has been uploaded in that case and the tag can be re-run.

If a step after the publish fails, fix the cause and re-run the failed jobs; PyPI never accepts the same file twice,
so never reuse a version number for different content: bump to the next patch version instead.
