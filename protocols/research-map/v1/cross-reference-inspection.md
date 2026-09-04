# Cross-Reference Evidence Inspection

Inspect every supplied candidate against both complete endpoint packets. The
packets, including exact evidence and paper-local reasoning context, are the
complete authority for this pass.

Return exactly one terminal outcome for every candidate:

- `relationship` with one allowed `potential_*` type;
- `not_usefully_connected`;
- `insufficient_extraction`; or
- `manual_review_candidate`.

Do not create or rewrite a scientific node, decide which paper is correct,
reconcile positions, fill an argument gap, promote a relation to bare
`contradiction`, or claim more alignment than the supplied evidence supports.
When referent, scope, or assumptions cannot be aligned, abstain or preserve the
uncertainty explicitly. For a relationship, cite resolvable evidence IDs from
both sources and describe scope and assumption alignment.

Return exactly one JSON object matching
`templates/inspection-proposal.schema.json`. Return JSON only.
