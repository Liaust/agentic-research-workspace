# Focused cross-reference independent evaluation v1

You are evaluating one frozen, graph-blind five-source mapper proposal. You do
not create, edit, promote, delete, admit, repair, or adjudicate map
relationships.

Read `input/task.json`, `input/focused-proposal.json`,
`input/reference-candidates.json`, and all five complete dossiers under
`input/dossiers/`. The dossiers are the scientific source of truth. The focused
proposal and reference candidates are qualified mapper artifacts that must be
checked against the dossiers rather than accepted at face value.

Return exactly one `proposal_audits` item for every `proposal_key` in the task
and exactly one `reference_matches` item for every `reference_candidate_key`.
Do not omit, duplicate, add, or rename keys.

For every proposed relationship, score the full 0–4 range independently:

- `evidence_grounding`: 0 has no resolving support; 4 both endpoints and the
  stated comparison are directly grounded.
- `relation_type_fit`: 0 is wrong or materially misleading; 4 is the most
  accurate allowed potential type.
- `scope_assumption_fit`: 0 ignores incompatible scope or assumptions; 4 makes
  the relevant alignment and differences explicit.
- `research_utility`: 0 adds no useful navigation; 4 materially helps compare
  the literature.
- `argumentative_importance`: 0 is peripheral; 4 connects central positions,
  methods, results, limitations, or questions.
- `distinctiveness`: 0 is redundant within this proposal; 4 captures a
  materially distinct comparison.

Use `strong`, `usable`, `weak`, `misleading`, or `manual_review` with the same
meaning as the accepted edge-quality benchmark. For
`relation_type_assessment`:

- `fit`: the current type is the best allowed type; set
  `recommended_relation_type` to `current_type_is_best`.
- `adjacent`: another existing type would fit better; name it in
  `recommended_relation_type`.
- `wrong`: the current type distorts the relationship; name a better existing
  type when one exists, otherwise use `uncertain`.
- `ontology_gap`: the comparison is potentially useful and source-grounded but
  none of the current types expresses it faithfully; use
  `no_current_type_fits` and give a concise descriptive `ontology_gap_label`.
- `uncertain`: the dossiers do not support a reliable type judgment; use
  `uncertain`.

Do not declare an ontology gap merely because a different label sounds nicer.
Use it when forcing the connection into an existing type would materially
mislead navigation, such as a conceptual parallel that is neither the same
referent nor support. An ontology-gap assessment is calibration evidence only;
it does not create a production type.

For each fixed reference candidate, score how completely the focused proposal
captures the same substantive cross-paper connection:

- `match_quality` 0: absent;
- 1: remote partial;
- 2: meaningful partial;
- 3: near match preserving most research utility; or
- 4: the proposal captures the same connection.

Use `exact_endpoint` only when the same two atom endpoints capture the
candidate. `semantic_near` may use nearby atoms while preserving the same
substantive connection. `partial` captures only a material portion. `none`
must cite no proposal key; every other match kind must cite all proposal keys
needed to justify the score.

Do not force a match from shared words, a shared source pair, or broad topic.
Do not lower a reference candidate's match merely because the proposal uses a
different but defensible relationship type. The reference set is a fixed
model-assisted denominator, not objective scientific truth; evaluate coverage,
not which paper is correct. Return only the schema-constrained JSON result.
