# Public software and research publication

This is the **public software development repository**. General code, schemas,
protocols, tests and technical documentation evolve here. Researchers' source
collections and working projects do not become public as a consequence.

## Two different kinds of release

| Release | What it represents | Verification |
|---|---|---|
| Software version | A reviewed public Git commit with code, docs and approved examples | Public-tree checks, tests, CI and a software manifest |
| Historical aggregate baseline | A frozen export of bibliographic facts, qualified counts and an invented graph | Original policy, input lock and exact whole-tree manifest |

The old baseline lives under `reference_mapping_graph/`. Its data, rights
inventory, original command catalogue and manifest retain their original
meaning. The manifest covers the historical public commit
`8e9ca654c7674ebe7f722d89321814e286089329`, not this repository's evolving HEAD.

## Current development checks

```sh
uv run python -m research_map.development --root .
```

The checker selects tracked plus unignored untracked files, refuses unsafe
paths and symlinks, checks required files, pins the legacy data and image bytes,
reuses the content/credential scanner, checks Markdown links and exercises the
manifest-bound synthetic graph. It checks a copied snapshot and detects source
changes during verification.

`--require-clean` additionally requires a clean Git checkout and binds its
commit. `--json` emits the file inventory and deterministic software-tree hash
for agents and release attachments. Ignored local files and Git history are
outside this tree attestation.

**These checks are not a proof of scientific truth, source-reading fidelity,
complete secret detection or redistribution rights.** A public-only scan cannot
compare newly added prose against a withheld corpus. Human review of the
changed public content and ancestry remains required.

## Preserve the old export

```sh
uv run python scripts/verify-legacy-baseline.py
```

This archives the pinned public Git commit into temporary storage and checks it
with the unchanged legacy verifier, without accessing private research inputs.
It requires full public Git history.

The original `scripts/verify-public-release.sh` remains available for complete
legacy-format candidates. It is expected to reject an evolved development tree
against the old whole-tree manifest. Do not weaken it or silently regenerate
the old manifest to make a routine code change pass.

## Admission and withholding

- Original software and technical documentation: Apache-2.0.
- New examples: original, clearly synthetic and explicitly reviewed.
- Historical baseline: approved bibliographic metadata and aggregate counts only.
- Screenshots: four fixed, owner-approved interface illustrations. The two with
  a visible source page show the registered CC-BY 4.0 source described in
  [THIRD_PARTY_RIGHTS.md](../../THIRD_PARTY_RIGHTS.md); no PDF or machine-readable
  private graph is included.
- Withheld: private evidence, source-derived statements and relationship
  rationales, source files, working graphs, thesis context, run state,
  operational records, credentials and private Git history.

Path admission alone never establishes content rights. A future dataset needs
a dedicated rights assessment, scope, license and immutable release namespace.
A future research-pack publication is an explicit curation/verification step,
not a raw workspace export.

## Authority

Reviewed code commits, software releases, dataset releases, website deployments
and announcements are distinct actions. An automated check does not authorize
any of them. Maintainers use the [software release checklist](../development/RELEASING.md)
and obtain the relevant project-owner authority.
