# Hierarchical Visual Whole-Paper Reader

## Assignment

Read the one supplied scientific source completely in one continuous turn and
write one source-local research-map proposal matching the supplied JSON Schema
to `output/proposal.json`.

Represent what the paper says. Do not decide whether it is scientifically
correct, fill a missing premise, reconcile it with another paper, or create new
scientific information.

## Input Boundary

The workspace contains only:

- one immutable PDF;
- its registered source and asset metadata;
- a task manifest;
- ordered full-page images of that PDF;
- this protocol; and
- the final output schema.

Use the PDF and attached page images directly. You may zoom, inspect, and use
local scratch files inside this one turn. Do not use the network or seek other
papers, accepted maps, earlier proposals, or source-specific correction lists.

The page images are visual transport for the same PDF, not a second source.
One-based PDF page numbers in the proposal must match their ordered image.

## Output Principle

The proposal has two simultaneous scales:

```text
paper
  -> source-ordered argument blocks
     -> reasoning moves and granular atoms
        -> contiguous source evidence segments
```

Argument blocks preserve the paper's narrative and derivational continuity.
Atoms preserve independently searchable and cross-paper-connectable scientific
content. Moves explain how the source uses atoms. Threads reconstruct longer
arguments across one or more blocks.

Do not choose between a coherent paper summary and granular records. Preserve
both through explicit containment and references.

## Continuous Reading Loop

Read every page in source order, including terminal matter so that it can be
excluded deliberately. For each coherent scientific section or transition:

1. inspect its prose, equations, figures, captions, footnotes, and layout;
2. identify the source-native role of the passage;
3. update the source-ordered argument block that contains it;
4. collect contiguous evidence segments before drafting semantic claims;
5. add or refine independently useful atoms;
6. add moves that preserve what the source takes as input and produces next;
7. place moves into reconstructable ordered threads; and
8. carry qualifications, attribution, symbol roles, objections, responses, and
   limitations forward without silently normalizing them.

Finish the complete paper before finalizing the authoritative JSON. You may
revise private scratch notes during this turn, but maintain only one proposal
artifact.

## Argument Blocks

Create a small source-ordered sequence of coherent scientific blocks. A block
may correspond to a section, a multi-paragraph reasoning phase, a derivation,
an explicit model construction, or an objection-response phase. It is not a
fixed generic template.

Each block must:

- have a consecutive `sequence` beginning at 1;
- use its real page interval;
- summarize the scientific work performed in that portion of the paper;
- list every atom it contains exactly once; and
- list every move it contains exactly once.

Every atom and every move belongs to exactly one block. Threads may span
blocks. Blocks may overlap pages when multiple scientific phases share a page,
but their source order must not move backwards.

Do not create blocks for acknowledgments, bibliography entries, publication
boilerplate, running headers, or other non-scientific terminal matter.

## Granular Atoms

Create a separate atom when a source-grounded item would be useful to retrieve,
cite, compare, or connect independently. Relevant kinds normally include:

- definition;
- concept;
- assumption;
- reported literature position;
- claim;
- question;
- objection;
- response;
- qualification;
- limitation;
- equation;
- figure;
- method or construction step;
- check or intermediate result; and
- final result or conclusion.

Atomize by scientific use, not by sentence count. Keep dependent clauses
together when splitting them would destroy their meaning. Do not create
duplicate atoms that merely paraphrase the same source position.

Work block by block in source order. Within each block, separately inspect the
surrounding prose for independently useful definitions, assumptions, reported
positions, premise consequences, questions, qualifications, objections,
responses, limitations, and conclusions. Numbered equations and figures do not
by themselves cover the scientific prose that introduces, interprets, limits,
or argues from them. Preserve an item as its own atom when another paper could
meaningfully agree with, refine, use, or dispute it without also agreeing with
the rest of the block.

Every atom must participate in a move unless it has a non-empty
`standalone_context` explaining why it is independently useful without being a
reasoning step. Standalone context is exceptional, not a substitute for mapping
the paper's logic.

Preserve attribution and epistemic posture. Distinguish what the paper:

- asserts or concludes;
- assumes or defines;
- reports others accept, omit, or object to;
- proposes as a model or method;
- says follows from a derivation or check; and
- limits, qualifies, or leaves open.

Do not convert a reported view into the author's result or an attributed
objection into a paper-wide conclusion.

## Moves And Threads

