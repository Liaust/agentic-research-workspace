# Experimental Retrievability-Enforced Source-First Reviewer

## Boundary

The immutable PDF is the source of truth. Map what the paper contains; do not
decide whether its scientific claims are true, repair its argument, import
another source, or create an answer that the paper does not give.

Use only this workspace. Do not access the repository, network, prior
evaluations, benchmarks, defect lists, extracted page text, canonical records,
or files outside the workspace except through the supplied deterministic
helpers. The first proposal is deliberately absent at launch.

The workflow has three irreversible checkpoints inside this one Codex turn:

```text
source-only inventory -> proposal reveal -> frozen comparison -> patch -> candidate validation
```

## Phase A: Inventory The Source Before Seeing The Map

1. Read `input/paper.pdf` and every ordered page image completely in source
   order. Revisit and zoom whenever prose, equations, figures, or layout are
   uncertain.
2. Write `output/source-inventory.json` against
   `templates/source-inventory.schema.json`.
3. Inventory definitions, claims, questions, assumptions, qualifications,
   objections, responses, reported positions, methods, constructions,
   intermediate results, conclusions, transitions, equations, figures, tables,
   and other scientifically connective positions.
4. Preserve attribution and epistemic posture. A view reported by the author is
   not automatically the author's own assertion.
5. Preserve the paper's logic with predecessor links. Granularity must not turn
   the paper into a bag of disconnected statements.

### Independent-Retrieval Split Test

One inventory position must express one independently retrievable scientific
role. Split a position whenever a component could independently be:

- searched for or cited;
- compared or contradicted by another paper;
- attributed to a different speaker or source;
- qualified without qualifying the other component;
- assigned a different epistemic posture;
- represented as a distinct equation, figure, table, definition, premise,
  objection, response, transition, or conclusion; or
- used as a distinct step in reconstructing the argument.

Do not split merely by sentence count. Closely connected material may share a
thread or predecessor relation while remaining independently retrievable.
Record one plausible retrieval query and an explicit split rationale for every
position. `separable_positions_remaining` must be false only after applying
the test.

Use exact, single-page source evidence. Do not insert ellipses into content
labeled `verbatim_text`; use multiple evidence objects when noncontiguous or
cross-page support is needed.

## Inventory Checkpoint And Proposal Reveal

Run exactly:

```sh
python tools/reveal_proposal.py
```

The helper validates and freezes the inventory, then reveals the immutable
proposal as `input/original-proposal.json`. Fix only mechanical inventory
contract errors if it refuses. Do not modify the inventory after success.

## Phase B: Compare Every Position At Its Own Granularity

Read the proposal fully, then write `output/inventory-comparison.json` against
`templates/inventory-comparison.schema.json`.

Classify every inventory position exactly once:

- `fully_retrievable`: its material meaning, attribution, qualifications,
  epistemic posture, exact evidence, source role, and logical placement are all
  intact through named evidence, atom, and move routes;
- `partially_retrievable`: some material part is explicit, but independent
  retrieval or fidelity is incomplete;
- `missing`: no adequate independent semantic record exists;
- `misrepresented`: a record exists but materially changes or misattributes
  the source; or
- `unresolved`: source fidelity cannot be established safely.

Broad neighboring evidence, a containing block, a related thread, or an atom
about the same general topic does not make a position fully retrievable.
Equations, figures, and tables also require their specific source-object route.

For every repairable position, plan the exact additions and same-ID
replacements the final patch will contain. Do not draft the patch yet.

## Comparison Checkpoint

After every inventory item is classified, run exactly:

```sh
python tools/freeze_comparison.py
```

The helper refuses if `output/review-patch.json` already exists, validates the
complete ledger, and freezes its hash. Fix only mechanical comparison errors
before success. Do not change the comparison afterward.

## Phase C: Patch And Validate One Combined Candidate

Write `output/review-patch.json` against
`templates/review-patch.schema.json`. Additions and same-ID replacements are
allowed; deletions and silent semantic mutation are forbidden. Every operation
and unresolved finding must match the frozen comparison exactly.

Then run:

```sh
python tools/validate_candidate.py
```

The helper applies the patch to the immutable proposal, writes the sole derived
map to `output/candidate-proposal.json`, and validates hierarchy, vocabulary,
dossier invariants, canonical round-trip, and graph compilation. Read
`output/candidate-validation.json`.

If validation reports a mechanical defect, repair the patch within this same
turn without changing the frozen inventory or comparison meaning, then rerun
the validator. Do not weaken the schema, remove a scientific position to make
validation pass, or request another semantic turn. If a safe candidate cannot
be produced, retain the unresolved result honestly.

Return exactly the final `output/review-patch.json` object. The deterministic
combined candidate is the single experimental map artifact; inventory,
comparison, patch, and receipts are audit surfaces and none is canonical state.
