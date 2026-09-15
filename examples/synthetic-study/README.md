# A source, its records, and a compiled graph

This original invented study has six **manually authored** records: two exact
evidence spans, a result, a limitation, a reasoning move and an argument thread.
No model reads the source and no real experiment took place.

1. Read [the source](source.md): a timing pattern and its explicit causal limit.
2. Read [the dossier](dossier.md): each assertion points to its own evidence;
   the move and thread preserve how the source qualifies its result.
3. From the repository root, compile and inspect it:

   ```sh
   uv run python -m research_map.demo --output build/synthetic-study
   uv run research-map search "causation" --graph build/synthetic-study/graph.json
   uv run research-map explore --id LIB-903:atom:causal-limit --graph build/synthetic-study/graph.json --json
   ```

The compiler checks the source hash, exact text spans, schema and references,
round-trips the Markdown, and builds a six-node/nine-edge graph plus context
envelopes. `receipt.json` records hashes and the manually authored origin.
Identical reruns are read-only; changed output is never overwritten.

This demonstrates deterministic compilation and inspection, not scientific
validation, a completed research pack, or live PDF atomization. The separate
[two-source fixture](../../reference_mapping_graph/synthetic-example/README.md)
demonstrates a potential cross-source relationship and its provenance.
