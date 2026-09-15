# Synthetic pulse study — authored record example

Six manually authored records preserve the invented source's observation and
its own limitation. This is a fixture, not the output of a live extraction.

## Observation evidence

```research-map
schema_version: 1
record_type: evidence
id: LIB-903:evidence:timing
revision: 1
source_id: LIB-903
asset_sha256: c671ee17530d8bc5992cffb9defaf3147a6927940391505777d7fda4feb65f66
page_start: 1
page_end: 1
locator: "Synthetic single-page source, observation paragraph"
exact_span: "Three fictional trials logged a blue indicator one step after each pulse."
```

## Limitation evidence

```research-map
schema_version: 1
record_type: evidence
id: LIB-903:evidence:control-limit
revision: 1
source_id: LIB-903
asset_sha256: c671ee17530d8bc5992cffb9defaf3147a6927940391505777d7fda4feb65f66
page_start: 1
page_end: 1
locator: "Synthetic single-page source, limitation paragraph"
exact_span: "This timing pattern does not establish causation because no control condition was tested."
```

## The observation is retained without overstating it

```research-map
schema_version: 1
record_type: atom
id: LIB-903:atom:timing-pattern
revision: 1
source_id: LIB-903
kind: result
label: Fictional pulse-response timing
statement: Three invented trials place an indicator response one step after a pulse.
evidence_ids: [LIB-903:evidence:timing]
scope: A fictional toy example, not empirical research.
attribution: Synthetic pulse study
epistemic_posture: Invented observation supplied to demonstrate record structure.
qualifications: [No real trial was performed.]
connectable: true
standalone_context: The source describes a fictional indicator and pulse.
type_gap: null
fidelity: null
```

## The source's limitation remains a separate object

```research-map
schema_version: 1
record_type: atom
id: LIB-903:atom:causal-limit
revision: 1
source_id: LIB-903
kind: limitation
label: Timing is not a causal conclusion
statement: The invented timing pattern alone does not establish causation without a control condition.
evidence_ids: [LIB-903:evidence:control-limit]
scope: The interpretation of the fictional pulse-response timing pattern.
attribution: Synthetic pulse study
epistemic_posture: Explicit limitation in the invented source.
qualifications: [This is not a claim about a real experiment.]
connectable: true
standalone_context: The fictional description contains no tested control condition.
type_gap: null
fidelity: null
```

## A move records how the source qualifies its result

```research-map
schema_version: 1
record_type: move
id: LIB-903:move:qualify-timing
revision: 1
source_id: LIB-903
role: qualifies
label: Qualify the timing interpretation
summary: The source limits what can be inferred from its invented timing pattern.
inputs: [LIB-903:atom:timing-pattern]
outputs: [LIB-903:atom:causal-limit]
evidence_ids: [LIB-903:evidence:timing, LIB-903:evidence:control-limit]
thread_id: LIB-903:thread:result-and-limitation
type_gap: null
```

## A thread preserves the argument's local structure

```research-map
schema_version: 1
record_type: thread
id: LIB-903:thread:result-and-limitation
revision: 1
source_id: LIB-903
kind: argument
title: An invented result and its interpretive limit
summary: The source states a timing pattern and explicitly limits its interpretation.
move_ids: [LIB-903:move:qualify-timing]
entry_points: [LIB-903:atom:timing-pattern]
branches: []
evidence_ids: [LIB-903:evidence:timing, LIB-903:evidence:control-limit]
type_gap: null
```