A move represents one source-grounded scientific transition. Its `inputs` and
`outputs` reference atom IDs and explain how the source moves from one mapped
position to another. Use only the exact recommended role slugs `introduces`,
`defines`, `assumes`, `derives`, `supports`, `justifies`, `applies`,
`qualifies`, `contrasts`, `interprets`, `objects`, `responds`, and `concludes`
when one fits. Do not change their grammatical form. A source-specific role is
allowed only through the explicit `type_gap` mechanism below.

A move that derives or concludes something must have material inputs. A move's
outputs must be independently identifiable atoms. Do not create an unsupported
transition merely to make a thread look continuous.

Keep one scientific transition per move. Do not hide a target, construction,
assignment, check, equivalent form, and result inside one broad move merely
because they occur in the same paragraph or derivation. A move may connect
multiple atoms when the source actually performs one relation over them.

Every move belongs to exactly one thread and uses a unique
`presentation_index` within that thread. A thread lists its moves in increasing
presentation order. Prefer a small number of reconstructable arguments,
derivations, model constructions, definition developments, comparisons, or
objection-response paths over a thread for each paragraph.

Use thread branches only when the source itself branches, contrasts paths, or
returns to an earlier step.

## Exact Evidence Segments

Evidence segments are the proposal's sole quotation surface. There is no
separate `exact_span` summary.

For prose:

- use `verbatim_text` only for an exact contiguous transcription from one PDF
  page;
- preserve the actual source wording and punctuation;
- do not insert ellipses, bracketed bridges, paraphrases, corrections, or
  synthesized text;
- use separate segments for non-contiguous passages or passages on different
  pages; and
- route every segment to its real one-based page and an inspectable locator.

For displayed mathematics:

- use one `displayed_math` segment for the visible source form on one page;
- preserve direction, operators, signs, indices, accents, script or Greek
  characters, products, tensor ordering, limits, denominators,
  normalization, and conditioning; and
- inspect the page image instead of trusting OCR-like text alone.

For a figure, diagram, or visually represented table:

- use `figure_region` and faithfully describe the identified region, labels,
  and scientific role;
- route it to the exact page; and
- preserve visible distinctions rather than inventing missing detail.

Every atom, move, and thread must cite all evidence needed for every material
statement and qualification it contains. If exact support cannot be supplied,
remove or narrow the unsupported clause, or return `manual_review` with an
explicit reason. Do not certify your own proposal with a separate entailment
ledger.

## Equations, Figures, And Source Objects

Create one `source_object` entry for every numbered displayed equation and
every scientific figure. Add other tables or diagrams when they have a
scientific role worth routing explicitly.

Each source object must identify:

- its source-native label;
- page;
- scientific role;
- displayed-math or figure-region evidence;
- independently connectable atom IDs; and
- the move IDs in which the paper uses it.

Every `equation` source object must reference only `kind: equation` atoms, and
every `figure` source object must reference only `kind: figure` atoms. A
displayed equation that functions as a definition or assumption still needs an
equation atom with fidelity; express its scientific role on the source object
and in the connected move rather than changing the atom to a non-equation
kind.

Choose the role that the source gives the object: `target`, `definition`,
`assumption`, `construction`, `assignment`, `check`, `equivalent-form`,
`result`, `illustration`, or `other`.

Different roles must not be collapsed into one atom. In particular, preserve a
sequence such as target -> construction -> assignments -> checks -> equivalent
form -> result as independently searchable objects and connected moves when
the source presents those distinct roles.

Multiple numbered objects may share one atom only when they have the same role,
their meaning genuinely depends on being read together, and every shared
source-object entry gives the same explicit `grouping_rationale`. A consecutive
number range alone is not a rationale.

Equation atoms require non-null `fidelity` containing:

- visible source form;
- symbol and element context;
- source-stated scope and assumptions; and
- an honest visual disposition.

Figure atoms also require non-null fidelity with their visible elements and
scientific scope.

The deterministic admission checker cannot decide whether displayed
mathematics or a figure region matches its page image. It therefore reports
those segments as `uncheckable` and `visual_review_required` even when the
proposal is otherwise valid. This is a delegation to your direct visual
inspection, not a failed check and not by itself a reason for manual review. Inspect each implicated
page image, record an honest fidelity disposition, and return `covered` when
you can verify the source object. Use `manual_review` only when your own visual
inspection cannot resolve its fidelity.

## Terminal And Scope Boundary

Inspect the entire paper, but map only research-relevant scientific content.
Do not create atoms, moves, threads, or blocks merely for:

- acknowledgments;
- bibliography entries;
- publication metadata;
- running headers or footers;
- copyright boilerplate; or
- author contact information.

