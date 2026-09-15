# Limitations

## Alpha support boundary

- Run from a source checkout. Some runtime schemas and protocols are discovered
  relative to the repository; a standalone installed package is not yet supported.
- The walkthrough starts from manually authored synthetic records. It validates
  and compiles them; it does not demonstrate automatic reading accuracy.
- The graphical viewer in the screenshots is not bundled in the alpha.
- Real-corpus/model workflows require operator configuration and source review;
  the public test suite does not certify them on new scientific domains.
- Later long-form ingestion, checkpoint recovery and quality refinements have
  not all been integrated into this public baseline. See the
  [migration inventory](../public-roadmap/MIGRATION.md).
- The living workspace, project-wide agent and research-pack publisher remain
  design-stage capabilities, not implemented features.

## Baseline limitations

- The public release contains aggregates, not the evidence-bearing graph, so
  readers cannot independently evaluate its source-level scientific content.
- The retained graph covers 52 of 53 registered sources. One source pair
  remains unresolved.
- Audit depth is uneven: 35 sources are unaudited and provisional, 11 were
  audited with findings, five passed audit, and one audit was interrupted.
- Record and relationship counts measure retained output, not correctness,
  coverage, importance, or scientific agreement.
- Potential relationships are mapper proposals. In particular, “potential
  contradiction” is not an adjudication that either endpoint is false.

## System limitations

- Lexical search ranks existing fields; it is not semantic retrieval and does
  not synthesize an answer.
- One-hop exploration shows bounded existing context and may omit relevant
  objects beyond the limit or outside the selected graph.
- Schema validity does not guarantee faithful interpretation of a source.
- Language-model reading can miss qualifications, equations, figures, and
  argument structure. Source-wide audit reduces but does not eliminate this
  risk.
- Rights status is incomplete for most registered assets, which is why the
  full dataset remains private.

## Safe interpretation

Use the software to inspect provenance and disagreement, not to substitute for
reading sources or conducting scientific evaluation. Treat every result in the
context of its source, scope, evidence, quality label, and recorded
qualifications.
