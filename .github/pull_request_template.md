## What and why

<!-- One concern per pull request. Link the issue if there is one. -->

## How it was tested

<!-- Commands you ran and their results. Note anything you could not run. -->

## Checklist

- [ ] A failing test came first, and it passes now
- [ ] `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest` pass
- [ ] `node scripts/check-source-hygiene.mjs` exits 0
- [ ] README examples and the changelog are updated if the public API changed
- [ ] `contracts/` and `contracts/CORE_VERSION` are updated together if koldera-core's API changed
- [ ] No real documents, tokens, keys or personal data
- [ ] Nothing here makes the SDK claim more than a machine check
