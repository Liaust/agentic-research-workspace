# Outcome roadmap

Agentic Research Workspace is being built in public. This roadmap separates
working software from design targets; it is not a schedule or a promise that
every idea will be implemented. No delivery dates are assigned.

## 1. Experimental public baseline

**First alpha:** a researcher or agent can clone the repository, run a
model-free example, inspect evidence-linked records, search a graph and
contribute without access to a private research project.

- Original synthetic source → authored dossier → validated graph walkthrough.
- Existing two-source relationship fixture and frozen, qualified aggregate results.
- Public-only tests and CI, explicit command discovery and contribution guidance.
- Separate public-development checks from immutable historical export checks.
- Honest release notes and supported source-checkout installation.

This does not deliver the graphical viewer shown in the case-study screenshots,
a self-contained installed package, or the later long-form ingestion work.

## 2. Research-pack reference design — next design milestone

**Outcome:** one small publishable example answers the
[research-pack questions](../architecture/RESEARCH_PACK_QUESTIONS.md) through
both human-facing and agent-facing routes.

Design the published output first: claims, evidence, derivations, calculations,
code, data, experiments, history, verification, rights and declared omissions.
Distinguish unknown, not performed, withheld and unavailable answers.

Acceptance should show a reader following a claim to its support, an agent
resolving the same objects, and a reproducer finding the exact inputs and code
for a result. Choose the minimum coherent contract from those journeys—not
from a premature IDE layout. Serialization, interfaces and conformance are
still design questions.

## 3. Living-workspace foundation — later

**Outcome:** research can accumulate without losing how an idea, result or
artifact came to exist.

Work backward from the reference pack to capture notes, questions, derivations,
experiments, code, data, decisions, corrections and provenance. A mutable,
private-by-default workspace and a curated immutable pack are different
products with an explicit publication boundary.

Acceptance should demonstrate change history, stable identity, source-derived
versus researcher-authored origins, and intentional selection into a pack.
Capturing something must never automatically publish it.

## 4. Coordinator-led workflows — later

**Outcome:** one project-wide coordinator can understand the authorized project,
delegate focused tasks and integrate inspectable results.

Whole-project access means discoverable context, not one enormous prompt.
Workers need explicit tasks and return contracts; durable records must preserve
continuity across sessions. Scientific acceptance, external actions and
publication remain separately governed.

Acceptance should include restart recovery, conflicting returns, permission
boundaries and a workflow spanning literature, a derivation and code.

## Literature-mapping improvements alongside the design work

- Ship a public viewer with original synthetic data and test human/agent navigation.
- Improve source-reading fidelity and equation/figure treatment with rights-safe benchmarks.
- Make quality states, review findings and missing coverage clearer.
- Bring later source-general ingestion and recovery improvements into public
  history as reviewed increments.
- Make installed-package resources self-contained and test supported platforms.
- Expand regression coverage beyond publication checks and synthetic navigation.

See the [migration inventory](MIGRATION.md). A listed improvement is not a claim
that it is shipped. Concrete work belongs in focused
[GitHub issues](https://github.com/Liaust/agentic-research-workspace/issues);
this document remains the outcome map.

## Tracked next work

- [Reference research-pack design](https://github.com/Liaust/agentic-research-workspace/issues/1)
  — [design milestone](https://github.com/Liaust/agentic-research-workspace/milestone/1).
- [Public synthetic graph viewer](https://github.com/Liaust/agentic-research-workspace/issues/2)
  and [self-contained package resources](https://github.com/Liaust/agentic-research-workspace/issues/3)
  — [mapper usability milestone](https://github.com/Liaust/agentic-research-workspace/milestone/2).
- [Reviewed long-form ingestion/recovery migration](https://github.com/Liaust/agentic-research-workspace/issues/4).

These issues define scope and acceptance, not permission to publish private
research or automatically start a corpus run.
