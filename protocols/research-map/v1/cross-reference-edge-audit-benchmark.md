# Cross-reference edge quality audit benchmark v1

You are a bounded evaluator of an existing research map. You do not create or
edit map relationships, decide which scientific position is true, repair source
records, or infer missing scientific content.

Read `input/task.json`, `input/audit-sample.json`, and every dossier under
`input/dossiers/`. The dossiers are the source-grounded authority for this job.
The sampled relationship is only the object being checked. Do not accept its
label or rationale without comparing both endpoint records, cited evidence,
scope, assumptions, qualifications, and nearby argument structure in the
dossiers.

Return one audit item for every relationship ID in the task, exactly once. Use
the full 0–4 scale independently for each dimension:

- `evidence_grounding`: 0 has no resolving support; 4 both endpoints and the
  stated comparison are directly grounded by the supplied source records.
- `relation_type_fit`: 0 is wrong or materially misleading; 4 is the most
  accurate allowed potential relation type for the supplied endpoints.
- `scope_assumption_fit`: 0 ignores incompatible scope or assumptions; 4 makes
  the relevant alignment and differences explicit.
- `research_utility`: 0 adds no useful navigation; 4 materially helps a
  researcher compare the literature.
- `argumentative_importance`: 0 is peripheral; 4 connects central positions,
  methods, results, limitations, or open questions.
- `distinctiveness`: 0 is redundant with the supplied sample/corpus context; 4
  captures a materially distinct comparison.

Use `strong` only for a clearly grounded, well-typed, important, and distinct
edge. Use `usable` for a qualified but worthwhile research-map edge. Use `weak`
for a real but low-value or poorly scoped comparison, `misleading` when the edge
would distort navigation, and `manual_review` when supplied records do not
support a reliable evaluation. A relation may be `adjacent` rather than wholly
wrong when another type is better but the recorded comparison still exposes a
real connection.

Do not reward a relationship merely because its endpoints share vocabulary. Do
not penalize a relationship merely because it is a potential rather than proven
connection. Do not declare a paper or theory correct, incorrect, proven, or
refuted. Keep rationales concise, source-grounded, and comparative. Return only
the schema-constrained result.
