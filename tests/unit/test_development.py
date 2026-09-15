from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from research_map.development import selected_paths, verify_tree
from research_map.public_release import PublicReleaseValidationError

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def public_tree(tmp_path: Path) -> Path:
    root = tmp_path / "public"
    root.mkdir()
    for relative in selected_paths(ROOT):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    return root


def test_development_snapshot_is_repeatable(public_tree: Path) -> None:
    first = verify_tree(public_tree)
    assert first == verify_tree(public_tree)
    assert first["repository_commit"] is None
    assert first["file_count"] > 170


def test_safe_documentation_edit_does_not_rewrite_legacy_manifest(public_tree: Path) -> None:
    manifest = public_tree / "reference_mapping_graph/baseline-v1/manifest.json"
    before = manifest.read_bytes()
    readme = public_tree / "README.md"
    readme.write_text(readme.read_text() + "\nA public contributor clarification.\n")
    assert verify_tree(public_tree)["ok"]
    assert manifest.read_bytes() == before


@pytest.mark.parametrize("name", [".env", "unreviewed.md", "vault/notes.md", "docs/paper.pdf"])
def test_non_admitted_paths_fail(public_tree: Path, name: str) -> None:
    target = public_tree / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("Not an admitted artifact.\n")
    with pytest.raises(PublicReleaseValidationError):
        verify_tree(public_tree)


@pytest.mark.parametrize(
    "relative",
    [
        "reference_mapping_graph/baseline-v1/manifest.json",
        "reference_mapping_graph/baseline-v1/aggregate-metrics.json",
        "reference_mapping_graph/publication-policy.yaml",
        "docs/images/dogfood/corpus-overview.png",
    ],
)
def test_frozen_artifacts_cannot_be_edited(public_tree: Path, relative: str) -> None:
    with (public_tree / relative).open("ab") as handle:
        handle.write(b"changed")
    with pytest.raises(PublicReleaseValidationError, match="changed"):
        verify_tree(public_tree)


def test_credential_pattern_fails(public_tree: Path) -> None:
    target = public_tree / "docs/development/credential-canary.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("gh" + "p_" + "X" * 40)
    with pytest.raises(PublicReleaseValidationError, match="credential"):
        verify_tree(public_tree)


def test_symlink_cannot_escape_root(public_tree: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("Do not read through this link.")
    (public_tree / "docs/development/link.md").symlink_to(outside)
    with pytest.raises(PublicReleaseValidationError, match="symbolic link"):
        verify_tree(public_tree)


def test_broken_markdown_link_fails(public_tree: Path) -> None:
    (public_tree / "README.md").write_text("[Missing](missing.md)\n")
    with pytest.raises(PublicReleaseValidationError, match="broken"):
        verify_tree(public_tree)


def test_git_surface_includes_new_files_and_rejects_dirty_release(public_tree: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(public_tree), *args], check=True, capture_output=True)

    git("init")
    git("add", ".")
    git(
        "-c",
        "user.name=Synthetic Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "Synthetic public fixture",
    )
    assert verify_tree(public_tree, require_clean=True)["repository_commit"]
    (public_tree / "unreviewed.md").write_text("New untracked file.")
    assert Path("unreviewed.md") in selected_paths(public_tree)
    with pytest.raises(PublicReleaseValidationError, match="uncommitted"):
        verify_tree(public_tree, require_clean=True)
    with pytest.raises(PublicReleaseValidationError, match="publication review"):
        verify_tree(public_tree)


def test_clean_release_requires_git(public_tree: Path) -> None:
    with pytest.raises(PublicReleaseValidationError, match="Git checkout"):
        verify_tree(public_tree, require_clean=True)
