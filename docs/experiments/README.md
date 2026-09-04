# Experiments

The repository treats language-model work as a bounded experiment whose output
must pass deterministic validation before it becomes durable research-map
state. Prompts, schemas, frozen inputs, model identity, and evaluation results
are separate artifacts.

The published code includes protocols for orientation, source reading, audit,
cross-source discovery, inspection, recovery, and calibration. Those protocols
do not make their outputs authoritative merely because a model produced them.

## Current evidence

The first retained private baseline demonstrates that the pipeline can compile
a 52-source graph and retain 644 potential cross-source relationships. Its
aggregate counts are published in the [baseline directory](../../reference_mapping_graph/baseline-v1/README.md).
The uneven source-quality labels are part of the result, not an omitted detail.

## Reproducible public experiment

The [synthetic example](../../reference_mapping_graph/synthetic-example/README.md)
exercises the same schemas, relationship Markdown, graph manifest, search, and
exploration code while making no claim about real literature. It is the safe
starting point for testing new interfaces.

Future experiments should declare a hypothesis, frozen input set, acceptance
metric, failure interpretation, and whether the result changes production
policy. Negative results and unresolved cases should remain visible.
