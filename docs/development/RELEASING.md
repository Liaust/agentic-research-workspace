# Experimental software releases

The first software prerelease is `v0.1.0-alpha.1` (Python metadata `0.1.0a1`).
This is a GitHub source-checkout release, not a PyPI release or a research-data
publication. Alpha versions may change CLI and record contracts.

## Maintainer checklist

1. Review a focused public-history branch. Keep frozen baseline data and approved
   screenshot bytes unchanged. No private repository ancestry may enter a push.
2. Align package metadata, CLI version, citation metadata, changelog and release
   notes. Describe supported paths, limitations and migration implications.
3. Run every command in CONTRIBUTING.md. Replay the quick start from a clean
   clone and inspect its output. All tests must run without private corpora or
   model credentials. Inspect source/package archives for unintended files.
4. Commit and push the reviewed source, then require green CI for that exact
   commit before tagging it. Do not bypass a failing gate or move an existing tag.
5. Generate a software manifest from the clean source tree using:

   ```sh
   uv run python -m research_map.development --root . --require-clean --json
   ```

   Preserve the JSON output as a release attachment. Its `repository_commit`
   must equal the release tag's commit. It lists file hashes and a deterministic
   tree hash; it is not the older aggregate-data manifest.
6. Create an annotated tag and a GitHub **prerelease** with the curated notes.
   Attach the software manifest. GitHub's source archives provide the supported
   source distribution. Do not attach a wheel as a supported standalone install
   until schema/protocol resource discovery passes installed-package tests.
7. Verify the remote tag, prerelease flag, release notes and downloaded manifest
   against the accepted source. Never infer publication success from a local tag.

## Historical baseline

```sh
uv run python scripts/verify-legacy-baseline.py
```

This needs the public repository's full Git history and verifies the pinned
September snapshot independently. The original `scripts/verify-public-release.sh`
still checks a complete legacy-format candidate; it intentionally does not
accept a changed development tree as that old release.

## Scope limits

Publishing code does not authorize a new dataset, scientific claim, private
research-pack export, website deployment or external announcement. Each needs
its own explicit scope and review. Avoid automated publication on every push.
