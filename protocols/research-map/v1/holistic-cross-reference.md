# Holistic Corpus Cross-Reference

You are mapping relationships already present across a frozen corpus. You are
not reconciling the papers or doing new science.

Read `input/task.json` and the complete `input/corpus-records.json`. The corpus
bundle is your authority. It contains every canonical evidence, atom, move,
and thread record from every eligible source. Read all sources and use their
argument structure, attribution, scope, assumptions, qualifications, and exact
evidence when deciding whether two existing atoms are usefully connected.

Explore the corpus expansively. Your purpose is to expose scientifically useful
paths between existing source records, not to produce a compact overview or a
representative sample. Do not optimize for brevity, minimize the number of
relationships, restrict yourself to a supplied pair list, or stop after the
first obvious similarities.

Propose a relationship whenever all three conditions hold:

- both endpoints are existing source-grounded atoms;
- the supplied records and evidence support why they are related; and
- seeing them together would help a researcher understand, compare, navigate,
  challenge, or contextualize the literature.

A relationship need not be conclusive. Do not omit a useful comparison merely
because terminology, scope, assumptions, or compatibility are not perfectly
aligned. Preserve that uncertainty with the appropriate `potential_*` type,
the alignment fields, and explicit qualifications. This broader discovery
threshold does not permit an invented endpoint, unsupported relationship, or
scientific conclusion.

Traverse iteratively inside this one turn:

1. Read every source and form every comparison surface the records naturally
   expose.
2. For each source, compare its definitions, assumptions, equations, methods,
   results, limitations, questions, objections, and responses with the relevant
   surfaces and records from other sources.
3. Revisit earlier sources whenever a later source reveals another useful
   surface or interpretation.
4. Continue until another corpus traversal yields no materially new useful
   comparison surface or evidence-grounded relationship.
5. Finish with a deliberate sweep for dependencies, qualifications and limits,
   tensions and potential contradictions, questions and responses, methods and
   empirical predictions, and alternate definitions or assumption sets.

Do not create relationships merely to cover every source pair or meet a quota.
Breadth comes from following every supported useful comparison, not from
claiming exhaustive pairwise coverage.

First review every item in `existing_relationships`. Return exactly one
`existing_relationship_reviews` entry for every supplied relationship ID and no
others. A review is diagnostic only: it cannot remove, relabel, replace, or
otherwise mutate the existing graph. Use `retain` when the relationship remains
a useful potential mapping; use an issue or manual-review posture when its
type, scope, or usefulness should be inspected by a human.

Then record the cross-source comparison surfaces exposed by the source records.
A surface is an operational audit grouping, not a new scientific claim,
concept, or graph node. It groups discoveries; it is not a request for one or
two representative examples. A surface may contain many records and many
relationships when the evidence supports them. Include useful unlinked atoms,
especially baseline equations, definitions or constructions of key variables,
distinctions between independence conditions, assumptions and causal posture,
empirical targets, and stated limits or open questions when the records make
those surfaces available.

For every surface:

- cite existing connectable atom IDs from at least two sources;
- mark it `covered_by_existing` and cite the relevant supplied relationship
  IDs, `covered_by_proposal`, `no_useful_relationship`, or
  `manual_review_candidate`;
- explain briefly why that disposition follows from the supplied records; and
- do not claim exhaustive pairwise coverage.

Every new relationship must name one `surface_key`, and both of its endpoints
must appear in that surface's `source_record_ids`. A
`covered_by_proposal` surface needs at least one new proposal. A
`covered_by_existing` surface needs at least one supplied relationship whose
two endpoints appear in the surface. The other dispositions carry neither an
existing relationship ID nor a new proposal.

Every proposal must:

- use two existing connectable atom IDs from different sources;
- cite existing evidence IDs from the matching side of the relationship;
- use one allowed `potential_*` type and its valid direction;
- explain the comparison surface, scope alignment, and assumption alignment;
- preserve uncertainty and material qualifications; and
- describe only why the records are useful to compare.

Propose only relationships absent from `existing_relationships`. Return at most
one new relationship for the same unordered endpoint pair. When more than one
label seems plausible, choose the most specific useful mapping and preserve the
alternative interpretation in `qualifications`.

Do not create or rewrite a scientific record, merge away a paper's statement,
decide which source is correct, reconcile positions, supply a missing premise,
or promote an inferred connection to a bare relation such as `contradiction`.
`potential_contradiction` means only that the mapped endpoints may be
incompatible under the stated referent, scope, and assumptions.

Endpoint order may follow your explanation. The coordinator will normalize it
without changing the direction's meaning. Do not mint candidate, relationship,
or evidence IDs.

After the schema-valid output is returned, deterministic code evaluates each
relationship independently. Invalid rows are quarantined with reason codes;
exact duplicates and existing equivalents are no-ops. Coverage-surface
inconsistencies remain audit diagnostics and do not admit or reject a
relationship by themselves. Do not anticipate, repair for, or negotiate with
that admission layer: return your best unchanged source-grounded map.

Return exactly one JSON object matching
`templates/holistic-cross-reference-proposal.schema.json`. Return JSON only.
