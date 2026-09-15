# Contributing

Help make research easier to inspect, verify and reuse. Useful contributions
include reproducible bugs, original synthetic examples, tests, navigation
improvements and well-scoped design proposals.

Start with the [roadmap](docs/public-roadmap/README.md) and
[repository guide](docs/development/README.md). Later roadmap items are not
already implemented or automatically accepted feature scope.

## Development setup

Python 3.12+ and uv are required. From a source checkout:

```sh
uv sync --frozen --all-groups
uv run python -m research_map.demo --output build/synthetic-study
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv build
uv run python -m research_map.development --root .
uv run python scripts/verify-legacy-baseline.py
```

The suite uses public synthetic fixtures and makes no model calls. The historical
snapshot check needs full Git history; shallow clones must fetch it first.
CI runs on Python 3.12 and 3.13.

The development checker examines tracked and unignored untracked files, pins
legacy public artifacts and scans for prohibited paths, content and broken
links. Before a release, add `--require-clean`. It does not read withheld
research corpora or prove redistribution rights.

## Propose a change

1. Open an issue with the desired outcome, current behavior and a minimal public
   reproduction. Never attach private source material or unredacted logs.
2. Use a focused branch and explain the intended scope.
3. Add tests and update both human documentation and agent command discovery
   when interfaces change.
4. Run the relevant checks and report failures or skipped checks honestly.
5. Submit a pull request with evidence and migration implications. A maintainer
   reviews the diff before integration.

Changes to record meaning, schemas, relationship types, execution authority or
publication policy need an explicit rationale. Avoid opportunistic rewrites.

## Research integrity boundary

- Keep logical-source identity distinct from asset versions.
- Retain each source's own assertions and exact evidence routes.
- Preserve reasoning structure, uncertainty, qualifications and disagreement.
- Keep mapper-inferred relationships explicitly potential.
- Never treat a valid schema or a successful model response as scientific proof.
- Keep fixtures original, minimal and clearly synthetic.
- Do not change frozen non-prose baseline artifacts or approved screenshots
  as part of normal code maintenance.

## Public/private boundary

Keep private papers, evidence-bearing research records, run state, credentials
and personal operational context outside this checkout. Never merge private
Git history into it. Permission to possess or cite a paper is not permission
to redistribute it.

Automated checks are guardrails, not a rights decision. Every newly published
dataset or source example requires explicit provenance, scope and licensing
review. Use [SECURITY.md](SECURITY.md) for sensitive reports.

Maintainers follow the [experimental release checklist](docs/development/RELEASING.md).
