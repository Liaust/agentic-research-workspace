# Provisional aggregate baseline v1

This directory is generated from frozen private inputs by the deterministic
public-release builder. All non-prose artifacts, including the input lock, are
frozen historical outputs and must not be edited in place.

The manifest covers public commit `8e9ca654c7674ebe7f722d89321814e286089329`,
not the current software version. Verify it with
`uv run python scripts/verify-legacy-baseline.py` from a full source checkout.

## Artifacts

- [`aggregate-metrics.json`](aggregate-metrics.json) records source, record,
  edge, pair, and source-quality counts.
- [`relationship-summary.json`](relationship-summary.json) records counts by
  potential relationship kind without endpoints or source pairs.
- [`source-catalog.yaml`](source-catalog.yaml) contains the specifically
  admitted bibliographic fields and normalized rights category.
- [`publication-report.json`](publication-report.json) states admitted and
  withheld scope plus build checks.
- [`manifest.json`](manifest.json) binds every non-transient candidate file by
  path, byte length, mode, and SHA-256, binds the candidate tree digest, and
  records the exact private-repository revision from which public files were
  projected.
- [`input-lock.json`](input-lock.json) freezes private input identities and
  expected aggregate counts.

The `private_repository_commit` identifies the frozen private corpus state.
The generated `projection_repository_commit` identifies the exact clean Git
revision supplying the copied public code, schemas, protocols, and guides.

## Interpretation

The baseline is provisional and intentionally aggregate-only. Counts establish
that a particular mapping run retained particular object classes. They do not
establish scientific correctness, consensus, uniform source coverage, or a
complete map of the field. The evidence-bearing nodes, endpoints, source pairs,
rationales, scopes, and qualifications remain private.

See the repository [limitations](../../docs/limitations/README.md) and
[third-party rights statement](../../THIRD_PARTY_RIGHTS.md).
