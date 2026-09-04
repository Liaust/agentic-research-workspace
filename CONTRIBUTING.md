# Contributing

Contributions are welcome when they strengthen reproducibility, provenance,
schema enforcement, or the usability of the published system.

## Development setup

```sh
uv sync --frozen --all-groups
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv build
scripts/verify-public-release.sh .
```

Keep CLI inputs explicit and preserve equivalent human-readable and JSON
results. Add focused tests for behavioral changes and run the complete suite
before proposing a change.

## Research integrity boundary

- Preserve source identity and provenance instead of merging conflicting
  statements into a consensus summary.
- Describe cross-source connections as potential relationships unless the
  recorded method supports a narrower statement.
- Never add copyrighted source files, private evidence text, credentials,
  private work URLs, or machine-local paths.
- Do not infer permission to redistribute a paper or derived dataset from
  possession, citation, or public availability.
- Label synthetic fixtures clearly and keep them separate from measured
  aggregate results.

Changes to record meaning, schemas, relationship types, or public-release
policy need an explicit rationale and migration impact. Security-sensitive
reports belong in the process described by [SECURITY.md](SECURITY.md), not in a
public patch.
