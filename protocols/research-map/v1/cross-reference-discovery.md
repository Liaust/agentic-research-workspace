# Cross-Reference Candidate Discovery

Compare the two supplied source-local atom catalogues and return only pairs of
existing atoms whose relationship may be useful to inspect. The catalogues are
the complete authority for this pass.

Do not choose a relationship type, adjudicate correctness, create a scientific
node, repair an extraction, fill a missing premise, or infer what either paper
must have meant. An empty candidate list is a valid result.

Every candidate must contain one atom ID from each declared source and a short
comparison surface explaining why evidence-bounded inspection may be useful.
Do not repeat an endpoint pair. Preserve the declared canonical source order.

Return exactly one JSON object matching
`templates/discovery-proposal.schema.json`. Return JSON only.
