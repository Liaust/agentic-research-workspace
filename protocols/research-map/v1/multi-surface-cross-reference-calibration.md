# Multi-surface five-source cross-reference calibration v1

You are mapping relationships already present across five frozen source-local
dossiers. You are not reconciling papers, deciding scientific truth, filling
gaps, or doing new science. This is graph-blind: the current graph, reference
candidates, and earlier mapper results are unavailable and must not be sought.

Read `input/task.json` and all five complete dossiers in `input/dossiers/`.
They are the only scientific authority. Read every source's argument structure,
definitions, claims, assumptions, equations, methods, results, quantitative
findings, limitations, objections, responses, and open questions.

The task declares all ten unordered source pairs. Pair accounting and semantic
comparison are separate outputs:

- `pair_coverage` contains exactly one entry for every declared pair;
- `comparison_surfaces` may contain zero, one, or many materially distinct,
  source-grounded comparisons for a pair; and
- `relationships` contains only faithful `potential_*` edges supported by a
  comparison surface.

There is no target, minimum, or maximum for comparison surfaces or
relationships. Do not compress distinct formal, architectural, experimental,
quantitative, definitional, or argumentative connections into one broad theme
merely to keep the output short. Do not split one semantic connection into
paraphrases merely to increase breadth. Abstain when no useful connection is
supported.

Within this single turn:

1. Read and outline the central objects and argument path of every dossier.
2. Inspect every declared pair across each relevant comparison dimension.
3. Create a separate comparison surface whenever a materially distinct
   endpoint pair or comparison dimension would help a researcher navigate,
   compare, challenge, or contextualize the literature.
4. Revisit earlier pairs after reading later pairs when a more precise endpoint,
   missing distinction, or better relationship type becomes visible.
5. Audit the pair ledger, surface ownership, endpoint evidence, and relationship
   types before returning the result.

For each pair ledger, use the canonical `pair_key` from `input/task.json`, list
the exact two canonical source IDs in their declared order, and cite every
surface key owned by that pair exactly once. Use `surfaces_found` when the list
is non-empty, `no_useful_relationship` when inspection found nothing material,
and `manual_review_candidate` only when a potentially useful connection cannot
be represented faithfully with the source records or allowed ontology.

Every comparison surface must:

- belong to exactly one declared pair and use its two source IDs;
- cite one existing `connectable: true` atom from each source and resolving
  evidence from the matching side;
- identify one comparison dimension and state the precise connection, scope
  alignment, assumption alignment, rationale, and qualifications; and
- use `relationship_proposed` only when an allowed edge is emitted for that
  surface, `ontology_gap_candidate` when the vocabulary does not faithfully
  express the material connection, or `no_allowed_relationship` when the
  comparison is useful context but should not become an edge.

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
consistency are not support or same-referent edges. When none of the current
types fits, preserve the comparison as an ontology-gap surface instead of
forcing a label.

Each relationship must use the exact endpoint and evidence IDs of its cited
surface, an allowed direction, and qualified descriptions of comparison,
scope, assumptions, rationale, and limitations. Emit at most one relationship
for an unordered endpoint pair. Do not mint scientific records, evidence IDs,
or graph IDs, and never declare a source correct or incorrect.

Return the original `batch_id` and canonical `source_ids`, all ten pair ledgers,
the variable comparison surfaces, relationships, and warnings. Return exactly
one JSON object matching the supplied schema. Return JSON only.
