from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import unicodedata
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]

import research_map.public_release as public_release_module
from research_map.cli import _human_output, build_parser, dispatch
from research_map.cross_reference import Relationship
from research_map.exploration import GraphExplorer
from research_map.ids import cross_reference_relationship_id
from research_map.markdown import load_markdown, render_markdown
from research_map.public_release import (
    MANIFEST_PATH,
    METRICS_PATH,
    REPORT_PATH,
    SOURCE_CATALOG_PATH,
    SYNTHETIC_GRAPH_PATH,
    PublicReleaseBuilder,
    PublicReleaseValidationError,
    _collect_private_source_state,
    verify_public_release,
)
from research_map.receipts import fingerprint
from research_map.relationship_markdown import render_relationship_markdown

PRIVATE_EVIDENCE = (
    "The private synthetic input states a deliberately distinctive observation "
    "that must never enter the candidate."
)
VAULT_PRIVATE_EVIDENCE = (
    "A canonical vault span absent from the compiled private graph must remain withheld."
)
VAULT_PRIVATE_RELATIONSHIP = (
    "A canonical relationship rationale absent from the locked graph must remain withheld."
)
FIXTURE_PAIR_DIRECTORY = "-".join(("LIB-991", "", "LIB-992"))
FIXTURE_ACCOUNT_ID = "".join(("731", "9428"))
FIXTURE_PROJECT_ID = "".join(("864", "20951"))
FIXTURE_TASK_ID = f"{0xDECAFBAD:08x}-{0x1234:04x}-{0x4ABC:04x}-{0x8DEF:04x}-{0x0123456789AB:012x}"
FIXTURE_PRIVATE_REPOSITORY = "/".join(("fixture-private-owner", "research-lab"))
NUL_BYTE = bytes((0,))
NON_UTF8_BYTE = bytes((255,))
PDF_PREFIX = bytes((37, 80, 68, 70))
ZIP_PREFIX = bytes((80, 75, 3, 4))


@dataclass(frozen=True, slots=True)
class ReleaseFixture:
    repository_root: Path
    registration_root: Path
    asset_root: Path
    graph_path: Path
    graph_manifest_path: Path
    report_path: Path
    policy_path: Path
    input_lock_path: Path

    def builder(self) -> PublicReleaseBuilder:
        return PublicReleaseBuilder(
            repository_root=self.repository_root,
            source_registration_root=self.registration_root,
            asset_root=self.asset_root,
            graph_path=self.graph_path,
            graph_manifest_path=self.graph_manifest_path,
            private_report_path=self.report_path,
            policy_path=self.policy_path,
            input_lock_path=self.input_lock_path,
        )

    def cli_arguments(self, output: Path) -> list[str]:
        return [
            "build-public-release",
            "--repository-root",
            str(self.repository_root),
            "--source-registration-root",
            str(self.registration_root),
            "--asset-root",
            str(self.asset_root),
            "--graph",
            str(self.graph_path),
            "--graph-manifest",
            str(self.graph_manifest_path),
            "--private-report",
            str(self.report_path),
            "--policy",
            str(self.policy_path),
            "--input-lock",
            str(self.input_lock_path),
            "--output",
            str(output),
        ]

    def commit(self, message: str) -> str:
        return _commit_repository(self.repository_root, message)


