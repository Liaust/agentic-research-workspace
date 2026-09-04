# Source-local research-map worker authority

This workspace contains one immutable registered source. Work only from files
enumerated in `manifest.json`; never access other sources, the parent repository,
the network, or user configuration.

Read the complete assigned paper and the stage protocol named in the prompt.
Use `scratch/` for disposable extraction, page renders, crops, notes, or other
inspection aids. Use `output/` for durable proposals and prior WIP. Never modify
anything under `input/`, `templates/`, `AGENTS.md`, `task.json`, or
`manifest.json`.

The mapper is descriptive. Attribute claims, assumptions, equations, objections,
responses, and conclusions to the source. Do not decide scientific truth, repair
an argument, invent a missing step, or silently strengthen the source. Preserve
qualifications and distinguish the source's assertions from your analysis.

Return only the JSON object requested by the stage prompt and output schema.
Every source-derived semantic record needs exact evidence tied to the registered
asset hash and PDF page. Preserve reasoning as ordered, branching, or converging
moves and threads rather than a bag of atoms. Unknown source-native kinds remain
valid only with an explicit type-gap rationale.
