# Focused five-source cross-reference calibration v1

You are mapping relationships already present across five frozen source-local
dossiers. You are not reconciling the papers, deciding scientific truth, or
doing new science. This is a graph-blind calibration: the current graph and the
fixed benchmark candidates are deliberately unavailable. Do not search for or
infer them from any other workspace state.

Read `input/task.json` and all five complete dossiers under `input/dossiers/`.
The dossiers are the only scientific authority for this job. Read every source,
including its evidence, argument moves, threads, scope, assumptions,
qualifications, definitions, equations, methods, results, limitations, and open
questions.

The task lists exactly ten `owned_source_pairs`, one for every unordered pair
of the five sources. Attend to every pair, but do not manufacture a
relationship to fill a pair or meet a count. Pair attention and relationship
creation are separate:

- return exactly one coverage surface for every owned pair;
- a pair with one or more useful supported relationships is
  `covered_by_proposal`;
- a pair that was inspected but exposes no relationship expressible under the
  current ontology is `no_useful_relationship`;
- use `manual_review_candidate` when a potentially useful comparison exists but
  the supplied records or current relationship vocabulary do not support a
  faithful edge; and
- never use `covered_by_existing`, because no existing relationships are
  available.

Each pair surface must cite at least two existing connectable atom IDs, contain
records from exactly its two sources, and use an empty
`existing_relationship_ids` list. A surface is an attention ledger, not a new
scientific claim. Its label and rationale should briefly identify what was
actually inspected. Do not use one broad five-source surface to stand in for
pair-by-pair attention.

Within this single turn:

1. Read and outline the central objects and argument structure of each dossier.
2. For each owned pair, compare definitions, claims, assumptions, equations,
   methods, results, limitations, questions, objections, and responses.
3. Revisit earlier pairs when later comparisons reveal a more precise endpoint
   or type.
4. For every proposed relationship, test the type against the selection ladder
   below and preserve material scope or assumption differences.
5. Finish only after all ten pair surfaces are accounted for and another sweep
   yields no materially new useful relationship.

Propose a relationship only when both endpoints are existing `connectable:
true` atoms, both sides resolve to existing evidence in their dossiers, and
seeing the records together would materially help a researcher understand,
compare, navigate, challenge, or contextualize the literature. Similar words,
broad topic overlap, mere consistency, and generic methodological advice are
not enough.

Use this type-selection ladder before writing each relationship:

- `potential_same_definition`: both records define substantially the same
  term, condition, or quantity; a positive condition and its failure are not
  automatically the same definition.
- `potential_equivalence`: the two statements, equations, procedures, or
  conditions are presented as interchangeable under the recorded scope and
  assumptions.
- `potential_same_referent`: both records address the same identifiable claim,
  object, loophole, result, or limitation. Two analogous mechanisms or two
  members of a broad topic are not the same referent.
- `potential_support`: one record supplies evidence, a derivation, an argument,
  or a result that supports the other. Compatibility or consistency alone is
  not support.
- `potential_qualification`: one record narrows, conditions, bounds, or changes
  how the other should be interpreted under stated scope or assumptions.
- `potential_dependency`: understanding, applying, or establishing one record
  depends on the method, condition, definition, or result in the other.
- `potential_tension`: the records create a substantive pressure or contrast,
  but different scope, assumptions, or referents prevent a direct
  incompatibility claim.
- `potential_contradiction`: the records may be incompatible only after the
  same referent, scope, and assumptions are made explicit. Do not use this for
  different modeling choices or interpretations alone.

If a connection is only an analogy, conceptual parallel, shared vocabulary, or
generic contextual association and no current type fits faithfully, do not
force it into `potential_same_referent` or `potential_support`. Record the pair
surface as `manual_review_candidate` when that limitation is important.

Every proposed relationship must:

- name the pair's surface key;
- use two existing connectable atom IDs from that pair and include both atom
  IDs in the surface's `source_record_ids`;
- cite existing resolving evidence IDs from each matching source;
- use one allowed `potential_*` type and its valid direction;
- explain one precise comparison surface, scope alignment, assumption
  alignment, rationale, and material qualifications; and
- remain a qualified mapper proposal rather than a claim of scientific fact.

Return at most one relationship for an unordered endpoint pair. When more than
one label seems plausible, choose the most precise defensible type and record
the alternative in `qualifications`. Do not mint candidate, relationship, or
evidence IDs.

Return the exact original `batch_id` and canonical `source_ids` from the task,
an empty `existing_relationship_reviews` array, the ten pair surfaces, and the
proposed relationships. Do not create or rewrite a scientific node, merge away
a paper's statement, fill a missing premise, resolve an open question, or
declare which source is correct. Return exactly one JSON object matching the
provided holistic cross-reference proposal schema. Return JSON only.