def test_two_builds_are_byte_identical_and_self_verifying(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    private_before = _private_fingerprints(fixture)

    first = fixture.builder().build(tmp_path / "candidate-a")
    second = fixture.builder().build(tmp_path / "candidate-b")

    assert first.tree_sha256 == second.tree_sha256
    assert _tree_bytes(first.candidate_root) == _tree_bytes(second.candidate_root)
    assert verify_public_release(first.candidate_root).tree_sha256 == first.tree_sha256
    assert _private_fingerprints(fixture) == private_before
    manifest = _read_json(first.candidate_root / MANIFEST_PATH)
    projection_commit = subprocess.run(
        ("git", "-C", str(fixture.repository_root), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    private_inputs = manifest["private_inputs"]
    assert isinstance(private_inputs, dict)
    assert private_inputs["projection_repository_commit"] == projection_commit
    assert manifest["public_scope"] == {
        "data": "aggregate_results_plus_synthetic_demonstration",
        "dataset_license": "none_granted",
        "external_actions_authorized": False,
        "license": "Apache-2.0",
        "third_party_rights": "separate",
    }
    metrics = _read_json(first.candidate_root / METRICS_PATH)
    counts = metrics["counts"]
    assert isinstance(counts, dict)
    assert counts["records"] == 4
    assert counts["cross_source_relationships"] == 1
    catalog = yaml.safe_load((first.candidate_root / SOURCE_CATALOG_PATH).read_text())
    assert [source["source_id"] for source in catalog["sources"]] == list(("LIB-991", "LIB-992"))
    assert all(
        set(source)
        == {"source_id", "title", "creators", "year", "type", "language", "rights_category"}
        for source in catalog["sources"]
    )
    candidate_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in first.candidate_root.rglob("*")
        if path.is_file()
    )
    assert PRIVATE_EVIDENCE not in candidate_text
    explorer = GraphExplorer(
        graph_path=first.candidate_root / SYNTHETIC_GRAPH_PATH,
        schema_directory=first.candidate_root / "schemas/research-map/v1",
    )
    search = explorer.search("indicator")
    explored = explorer.explore("LIB-901:atom:indicator-response")
    assert search["returned_count"] >= 1
    assert explored["graph"]["manifest_verified"] is True
    assert len(explored["context"]["cross_source_relationships"]) == 1


def test_fixture_commits_ignore_environment_dates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commits: list[str] = []
    for index, date in enumerate(("2026-02-01T00:00:00Z", "2026-03-01T00:00:00Z")):
        monkeypatch.setenv("GIT_AUTHOR_DATE", date)
        monkeypatch.setenv("GIT_COMMITTER_DATE", date)
        fixture = _release_fixture(tmp_path / str(index))
        (fixture.repository_root / "README.md").write_text("# Identical synthetic revision\n")
        commits.append(fixture.commit("identical synthetic revision"))
    assert commits[0] == commits[1]


def test_build_binds_copied_files_to_exact_projection_revision(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    first = fixture.builder().build(tmp_path / "candidate-first")
    first_manifest = _read_json(first.candidate_root / MANIFEST_PATH)
    first_private_inputs = first_manifest["private_inputs"]
    assert isinstance(first_private_inputs, dict)
    first_commit = first_private_inputs["projection_repository_commit"]

    readme = fixture.repository_root / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8") + "\nA safe public revision marker.\n",
        encoding="utf-8",
    )
    second_commit = fixture.commit("change a projected public file")
    second = fixture.builder().build(tmp_path / "candidate-second")
    second_manifest = _read_json(second.candidate_root / MANIFEST_PATH)
    second_private_inputs = second_manifest["private_inputs"]
    assert isinstance(second_private_inputs, dict)

    assert first_commit != second_commit
    assert second_private_inputs["projection_repository_commit"] == second_commit
    assert first.tree_sha256 != second.tree_sha256


def test_build_reads_projected_bytes_from_recorded_git_tree(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    readme = fixture.repository_root / "README.md"
    committed = readme.read_bytes()
    subprocess.run(
        (
            "git",
            "-C",
            str(fixture.repository_root),
            "update-index",
            "--assume-unchanged",
            "README.md",
        ),
        check=True,
    )
    readme.write_text("# Hidden mutable worktree content\n", encoding="utf-8")
    assert not subprocess.run(
        ("git", "-C", str(fixture.repository_root), "status", "--porcelain"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    result = fixture.builder().build(tmp_path / "candidate")

    assert (result.candidate_root / "README.md").read_bytes() == committed


@pytest.mark.parametrize("index_flag", ["--assume-unchanged", "--skip-worktree"])
def test_build_rejects_hidden_private_worktree_bytes(
    tmp_path: Path,
    index_flag: str,
) -> None:
    fixture = _release_fixture(tmp_path)
    relative = Path("vault/sources/LIB-991/paper.md")
    vault_record = fixture.repository_root / relative
    subprocess.run(
        (
            "git",
            "-C",
            str(fixture.repository_root),
            "update-index",
            index_flag,
            relative.as_posix(),
        ),
        check=True,
    )
    vault_record.write_text("# Hidden replacement\n", encoding="utf-8")
    assert not subprocess.run(
        ("git", "-C", str(fixture.repository_root), "status", "--porcelain"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    with pytest.raises(PublicReleaseValidationError, match="worktree bytes differ"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_reads_projected_mode_from_recorded_git_tree(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    verifier = fixture.repository_root / "scripts/verify-public-release.sh"
    subprocess.run(
        ("git", "-C", str(fixture.repository_root), "config", "core.filemode", "false"),
        check=True,
    )
    verifier.chmod(0o644)
    assert not subprocess.run(
        ("git", "-C", str(fixture.repository_root), "status", "--porcelain"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    result = fixture.builder().build(tmp_path / "candidate")

    assert result.candidate_root.joinpath("scripts/verify-public-release.sh").stat().st_mode & 0o111


@pytest.mark.parametrize(
    ("unsafe_content", "message"),
    [
        ("/" + "Users/example/private.txt", "machine-local path"),
        ("/" + "root/private/secret.txt", "machine-local path"),
        ("https://app.base" + "camp.com/123/projects/456", "private work URL"),
        ("gh" + "p_" + "a" * 36, "credential pattern"),
        ("github" + "_pat_" + "a" * 30 + "_" + "b" * 30, "credential pattern"),
        ("sk" + "-proj-" + "a" * 48 + "-" + "b" * 24, "credential pattern"),
        ("sk" + "_live_" + "a" * 32, "credential pattern"),
        ('"pass' + 'word": "correcthorsebattery"', "credential pattern"),
        ("pass" + "word: correct horse battery staple", "credential pattern"),
        ("pass" + 'word = "correct horse battery staple"', "credential pattern"),
        ("pass" + "word = p@ssword!", "credential pattern"),
        ("pass" + "word = abcd.efgh", "credential pattern"),
        ("sec" + "ret = foo:bar:baz", "credential pattern"),
        ("to" + "ken = tok$en123", "credential pattern"),
        ("SERVICE_API_" + "KEY=" + "a" * 32, "credential pattern"),
        ("service_api_" + "key=" + "a" * 32, "credential pattern"),
        ("HF_" + "TOKEN=hf_" + "a" * 32, "credential pattern"),
        ("api_" + "key: |\n  " + "a" * 32, "credential pattern"),
        ("- pass" + "word: |\n    " + "a" * 32, "credential pattern"),
        ("- api_" + "key: >\n    " + "a" * 32, "credential pattern"),
        ("to" + 'ken = """\n' + "a" * 32 + '\n"""', "credential pattern"),
        ("sec" + "ret: !!str " + "a" * 32, "credential pattern"),
        (
            "DATABASE_URL=" + "postgresql://" + "admin:supersecret@private-db.invalid/db",
            "credential pattern",
        ),
        ("C:" + "\\Users\\Leonardo\\private\\secret.txt", "machine-local path"),
        ("-----BEGIN OPENSSH " + "PRIVATE KEY-----", "private key"),
        ("-----BEGIN PGP " + "PRIVATE KEY BLOCK-----", "private key"),
        ("AGE-SECRET-" + "KEY-1" + "Q" * 40, "credential pattern"),
        ("https://github.com/" + FIXTURE_PRIVATE_REPOSITORY + ".git", "private work URL"),
        ("git@github.com:" + FIXTURE_PRIVATE_REPOSITORY + ".git", "private work URL"),
        (PRIVATE_EVIDENCE, "private graph text"),
    ],
)
def test_build_fails_closed_on_sensitive_content(
    tmp_path: Path, unsafe_content: str, message: str
) -> None:
    fixture = _release_fixture(tmp_path)
    (fixture.repository_root / "README.md").write_text(unsafe_content, encoding="utf-8")
    fixture.commit("add sensitive content fixture")
    output = tmp_path / "candidate"

    with pytest.raises(PublicReleaseValidationError, match=message):
        fixture.builder().build(output)

    assert not output.exists()


def test_build_rejects_forbidden_suffix_and_nonempty_destination(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    forbidden = fixture.repository_root / "src/research_map/leak.pdf"
    forbidden.write_bytes(PDF_PREFIX + b"-1.7\nnot public\n")
    fixture.commit("add forbidden suffix fixture")

    with pytest.raises(PublicReleaseValidationError, match="forbidden file suffix"):
        fixture.builder().build(tmp_path / "candidate-forbidden")

    forbidden.unlink()
    fixture.commit("remove forbidden suffix fixture")
    destination = tmp_path / "candidate-existing"
    destination.mkdir()
    (destination / "sentinel").write_text("keep", encoding="utf-8")
    with pytest.raises(PublicReleaseValidationError, match="must not already exist"):
        fixture.builder().build(destination)
    assert (destination / "sentinel").read_text(encoding="utf-8") == "keep"


def test_build_rejects_stale_graph_hash_without_writing(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    fixture.graph_path.write_text("{}\n", encoding="utf-8")
    output = tmp_path / "candidate"

    with pytest.raises(PublicReleaseValidationError, match="stale private input hash"):
        fixture.builder().build(output)

    assert not output.exists()


def test_build_rejects_output_inside_private_input_root(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    output = fixture.asset_root / "public-candidate"

    with pytest.raises(PublicReleaseValidationError, match="overlaps a private input root"):
        fixture.builder().build(output)

    assert not output.exists()


def test_build_rejects_transient_segment_in_allowlisted_source(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    leak = fixture.repository_root / "docs/architecture/dist/leak.txt"
    leak.parent.mkdir(parents=True)
    leak.write_text("not manifestable", encoding="utf-8")
    fixture.commit("add transient path fixture")

    with pytest.raises(PublicReleaseValidationError, match="forbidden path segment"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_case_variant_transient_segment(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    leak = fixture.repository_root / "src/research_map/.CACHE/leak.txt"
    leak.parent.mkdir(parents=True)
    leak.write_text("safe-looking cached output\n", encoding="utf-8")
    fixture.commit("add case-variant transient path fixture")

    with pytest.raises(PublicReleaseValidationError, match="forbidden path segment"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_tracked_environment_file(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    environment = fixture.repository_root / "src/research_map/.env.production"
    environment.write_text("PASS" + "WORD=hunter2\n", encoding="utf-8")
    fixture.commit("add tracked environment fixture")

    with pytest.raises(PublicReleaseValidationError, match="forbidden file name"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_canonical_vault_evidence(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    (fixture.repository_root / "README.md").write_text(VAULT_PRIVATE_EVIDENCE, encoding="utf-8")
    fixture.commit("copy canonical vault evidence fixture")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_canonical_vault_relationship_text(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    relationship_path = (
        fixture.repository_root
        / "vault/relationships"
        / FIXTURE_PAIR_DIRECTORY
        / "relationships.md"
    )
    before = fingerprint(relationship_path)
    (fixture.repository_root / "README.md").write_text(
        VAULT_PRIVATE_RELATIONSHIP,
        encoding="utf-8",
    )
    fixture.commit("copy canonical vault relationship fixture")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")

    assert fingerprint(relationship_path) == before


def test_rejects_noncanonical_vault_prose_outside_record_fences(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    record_path = fixture.repository_root / "vault/sources/LIB-991/paper.md"
    record_path.write_text(
        record_path.read_text(encoding="utf-8") + "\nPrivate prose outside the record fence.\n",
        encoding="utf-8",
    )

    with pytest.raises(PublicReleaseValidationError, match="not in canonical form"):
        public_release_module._collect_canonical_vault_state(fixture.repository_root)


@pytest.mark.parametrize(
    "short_evidence",
    ["q(A|x,lambda)=q(A|lambda)", "Bell wins.", "".join(("See F", "ig. 2"))],
)
def test_build_rejects_short_exact_evidence_span(
    tmp_path: Path,
    short_evidence: str,
) -> None:
    fixture = _release_fixture(tmp_path)
    graph = _read_json(fixture.graph_path)
    nodes = graph["nodes"]
    assert isinstance(nodes, list)
    first_node = nodes[0]
    assert isinstance(first_node, dict)
    payload = first_node["payload"]
    assert isinstance(payload, dict)
    payload["exact_span"] = short_evidence
    _write_json(fixture.graph_path, graph)
    _refresh_locked_private_descriptors(fixture)
    (fixture.repository_root / "README.md").write_text(short_evidence, encoding="utf-8")
    fixture.commit("add short evidence fixture")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_short_explicit_base64_evidence(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    short_evidence = "E=mc²"
    graph = _read_json(fixture.graph_path)
    nodes = graph["nodes"]
    assert isinstance(nodes, list)
    first_node = nodes[0]
    assert isinstance(first_node, dict)
    payload = first_node["payload"]
    assert isinstance(payload, dict)
    payload["exact_span"] = short_evidence
    _write_json(fixture.graph_path, graph)
    _refresh_locked_private_descriptors(fixture)
    encoded = base64.b64encode(short_evidence.encode()).decode("ascii")
    (fixture.repository_root / "README.md").write_text(
        "base64:" + encoded + "\n",
        encoding="utf-8",
    )
    fixture.commit("add short explicit Base64 evidence fixture")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")


@pytest.mark.parametrize(
    ("needle", "public_text", "private_text"),
    [
        ("1.", "schema version 1.2.0\n", "private value: 1.\n"),
        (
            "relationship",
            "Potential relationships are public.\n",
            "Private kind: relationship.\n",
        ),
    ],
)
def test_atomic_exact_evidence_requires_complete_token_boundaries(
    tmp_path: Path,
    needle: str,
    public_text: str,
    private_text: str,
) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    readme = candidate / "README.md"
    readme.write_text(public_text, encoding="utf-8")

    public_release_module._scan_candidate_text_against_private_needles(
        candidate,
        needles=(needle,),
    )

    readme.write_text(private_text, encoding="utf-8")
    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        public_release_module._scan_candidate_text_against_private_needles(
            candidate,
            needles=(needle,),
        )


def test_build_rejects_short_source_derived_statement(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    private_statement = "A source-only claim stays private."
    graph = _read_json(fixture.graph_path)
    nodes = graph["nodes"]
    assert isinstance(nodes, list)
    atom = nodes[1]
    assert isinstance(atom, dict)
    payload = atom["payload"]
    assert isinstance(payload, dict)
    payload["statement"] = private_statement
    _write_json(fixture.graph_path, graph)
    _refresh_locked_private_descriptors(fixture)
    (fixture.repository_root / "README.md").write_text(private_statement, encoding="utf-8")
    fixture.commit("copy short private statement fixture")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_partial_exact_evidence_excerpt(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    private_excerpt = "first clause is uniquely private"
    graph = _read_json(fixture.graph_path)
    nodes = graph["nodes"]
    assert isinstance(nodes, list)
    evidence = nodes[0]
    assert isinstance(evidence, dict)
    payload = evidence["payload"]
    assert isinstance(payload, dict)
    payload["exact_span"] = (
        "The first clause is uniquely private and should never be published together "
        "with this much longer source sentence."
    )
    _write_json(fixture.graph_path, graph)
    _refresh_locked_private_descriptors(fixture)
    (fixture.repository_root / "README.md").write_text(private_excerpt, encoding="utf-8")
    fixture.commit("copy partial exact evidence fixture")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")


@pytest.mark.parametrize("private_field", ["statement", "rationale", "scope", "qualification"])
def test_build_rejects_partial_excerpt_from_private_prose_field(
    tmp_path: Path,
    private_field: str,
) -> None:
    fixture = _release_fixture(tmp_path)
    private_excerpt = "middle phrase from private prose"
    graph = _read_json(fixture.graph_path)
    nodes = graph["nodes"]
    assert isinstance(nodes, list)
    atom = nodes[1]
    assert isinstance(atom, dict)
    payload = atom["payload"]
    assert isinstance(payload, dict)
    payload[private_field] = (
        "An opening clause surrounds the middle phrase from private prose and "
        "a closing clause keeps the complete source-derived sentence much longer."
    )
    _write_json(fixture.graph_path, graph)
    _refresh_locked_private_descriptors(fixture)
    (fixture.repository_root / "README.md").write_text(private_excerpt + "\n", encoding="utf-8")
    fixture.commit("copy partial private prose fixture")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_source_derived_scope(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    private_scope = (
        "This is a distinctive source-derived scope that must remain private and withheld."
    )
    graph = _read_json(fixture.graph_path)
    nodes = graph["nodes"]
    assert isinstance(nodes, list)
    atom = nodes[1]
    assert isinstance(atom, dict)
    payload = atom["payload"]
    assert isinstance(payload, dict)
    payload["scope"] = private_scope
    _write_json(fixture.graph_path, graph)
    _refresh_locked_private_descriptors(fixture)
    (fixture.repository_root / "README.md").write_text(private_scope, encoding="utf-8")
    fixture.commit("add private scope fixture")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_manifest_bound_semantic_text(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    rationale = (
        "This manifest-bound relationship rationale is private semantic text "
        "and must remain withheld."
    )
    semantic_input = fixture.graph_path.parents[1] / "semantic/relationship.json"
    _write_json(semantic_input, {"rationale": rationale})
    graph_manifest = _read_json(fixture.graph_manifest_path)
    descriptor = _descriptor(semantic_input)
    descriptor["path"] = "semantic/relationship.json"
    graph_manifest["inputs"] = [descriptor]
    _write_json(fixture.graph_manifest_path, graph_manifest)
    _refresh_locked_private_descriptors(fixture)
    (fixture.repository_root / "README.md").write_text(rationale, encoding="utf-8")
    fixture.commit("add manifest semantic text fixture")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_unknown_manifest_bound_input_type(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    semantic_input = fixture.graph_path.parents[1] / "semantic/private.txt"
    semantic_input.parent.mkdir(parents=True)
    semantic_input.write_text("Distinctive private text input.\n", encoding="utf-8")
    graph_manifest = _read_json(fixture.graph_manifest_path)
    descriptor = _descriptor(semantic_input)
    descriptor["path"] = "semantic/private.txt"
    graph_manifest["inputs"] = [descriptor]
    _write_json(fixture.graph_manifest_path, graph_manifest)
    _refresh_locked_private_descriptors(fixture)
    fixture.commit("add unsupported manifest input fixture")

    with pytest.raises(PublicReleaseValidationError, match="input type is unsupported"):
        fixture.builder().build(tmp_path / "candidate")


def test_cli_wraps_malformed_manifest_bound_json(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    semantic_input = fixture.graph_path.parents[1] / "semantic/broken.json"
    semantic_input.parent.mkdir(parents=True)
    semantic_input.write_text("{broken", encoding="utf-8")
    graph_manifest = _read_json(fixture.graph_manifest_path)
    descriptor = _descriptor(semantic_input)
    descriptor["path"] = "semantic/broken.json"
    graph_manifest["inputs"] = [descriptor]
    _write_json(fixture.graph_manifest_path, graph_manifest)
    _refresh_locked_private_descriptors(fixture)
    fixture.commit("add malformed manifest input fixture")

    result = dispatch(build_parser().parse_args(fixture.cli_arguments(tmp_path / "candidate")))

    assert result.ok is False
    assert result.classification.value == "validation_failure"
    assert "manifest-bound structured input is invalid" in result.errors[0]


def test_build_rejects_graph_manifest_semantic_text(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    private_note = "A graph manifest operator note contains unique private semantic context."
    graph_manifest = _read_json(fixture.graph_manifest_path)
    graph_manifest["operator_note"] = private_note
    _write_json(fixture.graph_manifest_path, graph_manifest)
    _refresh_locked_private_descriptors(fixture)
    (fixture.repository_root / "README.md").write_text(private_note, encoding="utf-8")
    fixture.commit("copy graph manifest private note fixture")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")


@pytest.mark.parametrize(
    ("quality_counts", "message"),
    [
        ({"Source quotation hidden in label": 2}, "unknown label"),
        ({"audited_passed": 3, "unaudited_provisional": -1}, "must be nonnegative"),
    ],
)
def test_build_rejects_invalid_quality_label_counts(
    tmp_path: Path,
    quality_counts: dict[str, int],
    message: str,
) -> None:
    fixture = _release_fixture(tmp_path)
    graph_manifest = _read_json(fixture.graph_manifest_path)
    graph_manifest["quality_label_counts"] = quality_counts
    _write_json(fixture.graph_manifest_path, graph_manifest)
    _refresh_locked_private_descriptors(fixture)
    fixture.commit("add invalid quality count fixture")

    with pytest.raises(PublicReleaseValidationError, match=message):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_unknown_relationship_kind_count(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    graph = _read_json(fixture.graph_path)
    edges = graph["edges"]
    assert isinstance(edges, list)
    cross_source_edge = edges[-1]
    assert isinstance(cross_source_edge, dict)
    cross_source_edge["kind"] = "Private relationship label"
    _write_json(fixture.graph_path, graph)
    _refresh_locked_private_descriptors(fixture)
    fixture.commit("add unknown relationship kind fixture")

    with pytest.raises(PublicReleaseValidationError, match="unknown kind"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_noncanonical_policy_input(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    alternate = tmp_path / "contract/publication-policy.yaml"
    alternate.parent.mkdir(parents=True)
    shutil.copyfile(fixture.policy_path, alternate)
    builder = PublicReleaseBuilder(
        repository_root=fixture.repository_root,
        source_registration_root=fixture.registration_root,
        asset_root=fixture.asset_root,
        graph_path=fixture.graph_path,
        graph_manifest_path=fixture.graph_manifest_path,
        private_report_path=fixture.report_path,
        policy_path=alternate,
        input_lock_path=fixture.input_lock_path,
    )

    with pytest.raises(PublicReleaseValidationError, match="canonical policy"):
        builder.build(tmp_path / "candidate")


def test_build_rejects_weakening_any_v1_policy_boundary(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    policy = yaml.safe_load(fixture.policy_path.read_text(encoding="utf-8"))
    policy["forbidden_path_segments"].remove(".project")
    policy["public_path_allowlist"].append(".project/public-note.md")
    fixture.policy_path.write_text(yaml.safe_dump(policy, sort_keys=False), encoding="utf-8")
    public_note = fixture.repository_root / ".project/public-note.md"
    public_note.parent.mkdir(parents=True)
    public_note.write_text("This path is outside the approved v1 surface.\n", encoding="utf-8")
    fixture.commit("weaken publication policy fixture")

    with pytest.raises(PublicReleaseValidationError, match="publication policy does not validate"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_unknown_private_input_lock_field(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    input_lock = _read_json(fixture.input_lock_path)
    input_lock["operator_note"] = "This unbound field must never enter the public contract."
    _write_json(fixture.input_lock_path, input_lock)
    fixture.commit("add unknown input lock field fixture")

    with pytest.raises(PublicReleaseValidationError, match="private input lock does not validate"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_pdf_header_after_leading_bytes(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    (fixture.repository_root / "README.md").write_bytes(
        b"leading comment\n" + PDF_PREFIX + b"-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
    )
    fixture.commit("add embedded PDF fixture")

    with pytest.raises(PublicReleaseValidationError, match="PDF bytes"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_pdf_header_after_initial_scan_window(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    (fixture.repository_root / "README.md").write_bytes(
        b"safe text\n" + b"x" * 2048 + b"\n" + PDF_PREFIX + b"-1.7\nprivate payload\n"
    )
    fixture.commit("add late embedded PDF fixture")

    with pytest.raises(PublicReleaseValidationError, match="PDF bytes"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_pdf_bytes_in_python_bytes_literal(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    leak = fixture.repository_root / "src/research_map/private_asset_fixture.py"
    leak.write_text(
        'PAYLOAD = b"\\x25\\x50\\x44\\x46\\x2d1.7\\nprivate payload\\n"\n',
        encoding="utf-8",
    )
    fixture.commit("encode PDF as Python bytes literal")

    with pytest.raises(PublicReleaseValidationError, match="encoded source asset"):
        fixture.builder().build(tmp_path / "candidate")


def test_candidate_scan_rejects_exact_registered_text_asset(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    asset = b"Plain-text registered source asset that cannot be redistributed.\n"
    (candidate / "README.md").write_bytes(asset)

    with pytest.raises(PublicReleaseValidationError, match="source asset"):
        public_release_module._scan_candidate_content(
            candidate,
            registered_asset_fingerprints=(
                ("private/source.txt", hashlib.sha256(asset).hexdigest(), len(asset)),
            ),
        )


@pytest.mark.parametrize(
    "relative",
    [
        "docs/images/dogfood/corpus-overview.png",
        "docs/images/dogfood/record-graph.png",
        "docs/images/dogfood/tension-lens.png",
        "docs/images/dogfood/source-detail.png",
    ],
)
def test_candidate_scan_accepts_only_exact_documentation_images(
    tmp_path: Path,
    relative: str,
) -> None:
    source = Path(__file__).parents[2] / relative
    candidate = tmp_path / "candidate"
    target = candidate / relative
    target.parent.mkdir(parents=True)
    shutil.copyfile(source, target)

    public_release_module._scan_candidate_content(candidate)

    altered = bytearray(target.read_bytes())
    altered[-1] ^= 1
    target.write_bytes(altered)
    with pytest.raises(PublicReleaseValidationError, match="image fingerprint differs"):
        public_release_module._scan_candidate_content(candidate)


def test_build_rejects_base64_encoded_registered_asset(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    asset = next((fixture.asset_root / "sources/library").glob("*/*.pdf"))
    encoded = base64.b64encode(asset.read_bytes()).decode("ascii")
    (fixture.repository_root / "README.md").write_text(encoded + "\n", encoding="utf-8")
    fixture.commit("add encoded source asset fixture")

    with pytest.raises(PublicReleaseValidationError, match="encoded source asset"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_base64_asset_after_assignment_delimiter(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    asset = next((fixture.asset_root / "sources/library").glob("*/*.pdf"))
    encoded = base64.b64encode(asset.read_bytes()).decode("ascii")
    (fixture.repository_root / "README.md").write_text(
        "payload=" + encoded + "\n",
        encoding="utf-8",
    )
    fixture.commit("add assignment-delimited encoded source asset fixture")

    with pytest.raises(PublicReleaseValidationError, match="encoded source asset"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_base64_private_identifier_in_filename(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    private_identifier = "LIB-991:atom:a1"
    encoded = base64.urlsafe_b64encode(private_identifier.encode()).decode().rstrip("=")
    leak = fixture.repository_root / "docs/architecture" / f"{encoded}.md"
    leak.write_text("# Otherwise safe content\n", encoding="utf-8")
    fixture.commit("add encoded private identifier filename fixture")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_wrapped_base64_encoded_registered_asset(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    asset = next((fixture.asset_root / "sources/library").glob("*/*.pdf"))
    encoded = base64.b64encode(asset.read_bytes()).decode("ascii")
    wrapped = "\n".join(encoded[index : index + 12] for index in range(0, len(encoded), 12))
    (fixture.repository_root / "README.md").write_text(wrapped + "\n", encoding="utf-8")
    fixture.commit("add wrapped encoded source asset fixture")

    with pytest.raises(PublicReleaseValidationError, match="encoded source asset"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_escaped_wrapped_base64_encoded_registered_asset(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    asset = next((fixture.asset_root / "sources/library").glob("*/*.pdf"))
    encoded = base64.b64encode(asset.read_bytes()).decode("ascii")
    wrapped = "\\n".join(encoded[index : index + 12] for index in range(0, len(encoded), 12))
    (fixture.repository_root / "README.md").write_text(
        '{"payload":"' + wrapped + '"}\n', encoding="utf-8"
    )
    fixture.commit("add escaped wrapped encoded source asset fixture")

    with pytest.raises(PublicReleaseValidationError, match="encoded source asset"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_nested_base64_encoded_registered_asset(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    asset = next((fixture.asset_root / "sources/library").glob("*/*.pdf"))
    encoded = base64.b64encode(base64.b64encode(asset.read_bytes())).decode("ascii")
    (fixture.repository_root / "README.md").write_text(encoded + "\n", encoding="utf-8")
    fixture.commit("add nested encoded source asset fixture")

    with pytest.raises(PublicReleaseValidationError, match="encoded source asset"):
        fixture.builder().build(tmp_path / "candidate")


@pytest.mark.parametrize(
    "binary_payload",
    [
        "SQLite format 3".encode("ascii") + NUL_BYTE + b"private rows",
        ZIP_PREFIX + NUL_BYTE + NON_UTF8_BYTE + b"private archive",
        b"opaque" + NUL_BYTE + b"private cache rows without a known signature",
        b"opaque non-UTF-8 private cache rows: " + NON_UTF8_BYTE + bytes((254,)),
    ],
)
def test_build_rejects_base64_encoded_binary_payload(tmp_path: Path, binary_payload: bytes) -> None:
    fixture = _release_fixture(tmp_path)
    encoded = base64.b64encode(binary_payload).decode("ascii")
    (fixture.repository_root / "README.md").write_text(encoded + "\n", encoding="utf-8")
    fixture.commit("add encoded binary fixture")

    with pytest.raises(PublicReleaseValidationError, match="encoded .*binary|encoded SQLite"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_base64_binary_payload_after_assignment(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    encoded = base64.b64encode(b"opaque" + NUL_BYTE + b"private cache rows").decode("ascii")
    (fixture.repository_root / "README.md").write_text(
        "payload=" + encoded + "\n", encoding="utf-8"
    )
    fixture.commit("add assigned encoded binary fixture")

    with pytest.raises(PublicReleaseValidationError, match="encoded unexplained binary"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_encoded_pdf_header_after_long_prefix(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    payload = b"x" * 2048 + PDF_PREFIX + b"-1.7\nprivate payload"
    encoded = base64.b64encode(payload).decode("ascii")
    (fixture.repository_root / "README.md").write_text(encoded + "\n", encoding="utf-8")
    fixture.commit("add prefixed encoded PDF fixture")

    with pytest.raises(PublicReleaseValidationError, match="encoded source asset"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_hex_encoded_registered_asset(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    asset = next((fixture.asset_root / "sources/library").glob("*/*.pdf"))
    encoded = asset.read_bytes().hex()
    (fixture.repository_root / "README.md").write_text(encoded + "\n", encoding="utf-8")
    fixture.commit("add hex encoded source asset fixture")

    with pytest.raises(PublicReleaseValidationError, match="encoded source asset"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_embedded_private_repository_url(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    private_url = "https://github.com/" + FIXTURE_PRIVATE_REPOSITORY + ".git"
    (fixture.repository_root / "README.md").write_text(
        f'The private remote is "{private_url}" and must stay private.\n',
        encoding="utf-8",
    )
    fixture.commit("add embedded private repository URL fixture")

    with pytest.raises(PublicReleaseValidationError, match="private work URL"):
        fixture.builder().build(tmp_path / "candidate")


@pytest.mark.parametrize(
    "private_path",
    [
        "/" + "opt/anaconda3/private/file.txt",
        "/" + "workspace",
        "/" + "secret",
        "/" + "etc",
        "/" + "Volumes",
        "\\" + "\\" + "server" + "\\" + "share",
        "/" + "/" + "server" + "/" + "share",
    ],
)
def test_build_rejects_arbitrary_absolute_posix_or_unc_path(
    tmp_path: Path,
    private_path: str,
) -> None:
    fixture = _release_fixture(tmp_path)
    (fixture.repository_root / "README.md").write_text(private_path + "\n", encoding="utf-8")
    fixture.commit("add arbitrary absolute path fixture")

    with pytest.raises(PublicReleaseValidationError, match="machine-local path"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_absolute_machine_path_in_gitignore(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    (fixture.repository_root / ".gitignore").write_text(
        ".venv/\n" + "/" + "opt/leonardo/private-run/\n",
        encoding="utf-8",
    )
    fixture.commit("add machine-local gitignore path fixture")

    with pytest.raises(PublicReleaseValidationError, match="machine-local path"):
        fixture.builder().build(tmp_path / "candidate")


@pytest.mark.parametrize(
    "private_path",
    [
        "D:" + r"\research\private\graph.json",
        "~" + "/" + "research/private/graph.json",
        "$" + "HOME" + "/" + "research/private/graph.json",
        "${" + "HOME}" + "/" + "research/private/graph.json",
        "%" + "USERPROFILE%\\research\\private\\graph.json",
    ],
)
def test_build_rejects_portable_machine_local_paths(
    tmp_path: Path,
    private_path: str,
) -> None:
    fixture = _release_fixture(tmp_path)
    (fixture.repository_root / "README.md").write_text(private_path + "\n", encoding="utf-8")
    fixture.commit("add portable local path fixture")

    with pytest.raises(PublicReleaseValidationError, match="machine-local path"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_allows_distinct_public_repository_url(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    public_url = "https://github.com/" + FIXTURE_PRIVATE_REPOSITORY + "-public"
    (fixture.repository_root / "README.md").write_text(public_url + "\n", encoding="utf-8")
    fixture.commit("add public repository URL fixture")

    assert fixture.builder().build(tmp_path / "candidate").file_count > 0


def test_build_rejects_unknown_private_report_prose(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    private_prose = "A confidential worker transcript explains why local acquisition failed."
    report = _read_json(fixture.report_path)
    report["worker_error"] = private_prose
    _write_json(fixture.report_path, report)
    _refresh_locked_private_descriptors(fixture)
    (fixture.repository_root / "README.md").write_text(private_prose + "\n", encoding="utf-8")
    fixture.commit("add unknown private report prose fixture")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_unknown_warning_prose(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    private_prose = "A confidential warning records source-derived details that stay private."
    report = _read_json(fixture.report_path)
    report["warnings"] = [private_prose]
    _write_json(fixture.report_path, report)
    _refresh_locked_private_descriptors(fixture)
    (fixture.repository_root / "README.md").write_text(private_prose + "\n", encoding="utf-8")
    fixture.commit("add unknown warning prose fixture")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")


@pytest.mark.parametrize("suffix", ["", ".", ", and public context"])
def test_build_rejects_four_word_exact_evidence_excerpt(
    tmp_path: Path,
    suffix: str,
) -> None:
    fixture = _release_fixture(tmp_path)
    private_excerpt = "uniquely private evidence phrase"
    graph = _read_json(fixture.graph_path)
    nodes = graph["nodes"]
    assert isinstance(nodes, list)
    evidence = nodes[0]
    assert isinstance(evidence, dict)
    payload = evidence["payload"]
    assert isinstance(payload, dict)
    payload["exact_span"] = (
        "A uniquely private evidence phrase appears inside this longer exact source span."
    )
    _write_json(fixture.graph_path, graph)
    _refresh_locked_private_descriptors(fixture)
    (fixture.repository_root / "README.md").write_text(
        private_excerpt + suffix + "\n",
        encoding="utf-8",
    )
    fixture.commit("add four word exact evidence fixture")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_ambiguous_or_negated_rights_note(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    registration = next((fixture.registration_root / "library").glob("*/source.yaml"))
    payload = yaml.safe_load(registration.read_text(encoding="utf-8"))
    payload["provenance"]["rights_notes"] = "".join(
        ("No CC-BY 4.0 license was found; redistribution rights were ", "not assessed.")
    )
    registration.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    source_commit = fixture.commit("add ambiguous rights fixture")
    input_lock = _read_json(fixture.input_lock_path)
    input_lock["private_repository_commit"] = source_commit
    _write_json(fixture.input_lock_path, input_lock)
    fixture.commit("refresh ambiguous rights fixture lock")

    with pytest.raises(PublicReleaseValidationError, match="ambiguous source rights posture"):
        fixture.builder().build(tmp_path / "candidate")


@pytest.mark.parametrize(
    "rights_note",
    [
        "CC-BY 4.0 does not apply to this PDF.",
        "The recorded license is CC BY 4.0, but that statement is incorrect.",
    ],
)
def test_build_rejects_trailing_or_qualified_rights_negation(
    tmp_path: Path, rights_note: str
) -> None:
    fixture = _release_fixture(tmp_path)
    registration = next((fixture.registration_root / "library").glob("*/source.yaml"))
    payload = yaml.safe_load(registration.read_text(encoding="utf-8"))
    payload["provenance"]["rights_notes"] = rights_note
    registration.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    source_commit = fixture.commit("add negated rights fixture")
    input_lock = _read_json(fixture.input_lock_path)
    input_lock["private_repository_commit"] = source_commit
    _write_json(fixture.input_lock_path, input_lock)
    fixture.commit("refresh negated rights fixture lock")

    with pytest.raises(PublicReleaseValidationError, match="ambiguous source rights posture"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_encoded_private_evidence_text(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    encoded = base64.b64encode(PRIVATE_EVIDENCE.encode()).decode()
    (fixture.repository_root / "README.md").write_text(encoded + "\n", encoding="utf-8")
    fixture.commit("add encoded private evidence fixture")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_private_evidence_in_many_python_literal_chunks(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    words = PRIVATE_EVIDENCE.split()
    chunks = [" ".join(words[index : index + 2]) for index in range(0, len(words), 2)]
    leak = fixture.repository_root / "src/research_map/private_evidence_fixture.py"
    leak.write_text(
        "PRIVATE_EVIDENCE = " + " + ".join(repr(chunk + " ") for chunk in chunks) + "\n",
        encoding="utf-8",
    )
    fixture.commit("split private evidence across Python literals")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_nfkc_disguised_private_evidence(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    fullwidth = "".join(
        chr(ord(character) + 0xFEE0) if 0x21 <= ord(character) <= 0x7E else character
        for character in PRIVATE_EVIDENCE
    )
    assert unicodedata.normalize("NFKC", fullwidth) == PRIVATE_EVIDENCE
    (fixture.repository_root / "README.md").write_text(fullwidth + "\n", encoding="utf-8")
    fixture.commit("disguise private evidence with compatibility characters")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_markdown_emphasized_private_evidence(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    emphasized = " ".join(f"**{word}**" for word in PRIVATE_EVIDENCE.split())
    (fixture.repository_root / "README.md").write_text(emphasized + "\n", encoding="utf-8")
    fixture.commit("hide private evidence behind Markdown emphasis")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")


@pytest.mark.parametrize("chunk_size", [12, 16])
def test_build_rejects_private_evidence_split_across_base64_scalars(
    tmp_path: Path,
    chunk_size: int,
) -> None:
    fixture = _release_fixture(tmp_path)
    encoded = base64.b64encode(PRIVATE_EVIDENCE.encode()).decode("ascii")
    chunks = [encoded[index : index + chunk_size] for index in range(0, len(encoded), chunk_size)]
    (fixture.repository_root / "README.md").write_text(json.dumps(chunks) + "\n", encoding="utf-8")
    fixture.commit("add chunked encoded private evidence fixture")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")


@pytest.mark.parametrize(
    "container",
    ["python_tuple", "yaml_sequence", "unquoted_yaml_sequence"],
)
@pytest.mark.parametrize("chunk_size", [12, 16])
def test_build_rejects_private_evidence_split_across_non_json_base64_scalars(
    tmp_path: Path,
    container: str,
    chunk_size: int,
) -> None:
    fixture = _release_fixture(tmp_path)
    encoded = base64.b64encode(PRIVATE_EVIDENCE.encode()).decode("ascii")
    chunks = [encoded[index : index + chunk_size] for index in range(0, len(encoded), chunk_size)]
    if container == "python_tuple":
        serialized = "chunks = (" + ", ".join(repr(chunk) for chunk in chunks) + ")\n"
    elif container == "yaml_sequence":
        serialized = "chunks:\n" + "".join(f"  - '{chunk}'\n" for chunk in chunks)
    else:
        serialized = "chunks:\n" + "".join(f"  - {chunk}\n" for chunk in chunks)
    (fixture.repository_root / "README.md").write_text(serialized, encoding="utf-8")
    fixture.commit("add non-JSON chunked encoded private evidence fixture")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")


@pytest.mark.parametrize(
    "encoded",
    [
        PRIVATE_EVIDENCE.replace(" ", "&#32;"),
        PRIVATE_EVIDENCE.replace(" ", r"\t"),
        PRIVATE_EVIDENCE.replace(" ", r"\x20"),
        "payload=" + base64.b64encode(PRIVATE_EVIDENCE.encode()).decode(),
    ],
)
def test_build_rejects_reversibly_encoded_private_evidence(
    tmp_path: Path,
    encoded: str,
) -> None:
    fixture = _release_fixture(tmp_path)
    (fixture.repository_root / "README.md").write_text(encoded + "\n", encoding="utf-8")
    fixture.commit("add reversibly encoded private evidence fixture")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_percent_encoded_private_evidence_and_credentials(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    credential = "gh" + "p_" + "a" * 36
    encoded = urllib.parse.quote(PRIVATE_EVIDENCE + "\n" + credential, safe="")
    (fixture.repository_root / "README.md").write_text(encoded + "\n", encoding="utf-8")
    fixture.commit("add percent encoded private content fixture")

    with pytest.raises(PublicReleaseValidationError, match="private graph text|credential pattern"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_wrapped_encoded_private_evidence_text(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    encoded = base64.b64encode(PRIVATE_EVIDENCE.encode()).decode()
    wrapped = "\n".join(encoded[index : index + 12] for index in range(0, len(encoded), 12))
    (fixture.repository_root / "README.md").write_text(wrapped + "\n", encoding="utf-8")
    fixture.commit("add wrapped private evidence fixture")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_escaped_wrapped_encoded_private_evidence_text(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    encoded = base64.b64encode(PRIVATE_EVIDENCE.encode()).decode()
    wrapped = "\\n".join(encoded[index : index + 12] for index in range(0, len(encoded), 12))
    (fixture.repository_root / "README.md").write_text(
        '{"payload":"' + wrapped + '"}\n', encoding="utf-8"
    )
    fixture.commit("add escaped wrapped private evidence fixture")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_nested_encoded_private_evidence_text(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    encoded = base64.b64encode(base64.b64encode(PRIVATE_EVIDENCE.encode())).decode()
    (fixture.repository_root / "README.md").write_text(encoded + "\n", encoding="utf-8")
    fixture.commit("add nested private evidence fixture")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_deeply_nested_encoded_private_evidence_text(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    encoded = PRIVATE_EVIDENCE.encode()
    for _ in range(8):
        encoded = base64.b64encode(encoded)
    (fixture.repository_root / "README.md").write_bytes(encoded)
    fixture.commit("add deeply nested private evidence fixture")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_encoded_private_text_revealed_by_unicode_escape(
    tmp_path: Path,
) -> None:
    fixture = _release_fixture(tmp_path)
    encoded = base64.b64encode(PRIVATE_EVIDENCE.encode()).decode("ascii")
    escaped = "".join(f"\\u{ord(character):04x}" for character in encoded)
    (fixture.repository_root / "README.md").write_text(escaped, encoding="utf-8")
    fixture.commit("add unicode escaped encoded private evidence fixture")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_json_unicode_escaped_private_text(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    escaped = "".join(f"\\u{ord(character):04x}" for character in PRIVATE_EVIDENCE)
    (fixture.repository_root / "README.md").write_text(
        '{"private":"' + escaped + '"}\n', encoding="utf-8"
    )
    fixture.commit("add JSON Unicode-escaped private evidence fixture")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")


@pytest.mark.parametrize(
    "format_character",
    ["\u034f", "\u200b", "\u2060", "\ufe0f", "\ufeff"],
)
def test_build_rejects_private_text_obscured_by_unicode_format_controls(
    tmp_path: Path,
    format_character: str,
) -> None:
    fixture = _release_fixture(tmp_path)
    obscured = format_character.join(PRIVATE_EVIDENCE)
    (fixture.repository_root / "README.md").write_text(obscured + "\n", encoding="utf-8")
    fixture.commit("add format-control-obscured private evidence fixture")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")


@pytest.mark.parametrize("format_character", ["\u034f", "\u200b", "\ufe0f"])
def test_build_rejects_credentials_obscured_by_default_ignorables(
    tmp_path: Path,
    format_character: str,
) -> None:
    fixture = _release_fixture(tmp_path)
    credential = "gh" + "p_" + "a" * 36
    obscured = format_character.join(credential)
    (fixture.repository_root / "README.md").write_text(obscured + "\n", encoding="utf-8")
    fixture.commit("add ignorable-obscured credential fixture")

    with pytest.raises(PublicReleaseValidationError, match="credential pattern"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_private_text_split_by_markup(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    obscured = PRIVATE_EVIDENCE.replace("private synthetic", "private <span>synthetic</span>")
    (fixture.repository_root / "README.md").write_text(obscured + "\n", encoding="utf-8")
    fixture.commit("add markup-obscured private evidence fixture")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_atomic_private_source_form(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    private_source_form = "E=mc²"
    graph = _read_json(fixture.graph_path)
    nodes = graph["nodes"]
    assert isinstance(nodes, list)
    atom = nodes[1]
    assert isinstance(atom, dict)
    payload = atom["payload"]
    assert isinstance(payload, dict)
    payload["fidelity"] = {"source_form": private_source_form}
    _write_json(fixture.graph_path, graph)
    _refresh_locked_private_descriptors(fixture)
    (fixture.repository_root / "README.md").write_text(private_source_form + "\n", encoding="utf-8")
    fixture.commit("add atomic source form fixture")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_atomic_private_relationship_id(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    graph = _read_json(fixture.graph_path)
    edges = graph["edges"]
    assert isinstance(edges, list)
    edge = edges[-1]
    assert isinstance(edge, dict)
    relationship_id = edge["relationship_id"]
    assert isinstance(relationship_id, str)
    (fixture.repository_root / "README.md").write_text(relationship_id + "\n", encoding="utf-8")
    fixture.commit("copy atomic relationship id fixture")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")


@pytest.mark.parametrize(
    "topology_value",
    [
        "LIB-991" + ":atom:a1",
        FIXTURE_PAIR_DIRECTORY,
    ],
)
def test_build_rejects_atomic_private_topology_value(tmp_path: Path, topology_value: str) -> None:
    fixture = _release_fixture(tmp_path)
    (fixture.repository_root / "README.md").write_text(topology_value + "\n", encoding="utf-8")
    fixture.commit("copy atomic private topology fixture")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_canonical_arrow_private_source_pair(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    source_pair = " ↔ ".join(("LIB-991", "LIB-992"))
    (fixture.repository_root / "README.md").write_text(
        source_pair + "\n",
        encoding="utf-8",
    )
    fixture.commit("copy canonical private source pair fixture")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_private_source_pair_as_json_list(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    left, right = "LIB-991", "LIB-992"
    (fixture.repository_root / "README.md").write_text(
        json.dumps({"source_pair": [left, right]}, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    fixture.commit("copy private source pair as JSON list")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_bare_basecamp_identifier(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    basecamp_root = fixture.repository_root / ".basecamp"
    basecamp_root.mkdir()
    account_id = FIXTURE_ACCOUNT_ID
    project_id = FIXTURE_PROJECT_ID
    _write_json(
        basecamp_root / "config.json",
        {"account_id": account_id, "project_id": project_id},
    )
    (fixture.repository_root / "README.md").write_text(
        "Internal project identifier: " + project_id + "\n",
        encoding="utf-8",
    )
    fixture.commit("copy private Basecamp identifier fixture")

    with pytest.raises(PublicReleaseValidationError, match="private work"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_basecamp_recording_identifier_from_project_url(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    recording_id = "".join(("975", "3108642"))
    project_record = fixture.repository_root / ".project/features/private/handoff.md"
    project_record.parent.mkdir(parents=True)
    project_record.write_text(
        "https://"
        + "3.base"
        + "camp.com/"
        + FIXTURE_ACCOUNT_ID
        + "/"
        + "buckets/"
        + FIXTURE_PROJECT_ID
        + "/"
        + "todos/"
        + recording_id
        + "\n",
        encoding="utf-8",
    )
    (fixture.repository_root / "README.md").write_text(
        "Internal recording: " + recording_id + "\n",
        encoding="utf-8",
    )
    fixture.commit("copy private Basecamp recording identifier fixture")

    with pytest.raises(PublicReleaseValidationError, match="private work"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_private_identifier_split_across_python_literals(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    basecamp_root = fixture.repository_root / ".basecamp"
    basecamp_root.mkdir()
    _write_json(
        basecamp_root / "config.json",
        {"account_id": FIXTURE_ACCOUNT_ID, "project_id": FIXTURE_PROJECT_ID},
    )
    split_at = len(FIXTURE_PROJECT_ID) // 2
    leak = fixture.repository_root / "src/research_map/private_identifier_fixture.py"
    leak.write_text(
        f'PROJECT_ID = "{FIXTURE_PROJECT_ID[:split_at]}" + "{FIXTURE_PROJECT_ID[split_at:]}"\n',
        encoding="utf-8",
    )
    fixture.commit("split private identifier across Python literals")

    with pytest.raises(PublicReleaseValidationError, match="private work"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_private_remote_split_across_python_literals(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    private_url = "https://github.com/" + FIXTURE_PRIVATE_REPOSITORY + ".git"
    split_at = len(private_url) // 2
    leak = fixture.repository_root / "src/research_map/private_remote_fixture.py"
    leak.write_text(
        f'PRIVATE_REMOTE = "{private_url[:split_at]}" + "{private_url[split_at:]}"\n',
        encoding="utf-8",
    )
    fixture.commit("split private remote across Python literals")

    with pytest.raises(PublicReleaseValidationError, match="private work"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_private_remote_in_implicit_python_concatenation(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    private_url = "https://github.com/" + FIXTURE_PRIVATE_REPOSITORY + ".git"
    split_at = len(private_url) // 2
    leak = fixture.repository_root / "src/research_map/private_remote_fixture.py"
    leak.write_text(
        f'PRIVATE_REMOTE = "{private_url[:split_at]}" "{private_url[split_at:]}"\n',
        encoding="utf-8",
    )
    fixture.commit("hide private remote in implicit Python concatenation")

    with pytest.raises(PublicReleaseValidationError, match="private work"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_private_codex_task_identifier(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    task_identifier = FIXTURE_TASK_ID
    project_record = fixture.repository_root / ".project/features/private/handoff.md"
    project_record.parent.mkdir(parents=True)
    project_record.write_text("Task: " + task_identifier + "\n", encoding="utf-8")
    (fixture.repository_root / "README.md").write_text(
        "Internal task: " + task_identifier + "\n",
        encoding="utf-8",
    )
    fixture.commit("copy private task identifier fixture")

    with pytest.raises(PublicReleaseValidationError, match="private work"):
        fixture.builder().build(tmp_path / "candidate")


@pytest.mark.parametrize("identifier_key", ["batch_id", "candidate_id", "inspection_job_id"])
def test_build_rejects_atomic_private_provenance_identifier(
    tmp_path: Path,
    identifier_key: str,
) -> None:
    fixture = _release_fixture(tmp_path)
    if identifier_key == "batch_id":
        identifier = _read_json(fixture.graph_path)[identifier_key]
    else:
        relationship = _fixture_relationship().to_dict()
        identifier = (
            relationship[identifier_key]
            if identifier_key == "candidate_id"
            else relationship["provenance"][identifier_key]
        )
    assert isinstance(identifier, str)
    (fixture.repository_root / "README.md").write_text(identifier + "\n", encoding="utf-8")
    fixture.commit("copy private provenance identifier fixture")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_atomic_registered_asset_path(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    registration = next((fixture.registration_root / "library").glob("*/source.yaml"))
    payload = yaml.safe_load(registration.read_text(encoding="utf-8"))
    asset_path = payload["assets"][0]["path"]
    assert isinstance(asset_path, str)
    (fixture.repository_root / "README.md").write_text(asset_path + "\n", encoding="utf-8")
    fixture.commit("copy atomic registered asset path fixture")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_canonically_equivalent_unicode_private_text(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    private_statement = "A distinctive Schrödinger argument must remain private."
    graph = _read_json(fixture.graph_path)
    nodes = graph["nodes"]
    assert isinstance(nodes, list)
    atom = nodes[1]
    assert isinstance(atom, dict)
    payload = atom["payload"]
    assert isinstance(payload, dict)
    payload["statement"] = private_statement
    _write_json(fixture.graph_path, graph)
    _refresh_locked_private_descriptors(fixture)
    (fixture.repository_root / "README.md").write_text(
        unicodedata.normalize("NFD", private_statement),
        encoding="utf-8",
    )
    fixture.commit("add decomposed Unicode private text fixture")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_edge_level_private_rationale(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    private_rationale = "This edge rationale is distinctive private semantic text."
    graph = _read_json(fixture.graph_path)
    edges = graph["edges"]
    assert isinstance(edges, list)
    assert isinstance(edges[-1], dict)
    edges[-1]["rationale"] = private_rationale
    _write_json(fixture.graph_path, graph)
    _refresh_locked_private_descriptors(fixture)
    (fixture.repository_root / "README.md").write_text(private_rationale + "\n", encoding="utf-8")
    fixture.commit("add private edge rationale fixture")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_short_unknown_private_prose(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    private_prose = "Do not publish this quote."
    report = _read_json(fixture.report_path)
    report["worker_error"] = private_prose
    _write_json(fixture.report_path, report)
    _refresh_locked_private_descriptors(fixture)
    (fixture.repository_root / "README.md").write_text(private_prose + "\n", encoding="utf-8")
    fixture.commit("add short unknown private prose fixture")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_does_not_select_unapproved_reference_mapping_path(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    unapproved = fixture.repository_root / "reference_mapping_graph/private-topology.json"
    _write_json(
        unapproved,
        {
            "source_pair": ["LIB-991", "LIB-992"],
            "endpoints": ["LIB-991:atom:a1", "LIB-992:atom:a1"],
        },
    )
    fixture.commit("add unapproved reference mapping fixture")

    result = fixture.builder().build(tmp_path / "candidate")

    assert not (result.candidate_root / "reference_mapping_graph/private-topology.json").exists()


def test_build_rejects_dirty_or_untracked_repository_state(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    readme = fixture.repository_root / "README.md"
    original = readme.read_text(encoding="utf-8")
    readme.write_text("unstaged public edit\n", encoding="utf-8")

    with pytest.raises(PublicReleaseValidationError, match="working tree must be clean"):
        fixture.builder().build(tmp_path / "candidate-dirty")

    readme.write_text(original, encoding="utf-8")
    untracked = fixture.repository_root / "docs/private-notes.md"
    untracked.write_text("untracked private note\n", encoding="utf-8")
    with pytest.raises(PublicReleaseValidationError, match="working tree must be clean"):
        fixture.builder().build(tmp_path / "candidate-untracked")


def test_build_rechecks_manifest_bound_inputs_after_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _release_fixture(tmp_path)
    semantic_input = fixture.graph_path.parents[1] / "semantic/relationship.json"
    _write_json(semantic_input, {"rationale": "Private manifest-bound rationale."})
    graph_manifest = _read_json(fixture.graph_manifest_path)
    descriptor = _descriptor(semantic_input)
    descriptor["path"] = "semantic/relationship.json"
    graph_manifest["inputs"] = [descriptor]
    _write_json(fixture.graph_manifest_path, graph_manifest)
    _refresh_locked_private_descriptors(fixture)
    fixture.commit("add manifest-bound mutation fixture")

    original_descriptors = public_release_module._candidate_descriptors
    changed = False

    def mutate_after_semantic_scan(
        candidate_root: Path, *, exclude: set[Path]
    ) -> list[dict[str, Any]]:
        nonlocal changed
        result = original_descriptors(candidate_root, exclude=exclude)
        if not changed:
            _write_json(semantic_input, {"rationale": "Changed during projection."})
            changed = True
        return result

    monkeypatch.setattr(
        public_release_module,
        "_candidate_descriptors",
        mutate_after_semantic_scan,
    )

    with pytest.raises(PublicReleaseValidationError, match="manifest-bound private inputs changed"):
        fixture.builder().build(tmp_path / "candidate")


def test_public_projection_verifies_after_autocrlf_clone(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    result = fixture.builder().build(tmp_path / "candidate")
    assert (result.candidate_root / ".gitattributes").read_text(encoding="utf-8") == (
        "* text=auto eol=lf\n"
    )
    _initialize_git(result.candidate_root)
    clone = tmp_path / "autocrlf-clone"
    subprocess.run(
        (
            "git",
            "-c",
            "core.autocrlf=true",
            "clone",
            "-q",
            "--no-local",
            str(result.candidate_root),
            str(clone),
        ),
        check=True,
    )

    verification = verify_public_release(clone)

    assert verification.tree_sha256 == result.tree_sha256


def test_verifier_script_uses_clean_git_export_and_external_environment(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    result = fixture.builder().build(tmp_path / "candidate")
    _initialize_git(result.candidate_root)
    ignored_environment_file = result.candidate_root / ".venv/local-marker"
    ignored_environment_file.parent.mkdir()
    ignored_environment_file.write_text("local only", encoding="utf-8")
    before = _tree_bytes(result.candidate_root)
    environment = os.environ.copy()
    environment.pop("UV_PROJECT_ENVIRONMENT", None)
    completed = subprocess.run(
        (
            str(result.candidate_root / "scripts/verify-public-release.sh"),
            str(result.candidate_root),
        ),
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert ignored_environment_file.read_text(encoding="utf-8") == "local only"
    assert _tree_bytes(result.candidate_root) == before


@pytest.mark.parametrize("short_evidence", ["χ ≼ ζ", "Z\\prec W"])
def test_build_rejects_short_unicode_or_latex_equation(tmp_path: Path, short_evidence: str) -> None:
    fixture = _release_fixture(tmp_path)
    graph = _read_json(fixture.graph_path)
    nodes = graph["nodes"]
    assert isinstance(nodes, list)
    first_node = nodes[0]
    assert isinstance(first_node, dict)
    payload = first_node["payload"]
    assert isinstance(payload, dict)
    payload["exact_span"] = short_evidence
    _write_json(fixture.graph_path, graph)
    _refresh_locked_private_descriptors(fixture)
    _write_json(fixture.repository_root / "README.md", {"exact_span": short_evidence})
    fixture.commit("add encoded short equation fixture")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_sentence_fragment_from_long_evidence(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    private_sentence = "This first private evidence sentence is long enough to remain protected."
    graph = _read_json(fixture.graph_path)
    nodes = graph["nodes"]
    assert isinstance(nodes, list)
    first_node = nodes[0]
    assert isinstance(first_node, dict)
    payload = first_node["payload"]
    assert isinstance(payload, dict)
    payload["exact_span"] = (
        private_sentence + " This second sentence makes the stored exact span longer."
    )
    _write_json(fixture.graph_path, graph)
    _refresh_locked_private_descriptors(fixture)
    (fixture.repository_root / "README.md").write_text(private_sentence, encoding="utf-8")
    fixture.commit("add partial private evidence fixture")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_withheld_source_registration_prose(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    private_note = (
        "Private source-derived pagination note that must not enter public documentation."
    )
    registration = next((fixture.registration_root / "library").glob("*/source.yaml"))
    payload = yaml.safe_load(registration.read_text(encoding="utf-8"))
    payload["structure"] = {"pagination_notes": private_note}
    registration.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    (fixture.repository_root / "README.md").write_text(private_note, encoding="utf-8")
    source_commit = fixture.commit("add withheld source registration fixture")
    input_lock = _read_json(fixture.input_lock_path)
    input_lock["private_repository_commit"] = source_commit
    source_state = _collect_private_source_state(fixture.registration_root, fixture.asset_root)
    source_integrity = input_lock["source_integrity"]
    assert isinstance(source_integrity, dict)
    source_integrity["sha256"] = source_state.source_integrity_sha256
    _write_json(fixture.input_lock_path, input_lock)
    fixture.commit("refresh source registration fixture lock")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_atomic_withheld_source_registration_identifier(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    private_identifier = "-".join(("FM", "02"))
    registration = next((fixture.registration_root / "library").glob("*/source.yaml"))
    payload = yaml.safe_load(registration.read_text(encoding="utf-8"))
    payload["coverage_guidance"] = [{"field_area": private_identifier}]
    registration.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    (fixture.repository_root / "README.md").write_text(private_identifier + "\n", encoding="utf-8")
    source_commit = fixture.commit("add withheld registration identifier fixture")
    input_lock = _read_json(fixture.input_lock_path)
    input_lock["private_repository_commit"] = source_commit
    source_state = _collect_private_source_state(fixture.registration_root, fixture.asset_root)
    source_integrity = input_lock["source_integrity"]
    assert isinstance(source_integrity, dict)
    source_integrity["sha256"] = source_state.source_integrity_sha256
    _write_json(fixture.input_lock_path, input_lock)
    fixture.commit("refresh registration identifier fixture lock")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_nested_bibliographic_value(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    registration = next((fixture.registration_root / "library").glob("*/source.yaml"))
    payload = yaml.safe_load(registration.read_text(encoding="utf-8"))
    payload["identity"]["title"] = {
        "display": "Synthetic source A",
        "private_note": "Source-only prose that must stay private.",
    }
    registration.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    source_commit = fixture.commit("add nested bibliographic fixture")
    input_lock = _read_json(fixture.input_lock_path)
    input_lock["private_repository_commit"] = source_commit
    _write_json(fixture.input_lock_path, input_lock)
    fixture.commit("refresh nested bibliographic fixture lock")

    with pytest.raises(PublicReleaseValidationError, match="identity value is invalid"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_rejects_raw_rights_note(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    raw_rights_note = "".join(
        ("Local possession only; ", "redistribution rights were ", "not assessed.")
    )
    (fixture.repository_root / "README.md").write_text(raw_rights_note, encoding="utf-8")
    fixture.commit("copy raw rights note fixture")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")


def test_build_and_verifier_reject_unsafe_rights_release_posture(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    inventory_path = fixture.repository_root / "reference_mapping_graph/rights-inventory.yaml"
    inventory = yaml.safe_load(inventory_path.read_text(encoding="utf-8"))
    inventory["categories"]["redistribution_not_assessed"]["release_posture"] = "publish_full_text"
    inventory_path.write_text(yaml.safe_dump(inventory, sort_keys=False), encoding="utf-8")
    fixture.commit("add unsafe rights posture fixture")

    with pytest.raises(PublicReleaseValidationError, match="release posture is unsafe"):
        fixture.builder().build(tmp_path / "candidate-unsafe")

    safe_fixture = _release_fixture(tmp_path / "safe")
    result = safe_fixture.builder().build(tmp_path / "candidate-safe")
    candidate_inventory_path = (
        result.candidate_root / "reference_mapping_graph/rights-inventory.yaml"
    )
    candidate_inventory = yaml.safe_load(candidate_inventory_path.read_text(encoding="utf-8"))
    candidate_inventory["categories"]["redistribution_not_assessed"]["release_posture"] = (
        "publish_full_text"
    )
    candidate_inventory_path.write_text(
        yaml.safe_dump(candidate_inventory, sort_keys=False), encoding="utf-8"
    )
    _refresh_candidate_manifest(result.candidate_root)

    with pytest.raises(PublicReleaseValidationError, match="release posture is unsafe"):
        verify_public_release(result.candidate_root)


def test_source_integrity_digest_uses_key_sorted_canonical_json(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    rows = []
    for registration in sorted((fixture.registration_root / "library").glob("*/source.yaml")):
        payload = yaml.safe_load(registration.read_text(encoding="utf-8"))
        assets = []
        for asset in payload["assets"]:
            match = __import__("re").search(
                r"SHA-256\s+([0-9a-f]{64});\s+(\d+)\s+bytes", asset["notes"]
            )
            assert match is not None
            assets.append(
                {
                    "role": asset["asset_role"],
                    "sha256": match.group(1),
                    "size_bytes": int(match.group(2)),
                }
            )
        rows.append(
            {
                "source_id": payload["library_id"],
                "source_yaml_sha256": fingerprint(registration).sha256,
                "assets": assets,
            }
        )
    expected = hashlib.sha256(
        json.dumps(rows, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()

    assert (
        _collect_private_source_state(
            fixture.registration_root, fixture.asset_root
        ).source_integrity_sha256
        == expected
    )


def test_candidate_verifier_rejects_unmanifested_file(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    result = fixture.builder().build(tmp_path / "candidate")
    (result.candidate_root / "rogue.txt").write_text("not in the manifest", encoding="utf-8")

    with pytest.raises(PublicReleaseValidationError, match="not allowlisted"):
        verify_public_release(result.candidate_root)


@pytest.mark.parametrize("identifier_kind", ["record", "relationship"])
def test_build_rejects_private_identifier_in_candidate_filename(
    tmp_path: Path,
    identifier_kind: str,
) -> None:
    fixture = _release_fixture(tmp_path)
    graph = _read_json(fixture.graph_path)
    if identifier_kind == "record":
        nodes = graph["nodes"]
        assert isinstance(nodes, list)
        node = nodes[1]
        assert isinstance(node, dict)
        private_identifier = node["id"]
    else:
        edges = graph["edges"]
        assert isinstance(edges, list)
        edge = edges[-1]
        assert isinstance(edge, dict)
        private_identifier = edge["relationship_id"]
    assert isinstance(private_identifier, str)
    leak = fixture.repository_root / "docs/architecture" / f"{private_identifier}.md"
    leak.write_text("# Otherwise safe content\n", encoding="utf-8")
    fixture.commit("add private identifier filename fixture")

    with pytest.raises(PublicReleaseValidationError, match="private graph text"):
        fixture.builder().build(tmp_path / "candidate")


def test_candidate_verifier_rejects_root_transient_directory(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    result = fixture.builder().build(tmp_path / "candidate")
    transient = result.candidate_root / "dist/private.pdf"
    transient.parent.mkdir()
    transient.write_bytes(PDF_PREFIX + b"-1.7\nprivate payload\n")

    with pytest.raises(PublicReleaseValidationError, match="forbidden path segment"):
        verify_public_release(result.candidate_root)


def test_candidate_verifier_rejects_unverified_root_git_metadata_file(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    result = fixture.builder().build(tmp_path / "candidate")
    (result.candidate_root / ".git").write_text(
        "gitdir: ../linked-worktree-metadata\n",
        encoding="utf-8",
    )

    with pytest.raises(PublicReleaseValidationError, match="unverified Git metadata"):
        verify_public_release(result.candidate_root)


def test_candidate_verifier_rejects_deleted_private_file_in_git_history(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    candidate = fixture.builder().build(tmp_path / "candidate").candidate_root
    hidden = candidate / "private.pdf"
    hidden.write_bytes(PDF_PREFIX + b"-1.7\nprivate historical payload\n")
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
    }
    for arguments in (
        ("init", "-q"),
        ("config", "user.name", "Fixture"),
        ("config", "user.email", "fixture@example.invalid"),
        ("add", "."),
        ("-c", "commit.gpgsign=false", "commit", "-q", "-m", "unsafe root"),
    ):
        subprocess.run(("git", "-C", str(candidate), *arguments), check=True, env=env)
    hidden.unlink()
    subprocess.run(("git", "-C", str(candidate), "add", "-A"), check=True, env=env)
    subprocess.run(
        (
            "git",
            "-C",
            str(candidate),
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "-m",
            "hide private history",
        ),
        check=True,
        env=env,
    )

    with pytest.raises(PublicReleaseValidationError, match="exactly one root commit"):
        verify_public_release(candidate)


def test_candidate_verifier_rejects_incomplete_command_catalog(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    result = fixture.builder().build(tmp_path / "candidate")
    command_path = result.candidate_root / "reference_mapping_graph/commands.json"
    catalog = _read_json(command_path)
    commands = catalog["commands"]
    assert isinstance(commands, list)
    catalog["commands"] = [command for command in commands if command["name"] == "test"]
    _write_json(command_path, catalog)
    _refresh_candidate_manifest(result.candidate_root)

    with pytest.raises(
        PublicReleaseValidationError,
        match="schema validation failed|missing one or more documented commands",
    ):
        verify_public_release(result.candidate_root)


def test_candidate_verifier_rejects_changed_command_contract(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    result = fixture.builder().build(tmp_path / "candidate")
    command_path = result.candidate_root / "reference_mapping_graph/commands.json"
    catalog = _read_json(command_path)
    commands = catalog["commands"]
    assert isinstance(commands, list)
    test_command = next(command for command in commands if command["name"] == "test")
    test_command["argv"] = ["uv", "run", "pytest", "--disable-warnings"]
    _write_json(command_path, catalog)
    _refresh_candidate_manifest(result.candidate_root)

    with pytest.raises(PublicReleaseValidationError, match="command catalog contract"):
        verify_public_release(result.candidate_root)


def test_candidate_verifier_rejects_broken_markdown_link(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    result = fixture.builder().build(tmp_path / "candidate")
    readme = result.candidate_root / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8") + "\n[Missing](missing.md)\n")

    with pytest.raises(PublicReleaseValidationError, match="Markdown link is broken"):
        verify_public_release(result.candidate_root)


def test_candidate_verifier_rejects_directory_symlink(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    result = fixture.builder().build(tmp_path / "candidate")
    (result.candidate_root / "rogue-link").symlink_to(tmp_path, target_is_directory=True)

    with pytest.raises(PublicReleaseValidationError, match="symbolic link"):
        verify_public_release(result.candidate_root)


def test_candidate_verifier_cross_checks_manifest_provenance(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    result = fixture.builder().build(tmp_path / "candidate")
    manifest = _read_json(result.candidate_root / MANIFEST_PATH)
    private_inputs = manifest["private_inputs"]
    assert isinstance(private_inputs, dict)
    private_inputs["graph_sha256"] = "0" * 64
    _write_json(result.candidate_root / MANIFEST_PATH, manifest)

    with pytest.raises(PublicReleaseValidationError, match="provenance differs"):
        verify_public_release(result.candidate_root)


def test_candidate_verifier_cross_checks_private_report_provenance(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    result = fixture.builder().build(tmp_path / "candidate")
    report_path = (
        result.candidate_root / "reference_mapping_graph/baseline-v1/publication-report.json"
    )
    report = _read_json(report_path)
    private_inputs = report["private_inputs"]
    assert isinstance(private_inputs, dict)
    private_inputs["private_report_sha256"] = "0" * 64
    _write_json(report_path, report)
    _refresh_candidate_manifest(result.candidate_root)

    with pytest.raises(PublicReleaseValidationError, match="provenance differs"):
        verify_public_release(result.candidate_root)


def test_candidate_verifier_cross_checks_withheld_asset_count(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    result = fixture.builder().build(tmp_path / "candidate")
    report_path = result.candidate_root / REPORT_PATH
    report = _read_json(report_path)
    withheld = report["withheld"]
    assert isinstance(withheld, dict)
    withheld["source_assets"] = 0
    _write_json(report_path, report)
    _refresh_candidate_manifest(result.candidate_root)

    with pytest.raises(PublicReleaseValidationError, match="withheld asset count"):
        verify_public_release(result.candidate_root)


@pytest.mark.parametrize(
    ("field", "unsafe_value"),
    [
        ("schema_version", "2.0"),
        ("baseline_id", "unrelated-baseline"),
        ("scope", "full_evidence"),
        ("experimental_status", "scientifically_validated"),
        ("qualification", "These counts establish correctness and consensus."),
    ],
)
def test_candidate_verifier_enforces_fixed_aggregate_metadata(
    tmp_path: Path,
    field: str,
    unsafe_value: str,
) -> None:
    fixture = _release_fixture(tmp_path)
    result = fixture.builder().build(tmp_path / "candidate")
    metrics = _read_json(result.candidate_root / METRICS_PATH)
    metrics[field] = unsafe_value
    _write_json(result.candidate_root / METRICS_PATH, metrics)
    _refresh_candidate_manifest(result.candidate_root)

    with pytest.raises(
        PublicReleaseValidationError,
        match="schema validation failed|unsafe fixed metadata",
    ):
        verify_public_release(result.candidate_root)


def test_candidate_verifier_rejects_unknown_input_lock_field(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    result = fixture.builder().build(tmp_path / "candidate")
    input_lock_path = result.candidate_root / "reference_mapping_graph/baseline-v1/input-lock.json"
    input_lock = _read_json(input_lock_path)
    input_lock["operator_note"] = "An unbound candidate field must be rejected."
    _write_json(input_lock_path, input_lock)
    _refresh_candidate_manifest(result.candidate_root)

    with pytest.raises(PublicReleaseValidationError, match="schema validation failed"):
        verify_public_release(result.candidate_root)


def test_candidate_verifier_wraps_invalid_schema_definition(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    result = fixture.builder().build(tmp_path / "candidate")
    commands_schema_path = result.candidate_root / "schemas/public-release/v1/commands.schema.json"
    _write_json(commands_schema_path, {"type": 7})
    _refresh_candidate_manifest(result.candidate_root)

    with pytest.raises(PublicReleaseValidationError, match="schema validation failed"):
        verify_public_release(result.candidate_root)

    command_result = dispatch(
        build_parser().parse_args(
            [
                "verify-public-release",
                "--candidate-root",
                str(result.candidate_root),
                "--json",
            ]
        )
    )
    assert command_result.ok is False
    assert command_result.classification.value == "validation_failure"
    assert "schema validation failed" in command_result.errors[0]


@pytest.mark.parametrize("structured_path", [MANIFEST_PATH, SOURCE_CATALOG_PATH])
def test_candidate_verifier_wraps_invalid_utf8_structured_input(
    tmp_path: Path,
    structured_path: Path,
) -> None:
    fixture = _release_fixture(tmp_path)
    result = fixture.builder().build(tmp_path / "candidate")
    (result.candidate_root / structured_path).write_bytes(NON_UTF8_BYTE)

    command_result = dispatch(
        build_parser().parse_args(
            [
                "verify-public-release",
                "--candidate-root",
                str(result.candidate_root),
                "--json",
            ]
        )
    )

    assert command_result.ok is False
    assert command_result.classification.value == "validation_failure"
    assert "cannot read" in command_result.errors[0] or "non-UTF-8" in command_result.errors[0]


def test_candidate_verifier_validates_policy_and_catalog_fields(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    invalid_policy = fixture.builder().build(tmp_path / "candidate-policy")
    policy_path = invalid_policy.candidate_root / "reference_mapping_graph/publication-policy.yaml"
    policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    policy["external_actions_authorized"] = True
    policy_path.write_text(yaml.safe_dump(policy, sort_keys=False), encoding="utf-8")
    _refresh_candidate_manifest(invalid_policy.candidate_root)
    with pytest.raises(PublicReleaseValidationError, match="schema validation failed"):
        verify_public_release(invalid_policy.candidate_root)

    invalid_catalog = fixture.builder().build(tmp_path / "candidate-catalog")
    catalog_path = invalid_catalog.candidate_root / SOURCE_CATALOG_PATH
    catalog = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    catalog["sources"][0]["asset_sha256"] = "a" * 64
    catalog_path.write_text(yaml.safe_dump(catalog, sort_keys=False), encoding="utf-8")
    _refresh_candidate_manifest(invalid_catalog.candidate_root)
    with pytest.raises(PublicReleaseValidationError, match="field allowlist"):
        verify_public_release(invalid_catalog.candidate_root)

    invalid_value = fixture.builder().build(tmp_path / "candidate-catalog-value")
    value_catalog_path = invalid_value.candidate_root / SOURCE_CATALOG_PATH
    value_catalog = yaml.safe_load(value_catalog_path.read_text(encoding="utf-8"))
    value_catalog["sources"][0]["title"] = {
        "display": "Synthetic source A",
        "private_note": "Private nested prose.",
    }
    value_catalog_path.write_text(yaml.safe_dump(value_catalog, sort_keys=False), encoding="utf-8")
    _refresh_candidate_manifest(invalid_value.candidate_root)
    with pytest.raises(PublicReleaseValidationError, match="invalid title"):
        verify_public_release(invalid_value.candidate_root)


def test_candidate_verifier_requires_verifier_script(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    result = fixture.builder().build(tmp_path / "candidate")
    (result.candidate_root / "scripts/verify-public-release.sh").unlink()
    _refresh_candidate_manifest(result.candidate_root)

    with pytest.raises(PublicReleaseValidationError, match="public surface is incomplete"):
        verify_public_release(result.candidate_root)


def test_cli_human_and_json_results_share_identifiers(tmp_path: Path) -> None:
    fixture = _release_fixture(tmp_path)
    parser = build_parser()
    result = dispatch(parser.parse_args(fixture.cli_arguments(tmp_path / "candidate")))

    assert result.ok is True
    payload = json.loads(result.to_json())
    human = _human_output(result)
    assert payload["identifiers"]["tree_sha256"] in human
    assert str(payload["data"]["file_count"]) in human


def _release_fixture(tmp_path: Path) -> ReleaseFixture:
    repository_root = tmp_path / "private-repository"
    registration_root = repository_root / "sources"
    asset_root = tmp_path / "asset-root"
    private_root = tmp_path / "private-inputs"
    for path in (
        repository_root / "src/research_map",
        repository_root / "schemas/public-release/v1",
        repository_root / "schemas/research-map/v1",
        repository_root / "reference_mapping_graph/baseline-v1",
        repository_root / "reference_mapping_graph/synthetic-example",
        repository_root / "docs/architecture",
        repository_root / "docs/experiments",
        repository_root / "docs/limitations",
        repository_root / "docs/public-roadmap",
        repository_root / "scripts",
        repository_root / "vault/sources/LIB-991",
        registration_root / "library/source-a",
        registration_root / "library/source-b",
        asset_root / "sources/library/source-a",
        asset_root / "sources/library/source-b",
        private_root / "graph",
    ):
        path.mkdir(parents=True, exist_ok=True)
    (repository_root / "README.md").write_text("# Safe fixture\n", encoding="utf-8")
    (repository_root / ".gitattributes").write_text("* text=auto eol=lf\n", encoding="utf-8")
    (repository_root / ".gitignore").write_text(".venv/\n__pycache__/\n", encoding="utf-8")
    (repository_root / "pyproject.toml").write_text(
        '[project]\nname = "safe-fixture"\nversion = "0.0.0"\n', encoding="utf-8"
    )
    (repository_root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (repository_root / "src/research_map/demo.py").write_text(
        '"""Safe public code."""\n', encoding="utf-8"
    )
    vault_record_path = repository_root / "vault/sources/LIB-991/paper.md"
    vault_record_path.write_text(
        "# Private vault fixture\n\n"
        "A private synthetic dossier used only for release-boundary tests.\n\n"
        "## Source-local research map\n\n"
        "```research-map\n"
        "schema_version: 1\n"
        "record_type: evidence\n"
        "id: LIB-991:evidence:vault-only\n"
        "revision: 1\n"
        "source_id: LIB-991\n"
        "asset_sha256: " + "a" * 64 + "\n"
        "page_start: 1\n"
        "page_end: 1\n"
        "locator: Private fixture\n"
        f"exact_span: {VAULT_PRIVATE_EVIDENCE}\n"
        "```\n",
        encoding="utf-8",
    )
    vault_document = load_markdown(vault_record_path)
    vault_record_path.write_text(
        render_markdown(vault_document.dossier),
        encoding="utf-8",
    )
    relationship_path = (
        repository_root / "vault/relationships" / FIXTURE_PAIR_DIRECTORY / "relationships.md"
    )
    relationship_path.parent.mkdir(parents=True)
    relationship_path.write_text(
        render_relationship_markdown(
            ("LIB-991", "LIB-992"),
            (_fixture_relationship(),),
        ),
        encoding="utf-8",
    )
    for name in ("LICENSE", "NOTICE", "CITATION.cff", "CONTRIBUTING.md", "SECURITY.md"):
        (repository_root / name).write_text(f"Safe fixture {name}.\n", encoding="utf-8")
    (repository_root / "THIRD_PARTY_RIGHTS.md").write_text("# Fixture rights\n", encoding="utf-8")
    verifier_script = repository_root / "scripts/verify-public-release.sh"
    verifier_script.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    verifier_script.chmod(0o755)
    for relative in (
        "docs/architecture/README.md",
        "docs/experiments/README.md",
        "docs/limitations/README.md",
        "docs/public-roadmap/README.md",
        "reference_mapping_graph/README.md",
        "reference_mapping_graph/baseline-v1/README.md",
        "reference_mapping_graph/synthetic-example/README.md",
    ):
        (repository_root / relative).write_text("# Safe fixture\n", encoding="utf-8")
    shutil.copyfile(
        Path(__file__).parents[2] / "reference_mapping_graph/commands.json",
        repository_root / "reference_mapping_graph/commands.json",
    )

    asset_a = asset_root / "sources/library/source-a/a.pdf"
    asset_b = asset_root / "sources/library/source-b/b.pdf"
    asset_a.write_bytes(PDF_PREFIX + b"-1.7\nsynthetic-a\n")
    asset_b.write_bytes(PDF_PREFIX + b"-1.7\nsynthetic-b\n")
    _write_source_registration(
        registration_root / "library/source-a/source.yaml",
        source_id="LIB-991",
        title="Synthetic source A",
        rights="".join(("The PDF states ", "CC-BY 4.0.")),
        asset_relative="sources/library/source-a/a.pdf",
        asset_path=asset_a,
    )
    _write_source_registration(
        registration_root / "library/source-b/source.yaml",
        source_id="LIB-992",
        title="Synthetic source B",
        rights="".join(("Local possession only; ", "redistribution rights were ", "not assessed.")),
        asset_relative="sources/library/source-b/b.pdf",
        asset_path=asset_b,
    )
    frozen_commit = _initialize_git(repository_root)

    schema_root = Path(__file__).parents[2] / "schemas/public-release/v1"
    for name in (
        "commands.schema.json",
        "input-lock.schema.json",
        "publication-policy.schema.json",
        "public-release-manifest.schema.json",
        "publication-report.schema.json",
    ):
        shutil.copyfile(schema_root / name, repository_root / "schemas/public-release/v1" / name)
    research_schema_root = Path(__file__).parents[2] / "schemas/research-map/v1"
    for name in (
        "atom.schema.json",
        "corpus-graph.schema.json",
        "evidence.schema.json",
        "relationship.schema.json",
    ):
        shutil.copyfile(
            research_schema_root / name,
            repository_root / "schemas/research-map/v1" / name,
        )
    policy = {
        "schema_version": "1.0",
        "policy_id": "public-experimental-repository-baseline-v1",
        "repository_topology": "separate_clean_history_projection",
        "public_data_scope": "aggregate_results_plus_synthetic_demonstration",
        "original_code_and_documentation_license": "Apache-2.0",
        "dataset_license": "none_granted",
        "third_party_rights": "separate",
        "external_actions_authorized": False,
        "admitted_source_fields": [
            "library_id",
            "identity.title",
            "identity.creators",
            "identity.year",
            "identity.type",
            "identity.language",
            "normalized_rights_category",
        ],
        "admitted_graph_fields": [
            "aggregate_counts",
            "aggregate_source_quality_label_counts",
            "aggregate_relationship_kind_counts",
        ],
        "withheld_graph_fields": [
            "nodes",
            "endpoints",
            "source_pairs",
            "statements",
            "evidence_spans",
            "rationales",
            "scopes",
            "qualifications",
        ],
        "forbidden_path_segments": [
            ".basecamp",
            ".cache",
            ".git",
            ".project",
            "build",
            "sources",
            "vault",
        ],
        "forbidden_suffixes": [
            ".pdf",
            ".pyc",
            ".pyo",
            ".sqlite",
            ".sqlite3",
            ".duckdb",
            ".pem",
            ".key",
            ".p12",
            ".pfx",
        ],
        "forbidden_name_patterns": [".env*"],
        "public_path_allowlist": [
            ".gitattributes",
            ".gitignore",
            "README.md",
            "LICENSE",
            "NOTICE",
            "CITATION.cff",
            "CONTRIBUTING.md",
            "SECURITY.md",
            "THIRD_PARTY_RIGHTS.md",
            "pyproject.toml",
            "uv.lock",
            "src/research_map/**",
            "schemas/**",
            "protocols/research-map/**",
            "docs/architecture/**",
            "docs/experiments/**",
            "docs/images/dogfood/corpus-overview.png",
            "docs/images/dogfood/record-graph.png",
            "docs/images/dogfood/tension-lens.png",
            "docs/images/dogfood/source-detail.png",
            "docs/limitations/**",
            "docs/public-roadmap/**",
            "docs/publication/PUBLICATION_POLICY.md",
            "reference_mapping_graph/README.md",
            "reference_mapping_graph/commands.json",
            "reference_mapping_graph/publication-policy.yaml",
            "reference_mapping_graph/rights-inventory.yaml",
            "reference_mapping_graph/baseline-v1/README.md",
            "reference_mapping_graph/baseline-v1/input-lock.json",
            "reference_mapping_graph/baseline-v1/aggregate-metrics.json",
            "reference_mapping_graph/baseline-v1/relationship-summary.json",
            "reference_mapping_graph/baseline-v1/source-catalog.yaml",
            "reference_mapping_graph/baseline-v1/publication-report.json",
            "reference_mapping_graph/baseline-v1/manifest.json",
            "reference_mapping_graph/synthetic-example/README.md",
            "reference_mapping_graph/synthetic-example/graph.json",
            "reference_mapping_graph/synthetic-example/manifest.json",
            (
                "reference_mapping_graph/synthetic-example/relationships/"
                "LIB-901--LIB-902/relationships.md"
            ),
            "scripts/verify-public-release.sh",
            "tests/public_release_candidate/**",
        ],
    }
    policy_path = repository_root / "reference_mapping_graph/publication-policy.yaml"
    policy_path.write_text(yaml.safe_dump(policy, sort_keys=False), encoding="utf-8")
    rights_inventory = {
        "schema_version": "1.0",
        "inventory_id": "fixture-rights",
        "source_count": 2,
        "asset_count": 2,
        "categories": {
            "recorded_cc_by_4_0": {
                "source_count": 1,
                "source_ids": ["LIB-991"],
                "release_posture": "metadata_only_in_baseline",
            },
            "redistribution_not_assessed": {
                "source_count": 1,
                "source_ids": ["LIB-992"],
                "release_posture": "metadata_only_in_baseline",
            },
        },
        "notes": ["Synthetic fixture."],
    }
    (repository_root / "reference_mapping_graph/rights-inventory.yaml").write_text(
        yaml.safe_dump(rights_inventory, sort_keys=False), encoding="utf-8"
    )

    graph = _private_graph()
    graph_path = private_root / "graph/graph.json"
    _write_json(graph_path, graph)
    graph_descriptor = _descriptor(graph_path)
    graph_descriptor["path"] = "graph/graph.json"
    graph_manifest = {
        "schema_version": "1.0",
        "kind": "deterministic_recovery_salvage_graph_manifest",
        "sources": ["LIB-991", "LIB-992"],
        "corpus_graph": graph_descriptor,
        "node_count": 4,
        "source_local_edge_count": 2,
        "cross_source_edge_count": 1,
        "quality_label_counts": {"unaudited_provisional": 2},
    }
    graph_manifest_path = private_root / "graph/manifest.json"
    _write_json(graph_manifest_path, graph_manifest)
    graph_manifest_descriptor = _descriptor(graph_manifest_path)
    private_report = {
        "schema_version": "1.0",
        "salvage_id": "fixture-salvage-v1",
        "state": "complete",
        "graph": graph_descriptor,
        "graph_manifest": graph_manifest_descriptor,
        "pair_partitions": {
            "origin_inspected": [["LIB-991", "LIB-992"]],
            "salvaged_inspected": [],
            "remaining_unresolved": [],
        },
    }
    report_path = private_root / "report.json"
    _write_json(report_path, private_report)
    source_state = _collect_private_source_state(registration_root, asset_root)
    input_lock = {
        "schema_version": "1.0",
        "repository_topology": "separate_clean_history_projection",
        "public_data_scope": "aggregate_results_plus_synthetic_demonstration",
        "external_actions_authorized": False,
        "private_repository_commit": frozen_commit,
        "baseline": {
            "salvage_id": "fixture-salvage-v1",
            "graph": _descriptor(graph_path),
            "graph_manifest": _descriptor(graph_manifest_path),
            "private_report": _descriptor(report_path),
        },
        "source_integrity": {
            "algorithm": "fixture",
            "sha256": source_state.source_integrity_sha256,
        },
        "counts": {
            "registered_sources": 2,
            "registered_assets": 2,
            "sources_in_baseline": 2,
            "records": 4,
            "evidence_records": 2,
            "atoms": 2,
            "moves": 0,
            "threads": 0,
            "source_local_edges": 2,
            "cross_source_relationships": 1,
            "inspected_source_pairs": 1,
            "unresolved_source_pairs": 0,
        },
    }
    input_lock_path = repository_root / "reference_mapping_graph/baseline-v1/input-lock.json"
    _write_json(input_lock_path, input_lock)
    _commit_repository(repository_root, "complete public release fixture")
    return ReleaseFixture(
        repository_root=repository_root,
        registration_root=registration_root,
        asset_root=asset_root,
        graph_path=graph_path,
        graph_manifest_path=graph_manifest_path,
        report_path=report_path,
        policy_path=policy_path,
        input_lock_path=input_lock_path,
    )


def _write_source_registration(
    path: Path,
    *,
    source_id: str,
    title: str,
    rights: str,
    asset_relative: str,
    asset_path: Path,
) -> None:
    observed = fingerprint(asset_path)
    payload = {
        "library_id": source_id,
        "identity": {
            "title": title,
            "creators": ["Example Author"],
            "year": 2026,
            "type": "synthetic article",
            "language": "English",
        },
        "assets": [
            {
                "path": asset_relative,
                "format": "pdf",
                "asset_role": "main",
                "notes": (
                    f"Synthetic fixture; SHA-256 {observed.sha256}; {observed.size_bytes} bytes."
                ),
            }
        ],
        "provenance": {"rights_notes": rights},
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _private_graph() -> dict[str, object]:
    return {
        "schema_version": 1,
        "batch_id": "xref_batch_aaaaaaaaaaaaaaaaaaaaaaaa",
        "sources": ["LIB-991", "LIB-992"],
        "nodes": [
            {
                "id": "LIB-991:evidence:e1",
                "source_id": "LIB-991",
                "record_type": "evidence",
                "revision": 1,
                "payload": {"exact_span": PRIVATE_EVIDENCE},
            },
            {
                "id": "LIB-991:atom:a1",
                "source_id": "LIB-991",
                "record_type": "atom",
                "revision": 1,
                "payload": {
                    "statement": (
                        "A private source-derived statement that remains withheld from release."
                    )
                },
            },
            {
                "id": "LIB-992:evidence:e1",
                "source_id": "LIB-992",
                "record_type": "evidence",
                "revision": 1,
                "payload": {
                    "exact_span": (
                        "A second distinctive synthetic private span used only to test withholding."
                    )
                },
            },
            {
                "id": "LIB-992:atom:a1",
                "source_id": "LIB-992",
                "record_type": "atom",
                "revision": 1,
                "payload": {
                    "statement": (
                        "Another private source-derived statement that stays outside the candidate."
                    )
                },
            },
        ],
        "edges": [
            {
                "source": "LIB-991:evidence:e1",
                "target": "LIB-991:atom:a1",
                "kind": "supports",
                "origin": "source_local",
                "relationship_id": None,
            },
            {
                "source": "LIB-992:evidence:e1",
                "target": "LIB-992:atom:a1",
                "kind": "supports",
                "origin": "source_local",
                "relationship_id": None,
            },
            {
                "source": "LIB-991:atom:a1",
                "target": "LIB-992:atom:a1",
                "kind": "potential_support",
                "origin": "cross_source",
                "relationship_id": "xref_rel_aaaaaaaaaaaaaaaaaaaaaaaa",
            },
        ],
    }


def _fixture_relationship() -> Relationship:
    left_identity = ("LIB-991:atom:a1", 1, "a" * 64)
    right_identity = ("LIB-992:atom:a1", 1, "b" * 64)
    left_endpoint = {
        "source_id": "LIB-991",
        "record_id": left_identity[0],
        "revision": left_identity[1],
        "record_sha256": left_identity[2],
    }
    right_endpoint = {
        "source_id": "LIB-992",
        "record_id": right_identity[0],
        "revision": right_identity[1],
        "record_sha256": right_identity[2],
    }
    relationship_id = cross_reference_relationship_id(
        left_identity,
        right_identity,
        relation_type="potential_support",
        direction="left_to_right",
    )
    return Relationship.from_mapping(
        {
            "schema_version": 1,
            "relationship_id": relationship_id,
            "revision": 1,
            "candidate_id": "xref_candidate_" + "a" * 24,
            "batch_id": "xref_batch_" + "b" * 24,
            "relation_type": "potential_support",
            "direction": "left_to_right",
            "left_endpoint": left_endpoint,
            "right_endpoint": right_endpoint,
            "left_evidence_ids": ["LIB-991:evidence:e1"],
            "right_evidence_ids": ["LIB-992:evidence:e1"],
            "comparison_surface": "A private comparison surface for vault scanning.",
            "scope_alignment": "The private fixture scopes overlap only for scanner tests.",
            "assumption_alignment": "The private fixture assumptions remain unreviewed.",
            "rationale": VAULT_PRIVATE_RELATIONSHIP,
            "qualifications": ["This relationship is synthetic and private."],
            "provenance": {
                "inspection_job_id": "xref_job_" + "c" * 24,
                "model": "fixture-model",
                "protocol_sha256": "d" * 64,
                "schema_sha256": "e" * 64,
                "input_sha256": "f" * 64,
            },
        }
    )


def _initialize_git(repository_root: Path) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
    }
    for arguments in (
        ("init", "-q"),
        ("config", "user.name", "Fixture"),
        ("config", "user.email", "fixture@example.invalid"),
        (
            "config",
            "remote.origin.url",
            "https://github.com/" + FIXTURE_PRIVATE_REPOSITORY + ".git",
        ),
        ("add", "."),
        ("-c", "commit.gpgsign=false", "commit", "-q", "-m", "frozen private input"),
    ):
        subprocess.run(("git", "-C", str(repository_root), *arguments), check=True, env=env)
    return subprocess.run(
        ("git", "-C", str(repository_root), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit_repository(repository_root: Path, message: str) -> str:
    # Every fixture revision, not just its first commit, must have stable
    # identity. Wall-clock hashes can alter the metadata scanned by each test.
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
    }
    subprocess.run(("git", "-C", str(repository_root), "add", "-A"), check=True)
    subprocess.run(
        (
            "git",
            "-C",
            str(repository_root),
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "-m",
            message,
        ),
        check=True,
        env=env,
    )
    return subprocess.run(
        ("git", "-C", str(repository_root), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _descriptor(path: Path) -> dict[str, object]:
    value = fingerprint(path)
    return {"sha256": value.sha256, "size_bytes": value.size_bytes}


def _refresh_locked_private_descriptors(fixture: ReleaseFixture) -> None:
    graph_manifest = _read_json(fixture.graph_manifest_path)
    graph_descriptor = _descriptor(fixture.graph_path)
    graph_descriptor["path"] = "graph/graph.json"
    graph_manifest["corpus_graph"] = graph_descriptor
    _write_json(fixture.graph_manifest_path, graph_manifest)
    report = _read_json(fixture.report_path)
    report["graph"] = _descriptor(fixture.graph_path)
    report["graph_manifest"] = _descriptor(fixture.graph_manifest_path)
    _write_json(fixture.report_path, report)
    input_lock = _read_json(fixture.input_lock_path)
    baseline = input_lock["baseline"]
    assert isinstance(baseline, dict)
    baseline["graph"] = _descriptor(fixture.graph_path)
    baseline["graph_manifest"] = _descriptor(fixture.graph_manifest_path)
    baseline["private_report"] = _descriptor(fixture.report_path)
    _write_json(fixture.input_lock_path, input_lock)


def _refresh_candidate_manifest(candidate_root: Path) -> None:
    manifest = _read_json(candidate_root / MANIFEST_PATH)
    descriptors = []
    for path in sorted(candidate_root.rglob("*")):
        if not path.is_file() or path == candidate_root / MANIFEST_PATH:
            continue
        value = fingerprint(path, relative_to=candidate_root)
        descriptors.append(
            {
                **value.to_dict(),
                "mode": "100755" if path.stat().st_mode & 0o100 else "100644",
            }
        )
    manifest["files"] = descriptors
    manifest["tree_sha256"] = fingerprint_bytes(descriptors)
    _write_json(candidate_root / MANIFEST_PATH, manifest)


def fingerprint_bytes(descriptors: list[dict[str, object]]) -> str:
    import hashlib

    data = json.dumps(descriptors, sort_keys=True, separators=(",", ":")) + "\n"
    return hashlib.sha256(data.encode()).hexdigest()


def _private_fingerprints(fixture: ReleaseFixture) -> dict[str, tuple[str, int]]:
    paths = {
        "graph": fixture.graph_path,
        "manifest": fixture.graph_manifest_path,
        "report": fixture.report_path,
    }
    return {
        key: (fingerprint(path).sha256, fingerprint(path).size_bytes) for key, path in paths.items()
    }


def _tree_bytes(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_mode & 0o777)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value
