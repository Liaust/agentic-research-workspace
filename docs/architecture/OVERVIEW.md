# Evidence-first research mapping architecture

Status: historical exploratory v0.1; non-authoritative where it conflicts
with the [current public architecture](README.md)
Version: 0.1
Date: 2026-08-21

## Outcome

The system should answer research questions that a document-summary library
cannot answer reliably:

- What exactly does each paper claim, under which assumptions and scope?
- Which papers define the same term differently?
- Which result supports, narrows, generalizes, operationalizes, or contradicts
  another result?
- Which equations are equivalent, special cases, or based on incompatible
  symbol conventions?
- Which open questions are answered later, remain open, or are revealed to be
  ill-posed?
- Where is a synthesis strongly evidenced, weakly inferred, disputed, or
  simply absent from the corpus?

## The five-layer model

### 1. Corpus layer

Owns logical works, editions, assets, hashes, bibliographic identity,
completeness, usability, rights notes, and document structure. A logical paper
and a particular PDF are different identities.

### 2. Evidence layer

Owns exact, immutable routes into an asset: pages, sections, paragraphs,
equation or figure labels, bounding boxes where needed, extraction method, and
content hashes. Evidence spans are the only bridge from a derived scientific
record back to source bytes.

### 3. Source-local semantic layer

Owns what one paper says. Every knowledge atom keeps the paper as its asserting
or defining context. Atoms are typed, but they are never merged
across papers at this layer.

This protects against the most dangerous failure in cross-paper synthesis:
turning several qualified positions into one unqualified model-written claim.

### 4. Reconciliation graph

Owns canonical referents, aliases, claim families, symbol mappings, and
adjudicated relationships. Canonicalization adds navigation; it never deletes
or rewrites source-local objects.

Relationships are reified records with their own provenance, status,
rationale, scope alignment, and review history. A contradiction, equivalence,
or derivation is therefore inspectable and reversible.

### 5. Projection layer

Owns human and machine interfaces: Wiki pages, paper dossiers, argument maps,
claim matrices, equation browsers, timelines, contradiction views, reports,
APIs, and search indexes. Every projection is disposable and reproducible from
validated upstream records.

## Boundary with the broader workspace

The implemented surface specializes in mapping published literature. The
repository is the umbrella for the broader Agentic Research Workspace. Its
planned workbench and research-pack surfaces may also expose raw and processed
data, protocols, software, workflow runs, failed or excluded runs, decisions,
manuscript versions, and access policy.

Do not overload the knowledge-atom ontology into a universal packaging or
workflow ontology. Use established outer standards where they fit:

- [RO-Crate 1.3](https://www.researchobject.org/ro-crate/specification.html)
  for packaging and describing research artifacts;
- [Workflow Run RO-Crate](https://www.researchobject.org/workflow-run-crate/)
  for computational workflow-run provenance;
- [W3C PROV](https://www.w3.org/TR/prov-primer/) for interoperable entities,
  activities, agents, derivations, and revisions; and
- [Frictionless Data Package](https://specs.frictionlessdata.io/) for portable
  dataset and table-resource metadata.

The literature map can later be embedded in or linked from such a package as
its epistemic layer. Knowledge atoms can point to published evidence spans or
to first-party results, datasets, and workflow runs. The outer research object
and the inner source-local knowledge atom remain distinct identities.

## Why this differs from the previous WikiFold system

The previous system made several strong decisions that should be retained:

- source bytes terminate evidence;
- extraction is interleaved with direct reading;
- every substantive source unit receives a durable disposition;
- named concepts and atomic claim anchors are separately addressable;
- cross-source relationships require a distinct reconciliation pass;
- large-corpus reconciliation uses bounded deterministic workloads;
- exact corpus fingerprints and readiness receipts protect publication; and
- equations and meaning-bearing displays receive explicit integrity checks.

Its limitation for scientific mapping is that the durable semantic unit still
lives mainly inside concept and idea Markdown. Claim anchors are addressable
to a reader, but they do not expose enough structured semantics for systematic
comparison of scope, polarity, assumptions, variables, equations, evidential
status, or unresolved questions.

This project moves the canonical atom below the page. Pages become coherent
compositions of knowledge atoms and relationship objects. That preserves both
machine comparison and
human-readable argument.

## Canonicalization without information loss

Source-local objects are immutable semantic testimony:

```text
A-LIB035-0042  Hossenfelder and Palmer assert P under scope S1
A-LIB038-0067  Sen and Valentini reject or qualify P under scope S2
```

The graph may later add:

```text
C-000184         canonical concept or claim family
R-000991         scope-qualified disagreement between the two source objects
```

It must never replace the source objects with `The literature says P`.

## Storage strategy

During the pilot, use Git-reviewable source bundles and language-neutral JSON
Schema:

```text
extractions/<source-id>/
  document.json
  evidence.jsonl
  objects.jsonl
  arguments.jsonl
  audit.json

graph/
  entities.jsonl
  clusters.jsonl
  relationships.jsonl
  decisions.jsonl
```

Use generated SQLite or DuckDB indexes for querying and full-text search, and a
generated vector index only for candidate discovery. If scale later requires
PostgreSQL or a graph database, the file contracts remain the portable source
of truth until an explicit migration decision changes that boundary.

LLMs should write proposal bundles, not mutate the canonical store directly.
A deterministic validator checks IDs, schemas, evidence routes, hashes, and
state transitions before applying a proposal.

## Version and identity rules

- Logical source IDs, asset IDs, evidence IDs, knowledge-atom IDs, and
  relationship IDs are different namespaces.
- IDs never encode mutable titles or ontology labels.
- Every record declares a schema version and producing run.
- A semantic correction supersedes a record and explains why.
- Canonical clusters have membership history; removal does not erase the prior
  decision.
- Every build records ordered source IDs, asset hashes, schema versions,
  extraction prompt/model/tool versions, and output fingerprints.

## Trust model

The system distinguishes:

- what a paper explicitly states;
- what follows by a recorded derivation;
- what the system proposes as a cross-source synthesis;
- what a reviewer verifies;
- what remains unresolved; and
- what has been rejected.

Confidence is not a substitute for this posture. A high-confidence system
synthesis is still a synthesis, not a source assertion.

## Readiness

A corpus scope is research-map-ready only when:

- every selected asset passes identity and integrity checks;
- every substantive document unit has an explicit disposition;
- every accepted object has exact evidence;
- every equation has its symbol and fidelity audit;
- every accepted cross-source relationship has evidence on all material sides;
- every contradiction passed the contradiction protocol;
- every candidate reconciliation workload unit has a terminal disposition;
- unresolved items and coverage consequences are visible; and
- the exact corpus, ontology, extraction, and graph fingerprints are recorded.
