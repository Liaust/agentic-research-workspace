# Failed Cross-Reference Shard Correction v1

You are correcting the mechanics of one retained multi-surface mapper proposal.
You are not rereading sources, adding scientific nodes, adjudicating papers, or
trying to increase the number of relationships.

Read only:

- `input/task.json` for immutable shard identity and owned pairs;
- `input/corpus-records.json` for the frozen records and evidence authority;
- `input/original-proposal.json` for the retained mapper output; and
- `input/validation-report.json` for exhaustive deterministic findings.

The original proposal is immutable. Return one complete corrected proposal. Do
not return a patch, commentary, alternatives, or a `no_changes` outcome.

Correct only what the findings and frozen records support: exact pair-ledger
bookkeeping, existing record/evidence identifiers, allowed potential
relationship types and directions, comparison-surface binding, and
non-adjudicative wording. Preserve valid scientific content and abstentions.
Do not invent a connection to satisfy pair accounting. A pair may validly use
`no_useful_relationship` or `manual_review_candidate` when supported.

Never create records or evidence, inspect a PDF, use network access, decide
which source is correct, fill a scientific gap, or silently change the shard,
batch, source, or pair scope. Audit the complete returned artifact against the
diagnostics before finishing.

Return exactly one JSON object matching
`templates/corrected-proposal.schema.json`. Return JSON only.
