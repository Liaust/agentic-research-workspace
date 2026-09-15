# Questions a published research pack should answer

This is a design target, not an implemented API or schema. These questions
describe the published output, not everything a private living workspace should
capture. Human and agent interfaces should reach the same release-bound answers.

| ID | Publication question |
|---|---|
| RPQ-01 | What exactly is this research pack? |
| RPQ-02 | What problem or question does the research address? |
| RPQ-03 | What is new or contributed? |
| RPQ-04 | What does the work claim or conclude? |
| RPQ-05 | Why should a particular claim be believed, qualified, or doubted? |
| RPQ-06 | How was a result derived or reasoned through? |
| RPQ-07 | How does this work relate to the existing literature? |
| RPQ-08 | What method or design was used, and why? |
| RPQ-09 | What data or inputs were used? |
| RPQ-10 | What code or computation produced an output? |
| RPQ-11 | What happened in an experiment or observation? |
| RPQ-12 | What exactly are the results and figures showing? |
| RPQ-13 | What was checked, and what was not? |
| RPQ-14 | What are the limitations and plausible ways the work could be wrong? |
| RPQ-15 | What was done, when, how, and why did the work change? |
| RPQ-16 | How has the published pack changed across releases? |
| RPQ-17 | Who may access, cite, reuse, or redistribute each part? |
| RPQ-18 | What is absent, and how complete is the pack relative to its declared scope? |
| RPQ-19 | How can someone cite, reproduce, reuse, or extend the work? |
| RPQ-20 | How can an agent discover and query the pack safely and efficiently? |

## Two interfaces, one answer

| Journey | Human-facing route | Agent-facing route |
|---|---|---|
| Inspect a claim | Read its scope and follow evidence/argument links | Resolve claim ID, qualifications, evidence IDs and provenance |
| Follow a derivation | Navigate intermediate steps with definitions and assumptions | Traverse typed dependencies, transformations and artifact versions |
| Reproduce a figure | Open its data, calculation and reproducibility instructions | Resolve exact input hashes, code version, environment and producing run |
| Understand an experiment | Explore apparatus, protocol, observations and review history | Retrieve protocol, configuration, instrument/artifact references and events |
| Check what is missing | Read declared limits, omitted material and review gaps | Query explicit answer states and policy-qualified omissions |

An answer should identify its release, scope, supporting objects, origin,
verification posture and rights. Distinguish **answered**, **partially answered**,
**not applicable**, **not performed**, **unknown**, **withheld** and
**unavailable**. A missing value must not conceal which of these applies.

The next design milestone is one small reference pack that makes these routes
coherent. Only then should capture workflows and concrete serialization be
derived. See [workspace and pack](WORKSPACE_AND_PACK.md) and the
[roadmap](../public-roadmap/README.md).