A scientific position attributed to prior work may be mapped when it matters
to this paper's reasoning, with its attribution preserved. A citation or
bibliography entry by itself is not a semantic record.

## IDs And Vocabulary

Use stable lowercase-hyphenated IDs:

- `<source-id>:block:<slug>`;
- `<source-id>:evidence:<slug>`;
- `<source-id>:atom:<slug>`;
- `<source-id>:move:<slug>`;
- `<source-id>:thread:<slug>`; and
- `<source-id>:object:<slug>`.

All record revisions are `1`. All references must resolve within this one
proposal.

Use these exact recommended atom-kind slugs when one fits: `claim`,
`definition`, `assumption`, `concept`, `equation`, `method`, `result`,
`question`, `objection`, `response`, `limitation`, and `figure`. A paper's final
conclusion is normally a `claim` or `result` atom reached by a `concludes` move;
`conclusion` is not a recommended atom kind.

Use these exact recommended thread-kind slugs when one fits: `argument`,
`derivation`, `model-construction`, `question-method-result`,
`definition-development`, `objection-response`, and `comparison`.

If the source genuinely requires an unlisted atom kind, move role, or thread
kind, use one stable source-neutral lowercase-hyphenated slug and provide the
matching `type_gap`. Otherwise set `type_gap` to `null`.

## Final Source-To-Map Coverage Sweep Inside The Turn

After the complete draft exists, return to the beginning of the PDF and page
images. Revisit every scientific block in source order and compare the source
against the working proposal. This is a source-to-map sweep, not a review
limited to records you already chose to write.

For each block, use private scratch notes to account for each materially
distinct scientific position or transition as one of:

- represented by explicit atom, move, thread, and evidence IDs;
- intentionally excluded because it is terminal or not scientific content; or
- unresolved because faithful representation requires manual review.

Do not return those scratch dispositions as a coverage ledger, confidence
score, or claim of completeness. Do not target a fixed number of records. The
purpose is to find omissions and broken scientific continuity, not to maximize
output count.

When the sweep finds an omitted independently useful position or source-grounded
transition, patch the working proposal before emission. Update every affected
evidence, atom, move, block, thread, and source-object reference together. When
splitting a broad record would destroy a dependency, preserve the dependency
and make the independently useful position retrievable through the hierarchy
instead of fragmenting it mechanically.

During this source-to-map sweep confirm that:

- every page was inspected;
- blocks reconstruct the scientific progression in source order;
- every independently useful definition, assumption, reported position,
  question, objection, response, qualification, limitation, method, equation,
  figure, check, and result has an explicit record or an honest manual-review
  reason;
- every atom and move belongs to exactly one block;
- every move belongs to exactly one ordered thread;
- every material clause is supported by linked contiguous evidence segments;
- prose segments are exact quotations without ellipses or synthesized bridges;
- every numbered equation and scientific figure has a source-object route;
- different equation roles remain independently connectable;
- symbols, equation direction, and visual distinctions match the source;
- terminal matter did not enter the semantic map;
- no scientific gap was filled by inference; and
- there are no duplicate IDs or dangling references.

The sweep improves the working proposal but is not an independent audit and
must not certify its own success. External validation and complete source review
occur after your turn.

## Authoritative Artifact Contract Check

Serialize the exact candidate JSON to `output/proposal.json` and run:

```bash
python tools/validate_proposal.py output/proposal.json
```

The command validates hierarchical source routes, adapted records and dossier
invariants, the canonical Markdown round-trip, and source-local graph
compilation under the task's canonical or calibration-tolerant policy. If it
reports failed checks, repair `output/proposal.json` in place and rerun it.
Do not inspect or use extracted page text as a semantic input; the tool exposes
only deterministic contract findings. Return `disposition: covered` only after
the checker exits successfully. Successful output may include `uncheckable`
math or figure findings: resolve those through your already-required direct
page-image inspection rather than treating them as failures. If a faithful
repair or visual verification cannot be made from the PDF and page images,
return `disposition: manual_review` with the exact reason. Scratch notes may
help your reasoning, but do not write a second proposal candidate anywhere in
the workspace. The coordinator validates, fingerprints, and admits this exact
authoritative file after the turn.

## Final Output

Leave one JSON object matching
`templates/hierarchical-reading-proposal.schema.json` at
`output/proposal.json`.

Do not reproduce that JSON in the terminal response and do not create a second
alternative or separate ledger beside it. After the authoritative file passes
the contract check, respond only with a short completion statement. Use
`disposition: manual_review` and explicit `manual_review_reasons` inside the
artifact when the source cannot be represented faithfully under this contract.
