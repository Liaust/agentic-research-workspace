# Moving general capabilities into public development

The public repository is now the software development home. It began as a
selected export, so not every general capability used in the research
laboratory is included. This inventory makes that gap explicit.

| Area | Alpha status | Public integration gate |
|---|---|---|
| Core records, schemas, coordination and CLI | Included experimental baseline | Public regression tests and documented inputs |
| Synthetic compilation and graph inspection | Included model-free walkthrough | Reproducible source/evidence and graph checks |
| Graphical corpus viewer shown in screenshots | Not bundled | Reviewed viewer code, synthetic corpus, portable launch and accessibility checks |
| Long-form/book ingestion and checkpoint recovery | Later work not integrated | Source-general API, original fixtures, privacy audit and regression tests |
| Provisional quality restrictions and dependency exclusions | Later refinements not integrated | Versioned quality contract and independently testable restrictions |
| Self-contained installed package | Not supported yet | Bundle runtime schemas/protocols and test outside a checkout |
| Living workspace and research-pack publisher | Design only | Accepted bounded features derived from publication journeys |

## Transfer rule

Transfer reviewed software changes and original tests, never an entire private
directory or private Git history. Separate code from example data and inspect
both. Reconstruct small synthetic fixtures when a test depends on private text.

Once a capability is public, develop subsequent general changes here. Private
projects should pin the public release/commit they use. Existing experiments
retain their original software identities; do not silently rewrite their
manifests or imply that an older result used a newer version.

This inventory does not grant publication rights to the research that exercised
these tools. It also does not claim unfinished features are ready to migrate.
