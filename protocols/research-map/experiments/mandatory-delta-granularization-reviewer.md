# Experimental Mandatory-Delta Granularization Reviewer

## Boundary

The immutable PDF is the source of truth. Map only what the paper contains.
Do not judge correctness, repair the science, import another source, or create
information absent from the paper.

Use only this workspace. Do not access the repository, network, benchmark,
prior semantic output, prior assessments, defect lists, canonical records,
cross-source material, or files outside the workspace except through the
supplied deterministic tools. The sealed proposal is absent at launch.

This one Codex turn has three irreversible checkpoints:

```text
eight closed page ledgers -> proposal reveal -> mandatory comparison freeze -> nonempty patch -> candidate validation
```

There is no record quota, per-page change quota, benchmark hint, known-defect
list, or permission to invent content. You choose which source-grounded delta
is required; you may not choose whether a delta occurs.

## Phase A: Close Every Page Before Seeing The Map

Read `input/paper.pdf` and all eight ordered page images visually in source
order. Zoom and revisit the original pages whenever prose, notation, figures,
tables, or layout are uncertain. Write `output/page-ledger.json` against
`templates/page-ledger.schema.json`.

Enumerate every visible block on every page in order. Each block must have
exact opening and closing source anchors and either route bidirectionally to
one or more semantic retrieval units or carry an explicit non-semantic
exclusion reason.

Each unit expresses one independently retrievable scientific role. Split a
block when attribution, qualification, epistemic posture, formal role, search
value, contradiction value, or argumentative role differs. Preserve the
paper's own narrative and keep closely connected units connected. Do not turn
the paper into disconnected sentences.

Every unit must have exact single-page evidence, block routes, a retrieval
query, a split rationale, and either an earlier predecessor or an explicit
thread opening. Formal units remain separately routed. No unit count is
required.

After every page is closed and `separable_units_remaining` is false, run:

```sh
python tools/reveal_proposal.py
```

Fix only mechanical ledger-contract defects if the tool refuses. After it
succeeds, do not change the page ledger.

## Phase B: Freeze A Mandatory Source-Grounded Delta

Read the revealed `input/original-proposal.json` completely. Write
`output/unit-comparison.json` against
`templates/unit-comparison.schema.json`, classifying every frozen unit once as
`fully_retrievable`, `partially_retrievable`, `missing`, `misrepresented`, or
`unresolved`.

`fully_retrievable` requires intact meaning, attribution, qualification,
epistemic posture, exact evidence, source role, and logical placement through
named evidence, atom, move, and formal-object routes. Neighboring evidence or
a containing block does not suffice.

At least one unit must be `partially_retrievable`, `missing`, or
`misrepresented`, with one or more exact planned additions or same-ID
replacements. An all-`fully_retrievable` comparison and unresolved-only output
are invalid. Do not draft the patch before every comparison is complete.

Run:

```sh
python tools/freeze_comparison.py
```

The tool proves the ledger and proposal are unchanged, rejects a pre-existing
patch, requires a repairable unit, and freezes the exact operation plan. Fix
only mechanical comparison defects before success. Do not change the frozen
comparison afterward.

## Phase C: Produce And Validate A Nonempty Candidate

Write `output/review-patch.json` against
`templates/review-patch.schema.json`. The only valid terminal disposition is
`patch_proposed`. `no_changes`, `manual_review`, unresolved-only output, and
empty additions plus replacements are invalid.

Every addition or replacement must appear in one or more repairable frozen
units, cite exact evidence records, cover every source page used by those
units, and retain their frozen block/predecessor narrative placement. Additions
and same-ID replacements are allowed; deletion, silent mutation, invented
content, and removal of source material are forbidden.

Run:

```sh
python tools/validate_candidate.py
```

The tool applies the immutable patch, reuses the complete hierarchy,
vocabulary, evidence, identity, equation, source-order, canonical round-trip,
and graph admission path, and writes the sole derived map to
`output/candidate-proposal.json`. It also rejects a candidate semantically
identical to the sealed proposal.

If validation reports a mechanical patch defect, repair only that patch inside
this same turn and run the same validator again. Do not alter frozen meaning,
weaken any validator, remove a scientific unit merely to pass, start another
turn, or request post-turn semantic repair. If no valid source-grounded delta
can be produced, retain the failed result honestly.

Return exactly the final `output/review-patch.json` object. Every artifact is
experimental and non-canonical.
