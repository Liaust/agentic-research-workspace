"""Public development-tree checks, separate from the frozen dataset export."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from research_map.exploration import GraphExplorer
from research_map.public_release import (
    MANIFEST_PATH,
    POLICY_PATH,
    PublicReleaseValidationError,
    _scan_candidate_content,
    _validate_relative_paths,
    _verify_markdown_links,
)
from research_map.receipts import canonical_json_bytes

LEGACY_COMMIT = "8e9ca654c7674ebe7f722d89321814e286089329"
LEGACY_MANIFEST_SHA256 = "3f15ae9bd408453ff3ebb15547da50e37402ca4dba6a0a5663e1adecea311518"
DEVELOPMENT_PATHS = (
    "AGENTS.md",
    "CHANGELOG.md",
    "commands.json",
    ".github/workflows/ci.yml",
    ".github/ISSUE_TEMPLATE/*.yml",
    ".github/pull_request_template.md",
    "docs/**/*.md",
    "examples/synthetic-study/*.md",
    "scripts/verify-legacy-baseline.py",
    "tests/unit/*.py",
)
REQUIRED_PATHS = (
    "README.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "THIRD_PARTY_RIGHTS.md",
    "pyproject.toml",
    "uv.lock",
    MANIFEST_PATH.as_posix(),
    POLICY_PATH.as_posix(),
)


def _git(root: Path, *arguments: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(root), *arguments], check=True, capture_output=True
    ).stdout


def selected_paths(root: Path) -> tuple[Path, ...]:
    """Inspect the public Git surface, including new unignored files.

    Git-ignored local research is not an assertion of publication safety. A
    release additionally requires a clean tree and scans its exact source set.
    Plain exported trees are checked in full, without ignoring any file.
    """
    if (root / ".git").exists():
        if (root / ".git").is_symlink():
            raise PublicReleaseValidationError("Git metadata may not be a symbolic link")
        actual_root = Path(_git(root, "rev-parse", "--show-toplevel").decode().strip())
        if actual_root.resolve() != root.resolve():
            raise PublicReleaseValidationError("root is not the Git checkout root")
        names = _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
        return tuple(sorted({Path(name.decode()) for name in names.split(bytes((0,))) if name}))
    return tuple(
        sorted(
            path.relative_to(root)
            for path in root.rglob("*")
            if path.is_symlink() or not path.is_dir()
        )
    )


def _descriptor(root: Path, relative: Path) -> dict[str, Any]:
    path = root / relative
    # Check all ancestors before reading so symlinked directories cannot escape.
    for ancestor in (path, *path.parents):
        if ancestor == root:
            break
        if ancestor.is_symlink():
            raise PublicReleaseValidationError(f"symbolic link is not publishable: {relative}")
    if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise PublicReleaseValidationError(f"not a regular in-root file: {relative}")
    raw = path.read_bytes()
    return {
        "path": relative.as_posix(),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
        "mode": "100755" if path.stat().st_mode & 0o111 else "100644",
    }


def verify_tree(root: Path, *, require_clean: bool = False) -> dict[str, Any]:
    root = root.resolve()
    if not root.is_dir():
        raise PublicReleaseValidationError("public root must be an existing directory")
    commit = None
    if require_clean:
        if not (root / ".git").exists():
            raise PublicReleaseValidationError("clean release check requires a Git checkout")
        if _git(root, "status", "--porcelain", "--untracked-files=all").strip():
            raise PublicReleaseValidationError("release checkout has uncommitted changes")
        commit = _git(root, "rev-parse", "HEAD").decode().strip()

    paths = selected_paths(root)
    missing = sorted(set(REQUIRED_PATHS) - {path.as_posix() for path in paths})
    if missing:
        raise PublicReleaseValidationError("missing required public files: " + ", ".join(missing))
    descriptors = [_descriptor(root, path) for path in paths]
    by_path = {item["path"]: item for item in descriptors}
    if by_path[MANIFEST_PATH.as_posix()]["sha256"] != LEGACY_MANIFEST_SHA256:
        raise PublicReleaseValidationError("legacy snapshot manifest changed")
    manifest = json.loads((root / MANIFEST_PATH).read_text(encoding="utf-8"))
    # Pin legacy non-prose artifacts and approved image bytes. A future dataset
    # release needs a new namespace and a reviewed change to this contract.
    for old in manifest["files"]:
        name = old["path"]
        frozen = (name.startswith("reference_mapping_graph/") and not name.endswith(".md")) or (
            name.startswith("docs/images/") or Path(name).name == "relationships.md"
        )
        if frozen and by_path.get(name) != old:
            raise PublicReleaseValidationError(f"frozen public artifact changed: {name}")

    policy = yaml.safe_load((root / POLICY_PATH).read_text(encoding="utf-8"))
    _validate_relative_paths(paths, policy)
    patterns = (*policy["public_path_allowlist"], *DEVELOPMENT_PATHS)
    for path in paths:
        if not any(fnmatch.fnmatchcase(path.as_posix(), pattern) for pattern in patterns):
            raise PublicReleaseValidationError(f"path needs explicit publication review: {path}")

    with tempfile.TemporaryDirectory(prefix="arw-public-check-") as temporary:
        snapshot = Path(temporary).resolve()
        for relative in paths:
            destination = snapshot / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / relative, destination)
        copied = [_descriptor(snapshot, path) for path in paths]
        if any(
            (before["sha256"], before["size_bytes"]) != (after["sha256"], after["size_bytes"])
            for before, after in zip(descriptors, copied, strict=True)
        ):
            raise PublicReleaseValidationError("files changed while building the check snapshot")
        _scan_candidate_content(snapshot)
        _verify_markdown_links(snapshot)
        explorer = GraphExplorer(
            graph_path=snapshot / "reference_mapping_graph/synthetic-example/graph.json",
            schema_directory=snapshot / "schemas/research-map/v1",
        )
        if not explorer.search("indicator")["returned_count"]:
            raise PublicReleaseValidationError("synthetic search returned no results")
        if not explorer.explore("LIB-901:atom:indicator-response")["graph"]["manifest_verified"]:
            raise PublicReleaseValidationError("synthetic graph is not manifest-bound")

    if selected_paths(root) != paths or [_descriptor(root, path) for path in paths] != descriptors:
        raise PublicReleaseValidationError("public tree changed during verification")
    if require_clean and (
        _git(root, "status", "--porcelain", "--untracked-files=all").strip()
        or _git(root, "rev-parse", "HEAD").decode().strip() != commit
    ):
        raise PublicReleaseValidationError("release checkout changed during verification")
    return {
        "schema_version": "1.0",
        "kind": "public_software_tree",
        "ok": True,
        "repository_commit": commit,
        "legacy_snapshot_commit": LEGACY_COMMIT,
        "file_count": len(descriptors),
        "tree_sha256": hashlib.sha256(canonical_json_bytes(descriptors)).hexdigest(),
        "checks": [
            "explicit_path_policy",
            "frozen_public_artifacts",
            "credential_and_content_scan",
            "local_markdown_links",
            "synthetic_search_and_exploration",
            "stable_source_bytes",
        ],
        "files": descriptors,
        "limitations": [
            "Does not certify scientific correctness or third-party redistribution rights.",
            "Does not compare new text against withheld private corpora.",
            "Git-ignored local files and Git history are outside this tree attestation.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--require-clean", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--write-manifest",
        type=Path,
        help="Write a new JSON manifest directly under the checkout's ignored dist directory",
    )
    arguments = parser.parse_args()
    if arguments.write_manifest and not arguments.require_clean:
        parser.error("--write-manifest requires --require-clean")
    try:
        result = verify_tree(arguments.root, require_clean=arguments.require_clean)
        if arguments.write_manifest:
            destination = arguments.write_manifest.absolute()
            expected_parent = arguments.root.resolve() / "dist"
            if destination.parent != expected_parent or destination.suffix != ".json":
                raise ValueError("manifest must be a new JSON file directly under the root's dist")
            if expected_parent.is_symlink() or destination.is_symlink():
                raise ValueError("manifest output must not be a symbolic link")
            ignored = subprocess.run(
                ["git", "-C", str(arguments.root), "check-ignore", "-q", str(destination)],
                check=False,
                capture_output=True,
            )
            if ignored.returncode != 0:
                raise ValueError("manifest output must be Git-ignored")
            expected_parent.mkdir(exist_ok=True)
            with destination.open("xb") as handle:
                handle.write(canonical_json_bytes(result))
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        if arguments.json:
            print(json.dumps({"ok": False, "error": str(error)}))
        else:
            print(f"Public tree check failed: {error}")
        return 1
    if arguments.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(f"Public tree verified: {result['file_count']} files; {result['tree_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
