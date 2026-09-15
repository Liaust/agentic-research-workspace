# Public architecture

## Workspace shape

Agentic Research Workspace is intended to support one continuous research
lifecycle through three separately deliverable surfaces:

1. the **literature map** turns a bounded paper corpus into evidence-linked,
   source-local records and qualified cross-source relationships;
2. the planned **research workbench** will connect questions, notes,
   decisions, data, experiments, and code without erasing their origins; and
3. the planned **research pack** will expose a curated, versioned selection of
   results, artifacts and provenance for both human and agent navigation—not a
   raw export of the living workspace.

Only the literature-map surface is implemented in this release. The broader
shape constrains shared identity, provenance, privacy, and human/agent parity;
it does not make the other two surfaces present-day implementation scope.

```text
literature map (implemented)
  -> research workbench (planned)
  -> research pack (planned)
```

## Literature-map boundary

The current system maps source-grounded literature records. It does not
adjudicate scientific truth or create missing scientific information. Original
source assets outrank registrations, evidence spans, semantic records, and
derived graphs whenever those representations disagree.

## Literature-map layers

1. **Corpus:** logical works, immutable assets, identity, hashes, usability,
   and rights notes.
2. **Evidence:** exact, version-bound routes into one asset.
3. **Source-local semantics:** atoms, moves, and threads attributed to one
   source and backed by evidence IDs.
4. **Cross-source mapping:** explicit potential relationships between stable,
   fingerprinted atoms.
5. **Projections:** deterministic graphs, search results, and exploration
   results compiled from validated records.

The canonical semantic surface is constrained Markdown. Operational graphs are
derived and disposable. Every cross-source endpoint includes a record revision
and fingerprint so stale relationships fail validation.

## Human and agent parity

Pipeline operations accept explicit inputs and provide the same identifiers in
human-readable and JSON output. Search and exploration require an explicit
graph. This avoids hidden selection of “latest” state and gives an orchestrator
the same control surface as a terminal user. Subsequent workspace surfaces must
preserve the same parity.

## Publication-first design

The next cross-surface design milestone is the research-pack reference design;
the living-workspace capture workflow is derived backward from that target.
See [workspace and pack](WORKSPACE_AND_PACK.md) and the
[twenty publication questions](RESEARCH_PACK_QUESTIONS.md).

The intended operating layer is one project-wide coordinator that delegates
focused work and integrates inspectable returns. This is distinct from both
the deterministic pipeline coordinator implemented today and external agents
querying a future immutable pack.

## Historical public projection

The public-release builder reads a frozen private commit, input lock, source
registrations and asset bytes, graph, graph manifest, publication policy, and
rights inventory. It selects allowlisted code and documentation, computes only
approved aggregates, adds an invented example, scans for forbidden material,
and writes a manifest-bound candidate atomically. Verification of that
candidate requires no private input.

The current public repository evolves through ordinary reviewed software
commits; it is no longer regenerated wholesale for every change. Its historical
aggregate export remains pinned. See the
[publication policy](../publication/PUBLICATION_POLICY.md).

More detailed **historical, exploratory** literature-map design hypotheses remain in
[OVERVIEW.md](OVERVIEW.md), [ONTOLOGY.md](ONTOLOGY.md), and
[PIPELINE.md](PIPELINE.md).
