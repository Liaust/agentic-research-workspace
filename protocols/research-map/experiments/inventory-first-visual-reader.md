# Inventory-First Override For The Hierarchical Visual Reader

This source-neutral experimental contract is appended after
`hierarchical-visual-reader.md` in the reader workspace. It changes cognition
order and adds auditable scratch validation. Every hierarchy, evidence,
atomization, continuity, source-object, vocabulary, terminal-boundary, and
final-output rule in the base protocol still applies.

## One-Call Boundary

Complete all phases in one continuous visual Codex turn. The PDF and its
ordered page images are the source of truth. Do not use another model call,
prior proposal, target list, defect list, external page-text extraction,
network source, or cross-source record.

The scratch inventory and resolution table are working artifacts, not
canonical scientific records, an independent audit, a completeness claim, or
additional final model outputs. Return exactly one final proposal object.

## Phase 1 - Read And Inventory Before Mapping

Read the complete PDF in source order, including terminal matter, before
writing any proposal candidate. During this first pass do not draft blocks,
atoms, moves, threads, or source objects. Instead create a lightweight ordered
inventory at `output/source-inventory.json` matching
`templates/source-position-inventory.schema.json`.

Inventory materially distinct, source-grounded items that might need an
independently retrievable atom or logical route: definitions, assumptions,
reported positions, claims, questions, objections, responses, qualifications,
limitations, methods, transitions, equations, figures, checks, results, and
other scientific positions. Also inventory terminal or non-scientific regions
only far enough to make their deliberate exclusion inspectable.

Use one position for one connectable scientific use, not one sentence. Preserve
dependencies through `dependency_position_ids`; do not fragment clauses whose
meaning would be destroyed by separation. Do not target a record count.

After writing the inventory, run:

```text
uv run python tools/freeze_inventory.py
```

Do not create `output/proposal-candidate.json` before the freeze succeeds. Do
not edit the inventory after it is frozen.

## Phase 2 - Construct The Hierarchical Map

Build the normal source-ordered hierarchical proposal from the frozen
inventory and the source itself. The inventory is navigation, not evidence;
every proposal statement still requires exact source evidence under the base
protocol.

Preserve the paper's connected argument rather than producing a bag of
positions. Map dependencies through blocks, one-transition moves, and ordered
threads. A broad block summary does not make a connectable source position
independently retrievable.

Write the complete draft proposal to `output/proposal-candidate.json`.

## Phase 3 - Resolve Every Inventory Position

Create `output/inventory-resolution.json` matching
`templates/inventory-resolution.schema.json`. Give every frozen position
exactly one disposition:

- `represented`: cite at least one explicit atom, move, and evidence record;
- `excluded_terminal_or_non_scientific`: only for a position inventoried with
  category `terminal_or_non_scientific`, with a specific reason; or
- `manual_review`: give a specific reason faithful representation is not safe.

All represented IDs must exist in the proposal candidate. Do not treat a block
summary, keyword overlap, or implicit context as representation. Do not mark a
scientific position excluded merely because it is difficult to map.

This resolution is generator accounting, not a coverage score or proof that
the inventory itself is complete.

## Phase 4 - Deterministic Candidate Validation

Run:

```text
uv run python tools/validate_candidate.py
```

The checker verifies the frozen inventory hash, one-to-one dispositions,
record references, final schema and hierarchy, controlled vocabulary/type
gaps, dossier validity, and graph compilation. If it reports a bounded
mechanical defect, repair the candidate or resolution inside this same turn and
rerun it. Never change the frozen inventory.

Do not emit a proposal that has not received a valid
`output/candidate-validation.json` receipt. If faithful correction is not
possible, use the base proposal's `manual_review` disposition and reasons
rather than inventing content.

## Final Emission

Return the JSON content of the validated `output/proposal-candidate.json` as
the single final response matching
`templates/hierarchical-reading-proposal.schema.json`. Return no inventory,
resolution table, validation receipt, commentary, Markdown fence, or second
alternative in the final response.
