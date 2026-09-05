# Agentic Research Workspace

An open, agent-native workspace for the full research lifecycle: mapping a
literature, developing questions and ideas, organizing growing research and
code, and publishing an inspectable research pack that people and agents can
navigate beyond a static PDF.

The workspace is being built in independent, provenance-compatible surfaces.
Only the first surface—evidence-first literature mapping—is implemented today.
Later surfaces are a roadmap, not functionality claimed by this release.

<p align="center">
  <img src="docs/images/dogfood/corpus-overview.png" alt="Paper-level view of the 52-source superdeterminism dogfood corpus beside an evidence viewer and research controls" width="100%">
</p>

<p align="center"><em>A private 52-source dogfood run viewed at paper level: sources stay inspectable while their recorded potential relationships form the navigable map.</em></p>

## Workspace surfaces

1. **Literature map (implemented):** ingest a bounded corpus, preserve what
   each source says as evidence-linked records, propose qualified cross-source
   relationships, and compile navigable maps and search views.
2. **Research workbench (planned):** develop literature gaps and research
   questions, organize notes, decisions, data, experiments, and code as the
   project grows, and let people and agents operate on the same explicit state.
3. **Research pack (planned):** package the resulting corpus, provenance,
   workflows, outputs, and narrative into an agent-navigable publication that
   complements conventional papers and PDFs.

This initial repository state publishes the deterministic literature-map
coordinator, schemas, protocols, aggregate results from one dogfood baseline,
and a wholly synthetic demonstration. Its evidence-bearing graph and source
assets are not included.

## What the system does

The mapper preserves four source-local record types—evidence, atoms, argument
moves, and threads—then compiles validated records into a graph. A separate
cross-source stage can propose qualified relationship types such as potential
support, qualification, equivalence, tension, or contradiction. Search and
one-hop exploration operate deterministically over one explicitly selected
compiled graph.

The map describes what literature records say and how records may connect. It
does not decide which scientific position is correct, manufacture missing
premises, or treat a potential relationship as proof.

## Superdeterminism dogfood baseline

Superdeterminism is the first testing corpus for the general system; it is not
the product identity or a constraint on what research domains the workspace
can support.

The frozen provisional baseline records:

- 53 registered logical sources and 54 registered assets;
- 52 sources retained in the compiled research graph;
- 6,872 records: 2,550 evidence records, 2,621 atoms, 1,416 moves, and 285
  threads;
- 14,442 source-local edges and 644 potential cross-source relationships; and
- 1,325 inspected source pairs with one unresolved pair.

These are aggregate measurements, not a completeness score, consensus result,
or claim about superdeterminism. Eleven retained sources were audited with
findings, five passed audit, 35 remain unaudited and provisional, and one audit
was interrupted. The exact machine-readable values and qualifications are in
[the baseline artifacts](reference_mapping_graph/baseline-v1/README.md).

## From corpus to source

The literature-map surface can move between a compact paper overview, the
complete record graph, relationship-specific lenses, and one source's internal
argument structure. These screenshots document the private dogfood viewer; the
underlying evidence-bearing graph and source files are not part of this public
repository.

![Complete record-level graph for the 52-source dogfood corpus](docs/images/dogfood/record-graph.png)

<p align="center"><em>The complete record layer: 6,872 source-grounded records, 14,442 source-local links, and 644 qualified cross-source relationships.</em></p>

<table>
  <tr>
    <td width="58%">
      <img src="docs/images/dogfood/tension-lens.png" alt="Tension and contradiction relationship lens across the paper corpus">
    </td>
    <td width="42%">
      <img src="docs/images/dogfood/source-detail.png" alt="Source-level argument map showing records and links within one paper">
    </td>
  </tr>
  <tr>
    <td><strong>Relationship lens.</strong> Isolate recorded potential tensions and contradictions without turning them into scientific verdicts.</td>
    <td><strong>Source detail.</strong> Follow evidence, atoms, reasoning moves, and threads inside an individual paper.</td>
  </tr>
</table>

## Quick start

Python 3.12 or newer and [uv](https://docs.astral.sh/uv/) are required.

```sh
uv sync --frozen --all-groups
uv run pytest
```

Search the bundled synthetic graph:

```sh
uv run research-map search "indicator" \
  --graph reference_mapping_graph/synthetic-example/graph.json \
  --json
```

Explore one stable record and its one-hop context:

```sh
uv run research-map explore \
  --id LIB-901:atom:indicator-response \
  --graph reference_mapping_graph/synthetic-example/graph.json \
  --json
```

Verify the candidate against its manifest, policy, schemas, links, and
synthetic graph:

```sh
scripts/verify-public-release.sh .
```

The same entry points and arguments are available in
[`commands.json`](reference_mapping_graph/commands.json) for agent and tool
discovery.

## Current architecture

```text
immutable source asset
  -> registered source identity
  -> exact evidence spans
  -> source-local semantic records
  -> validated Markdown record
  -> deterministic compiled graph
  -> potential cross-source relationships
  -> search, exploration, and views
```

The Markdown record is canonical; graphs and views are reproducible
projections. Semantic work is bounded by schemas and deterministic admission.
CLI commands use explicit inputs, provide machine-readable output, and do not
guess a “latest” graph. Shared identity, provenance, privacy, and
human/agent-parity constraints will carry across later workspace surfaces. See
[the public architecture guide](docs/architecture/README.md) and the versioned
[literature-map protocols](protocols/research-map/README.md).

## Public/private boundary

The public release is a clean-history projection, not a visibility change to
the working research repository. It contains aggregate results and an invented
example. It withholds PDFs, exact source evidence, source-derived statements,
relationship endpoints and rationales, private run state, project-management
records, local paths, and credentials. Four explicitly allowlisted screenshots
illustrate the private dogfood interface without releasing its underlying
machine-readable graph or source assets. The deterministic policy and inclusion
manifest make this boundary testable.

Apache-2.0 covers original code and documentation only. No dataset license is
granted for the private graph or third-party works. Read
[THIRD_PARTY_RIGHTS.md](THIRD_PARTY_RIGHTS.md) before reusing metadata or
research material.

## Experimental status

This is a research prototype. The retained baseline has uneven audit depth,
the public example is synthetic, lexical search is not semantic retrieval, and
the public aggregate release cannot be used to independently inspect the
private graph’s scientific content. The research workbench and research-pack
surfaces are not implemented. See the [limitations](docs/limitations/README.md),
[experiments](docs/experiments/README.md), and [public roadmap](docs/public-roadmap/README.md).

## Contributing and security

Contributions should preserve source identity, provenance, deterministic
validation, and the descriptive-mapper boundary. Start with
[CONTRIBUTING.md](CONTRIBUTING.md). Report sensitive issues according to
[SECURITY.md](SECURITY.md); never put private source material in a public issue.

## License and citation

Original code and documentation are licensed under
[Apache-2.0](LICENSE). Citation metadata is provided in
[CITATION.cff](CITATION.cff).
