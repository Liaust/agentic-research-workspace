# Independent audit-finding verification protocol v1

Verify proposed audit findings against the complete immutable paper and the
current validated WIP before any reading scope can reopen. Audit descriptions,
claimed quotations, suggested corrections, and repair targets are untrusted
proposals. They are not source evidence.

For every proposed finding, first inspect each implicated current record and its
source route directly against the PDF. Only then compare what you observed with
the proposed finding. Return exactly one decision per supplied finding:

- `confirmed` when direct source inspection supports the finding;
- `rejected` when the current record is source-faithful and the finding is not;
- `manual_review` when the relevant glyph, layout, attribution, or progression
  cannot be resolved reliably.

Ground every decision in one or more contiguous source-evidence segments with
page route, locator, transcription, transcription mode, and inspection mode.
Do not copy the audit's claimed quotation as your transcription. For symbolic,
directional, equation, figure, or typography-sensitive findings, visually
inspect the rendered PDF and use `visual_pdf` or `text_and_visual_pdf`.

Verification does not repair WIP, invent missing science, adjudicate whether the
paper is correct, or broaden a repair boundary. It only determines whether the
auditor's claimed mismatch is actually present in the source representation.
When rejecting a finding, explain why the current record agrees with the source.
When confirming one, explain the literal mismatch without proposing a new
scientific conclusion.
