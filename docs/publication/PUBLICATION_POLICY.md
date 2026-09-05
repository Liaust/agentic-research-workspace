# Public projection policy

This repository is the private, evidence-bearing laboratory. A public release is
a new tree produced by `research-map build-public-release`; it is not this
working tree with files removed, and it never inherits this repository's Git
history.

## Accepted boundary

- Topology: one separate clean-history public projection.
- Public data: aggregate corpus results and a synthetic demonstration only;
  four explicitly allowlisted screenshots document the private dogfood
  interface without releasing its underlying data.
- Original code and documentation: Apache-2.0.
- Third-party sources and source-derived datasets: no license is granted.
- External actions: repository creation, visibility changes, pushes, releases,
  and announcements require separate authorization.

The projection is deterministic selection, validation, aggregation, and
packaging. It does not invoke a model or paraphrase scientific claims.
Generated provenance distinguishes the frozen private corpus commit from the
exact clean repository commit that supplied copied public files. A build fails
if that projection revision or its working tree changes during construction.

## Field policy

| Input class | Field or content | Disposition | Reason |
|---|---|---|---|
| source registration | `library_id` | admit | Stable local catalogue identity. |
| source registration | `identity.title`, `identity.creators`, `identity.year`, `identity.type`, `identity.language` | admit | Bibliographic facts only. |
| source registration | normalized rights category | admit | Makes withholding explicit without copying acquisition notes. |
| source registration | asset path, original path, acquisition source, raw rights notes, notes, structure, extraction, coverage, limitations, unresolved questions | withhold | Private paths, operational notes, or source-derived prose. Only the normalized rights category is admitted. |
| corpus graph | total sources, record types, source-local edges, cross-source relationships, inspected/unresolved pair counts | admit | Aggregate measurements bound to an exact private graph hash. |
| corpus graph | aggregate relationship-kind counts | admit | Aggregate experimental result; no endpoints or topology. |
| corpus graph | nodes, endpoints, statements, evidence spans, rationales, scopes, qualifications, source pairs | withhold | Evidence-bearing or topology-bearing derived dataset. |
| project tree | paths named by the publication allowlist | admit after scanning | Original code, schemas, protocols, tests, and public documentation. |
| documentation screenshots | four named PNG files under `docs/images/dogfood/` | admit after scanning | Project-owner-supplied interface illustrations; the two screenshots containing a source page show only registered CC-BY 4.0 source `LIB-001`. No underlying graph or source asset is copied. |
| project tree | `.project`, `.basecamp`, `sources`, `vault`, caches, build output, databases, PDFs, credentials, private run state | deny | Private control plane, source data, generated evidence, or secrets. |
| demonstration | `DEMO-*` synthetic graph and CLI workflows | admit | Demonstrates behavior without source-derived text. |

Fields and paths not explicitly admitted are denied. The build fails if an
input hash is stale, the destination is absent from the caller's explicit
arguments or is non-empty, a selected path escapes the repository, a selected
path is not allowlisted, or the candidate contains a denylisted path/content
class.

## Frozen private inputs

`reference_mapping_graph/baseline-v1/input-lock.json` binds the integration
commit, source-registration/asset identity set, salvage graph, graph manifest,
report, counts, and external-action gate. The lock contains no machine-local
path. The build accepts the corresponding private paths explicitly and verifies
their bytes before writing the candidate.

The source integrity digest is SHA-256 over compact, key-sorted JSON containing,
in ascending `source_id` order, each source ID, source registration SHA-256,
and each registered asset's role, SHA-256, and byte size in registration order.
The private asset bytes are independently verified against those registrations.

## Candidate verification

The generated manifest records every public file by relative path, SHA-256, and
size. The publication report records admitted and withheld field classes and
the safety checks performed. `scripts/verify-public-release.sh` validates the
candidate without private context. Allowlisted files and their executable modes
are read from the exact Git tree named by `projection_repository_commit`, never
from mutable working-tree bytes. The build additionally rejects tracked `.env*`
files, malformed bibliographic values, raw rights-note prose, private work
identifiers, multiline credentials, and exact or source-derived text—including
meaningful partial excerpts and supported reversible encodings—from the private
graph, manifest-bound semantic inputs, source registrations, and canonical
vault records. It fingerprints the vault records before and after projection.

Slice 4 must place the output in a fresh one-commit local repository and inspect
both its tree and Git objects. Passing local verification is not permission to
publish.
