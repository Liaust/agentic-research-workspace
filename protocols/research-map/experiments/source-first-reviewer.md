# Experimental Source-First Reviewer Protocol

## Boundary

This protocol governs one experimental second-pass reviewer. The immutable PDF
is the source of truth. The reviewer maps what the source contains; it does not
adjudicate scientific truth, create missing arguments, or access other papers.

The reviewer must use only the supplied workspace. It must not access the
network, repository, prior evaluations, defect lists, extracted page text,
canonical records, or any file outside the workspace. The first proposal is
deliberately absent at the start.

## Phase A: Read The Source Before The Map

1. Inspect `input/paper.pdf` and every ordered image under `input/pages/`.
2. Read all pages in order, including terminal pages. Zoom or revisit the PDF
   and images whenever text, equations, or layout are uncertain.
3. Write `output/source-inventory.json` against
   `templates/source-inventory.schema.json`.
4. Inventory the paper's connectable scientific positions in source order:
   definitions, claims, questions, assumptions, argument steps, objections,
   replies, reported positions, qualifications, limitations, methods,
   constructions, intermediate results, conclusions, transitions, equations,
   figures, and tables when scientifically useful.
5. Preserve attribution and epistemic posture. A position reported by the
   author is not automatically the author's own assertion.
6. Give every position exact source evidence and say what makes it useful for
   reconstructing or connecting the paper. Use predecessor links to preserve
   the paper's actual reasoning and narrative transitions.
7. Give every page exactly one deliberate page review. Use
   `terminal_material_reviewed` only when the page has no connectable
   scientific content; record exclusions explicitly.

Do not infer a quota. Granularity is justified by independent scientific or
connective value, not sentence count. Do not inspect or seek the proposal
before the inventory has been written.

## Inventory Checkpoint

Run exactly:

```sh
python tools/reveal_proposal.py
```

The helper validates and freezes the inventory before revealing the immutable
proposal as `input/original-proposal.json`. If it refuses, fix only mechanical
inventory-contract errors and invoke it again. Do not change the inventory
after a successful reveal.

## Phase B: Compare The Source Inventory With The Map

1. Read the now-visible proposal fully.
2. Compare every inventory position with proposal evidence, atoms, moves,
   threads, blocks, and source objects.
3. Write `output/inventory-comparison.json` against
   `templates/inventory-comparison.schema.json`.
4. Classify each inventory position exactly once:
   - `covered`: independently retrievable with its material meaning,
     attribution, qualifications, and logical placement intact;
   - `requires_addition`: absent or not independently retrievable;
   - `requires_replacement`: present but a same-ID correction is necessary;
   - `unresolved`: fidelity cannot be established without human review.
5. Name exact proposal records and exact patch operations. Shared context does
   not count as coverage when the scientific position itself cannot be found.
6. Recheck source order and intra-paper connections. Do not reduce the paper
   to isolated statements.

## Final Patch

Write `output/review-patch.json` and return that same JSON object as the final
structured response. Additions and same-ID replacements are allowed; deletion
and silent mutation are forbidden. Every operation requires exact evidence and
must be accounted for by one inventory comparison. If no safe patch represents
an omission, use an unresolved finding rather than inventing content.

The original proposal, frozen inventory, comparison, and patch remain separate
inspectable artifacts. None is canonical research state.
