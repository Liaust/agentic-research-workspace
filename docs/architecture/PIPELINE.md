# Extraction and reconciliation pipeline

Status: historical exploratory v0.1; superseded by
[the current public architecture](README.md) where they conflict

## 0. Declare the research scope

Record the research objective, intended uses, corpus boundary, central
questions, excluded areas, and required comparison dimensions. The map guides
prioritization and audits; it does not authorize omission of unanticipated
source contributions.

## 1. Register and fingerprint the corpus

For every logical source and asset:

- establish identity, edition, completeness, usability, and rights posture;
- preserve source bytes unchanged;
- record SHA-256, page count, and extraction capability; and
- distinguish alternate captures from meaningful versions.

## 2. Build a layout-aware document representation

Extract text and structure while preserving page coordinates, reading order,
headings, footnotes, equations, tables, figures, captions, and references.
Store parser output as a reproducible intermediate artifact tied to the asset
hash. Visually inspect meaning-sensitive regions.

For Phase 0, run a bounded parser bake-off instead of prematurely declaring
one parser canonical:

- GROBID full-text TEI as a candidate for scholarly structure,
  bibliographic references, and coordinate-backed PDF overlays;
- Docling Document JSON as a candidate for unified layout, reading-order,
  formula, and table structure; and
- direct PDF rendering as the fidelity surface when either derived parse is
  ambiguous.

Measure heading order, equation association, footnote placement, table
structure, citation extraction, and locator round-tripping on the collision
set. Parser output is always reproducible evidence-routing infrastructure, not
the source itself.

## 3. Segment by coherent scientific moves

Use sections, subsections, proof blocks, experiment descriptions, result
blocks, and discussion moves rather than fixed token chunks. Every substantive
unit receives one state: included, excluded with reason, deferred, unresolved,
or complete.

## 4. Extract source-local objects while evidence is active

For one unit at a time:

1. identify candidate entities, statements, questions, symbolic objects, and
   argument moves;
2. split or join candidates using the atomicity tests;
3. attach exact evidence spans;
4. state scope, assumptions, modality, polarity, and source stance;
5. map symbols and equations;
6. preserve premise-to-conclusion structure; and
7. write the durable source bundle before advancing.

No cross-source merging happens here.

## 5. Validate the source bundle

Run deterministic and semantic gates:

- schema and referential integrity;
- evidence locator and asset-hash validity;
- evidence-to-object entailment;
- atomicity and hidden-conjunction audit;
- scope and qualification completeness;
- equation transcription and symbol-table audit;
- source-unit coverage and explicit skip audit; and
- argument continuity audit.

Semantic validators write findings or proposals; they do not silently repair
accepted records.

## 6. Resolve entities and notation

Generate candidate canonical referents using explicit aliases, citations,
shared definitions, normalized symbols, field areas, lexical retrieval, and
embedding retrieval. Adjudicate identity with evidence.

Keep source-local entities even after cluster membership is verified.

## 7. Generate relationship candidates

Use several high-recall routes because no single route finds every useful
connection:

- explicit citation and comparison language;
- shared canonical referents;
- normalized claim signatures;
- shared measured outcomes or equations;
- definition and symbol conflicts;
- premise/conclusion dependencies;
- field-map bridge questions; and
- capped lexical/vector neighbors.

Every emitted candidate enters a deterministic workload and must receive a
terminal disposition.

Start retrieval with a generated SQLite FTS5 index for exact, phrase, prefix,
and proximity search over labels, faithful object content, aliases, and symbol
definitions. Add a vector index as a second high-recall route only after a
literal-search baseline exists. Neither index accepts or rejects a scientific
relationship.

## 8. Adjudicate relationships

For each candidate, inspect the relevant source-local objects and original
evidence on all sides. Record relation type, endpoint roles, scope alignment,
scientific consequence, confidence, status, and rejection reason when
applicable.

Contradictions use the dedicated protocol. Missing extraction reopens the
source unit; the reconciler does not invent the absent object.

## 9. Reconcile globally in bounded partitions

Build compact source surfaces and destination families. Partition workloads
deterministically, process them sequentially or with conflict-free read-only
workers, and let one coordinator apply accepted graph changes. Fingerprints
make interruption and resume safe.

Completeness means every selected source entered the comparison surface and
every emitted candidate was dispositioned. It does not mean all possible
source pairs were exhaustively imagined.

## 10. Human review by expected value

Prioritize review for:

- high-centrality low-confidence objects;
- proposed contradictions and equivalences;
- definition or symbol collisions;
- claims used by many downstream arguments;
- source spans with weak extraction quality;
- surprising negative results;
- unresolved canonical membership; and
- ontology cases that could change extraction rules.

Reviewer decisions are durable records with actor, rationale, evidence, and
supersession history.

## 11. Compile projections

Generate paper dossiers, concept pages, claim comparison tables, equation
views, open-question maps, argument diagrams, contradiction dashboards, and a
reader-friendly Wiki. Each surface links back to source-local objects and exact
evidence.

## 12. Evaluate and version

For every release candidate, report:

- source-unit coverage;
- evidence coverage;
- object and relationship validation counts;
- unresolved and rejected counts;
- contradiction precision on the reviewed set;
- duplicate/canonicalization precision;
- equation fidelity findings;
- retrieval recall on benchmark research questions;
- reviewer disagreement; and
- exact corpus, schema, prompt/model/tool, and graph fingerprints.

## Implementation references

- [GROBID full-document and coordinate guidance](https://github.com/grobidOrg/grobid/blob/master/doc/Frequently-asked-questions.md)
- [Docling supported formats and unified document output](https://docling-project.github.io/docling/usage/supported_formats/)
- [Docling serialization guidance](https://docling-project.github.io/docling/concepts/serialization/)
- [SQLite FTS5](https://www.sqlite.org/fts5.html)
- [JSON Schema Draft 2020-12](https://json-schema.org/specification)
