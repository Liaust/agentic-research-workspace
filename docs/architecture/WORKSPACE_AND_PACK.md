# The living workspace and the published pack

The long-term goal is a research workspace for humans and agents, with
literature mapping as its first implemented surface. Preserve research structure
as it is created, instead of reconstructing every detail from a final PDF.

## Three responsibilities

| Responsibility | What belongs there | Current status |
|---|---|---|
| Living workspace | Evolving questions, notes, code, data, derivations, experiments, decisions and qualified context | Design stage |
| Publication compiler and gate | Select, pin, check, redact, record omissions and establish release scope | Design stage |
| Published research pack | An immutable, versioned representation for reading, inspection, reproduction and reuse | Design stage |

The workspace can retain noisy, private, unsuccessful and provisional material.
The pack may combine several working records into a clear published answer, or
explicitly say that an answer is unknown, withheld or unavailable. These layers
must not collapse into a single automatically public database.

## Humans and agents share the underlying publication

Humans may enter through a narrative, graph, derivation explorer or experiment
walkthrough. Agents may enter through a manifest, stable IDs, typed records,
dependency routes and explicit queries. The views differ; their canonical
objects, release identity and provenance must agree. A PDF can remain one
narrative projection rather than the authoritative container.

The [question catalogue](RESEARCH_PACK_QUESTIONS.md) is the design target. We
work backward from those answers to the records and capture events the living
workspace needs. This is a direction, not a finished pack schema or standard.

## One project-wide coordinator

The intended operating model has one logically continuous coordinator with
access to the authorized project. It retrieves relevant context, plans work,
creates focused tasks or subagents, validates their returns and integrates them
into durable state. It should not depend on one uninterrupted chat session.

Focused workers have explicit assignments, context, capabilities, outputs and
stop conditions. Their scope does not fragment the coordinator's project-wide
view. Access is not authority: scientific acceptance, destructive operations,
external actions and publication remain separately governed.

This coordinator is a future workspace capability. The current literature-map
CLI uses deterministic code to coordinate bounded semantic jobs; it is not
already the complete research-workspace agent.
