# Synthetic graph example

This example contains two invented sources (`LIB-901` and `LIB-902`). One toy
source records a fictional indicator response; the other records a fictional
delay model. A potential-support edge demonstrates how the mapper preserves
endpoint identity, evidence references, direction, qualification, and
provenance without making a scientific claim.

The builder generates and fingerprints:

- [`graph.json`](graph.json), containing two evidence records, two atom
  records, two source-local support edges, and one cross-source edge;
- [`relationships.md`](relationships/LIB-901--LIB-902/relationships.md), the
  canonical relationship detail; and
- [`manifest.json`](manifest.json), which binds the graph and relationship
  input before exploration loads them.

Run the real CLI against the example:

```sh
uv run research-map search "indicator" \
  --graph reference_mapping_graph/synthetic-example/graph.json \
  --json

uv run research-map explore \
  --id LIB-901:atom:indicator-response \
  --graph reference_mapping_graph/synthetic-example/graph.json \
  --json
```

Every sentence and identifier in this example was created for the fixture. It
does not paraphrase a paper, assert an experimental result, or supply evidence
about superdeterminism.
