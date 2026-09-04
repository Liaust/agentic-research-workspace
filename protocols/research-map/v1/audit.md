# Fresh source-wide audit protocol v2

Audit the complete immutable paper against the supplied cumulative WIP dossier
and coverage snapshot. This is a new ephemeral workspace: do not seek or rely
on reader scratch files, transcripts, hidden reasoning, earlier audit state, or
another source.

Perform three focused reviews. Finish each review's inspection ledger before
returning a summary outcome:

1. **Coverage review**: inspect every expected landmark against the paper and
   its recorded disposition. Challenge whether independently searchable,
   citable, comparable, or connectable material was omitted or hidden inside a
   qualification.
2. **Evidence and fidelity review**: inspect every expected evidence record and
   every fidelity-bearing equation or figure atom. Verify asset route,
   contiguous segments, transcription mode, attribution, qualification, symbol
   form, and visual fidelity directly against the PDF.
3. **Reasoning-direction review**: inspect every expected atom, move, and
   thread. Reconstruct major progressions and conclusions. Verify that thread
   order follows presentation order while move inputs/outputs describe
   scientific use, including later-presented support for an earlier result.

For each lane, echo the coordinator-supplied `expected_ids` and partition them
exactly once between `passed_ids` and `failed_ids`. Do not omit, duplicate, or
invent an ID. Aggregate booleans summarize these inspections; they do not
replace them.

Use only the schema's finding kinds. A finding describes a source-grounded gap;
it does not write replacement science or decide whether the source is correct.
Give repair authority explicitly and minimally:

- `revise_record_ids` for existing records whose representation is wrong;
- `add_for_landmark_ids` for inventoried landmarks whose records are absent;
- `reopen_scope_ids` for the bounded reading batches needed to inspect again.

A missing-landmark finding may have no existing record ID. It may name either an
existing unrepresented landmark or a new source-local landmark candidate that
the coverage review found directly in the paper; in both cases it must name one
existing batch to reopen. The finding's description and evidence ground a new
candidate until reading gives it a full disposition. Other record-specific
findings name existing records. Manual-review findings carry a reason and grant
no automatic mutation.

Return `pass` only when all expected IDs are partitioned, every failed-ID list
and finding list is empty, all deterministic checks are true, and no manual
review is required. Use `reopen` for a bounded correctable finding and
`manual_review` when source fidelity or structure cannot be resolved without a
human. Return only the schema-constrained JSON object.

Before returning, serialize the exact candidate once to
`scratch/audit-result.json` and run:

```bash
python tools/validate_audit.py scratch/audit-result.json
```

Repair every deterministic failure within this same audit turn. Do not return a
finding whose revision targets are absent from that finding's failed inspection
lanes, and do not alter the supplied inventories to make a finding pass. If a
coherent result cannot be produced, return `manual_review` with the precise
reason. The scratch candidate is validation evidence rather than a second
canonical output.

An attempt labelled `single reviewed verification` remains fresh and
independent. Inspect the revised dossier against the paper rather than trusting
the reviewed finding or correction. Echo supplied continuation metadata
exactly. A new finding on that verification stops the run; it never authorizes
another automatic correction.
