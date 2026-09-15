# Developing in public

This repository is the development home for reusable Agentic Research Workspace
software. It is not a mirror of a private research project.

## Repository map

| Path | Responsibility |
|---|---|
| `src/research_map/` | Experimental deterministic coordination, records and graph tools |
| `schemas/research-map/v1/` | Current record and pipeline contracts |
| `protocols/research-map/` | Source-reading and relationship protocols |
| `examples/` | Clearly labelled, original synthetic walkthroughs |
| `tests/` | Public-only regression tests; no model credentials |
| `docs/architecture/` | Current design and explicitly historical design notes |
| `docs/public-roadmap/` | Outcome roadmap and migration priorities |
| `reference_mapping_graph/` | Frozen aggregate baseline and original tiny example |
| `.github/` | CI and contributor templates |

See [Contributing](../../CONTRIBUTING.md) for commands and review expectations.

## Software development versus research publication

Code and docs can evolve through ordinary reviewed Git commits. Researchers keep
their source files, notes, records, runs and derived graphs in separate private
project storage and pin a software release or commit. A software update does
not silently upgrade an existing run or change its historical interpretation.

The September baseline was a clean-history export. Its manifest describes that
historical tree, not every future version of this repository. Do not regenerate
it after a README edit. Development checks reuse its privacy scanning rules and
pin its already-published data/image bytes, while allowing reviewed software and
documentation changes. [Publication policy](../publication/PUBLICATION_POLICY.md)
explains the two verification routes and their limits.

## Change workflow

1. Open a concrete issue or discuss a focused change. Reference an outcome in
   the public roadmap; do not treat later ideas as accepted implementation.
2. Work on a focused branch with tests and a short design rationale when
   changing contracts. Preserve unrelated changes.
3. Run the checks and review exactly what will be public. Automated scans do
   not establish scientific fidelity or permission to redistribute a dataset.
4. Submit a pull request. A maintainer reviews and integrates it.
5. Publish versioned software using the [release checklist](RELEASING.md).

The first prerelease supports running from a source checkout. The architecture
and data formats can change during the alpha series. Standalone package/runtime
resource support is a separately tracked packaging task.
