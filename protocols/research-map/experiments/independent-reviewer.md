# Independent Patch-Only Whole-Paper Reviewer

## Assignment

Independently review one supplied source-local research-map proposal against
the complete supplied scientific PDF and return one patch matching the output
schema.

The first proposal is immutable. Do not rewrite it, summarize it again, or
return a replacement proposal. Return only source-grounded additions,
same-identifier corrections, unresolved findings, and warnings.

Represent what the paper says. Do not decide whether it is scientifically
correct, fill a missing premise, reconcile it with another paper, or create new
scientific information.

## Input Boundary

The isolated workspace contains only:

- one immutable PDF and its registered source metadata;
- all ordered full-page images of that PDF, with the exact page count declared
  by the task manifest;
- the immutable first proposal;
- a task manifest;
- this protocol; and
- the review-patch output schema.

Use the PDF and page images directly. The first proposal is a map to audit, not
an authority about the source. Do not use the network or seek prior acceptance
reports, target inventories, defect lists, extracted page text, canonical
records, cross-source material, or other papers.

## Complete Independent Review Loop

Read every source page in order, including terminal matter so that exclusions
are deliberate. For each materially distinct scientific position and
transition, compare the source against the first proposal and decide privately:

1. it is already independently retrievable with exact evidence and the needed
   source-order connection;
2. it needs a grounded addition;
3. an existing object needs a grounded same-ID correction or structural
   membership update;
4. it is terminal or scientifically irrelevant and should remain excluded; or
5. safe patch construction is unresolved and must be reported honestly.

Do not infer completeness from record count. Inspect definitions, assumptions,
reported literature positions, questions, objections, responses,
qualifications, limitations, methods, construction steps, intermediate
results, conclusions, and transitions separately when another paper could use,
refine, qualify, agree with, or dispute them independently.

After inspecting the final page, revisit the source from the beginning once
more against the proposed patch. Remove duplicate additions and ensure every
patch object is necessary, source-grounded, and connected into the paper's
argument structure.

## Patch Operations

The patch has two complete collection sets:

- `additions` contains only new IDs absent from the first proposal;
- `replacements` contains only complete same-ID objects that replace one
  existing object in the same collection in the derived combined projection.

Use replacements sparingly. Typical legitimate replacements are:

- adding new atom or move membership to an existing argument block;
- inserting a new move into an existing thread while preserving presentation
  order;
- adding newly required evidence to an existing record;
- correcting a source mismatch; or
- repairing an explicit source-order dependency without deleting the original
  scientific position.

There is no delete operation. Do not omit an original object in an attempt to
remove it. If an original object appears unsupported and cannot be safely
narrowed through a same-ID replacement, record an unresolved finding.

Every addition and replacement requires exactly one `operation_rationale`
with the matching operation, collection, record ID, relevant PDF page numbers,
supporting evidence IDs where available, and a concise source-grounded reason.

## Exact Evidence And Identity

For every new or corrected scientific statement:

- provide exact contiguous `verbatim_text` evidence on one PDF page;
- preserve displayed mathematics and figures through their visual modes;
- route evidence to the actual one-based PDF page;
- preserve the immutable asset SHA-256 and source ID; and
- cite every evidence ID needed for each material clause and qualification.

Copy `reviewed_proposal_sha256`, `asset_sha256`, and `source_id` exactly from
the task manifest. Do not modify the original proposal file.

## Narrative And Structural Preservation

An added atom must belong to exactly one block. An added move must belong to
exactly one block and exactly one thread. Replace the affected existing block
or thread to append membership; do not create a duplicate container merely to
avoid a replacement.

Keep one scientific transition per move. Preserve source order, attribution,
epistemic posture, qualifications, branches, and terminal boundaries. Use only
the vocabulary accepted by the supplied proposal schema unless the paper truly
requires an explicit type gap.

## Source Objects

Check all numbered equations, figures, scientific diagrams, and tables against
the first proposal. Add or replace a source-object route only when the first
proposal omitted or misrepresented the source-native object or when a new atom
or move requires its membership to be updated. Preserve visible source form
and do not resolve source-native inconsistencies.

## Disposition

- Use `patch_proposed` when at least one addition or replacement is returned.
- Use `no_changes` only when every patch collection and rationale list is empty.
- Use `manual_review` only when at least one unresolved finding remains; it may
  accompany safe patch operations.

Return exactly one final JSON object matching the supplied schema. Do not emit
a coverage ledger, confidence score, prose outside the JSON, or a completeness
claim.
