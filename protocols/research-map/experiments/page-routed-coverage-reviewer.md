# Experimental Page-Routed Coverage Reviewer

## Boundary

The immutable PDF is the source of truth. Map what the paper contains; do not
judge whether its claims are correct, repair its science, import another source,
or create information absent from the paper.

Use only this workspace. Do not access the repository, network, benchmark,
prior assessments, defect lists, canonical records, cross-source material, or
files outside the workspace except through the supplied deterministic tools.
The immutable first proposal is deliberately absent at launch.

This one Codex turn has three irreversible checkpoints:

```text
eight closed page ledgers -> proposal reveal -> every unit compared and frozen -> patch -> candidate validation
```

## Phase A: Close Every Page Before Seeing The Map

Read `input/paper.pdf` and all ordered page images visually in source order.
Zoom and revisit the original pages whenever prose, notation, figures, tables,
or layout are uncertain. Write `output/page-ledger.json` against
`templates/page-ledger.schema.json`.

For every page, enumerate every visible source block in order. A block is a
meaningful paragraph, displayed equation, figure/caption, table, heading, list,
footnote, terminal region, or other visually distinct source region. Give each
block:

- a contiguous one-based index and globally unique stable ID;
- exact opening and closing anchors from that page, without inserted ellipses;
- a semantic route to one or more retrieval units; or
- an explicit non-semantic exclusion reason.

Do not close a page because it seems unimportant. Terminal or administrative
material is still a visible block and must be recorded and reasoned about.

### Independent-Retrieval Unit Test

One unit expresses one independently retrievable scientific role. Split a
semantic block whenever attribution, qualification, epistemic posture, formal
role, search value, contradiction value, or argumentative role differs. Split
claims, definitions, assumptions, objections, responses, reported positions,
methods, constructions, intermediate results, transitions, conclusions,
equations, figures, and tables when they can be used independently.

Every unit must:

- route bidirectionally to one or more semantic blocks;
- contain exact single-page evidence within those block routes;
- preserve attribution and epistemic posture;
- name a plausible retrieval query and split rationale; and
- route to an earlier predecessor unit or explicitly open a thread.

Closely connected units can share blocks and threads. Do not collapse them into
one unit merely to reduce count, and do not atomize the paper into disconnected
sentences. No minimum block, unit, or record count is implied.

All pages must have `status: closed`, block indexes must be contiguous, and
`separable_units_remaining` must be false before the reveal tool can succeed.

## Page-Ledger Checkpoint And Proposal Reveal

Run exactly:

```sh
python tools/reveal_proposal.py
```

The tool validates and freezes all page, block, unit, evidence, and narrative
routes, then reveals the byte-identical comparator as
`input/original-proposal.json`. If it refuses, fix only mechanical ledger
contract defects. Do not change the ledger after a successful reveal.

## Phase B: Compare Every Unit At Its Frozen Granularity

Read the revealed proposal completely. Write
`output/unit-comparison.json` against
`templates/unit-comparison.schema.json`, classifying every frozen unit exactly
once:

- `fully_retrievable`: meaning, attribution, qualifications, epistemic posture,
  exact evidence, source role, and logical placement are intact through named
  evidence, atom, and move routes;
- `partially_retrievable`: a material part is explicit but fidelity or
  independent retrieval is incomplete;
- `missing`: no adequate independent semantic record exists;
- `misrepresented`: a record materially changes or misattributes the source;
  or
- `unresolved`: fidelity cannot be established safely.

Broad neighboring evidence, a containing argument block, a related thread, or
an atom on the same topic does not make a unit fully retrievable. Equations,
figures, and tables also require their exact source-object route.

For every repairable unit, list the exact additions and same-ID replacements
the final patch will contain. Do not draft or create the patch yet.

## Comparison Checkpoint

After every unit is classified, run exactly:

```sh
python tools/freeze_comparison.py
```

The tool refuses when `output/review-patch.json` already exists. It proves the
page ledger and revealed proposal are unchanged, validates every unit and
block route, and freezes the complete comparison. Fix only mechanical
comparison defects before success. Do not change the comparison afterward.

## Phase C: Patch And Validate One Derived Candidate

Write `output/review-patch.json` against
`templates/review-patch.schema.json`. Additions and same-ID replacements are
allowed; deletions, silent semantic mutation, and removal of noticed source
material are forbidden. Every operation and unresolved finding must match the
frozen comparison exactly.

Then run:

```sh
python tools/validate_candidate.py
```

The tool reuses the validated immutable patch boundary, writes the sole
derived map to `output/candidate-proposal.json`, and performs complete
hierarchy, vocabulary, dossier, canonical round-trip, and graph admission.
Read `output/candidate-validation.json`.

If validation reports a mechanical defect, repair only the patch inside this
same turn and run the same validator again. Do not alter the frozen ledger or
comparison meaning, weaken a schema, remove a scientific unit to make the graph
pass, start another semantic turn, or request post-turn repair. If a valid
candidate cannot be produced, retain the failure honestly.

Return exactly the final `output/review-patch.json` object. The deterministic
candidate is experimental and non-canonical. The ledger, comparison, patch,
events, and receipts are audit surfaces, not research truth stores.
