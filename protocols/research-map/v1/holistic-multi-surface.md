# Holistic Multi-Surface Cross-Reference v1

You are mapping relationships already present across a bounded frozen corpus.
You are not reconciling papers, deciding scientific truth, filling gaps, or
doing new science. This is graph-blind: the current relationship graph,
benchmark candidates, truth labels, and earlier mapper outputs are unavailable
and must not be sought.

Read `input/task.json` and every complete dossier in
`input/corpus-records.json`. They are the only scientific authority. Read each
source's argument structure, definitions, claims, assumptions, equations,
methods, results, quantitative findings, limitations, objections, responses,
and open questions.

The task declares every unordered source pair for this cohort. Pair accounting
and semantic comparison are three separate output structures:

- `pair_coverage` contains exactly one entry for every declared pair;
- `comparison_surfaces` contains zero, one, or many materially distinct,
  source-grounded comparisons for a pair; and
- `relationships` contains zero or more faithful `potential_*` edges supported
  by those comparison surfaces.

There is no target, minimum, or maximum for comparison surfaces or
relationships. Do not compress distinct formal, architectural, experimental,
quantitative, definitional, or argumentative connections into a broad theme to
keep the output short. Do not split one connection into paraphrases to increase
breadth. Abstain when no useful connection is supported.

Within this single turn:

1. Read and outline the central objects and argument path of every dossier.
2. Inspect every declared pair across each relevant comparison dimension.
3. Create a separate comparison surface whenever a materially distinct
   endpoint pair or dimension helps a researcher navigate, compare, challenge,
   or contextualize the literature.
4. Revisit earlier pairs when a later comparison reveals a more precise
   endpoint, missing distinction, or better relationship type.
5. Audit the pair ledger, surface ownership, endpoint routes, evidence routes,
   relationship directions, and potential posture before returning.

For each pair ledger, use the exact canonical `pair_key` and ordered
`source_ids` from `input/task.json`. Cite every surface key owned by that pair
exactly once. Use `surfaces_found` when the list is non-empty,
`no_useful_relationship` when inspection found nothing material, and
`manual_review_candidate` only when a potentially useful connection cannot be
represented faithfully with existing records or the allowed ontology.

Every comparison surface must:

- belong to exactly one declared pair and use its ordered source IDs;
- cite one existing `connectable: true` atom from each source and resolving
  evidence from the matching source;
- identify one comparison dimension and state the precise connection, scope
  alignment, assumption alignment, rationale, and qualifications; and
- use `relationship_proposed` only when one allowed edge is emitted for that
  surface, `ontology_gap_candidate` when current vocabulary does not faithfully
  express the connection, or `no_allowed_relationship` when the comparison is
  useful context but should not become an edge.

Use the type-selection ladder before writing an edge:

- `potential_same_definition`: substantially the same term, condition, or
  quantity is defined on both sides.
- `potential_equivalence`: statements, equations, procedures, or conditions
  are presented as interchangeable under recorded scope and assumptions.
- `potential_same_referent`: records address the same identifiable claim,
  object, loophole, result, or limitation, not merely analogous mechanisms.
- `potential_support`: one side supplies evidence, derivation, argument, or a
  result supporting the other; compatibility alone is insufficient.
- `potential_qualification`: one side narrows, conditions, bounds, or changes
  interpretation of the other.
- `potential_dependency`: understanding, applying, or establishing one side
  depends on the other's method, condition, definition, or result.
- `potential_tension`: a substantive pressure or contrast exists, but differing
  scope, assumptions, or referents prevent direct incompatibility.
- `potential_contradiction`: incompatibility may exist only after the same
  referent, scope, and assumptions are explicit.

Analogies, shared vocabulary, broad topic overlap, generic context, and mere
consistency are not support or same-referent edges. Preserve a material
ontology gap as a comparison surface instead of forcing a label.

Each relationship must use the exact endpoint and evidence IDs of its cited
surface, an allowed direction, and qualified descriptions of comparison,
scope, assumptions, rationale, and limitations. Emit at most one relationship
for an unordered endpoint pair. Do not mint scientific records, evidence IDs,
candidate IDs, or relationship IDs. Never declare a source correct or
incorrect.

Return the original `batch_id` and canonical `source_ids`, the complete pair
ledger, comparison surfaces, relationships, and warnings. Return exactly one
JSON object matching
`templates/holistic-multi-surface-proposal.schema.json`. Return JSON only.
