# Reading protocol v2

Read the assigned landmark batch in the context of the complete paper and any
cumulative `output/wip.json`. Return proposal records only; deterministic code
owns WIP, coverage, state transitions, canonical Markdown, and the graph.

Account for every assigned landmark exactly once. Use `represented` when one or
more proposed or existing record IDs faithfully represent it,
`not_substantive` only when direct source inspection shows that the orientation
checkpoint does not carry semantic content worth mapping, and `manual_review`
when exact representation cannot be resolved. A landmark disposition is a
coverage claim, not permission to invent content.

Atomize by future use, not by sentence count. Create an atom when a source item
would be useful to search, cite, compare, or connect independently. A
qualification constrains another atom; do not use qualifications to hide a
separate definition, literature position, assertion, distinction, question,
or result. Conversely, do not split an item whose independent retrieval would
lose its meaning. Preserve attribution, epistemic posture, scope, and the
paper's own limits.

Atom kinds and reasoning-move roles are different classifications. A claim
reached by a `concludes` move is still normally a `claim` or `result` atom;
information constrained by a `qualifies` move is not thereby a `qualification`
atom. Classify what the atom makes independently addressable, then classify how
the paper uses it in the move. Any source-native kind outside the recommended
vocabulary needs a matching, justified type gap rather than a stylistic synonym.

Evidence records carry the registered asset hash and one or more contiguous
segments. Every segment needs an exact page route, inspectable locator,
transcription, and transcription mode: `verbatim_text`, `normalized_text`,
`displayed_math`, or `figure_region`. Multiple non-contiguous passages require
multiple segments. Never label an interval containing omitted material as one
exact span. Equations and figures additionally require source form or caption,
symbol or element context, scope and assumptions, and a visual-fidelity
disposition.

Reasoning has two axes:

- A thread's `move_ids` and every move's `presentation_index` record source
  presentation order.
- A move's `inputs`, `outputs`, and `scientific_use` record how the source uses
  material scientifically.

A later-presented equation, observation, or discussion may therefore support or
justify an earlier-stated result. Keep the supporting move later in the thread
while pointing its scientific use toward the supported result. Do not reverse
chronology or fabricate a derivation. Moves list all material inputs and
outputs; threads keep intermediate steps, branches, objections, and responses
reconstructable.

`derives` and `concludes` moves require explicit material inputs. Do not label a
move `concludes` merely because it appears near the end of the paper or reports
closing metadata. A genuine conclusion must expose the source-local material it
concludes from so its backward evidence trace remains inspectable.

Use stable IDs. When correcting an existing record from `output/wip.json`, emit
the next sequential revision; otherwise do not repeat the record. Do not fill a
scientific gap by inference. If evidence, visual fidelity, attribution, or a
source-native structure cannot be represented faithfully, request manual review
or use a justified type gap.

Only `output/wip.json` is revision history. A rejected proposal in `output/`
never entered WIP: reuse revision 1 for any of its IDs still absent from WIP,
rather than incrementing them because they appeared in rejected JSON.

When the coordinator supplies an audit repair boundary, treat its three parts
separately:

- `revise_record_ids` authorizes whole-record next revisions only;
- `add_for_landmark_ids` authorizes new source-grounded records tied to those
  missing landmarks only;
- `reopen_scope_ids` identifies the bounded batches that may be revisited.

Preserve all unaffected source-faithful content. Do not emit an unrelated new
record or revision. If the paper cannot support the requested repair, request
manual review rather than returning an empty or no-op proposal.

For an audit-authorized correction, do not return immediately after drafting.
Write the exact candidate to `scratch/reading-proposal.json`, run
`python tools/validate_reading.py scratch/reading-proposal.json`, and repair any
reported error within the same Codex turn. The validator overlays the candidate
on the current WIP using persistent revision semantics, then checks the complete
merged source map through dossier, evidence, argument-structure, canonical
round-trip, and graph invariants. Use its record-addressed findings to repair the
candidate; do not weaken or bypass them. The final response must be byte-for-byte
equivalent JSON to the candidate that passed. Never alter the correction contract
or current WIP to make a proposal pass. If a source-faithful correction cannot
pass, return a schema-valid manual-review proposal.
