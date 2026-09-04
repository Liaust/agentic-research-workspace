# Reference mapping graph

This directory is the public data surface of the experiment. It separates
measured aggregate results from a fully invented executable example.

- [`baseline-v1/`](baseline-v1/) contains the frozen input identity,
  aggregate metrics, relationship-kind totals, bibliographic source catalog,
  publication report, and complete candidate manifest.
- [`synthetic-example/`](synthetic-example/) contains two invented sources,
  four schema-valid records, and one potential relationship that can be
  searched and explored with the CLI. It contains no real scientific or
  source-derived claim.
- [`publication-policy.yaml`](publication-policy.yaml) is the field and path
  allowlist used by the deterministic builder.
- [`rights-inventory.yaml`](rights-inventory.yaml) records the conservative
  rights categories behind the metadata-only decision.
- [`commands.json`](commands.json) describes the stable public commands for
  tools and agents.

The full private graph is not recoverable from these aggregates. A public
manifest proves what candidate files were admitted and their exact hashes; it
does not grant publication rights or validate a scientific conclusion.
