"""Deterministic, fail-closed construction of the public repository projection."""

from __future__ import annotations

import ast
import base64
import binascii
import fnmatch
import hashlib
import html
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unicodedata
import urllib.parse
from collections import Counter, deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, cast

import jsonschema  # type: ignore[import-untyped]
import yaml  # type: ignore[import-untyped]

from research_map.cross_reference import (
    RELATIONSHIP_TYPES,
    EndpointRef,
    Relationship,
    record_fingerprint,
)
from research_map.exploration import GraphExplorer
from research_map.ids import (
    cross_reference_candidate_id,
    cross_reference_relationship_id,
)
from research_map.markdown import MarkdownRecordError, load_markdown, render_markdown
from research_map.receipts import canonical_json_bytes, fingerprint
from research_map.records import Record
from research_map.relationship_markdown import (
    load_relationship_markdown,
    render_relationship_markdown,
)

RELEASE_ID = "public-experimental-repository-baseline-v1"
MANIFEST_PATH = Path("reference_mapping_graph/baseline-v1/manifest.json")
REPORT_PATH = Path("reference_mapping_graph/baseline-v1/publication-report.json")
METRICS_PATH = Path("reference_mapping_graph/baseline-v1/aggregate-metrics.json")
RELATIONSHIP_SUMMARY_PATH = Path("reference_mapping_graph/baseline-v1/relationship-summary.json")
SOURCE_CATALOG_PATH = Path("reference_mapping_graph/baseline-v1/source-catalog.yaml")
SYNTHETIC_GRAPH_PATH = Path("reference_mapping_graph/synthetic-example/graph.json")
SYNTHETIC_MANIFEST_PATH = Path("reference_mapping_graph/synthetic-example/manifest.json")
SYNTHETIC_RELATIONSHIP_PATH = Path(
    "reference_mapping_graph/synthetic-example/relationships/LIB-901--LIB-902/relationships.md"
)
COMMANDS_PATH = Path("reference_mapping_graph/commands.json")
POLICY_PATH = Path("reference_mapping_graph/publication-policy.yaml")
RIGHTS_INVENTORY_PATH = Path("reference_mapping_graph/rights-inventory.yaml")
INPUT_LOCK_PATH = Path("reference_mapping_graph/baseline-v1/input-lock.json")
_PUBLIC_MANIFEST_SCHEMA = Path("schemas/public-release/v1/public-release-manifest.schema.json")
_PUBLIC_REPORT_SCHEMA = Path("schemas/public-release/v1/publication-report.schema.json")
_PUBLIC_POLICY_SCHEMA = Path("schemas/public-release/v1/publication-policy.schema.json")
_PUBLIC_COMMANDS_SCHEMA = Path("schemas/public-release/v1/commands.schema.json")
_PUBLIC_INPUT_LOCK_SCHEMA = Path("schemas/public-release/v1/input-lock.schema.json")
_MAX_ENCODED_PAYLOAD_CHARACTERS = 64 * 1024 * 1024
_MAX_DECODED_PAYLOADS_PER_FILE = 4096
_MAX_DECODED_PAYLOAD_BYTES_PER_FILE = 64 * 1024 * 1024
_NUL_BYTE = bytes((0,))
_PDF_HEADER = bytes((37, 80, 68, 70, 45))
_SQLITE_HEADER = "SQLite format 3".encode("ascii") + _NUL_BYTE
_BINARY_SIGNATURES = (
    bytes((80, 75, 3, 4)),
    bytes((80, 75, 5, 6)),
    bytes((80, 75, 7, 8)),
    bytes((31, 139)),
    bytes((137, 80, 78, 71, 13, 10, 26, 10)),
    bytes((255, 216, 255)),
    bytes((127, 69, 76, 70)),
    bytes((77, 90)),
)
_PUBLIC_GITIGNORE_ROOT_ANCHORS = frozenset(
    {".cache", "ai-dev-pack", "build", "dist", "indexes", "sources"}
)
_ASSET_FINGERPRINT = re.compile(r"SHA-256\s+([0-9a-f]{64});\s+(\d+)\s+bytes")
_PRIVATE_TOPOLOGY_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])LIB-[0-9]{3,}(?::[A-Za-z0-9_.:-]+|--LIB-[0-9]{3,})"
    r"(?![A-Za-z0-9])"
)
_JSON_UNICODE_ESCAPE_RUN = re.compile(r"(?:\\u[0-9A-Fa-f]{4})+")
_TEXT_ESCAPE = re.compile(r"\\(?:[tnr]|x[0-9A-Fa-f]{2})")
_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
_PRIVATE_TASK_IDENTIFIER = re.compile(
    r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}(?![0-9A-Fa-f])"
)
_INTERNAL_IDENTIFIER = re.compile(r"(?=.{4,}$)(?=.*[A-Za-z])(?=.*[0-9])[A-Za-z0-9_.:/-]+")
_SOURCE_QUALITY_LABELS = frozenset(
    {
        "audit_interrupted",
        "audited_passed",
        "audited_with_findings",
        "unaudited_provisional",
    }
)
_RIGHTS_RELEASE_POSTURES = {
    "recorded_cc_by_4_0": "metadata_only_in_baseline",
    "redistribution_not_assessed": "metadata_only_in_baseline",
}
_SAFE_OPERATIONAL_PRIVATE_TEXT = frozenset(
    {
        "(1)",
        "1.",
        "2.",
        "3.",
        "4.",
        "5.",
        "6.",
        "7.",
        "9.",
        "11.",
        "12.",
        "aligned.",
        "assumption " + "surfaced",
        "Appendix B",
        "both records address " + "the same",
        "Calibration coordinator recorded " + "source-native model",
        "comparison surface disposition does " + "not match proposed relationships",
        "conclusion",
        "coordinator recorded source-native " + "model vocabulary",
        "recorded source-native model " + "vocabulary that",
        "Read AGENTS.md, input/task.json, and every record in input/corpus-records.json.",
        "corrected proposal retained fatal " + "validation findings",
        "definition",
        "discussion",
        "equation (1)",
        "every source pair exactly " + "once",
        "fatal",
        "final conclusion",
        "interpretation.",
        "multi-surface proposal did not account for every " + "source pair exactly once",
        "source-native model vocabulary " + "that omitted",
        "model vocabulary that " + "omitted its",
        "vocabulary that omitted " + "its required",
        "that omitted its " + "required administrative",
        "omitted its required " + "administrative type-gap",
        "its required administrative " + "type-gap declaration.",
        "pair surface disposition does " + "not match proposed relationships",
        "paper-wide",
        "provisional calibration projection; source " + "audit was not claimed or run",
        "relationship",
        "relationship cites an invalid " + "comparison surface",
        "records address the " + "same identifiable",
        "records define substantially " + "the same",
        "surface",
        "table i",
        "this experiment",
        "this paper",
        "whole paper",
    }
)
_PARTIAL_SOURCE_TEXT_KEYS = frozenset(
    {
        "assumption_alignment",
        "comparison_surface",
        "context",
        "description",
        "evidence",
        "exact_span",
        "limitations",
        "precise_connection",
        "qualification",
        "qualifications",
        "rationale",
        "scope",
        "scope_alignment",
        "scope_assumptions",
        "scientific_use",
        "segments",
        "source_form",
        "standalone_context",
        "statement",
        "summary",
        "transcription",
    }
)
_FOUR_WORD_SOURCE_TEXT_KEYS = frozenset(
    {
        "exact_span",
        "source_form",
        "transcription",
    }
)
_ATOMIC_PRIVATE_TEXT_KEYS = frozenset(
    {
        "audit_job_id",
        "batch_id",
        "candidate_id",
        "id",
        "inspection_job_id",
        "job_id",
        "left_record_id",
        "origin_recovery_id",
        "origin_run_id",
        "original_job_id",
        "record_id",
        "recovery_id",
        "relationship_id",
        "right_record_id",
        "shard_id",
        "source",
        "target",
    }
)

_AGGREGATE_METRICS_FIXED_FIELDS = {
    "schema_version": "1.0",
    "baseline_id": RELEASE_ID,
    "scope": "aggregate_only",
    "experimental_status": "provisional_first_research_baseline",
    "qualification": (
        "Counts describe the retained mapping run; they do not establish scientific "
        "coverage, correctness, or consensus."
    ),
}
_RELATIONSHIP_SUMMARY_FIXED_FIELDS = {
    "schema_version": "1.0",
    "baseline_id": RELEASE_ID,
    "scope": "aggregate_only_no_endpoints_or_source_pairs",
}
_LOW_ENTROPY_CONTROLLED_TEXT_KEYS = frozenset(
    {
        "attribution",
        "epistemic_posture",
        "label",
    }
)
_TRANSIENT_DIRECTORIES = frozenset(
    {
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "dist",
    }
)
_PUBLIC_REQUIRED_PATHS = frozenset(
    {
        Path(".gitattributes"),
        Path(".gitignore"),
        Path("README.md"),
        Path("LICENSE"),
        Path("NOTICE"),
        Path("CITATION.cff"),
        Path("CONTRIBUTING.md"),
        Path("SECURITY.md"),
        Path("THIRD_PARTY_RIGHTS.md"),
        Path("docs/architecture/README.md"),
        Path("docs/experiments/README.md"),
        Path("docs/limitations/README.md"),
        Path("docs/public-roadmap/README.md"),
        Path("pyproject.toml"),
        Path("uv.lock"),
        Path("reference_mapping_graph/README.md"),
        Path("reference_mapping_graph/baseline-v1/README.md"),
        Path("reference_mapping_graph/synthetic-example/README.md"),
        Path("scripts/verify-public-release.sh"),
        COMMANDS_PATH,
        POLICY_PATH,
        RIGHTS_INVENTORY_PATH,
        INPUT_LOCK_PATH,
        _PUBLIC_INPUT_LOCK_SCHEMA,
    }
)
_PUBLIC_REFERENCE_MAPPING_PATHS = frozenset(
    {
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
    }
)
_EXPECTED_PUBLIC_COMMANDS: Mapping[str, tuple[tuple[str, ...], bool, bool, bool]] = {
    "build-public-release": (
        (
            "uv",
            "run",
            "research-map",
            "build-public-release",
            "--repository-root",
            "<repository-root>",
            "--source-registration-root",
            "<source-registration-root>",
            "--asset-root",
            "<asset-root>",
            "--graph",
            "<graph>",
            "--graph-manifest",
            "<graph-manifest>",
            "--private-report",
            "<private-report>",
            "--policy",
            "<policy>",
            "--input-lock",
            "<input-lock>",
            "--output",
            "<empty-output-path>",
            "--json",
        ),
        False,
        False,
        True,
    ),
    "test": (("uv", "run", "pytest"), False, False, False),
    "search-synthetic-example": (
        (
            "uv",
            "run",
            "research-map",
            "search",
            "indicator",
            "--graph",
            "reference_mapping_graph/synthetic-example/graph.json",
            "--json",
        ),
        False,
        False,
        False,
    ),
    "explore-synthetic-example": (
        (
            "uv",
            "run",
            "research-map",
            "explore",
            "--id",
            "LIB-901:atom:indicator-response",
            "--graph",
            "reference_mapping_graph/synthetic-example/graph.json",
            "--json",
        ),
        False,
        False,
        False,
    ),
    "verify-public-candidate": (
        (("scripts/verify-public-release.sh", ".")),
        False,
        False,
        False,
    ),
}


class PublicReleaseValidationError(ValueError):
    """Raised when a private input or candidate violates the publication contract."""


@dataclass(frozen=True, slots=True)
class PublicReleaseResult:
    candidate_root: Path
    manifest_path: Path
    report_path: Path
    tree_sha256: str
    file_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_root": str(self.candidate_root),
            "manifest_path": str(self.manifest_path),
            "report_path": str(self.report_path),
            "tree_sha256": self.tree_sha256,
            "file_count": self.file_count,
        }


@dataclass(frozen=True, slots=True)
class PublicReleaseVerification:
    candidate_root: Path
    tree_sha256: str
    file_count: int
    checks: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_root": str(self.candidate_root),
            "tree_sha256": self.tree_sha256,
            "file_count": self.file_count,
            "checks": list(self.checks),
        }


@dataclass(frozen=True, slots=True)
class _PrivateSnapshot:
    source_integrity_sha256: str
    registration_fingerprints: tuple[tuple[str, str, int], ...]
    asset_fingerprints: tuple[tuple[str, str, int], ...]
    catalog: tuple[dict[str, Any], ...]
    rights_categories: Mapping[str, tuple[str, ...]]
    withheld_registration_strings: tuple[str, ...]

    @property
    def source_count(self) -> int:
        return len(self.registration_fingerprints)

    @property
    def asset_count(self) -> int:
        return len(self.asset_fingerprints)

    @property
    def immutable_signature(self) -> tuple[object, ...]:
        return (
            self.source_integrity_sha256,
            self.registration_fingerprints,
            self.asset_fingerprints,
        )


@dataclass(frozen=True, slots=True)
class _PrivateVaultSnapshot:
    fingerprints: tuple[tuple[str, str, int], ...]
    semantic_strings: tuple[str, ...]


class PublicReleaseBuilder:
    """Build one public candidate from explicit, immutable private inputs."""

    def __init__(
        self,
        *,
        repository_root: Path,
        source_registration_root: Path,
        asset_root: Path,
        graph_path: Path,
        graph_manifest_path: Path,
        private_report_path: Path,
        policy_path: Path,
        input_lock_path: Path,
    ) -> None:
        self.repository_root = repository_root.resolve()
        self.source_registration_root = source_registration_root.resolve()
        self.asset_root = asset_root.resolve()
        self.graph_path = graph_path.resolve()
        self.graph_manifest_path = graph_manifest_path.resolve()
        self.private_report_path = private_report_path.resolve()
        self.policy_path = policy_path.resolve()
        self.input_lock_path = input_lock_path.resolve()

    def build(self, destination: Path) -> PublicReleaseResult:
        destination = destination.resolve()
        self._validate_destination(destination)
        policy, input_lock = self._load_contract()
        projection_repository_commit = self._verify_repository_identity(input_lock)
        private_files_before = self._private_file_fingerprints(input_lock)
        source_before = _collect_private_source_state(
            self.source_registration_root, self.asset_root
        )
        vault_before = _collect_canonical_vault_state(self.repository_root)
        self._verify_source_state(source_before, input_lock)
        self._verify_rights_inventory(source_before)
        graph = _load_json_mapping(self.graph_path, label="private graph")
        graph_manifest = _load_json_mapping(
            self.graph_manifest_path, label="private graph manifest"
        )
        manifest_inputs_before = _manifest_bound_input_fingerprints(
            graph_manifest=graph_manifest,
            graph_manifest_path=self.graph_manifest_path,
            validate_descriptors=True,
        )
        private_report = _load_json_mapping(
            self.private_report_path, label="private salvage report"
        )
        aggregates = _validate_and_aggregate_private_graph(
            graph=graph,
            graph_manifest=graph_manifest,
            private_report=private_report,
            input_lock=input_lock,
            source_state=source_before,
            graph_sha256=private_files_before["graph"][0],
            graph_manifest_sha256=private_files_before["graph_manifest"][0],
        )

        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(prefix=".public-release-", dir=destination.parent)
        ).resolve()
        try:
            selected_paths = _selected_public_paths(
                self.repository_root,
                policy,
                revision=projection_repository_commit,
            )
            for relative_path in selected_paths:
                _copy_public_file(
                    repository_root=self.repository_root,
                    revision=projection_repository_commit,
                    relative_path=relative_path,
                    destination=staging / relative_path,
                )

            _write_json_new(staging / METRICS_PATH, aggregates["metrics"])
            _write_json_new(
                staging / RELATIONSHIP_SUMMARY_PATH,
                aggregates["relationship_summary"],
            )
            _write_yaml_new(
                staging / SOURCE_CATALOG_PATH,
                {
                    "schema_version": "1.0",
                    "catalog_id": "public-source-metadata-baseline-v1",
                    "scope": "bibliographic_metadata_only",
                    "sources": list(source_before.catalog),
                },
            )
            _write_synthetic_example(staging)

            pre_report_files = _candidate_files(staging)
            anticipated_file_count = len(pre_report_files) + 2
            publication_report = {
                "schema_version": "1.0",
                "release_id": RELEASE_ID,
                "decision": "candidate_requires_clean_history_audit",
                "public_scope": {
                    "data": input_lock["public_data_scope"],
                    "license": policy["original_code_and_documentation_license"],
                    "dataset_license": policy["dataset_license"],
                    "third_party_rights": policy["third_party_rights"],
                    "external_actions_authorized": False,
                },
                "admitted": {
                    "file_count": anticipated_file_count,
                    "source_fields": policy["admitted_source_fields"],
                    "graph_fields": policy["admitted_graph_fields"],
                },
                "withheld": {
                    "graph_fields": policy["withheld_graph_fields"],
                    "source_assets": source_before.asset_count,
                    "evidence_bearing_graph": True,
                    "private_project_history": True,
                },
                "private_inputs": {
                    "private_repository_commit": input_lock["private_repository_commit"],
                    "projection_repository_commit": projection_repository_commit,
                    "graph_sha256": private_files_before["graph"][0],
                    "graph_manifest_sha256": private_files_before["graph_manifest"][0],
                    "private_report_sha256": private_files_before["private_report"][0],
                    "source_integrity_sha256": source_before.source_integrity_sha256,
                },
                "checks": {
                    "explicit_inputs_verified": "pass",
                    "source_assets_verified": "pass",
                    "field_allowlist_applied": "pass",
                    "no_semantic_model_call": "pass",
                    "forbidden_candidate_paths": "pass",
                    "credential_and_local_path_scan": "pass",
                    "private_evidence_text_scan": "pass",
                    "private_inputs_unchanged": "pass",
                },
                "limitations": [
                    "Aggregate counts do not establish scientific coverage or correctness.",
                    "The full evidence-bearing graph and source assets remain private.",
                    "Local verification does not authorize external publication.",
                ],
            }
            _write_json_new(staging / REPORT_PATH, publication_report)
            _scan_candidate_content(
                staging,
                registered_asset_fingerprints=source_before.asset_fingerprints,
                private_work_identifiers=_private_work_identifiers(self.repository_root),
            )
            private_semantic_needles = _scan_for_private_semantic_text(
                staging,
                graph=graph,
                graph_manifest=graph_manifest,
                graph_manifest_path=self.graph_manifest_path,
                private_report=private_report,
                public_catalog=source_before.catalog,
                withheld_registration_strings=source_before.withheld_registration_strings,
                canonical_vault_strings=vault_before.semantic_strings,
            )
            descriptors = _candidate_descriptors(staging, exclude={MANIFEST_PATH})
            tree_sha256 = _tree_sha256(descriptors)
            manifest = {
                "schema_version": "1.0",
                "release_id": RELEASE_ID,
                "policy_id": policy["policy_id"],
                "private_inputs": {
                    "private_repository_commit": input_lock["private_repository_commit"],
                    "projection_repository_commit": projection_repository_commit,
                    "graph_sha256": private_files_before["graph"][0],
                    "graph_manifest_sha256": private_files_before["graph_manifest"][0],
                    "private_report_sha256": private_files_before["private_report"][0],
                    "source_integrity_sha256": source_before.source_integrity_sha256,
                },
                "public_scope": {
                    "data": input_lock["public_data_scope"],
                    "license": policy["original_code_and_documentation_license"],
                    "dataset_license": policy["dataset_license"],
                    "third_party_rights": policy["third_party_rights"],
                    "external_actions_authorized": False,
                },
                "files": descriptors,
                "tree_sha256": tree_sha256,
            }
            manifest_schema = _load_json_mapping(
                staging / _PUBLIC_MANIFEST_SCHEMA, label="public manifest schema"
            )
            _validate_schema_instance(
                manifest,
                manifest_schema,
                label="public manifest",
            )
            _write_json_new(staging / MANIFEST_PATH, manifest)
            _scan_candidate_text_against_private_needles(
                staging,
                needles=private_semantic_needles,
            )
            verification = verify_public_release(staging)

            private_files_after = self._private_file_fingerprints(input_lock)
            manifest_inputs_after = _manifest_bound_input_fingerprints(
                graph_manifest=graph_manifest,
                graph_manifest_path=self.graph_manifest_path,
                validate_descriptors=False,
            )
            source_after = _collect_private_source_state(
                self.source_registration_root, self.asset_root
            )
            vault_after = _collect_canonical_vault_state(self.repository_root)
            if private_files_after != private_files_before:
                raise PublicReleaseValidationError(
                    "private graph inputs changed while the candidate was built"
                )
            if manifest_inputs_after != manifest_inputs_before:
                raise PublicReleaseValidationError(
                    "manifest-bound private inputs changed while the candidate was built"
                )
            if source_after.immutable_signature != source_before.immutable_signature:
                raise PublicReleaseValidationError(
                    "private source registrations or assets changed while the candidate was built"
                )
            if vault_after.fingerprints != vault_before.fingerprints:
                raise PublicReleaseValidationError(
                    "canonical vault records changed while the candidate was built"
                )
            projection_commit_after = _run_git(
                self.repository_root,
                ("rev-parse", "HEAD"),
            ).stdout.strip()
            repository_status_after = _run_git(
                self.repository_root,
                ("status", "--porcelain=v1", "--untracked-files=all", "-z"),
            )
            if (
                projection_commit_after != projection_repository_commit
                or repository_status_after.stdout
            ):
                raise PublicReleaseValidationError(
                    "repository projection source changed while the candidate was built"
                )

            os.replace(staging, destination)
            return PublicReleaseResult(
                candidate_root=destination,
                manifest_path=destination / MANIFEST_PATH,
                report_path=destination / REPORT_PATH,
                tree_sha256=verification.tree_sha256,
                file_count=verification.file_count,
            )
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def _load_contract(self) -> tuple[dict[str, Any], dict[str, Any]]:
        canonical_policy = (self.repository_root / POLICY_PATH).resolve()
        canonical_input_lock = (self.repository_root / INPUT_LOCK_PATH).resolve()
        if self.policy_path != canonical_policy:
            raise PublicReleaseValidationError(
                "publication policy must be the repository's canonical policy"
            )
        if self.input_lock_path != canonical_input_lock:
            raise PublicReleaseValidationError(
                "private input lock must be the repository's canonical input lock"
            )
        policy = _load_yaml_mapping(self.policy_path, label="publication policy")
        policy_schema = _load_json_mapping(
            self.repository_root / _PUBLIC_POLICY_SCHEMA,
            label="publication policy schema",
        )
        _validate_schema_instance(
            policy,
            policy_schema,
            label="publication policy",
        )
        input_lock = _load_json_mapping(self.input_lock_path, label="private input lock")
        input_lock_schema = _load_json_mapping(
            self.repository_root / _PUBLIC_INPUT_LOCK_SCHEMA,
            label="private input lock schema",
        )
        _validate_schema_instance(
            input_lock,
            input_lock_schema,
            label="private input lock",
        )
        if policy.get("external_actions_authorized") is not False:
            raise PublicReleaseValidationError("publication policy authorizes external actions")
        if input_lock.get("external_actions_authorized") is not False:
            raise PublicReleaseValidationError("private input lock authorizes external actions")
        if policy.get("repository_topology") != input_lock.get("repository_topology"):
            raise PublicReleaseValidationError(
                "repository topology differs between policy and lock"
            )
        if policy.get("public_data_scope") != input_lock.get("public_data_scope"):
            raise PublicReleaseValidationError("public data scope differs between policy and lock")
        _validate_reference_mapping_allowlist(policy)
        return policy, input_lock

    def _validate_destination(self, destination: Path) -> None:
        if destination.exists():
            raise PublicReleaseValidationError("public release destination must not already exist")
        if destination == self.repository_root:
            raise PublicReleaseValidationError(
                "public release destination cannot be the private root"
            )
        if destination.is_relative_to(self.repository_root):
            allowed_root = (self.repository_root / ".cache" / "public-release").resolve()
            if not destination.is_relative_to(allowed_root):
                raise PublicReleaseValidationError(
                    "a repository-local candidate must remain under .cache/public-release"
                )
        asset_private_root = self.asset_root
        if asset_private_root == self.repository_root:
            asset_private_root = asset_private_root / "sources" / "library"
        private_roots = {
            self.source_registration_root,
            asset_private_root,
            self.graph_path.parent,
            self.graph_manifest_path.parent,
            self.private_report_path.parent,
            self.policy_path.parent,
            self.input_lock_path.parent,
        }
        if any(
            destination == root
            or destination.is_relative_to(root)
            or root.is_relative_to(destination)
            for root in private_roots
        ):
            raise PublicReleaseValidationError(
                "public release destination overlaps a private input root"
            )

    def _verify_repository_identity(self, input_lock: Mapping[str, Any]) -> str:
        commit = input_lock.get("private_repository_commit")
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise PublicReleaseValidationError("private repository commit is invalid")
        _run_git(self.repository_root, ("cat-file", "-e", f"{commit}^{{commit}}"))
        _run_git(self.repository_root, ("merge-base", "--is-ancestor", commit, "HEAD"))
        repository_status = _run_git(
            self.repository_root,
            ("status", "--porcelain=v1", "--untracked-files=all", "-z"),
        )
        if repository_status.stdout:
            raise PublicReleaseValidationError(
                "private repository working tree must be clean before projection"
            )
        changed_private = _run_git(
            self.repository_root,
            ("diff", "--name-only", commit, "HEAD", "--", "sources", "vault"),
        )
        if changed_private.stdout.strip():
            raise PublicReleaseValidationError(
                "tracked private source or canonical vault state differs from the frozen commit"
            )
        _verify_tracked_private_worktree(self.repository_root, revision=commit)
        projection_commit = _run_git(self.repository_root, ("rev-parse", "HEAD")).stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{40}", projection_commit):
            raise PublicReleaseValidationError("projection repository commit is invalid")
        return projection_commit

    def _private_file_fingerprints(
        self, input_lock: Mapping[str, Any]
    ) -> dict[str, tuple[str, int]]:
        baseline = _mapping(input_lock.get("baseline"), label="input-lock baseline")
        expected = {
            "graph": _mapping(baseline.get("graph"), label="locked graph"),
            "graph_manifest": _mapping(
                baseline.get("graph_manifest"), label="locked graph manifest"
            ),
            "private_report": _mapping(
                baseline.get("private_report"), label="locked private report"
            ),
        }
        paths = {
            "graph": self.graph_path,
            "graph_manifest": self.graph_manifest_path,
            "private_report": self.private_report_path,
        }
        observed: dict[str, tuple[str, int]] = {}
        for key, path in paths.items():
            if not path.is_file() or path.is_symlink():
                raise PublicReleaseValidationError(
                    f"explicit private input is missing or not a regular file: {key}"
                )
            value = fingerprint(path)
            if value.sha256 != expected[key].get("sha256"):
                raise PublicReleaseValidationError(f"stale private input hash: {key}")
            if value.size_bytes != expected[key].get("size_bytes"):
                raise PublicReleaseValidationError(f"stale private input size: {key}")
            observed[key] = (value.sha256, value.size_bytes)
        return observed

    @staticmethod
    def _verify_source_state(source_state: _PrivateSnapshot, input_lock: Mapping[str, Any]) -> None:
        source_integrity = _mapping(
            input_lock.get("source_integrity"), label="source integrity lock"
        )
        if source_state.source_integrity_sha256 != source_integrity.get("sha256"):
            raise PublicReleaseValidationError("source integrity digest differs from input lock")
        counts = _mapping(input_lock.get("counts"), label="input-lock counts")
        if source_state.source_count != counts.get("registered_sources"):
            raise PublicReleaseValidationError("registered source count differs from input lock")
        if source_state.asset_count != counts.get("registered_assets"):
            raise PublicReleaseValidationError("registered asset count differs from input lock")

    def _verify_rights_inventory(self, source_state: _PrivateSnapshot) -> None:
        inventory = _load_yaml_mapping(
            self.repository_root / "reference_mapping_graph/rights-inventory.yaml",
            label="source rights inventory",
        )
        if inventory.get("source_count") != source_state.source_count:
            raise PublicReleaseValidationError("rights inventory source count is stale")
        if inventory.get("asset_count") != source_state.asset_count:
            raise PublicReleaseValidationError("rights inventory asset count is stale")
        categories = _mapping(inventory.get("categories"), label="rights inventory categories")
        if set(categories) != set(source_state.rights_categories):
            raise PublicReleaseValidationError("rights inventory categories are incomplete")
        for category, expected_ids in source_state.rights_categories.items():
            recorded = _mapping(categories.get(category), label=f"rights category {category}")
            _validate_rights_release_posture(category, recorded)
            source_ids = recorded.get("source_ids")
            if source_ids != list(expected_ids) or recorded.get("source_count") != len(
                expected_ids
            ):
                raise PublicReleaseValidationError(
                    f"rights inventory category is stale: {category}"
                )


def verify_public_release(candidate_root: Path) -> PublicReleaseVerification:
    """Verify a candidate without reading any private source or graph input."""

    candidate_root = candidate_root.resolve()
    if not candidate_root.is_dir():
        raise PublicReleaseValidationError("public release candidate root does not exist")
    manifest = _load_json_mapping(candidate_root / MANIFEST_PATH, label="public manifest")
    policy = _load_yaml_mapping(
        candidate_root / POLICY_PATH,
        label="candidate publication policy",
    )
    policy_schema = _load_json_mapping(
        candidate_root / _PUBLIC_POLICY_SCHEMA,
        label="candidate publication policy schema",
    )
    manifest_schema = _load_json_mapping(
        candidate_root / _PUBLIC_MANIFEST_SCHEMA,
        label="candidate public manifest schema",
    )
    report_schema = _load_json_mapping(
        candidate_root / _PUBLIC_REPORT_SCHEMA,
        label="candidate publication report schema",
    )
    report = _load_json_mapping(candidate_root / REPORT_PATH, label="publication report")
    commands_schema = _load_json_mapping(
        candidate_root / _PUBLIC_COMMANDS_SCHEMA,
        label="candidate commands schema",
    )
    input_lock_schema = _load_json_mapping(
        candidate_root / _PUBLIC_INPUT_LOCK_SCHEMA,
        label="candidate input lock schema",
    )
    commands = _load_json_mapping(candidate_root / COMMANDS_PATH, label="candidate commands")
    input_lock = _load_json_mapping(candidate_root / INPUT_LOCK_PATH, label="candidate input lock")
    for instance, schema, label in (
        (policy, policy_schema, "publication policy"),
        (manifest, manifest_schema, "public manifest"),
        (report, report_schema, "publication report"),
        (commands, commands_schema, "commands"),
        (input_lock, input_lock_schema, "private input lock"),
    ):
        _validate_schema_instance(
            instance,
            schema,
            label=f"public candidate {label}",
            failure_message="public candidate schema validation failed",
        )
    _validate_command_catalog(commands)
    _validate_reference_mapping_allowlist(policy)
    if manifest.get("release_id") != RELEASE_ID or report.get("release_id") != RELEASE_ID:
        raise PublicReleaseValidationError("public release identity is inconsistent")
    manifest_private = _mapping(manifest.get("private_inputs"), label="manifest private inputs")
    report_private = _mapping(report.get("private_inputs"), label="report private inputs")
    projection_repository_commit = manifest_private.get("projection_repository_commit")
    expected_private = {
        "private_repository_commit": input_lock.get("private_repository_commit"),
        "projection_repository_commit": projection_repository_commit,
        "graph_sha256": _mapping(
            _mapping(input_lock.get("baseline"), label="input-lock baseline").get("graph"),
            label="input-lock graph",
        ).get("sha256"),
        "graph_manifest_sha256": _mapping(
            _mapping(input_lock.get("baseline"), label="input-lock baseline").get("graph_manifest"),
            label="input-lock graph manifest",
        ).get("sha256"),
        "private_report_sha256": _mapping(
            _mapping(input_lock.get("baseline"), label="input-lock baseline").get("private_report"),
            label="input-lock private report",
        ).get("sha256"),
        "source_integrity_sha256": _mapping(
            input_lock.get("source_integrity"), label="input-lock source integrity"
        ).get("sha256"),
    }
    if dict(manifest_private) != expected_private:
        raise PublicReleaseValidationError("public manifest provenance differs from the input lock")
    if dict(report_private) != expected_private:
        raise PublicReleaseValidationError(
            "publication report provenance differs from the input lock"
        )
    expected_public_scope = {
        "data": policy.get("public_data_scope"),
        "license": policy.get("original_code_and_documentation_license"),
        "dataset_license": policy.get("dataset_license"),
        "third_party_rights": policy.get("third_party_rights"),
        "external_actions_authorized": False,
    }
    public_scope = _mapping(manifest.get("public_scope"), label="manifest public scope")
    report_scope = _mapping(report.get("public_scope"), label="report public scope")
    if dict(public_scope) != expected_public_scope or dict(report_scope) != expected_public_scope:
        raise PublicReleaseValidationError("candidate public scope differs from its policy")
    if manifest.get("policy_id") != policy.get("policy_id"):
        raise PublicReleaseValidationError("public manifest policy identity is inconsistent")
    if public_scope.get("external_actions_authorized") is not False:
        raise PublicReleaseValidationError("candidate authorizes an external action")
    checks = _mapping(report.get("checks"), label="publication report checks")
    if not checks or any(result != "pass" for result in checks.values()):
        raise PublicReleaseValidationError("publication report contains a non-passing check")

    _validate_candidate_paths(candidate_root, policy)
    _scan_candidate_content(candidate_root)
    _validate_public_surface(candidate_root)
    _validate_public_data_artifacts(
        candidate_root,
        policy=policy,
        input_lock=input_lock,
        manifest=manifest,
        report=report,
    )
    _verify_markdown_links(candidate_root)
    try:
        explorer = GraphExplorer(
            graph_path=candidate_root / SYNTHETIC_GRAPH_PATH,
            schema_directory=candidate_root / "schemas/research-map/v1",
        )
        search_result = explorer.search("indicator")
        explore_result = explorer.explore("LIB-901:atom:indicator-response")
    except ValueError as error:
        raise PublicReleaseValidationError(
            f"synthetic example does not pass real graph exploration: {error}"
        ) from error
    if search_result["returned_count"] < 1:
        raise PublicReleaseValidationError("synthetic example search returned no results")
    if explore_result["graph"]["manifest_verified"] is not True:
        raise PublicReleaseValidationError("synthetic example manifest was not verified")
    expected_descriptors = manifest.get("files")
    if not isinstance(expected_descriptors, list):
        raise PublicReleaseValidationError("public manifest files must be an array")
    observed_descriptors = _candidate_descriptors(candidate_root, exclude={MANIFEST_PATH})
    if observed_descriptors != expected_descriptors:
        raise PublicReleaseValidationError("candidate files differ from the public manifest")
    observed_tree = _tree_sha256(observed_descriptors)
    if observed_tree != manifest.get("tree_sha256"):
        raise PublicReleaseValidationError("candidate tree digest differs from the manifest")
    return PublicReleaseVerification(
        candidate_root=candidate_root,
        tree_sha256=observed_tree,
        file_count=len(observed_descriptors) + 1,
        checks=(
            "publication_policy_schema",
            "manifest_schema",
            "publication_report_schema",
            "commands_schema",
            "input_lock_schema",
            "path_allowlist",
            "forbidden_suffixes",
            "credential_and_local_path_scan",
            "required_public_surface",
            "public_data_field_allowlist",
            "markdown_links",
            "synthetic_search_and_explore",
            "file_hashes_and_modes",
            "tree_digest",
        ),
    )


def _collect_private_source_state(
    source_registration_root: Path, asset_root: Path
) -> _PrivateSnapshot:
    library_root = source_registration_root / "library"
    if not library_root.is_dir():
        raise PublicReleaseValidationError("source registration root has no library directory")
    allowed_asset_root = (asset_root / "sources" / "library").resolve()
    rows: list[dict[str, Any]] = []
    registrations: list[tuple[str, str, int]] = []
    assets: list[tuple[str, str, int]] = []
    catalog: list[dict[str, Any]] = []
    withheld_registration_strings: set[str] = set()
    rights: dict[str, list[str]] = {
        "recorded_cc_by_4_0": [],
        "redistribution_not_assessed": [],
    }
    seen_source_ids: set[str] = set()

    for registration_path in sorted(library_root.glob("*/source.yaml")):
        if registration_path.is_symlink() or not registration_path.is_file():
            raise PublicReleaseValidationError("source registration is not a regular file")
        payload = _load_yaml_mapping(registration_path, label="source registration")
        withheld_registration_strings.update(_source_registration_withheld_strings(payload))
        source_id = payload.get("library_id")
        if not isinstance(source_id, str) or not re.fullmatch(r"LIB-[0-9]{3}", source_id):
            raise PublicReleaseValidationError("source registration has an invalid library ID")
        if source_id in seen_source_ids:
            raise PublicReleaseValidationError(f"duplicate source registration: {source_id}")
        seen_source_ids.add(source_id)
        registration_fingerprint = fingerprint(registration_path)
        registration_relative = (
            Path("sources") / registration_path.relative_to(source_registration_root)
        ).as_posix()
        registrations.append(
            (
                registration_relative,
                registration_fingerprint.sha256,
                registration_fingerprint.size_bytes,
            )
        )

        raw_assets = payload.get("assets")
        if not isinstance(raw_assets, list) or not raw_assets:
            raise PublicReleaseValidationError(f"source has no registered assets: {source_id}")
        source_assets: list[dict[str, Any]] = []
        for raw_asset in raw_assets:
            asset = _mapping(raw_asset, label=f"asset registration for {source_id}")
            relative_text = asset.get("path")
            role = asset.get("asset_role")
            notes = asset.get("notes")
            if not isinstance(relative_text, str) or not isinstance(role, str):
                raise PublicReleaseValidationError(f"incomplete asset registration: {source_id}")
            if not isinstance(notes, str) or (match := _ASSET_FINGERPRINT.search(notes)) is None:
                raise PublicReleaseValidationError(
                    f"asset registration lacks a fingerprint: {source_id}"
                )
            relative_path = PurePosixPath(relative_text)
            if relative_path.is_absolute() or ".." in relative_path.parts:
                raise PublicReleaseValidationError(f"asset path escapes its root: {source_id}")
            asset_path = asset_root.joinpath(*relative_path.parts)
            resolved_asset = asset_path.resolve()
            if not resolved_asset.is_relative_to(allowed_asset_root):
                raise PublicReleaseValidationError(
                    f"asset path escapes sources/library: {source_id}"
                )
            if asset_path.is_symlink() or not asset_path.is_file():
                raise PublicReleaseValidationError(
                    f"registered asset is missing or not a regular file: {source_id}"
                )
            expected_sha256, expected_size_text = match.groups()
            observed = fingerprint(asset_path)
            if observed.sha256 != expected_sha256 or observed.size_bytes != int(expected_size_text):
                raise PublicReleaseValidationError(
                    f"registered asset fingerprint changed: {source_id}"
                )
            assets.append((relative_path.as_posix(), observed.sha256, observed.size_bytes))
            source_assets.append(
                {
                    "role": role,
                    "sha256": expected_sha256,
                    "size_bytes": int(expected_size_text),
                }
            )

        identity = _mapping(payload.get("identity"), label=f"identity for {source_id}")
        creators = identity.get("creators")
        if (
            not isinstance(creators, list)
            or not creators
            or not all(isinstance(creator, str) and creator.strip() for creator in creators)
        ):
            raise PublicReleaseValidationError(f"source creators are invalid: {source_id}")
        for key in ("title", "type", "language"):
            value = identity.get(key)
            if not isinstance(value, str) or not value.strip():
                raise PublicReleaseValidationError(
                    f"source identity value is invalid: {source_id} {key}"
                )
        year = identity.get("year")
        if not isinstance(year, int) or isinstance(year, bool):
            raise PublicReleaseValidationError(
                f"source identity value is invalid: {source_id} year"
            )
        provenance = _mapping(payload.get("provenance"), label=f"provenance for {source_id}")
        rights_category = _normalize_rights(provenance.get("rights_notes"), source_id)
        rights[rights_category].append(source_id)
        catalog.append(
            {
                "source_id": source_id,
                "title": identity["title"],
                "creators": creators,
                "year": identity["year"],
                "type": identity["type"],
                "language": identity["language"],
                "rights_category": rights_category,
            }
        )
        rows.append(
            {
                "source_id": source_id,
                "source_yaml_sha256": registration_fingerprint.sha256,
                "assets": source_assets,
            }
        )

    rows.sort(key=lambda item: str(item["source_id"]))
    catalog.sort(key=lambda item: str(item["source_id"]))
    canonical_rows = json.dumps(
        rows,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    source_integrity_sha256 = hashlib.sha256(canonical_rows).hexdigest()
    return _PrivateSnapshot(
        source_integrity_sha256=source_integrity_sha256,
        registration_fingerprints=tuple(sorted(registrations)),
        asset_fingerprints=tuple(sorted(assets)),
        catalog=tuple(catalog),
        rights_categories={key: tuple(sorted(value)) for key, value in rights.items()},
        withheld_registration_strings=tuple(sorted(withheld_registration_strings)),
    )


def _normalize_rights(value: object, source_id: str) -> str:
    if not isinstance(value, str):
        raise PublicReleaseValidationError(f"source rights posture is missing: {source_id}")
    lowered = value.casefold().replace("‑", "-").replace("–", "-")
    normalized = " ".join(lowered.split())
    if re.fullmatch(r"the pdfs? states? cc(?:-|\s+)by 4\.0\.", normalized):
        return "recorded_cc_by_4_0"
    if re.fullmatch(
        r"local possession only; redistribution rights were not assessed\.",
        normalized,
    ):
        return "redistribution_not_assessed"
    if re.search(r"cc(?:-|\s+)by\s+4\.0", normalized) or (
        "redistribution rights were " + "not assessed" in normalized
    ):
        raise PublicReleaseValidationError(f"ambiguous source rights posture: {source_id}")
    raise PublicReleaseValidationError(f"unrecognized source rights posture: {source_id}")


def _collect_canonical_vault_state(repository_root: Path) -> _PrivateVaultSnapshot:
    vault_root = repository_root / "vault"
    if not vault_root.exists():
        return _PrivateVaultSnapshot(fingerprints=(), semantic_strings=())
    if vault_root.is_symlink() or not vault_root.is_dir():
        raise PublicReleaseValidationError("canonical vault root is not a regular directory")

    fingerprints: list[tuple[str, str, int]] = []
    semantic_strings: set[str] = set()
    source_root = vault_root / "sources"
    if source_root.exists() and (source_root.is_symlink() or not source_root.is_dir()):
        raise PublicReleaseValidationError("canonical vault source root is not a regular directory")
    for path in sorted(source_root.glob("*/paper.md")) if source_root.exists() else ():
        resolved = path.resolve()
        if (
            path.is_symlink()
            or not path.is_file()
            or not resolved.is_relative_to(vault_root.resolve())
        ):
            raise PublicReleaseValidationError(
                f"canonical vault record is missing or unsafe: {path}"
            )
        try:
            document = load_markdown(path)
        except MarkdownRecordError as error:
            raise PublicReleaseValidationError(
                f"canonical vault record is invalid: {path}"
            ) from error
        if document.text != render_markdown(document.dossier):
            raise PublicReleaseValidationError(
                f"canonical vault record is not in canonical form: {path}"
            )
        relative = path.relative_to(repository_root).as_posix()
        observed = fingerprint(path)
        fingerprints.append((relative, observed.sha256, observed.size_bytes))
        semantic_strings.update(
            _semantic_strings(document.dossier.to_dict(), semantic_context=False)
        )
    relationship_root = vault_root / "relationships"
    if relationship_root.exists() and (
        relationship_root.is_symlink() or not relationship_root.is_dir()
    ):
        raise PublicReleaseValidationError(
            "canonical vault relationship root is not a regular directory"
        )
    relationship_paths = (
        sorted(relationship_root.glob("*/relationships.md")) if relationship_root.exists() else ()
    )
    for path in relationship_paths:
        resolved = path.resolve()
        if (
            path.is_symlink()
            or not path.is_file()
            or not resolved.is_relative_to(vault_root.resolve())
        ):
            raise PublicReleaseValidationError(
                f"canonical vault relationship is missing or unsafe: {path}"
            )
        try:
            relationship_document = load_relationship_markdown(path)
        except ValueError as error:
            raise PublicReleaseValidationError(
                f"canonical vault relationship is invalid: {path}"
            ) from error
        if relationship_document.text != render_relationship_markdown(
            relationship_document.source_pair,
            relationship_document.relationships,
        ):
            raise PublicReleaseValidationError(
                f"canonical vault relationship is not in canonical form: {path}"
            )
        relative = path.relative_to(repository_root).as_posix()
        observed = fingerprint(path)
        fingerprints.append((relative, observed.sha256, observed.size_bytes))
        for relationship in relationship_document.relationships:
            semantic_strings.update(
                _semantic_strings(relationship.to_dict(), semantic_context=False)
            )
    return _PrivateVaultSnapshot(
        fingerprints=tuple(sorted(fingerprints)),
        semantic_strings=tuple(sorted(semantic_strings)),
    )


def _validate_and_aggregate_private_graph(
    *,
    graph: Mapping[str, Any],
    graph_manifest: Mapping[str, Any],
    private_report: Mapping[str, Any],
    input_lock: Mapping[str, Any],
    source_state: _PrivateSnapshot,
    graph_sha256: str,
    graph_manifest_sha256: str,
) -> dict[str, dict[str, Any]]:
    nodes = _sequence(graph.get("nodes"), label="private graph nodes")
    edges = _sequence(graph.get("edges"), label="private graph edges")
    sources = _sequence(graph.get("sources"), label="private graph sources")
    record_types: Counter[str] = Counter()
    for node in nodes:
        record_types[str(_mapping(node, label="private graph node").get("record_type"))] += 1
    origins: Counter[str] = Counter()
    relationship_kinds: Counter[str] = Counter()
    for edge_value in edges:
        edge = _mapping(edge_value, label="private graph edge")
        origin = str(edge.get("origin"))
        origins[origin] += 1
        if origin == "cross_source":
            relationship_kinds[str(edge.get("kind"))] += 1

    pair_partitions = _mapping(
        private_report.get("pair_partitions"), label="private report pair partitions"
    )
    inspected_pairs = len(_sequence(pair_partitions.get("origin_inspected"), label="origin pairs"))
    inspected_pairs += len(
        _sequence(pair_partitions.get("salvaged_inspected"), label="salvaged pairs")
    )
    unresolved_pairs = len(
        _sequence(pair_partitions.get("remaining_unresolved"), label="unresolved pairs")
    )
    observed_counts = {
        "registered_sources": source_state.source_count,
        "registered_assets": source_state.asset_count,
        "sources_in_baseline": len(sources),
        "records": len(nodes),
        "evidence_records": record_types["evidence"],
        "atoms": record_types["atom"],
        "moves": record_types["move"],
        "threads": record_types["thread"],
        "source_local_edges": origins["source_local"],
        "cross_source_relationships": origins["cross_source"],
        "inspected_source_pairs": inspected_pairs,
        "unresolved_source_pairs": unresolved_pairs,
    }
    expected_counts = _mapping(input_lock.get("counts"), label="input-lock counts")
    if observed_counts != expected_counts:
        raise PublicReleaseValidationError(
            f"private aggregate counts differ from input lock: {observed_counts}"
        )
    if len(nodes) != graph_manifest.get("node_count"):
        raise PublicReleaseValidationError("graph manifest node count is stale")
    if origins["source_local"] != graph_manifest.get("source_local_edge_count"):
        raise PublicReleaseValidationError("graph manifest local-edge count is stale")
    if origins["cross_source"] != graph_manifest.get("cross_source_edge_count"):
        raise PublicReleaseValidationError("graph manifest cross-source count is stale")
    graph_descriptor = _mapping(
        graph_manifest.get("corpus_graph"), label="graph manifest corpus graph"
    )
    if graph_descriptor.get("sha256") != graph_sha256:
        raise PublicReleaseValidationError("graph manifest does not bind the supplied graph")
    report_graph = _mapping(private_report.get("graph"), label="private report graph")
    report_manifest = _mapping(
        private_report.get("graph_manifest"), label="private report graph manifest"
    )
    if report_graph.get("sha256") != graph_sha256:
        raise PublicReleaseValidationError("private report does not bind the supplied graph")
    if report_manifest.get("sha256") != graph_manifest_sha256:
        raise PublicReleaseValidationError(
            "private report does not bind the supplied graph manifest"
        )
    quality_counts = _mapping(
        graph_manifest.get("quality_label_counts"), label="graph quality counts"
    )
    if not set(quality_counts).issubset(_SOURCE_QUALITY_LABELS):
        raise PublicReleaseValidationError("graph quality-label counts use an unknown label")
    validated_quality_counts = {
        label: _nonnegative_integer(value, label="quality-label count")
        for label, value in quality_counts.items()
    }
    if sum(validated_quality_counts.values()) != len(sources):
        raise PublicReleaseValidationError("graph quality-label counts do not cover all sources")
    if not set(relationship_kinds).issubset(RELATIONSHIP_TYPES):
        raise PublicReleaseValidationError("relationship-kind counts use an unknown kind")
    if sum(relationship_kinds.values()) != origins["cross_source"]:
        raise PublicReleaseValidationError("relationship-kind counts are incomplete")

    metrics = {
        **_AGGREGATE_METRICS_FIXED_FIELDS,
        "private_graph_sha256": graph_sha256,
        "counts": observed_counts,
        "source_quality_labels": dict(sorted(validated_quality_counts.items())),
    }
    relationship_summary = {
        **_RELATIONSHIP_SUMMARY_FIXED_FIELDS,
        "private_graph_sha256": graph_sha256,
        "relationship_count": origins["cross_source"],
        "relationship_kind_counts": dict(sorted(relationship_kinds.items())),
        "inspected_source_pairs": inspected_pairs,
        "unresolved_source_pairs": unresolved_pairs,
        "withheld_fields": [
            "relationship_ids",
            "source_pairs",
            "endpoints",
            "statements",
            "evidence_spans",
            "rationales",
            "scopes",
            "qualifications",
        ],
    }
    return {"metrics": metrics, "relationship_summary": relationship_summary}


def _selected_public_paths(
    repository_root: Path,
    policy: Mapping[str, Any],
    *,
    revision: str,
) -> tuple[Path, ...]:
    allowlist = policy.get("public_path_allowlist")
    if not isinstance(allowlist, list) or not all(isinstance(item, str) for item in allowlist):
        raise PublicReleaseValidationError("publication allowlist is invalid")
    completed = _run_git(
        repository_root,
        ("ls-tree", "-r", "--name-only", "-z", revision),
    )
    selected: list[Path] = []
    for text in completed.stdout.split("\0"):
        if not text:
            continue
        relative = PurePosixPath(text)
        if relative.is_absolute() or ".." in relative.parts:
            raise PublicReleaseValidationError(f"repository path escapes root: {text}")
        if not any(fnmatch.fnmatchcase(relative.as_posix(), pattern) for pattern in allowlist):
            continue
        path = Path(*relative.parts)
        selected.append(path)
    required = {
        *_PUBLIC_REQUIRED_PATHS,
        Path("pyproject.toml"),
        Path("uv.lock"),
        Path("reference_mapping_graph/publication-policy.yaml"),
        Path("reference_mapping_graph/rights-inventory.yaml"),
        Path("reference_mapping_graph/baseline-v1/input-lock.json"),
    }
    missing = sorted(path.as_posix() for path in required.difference(selected))
    if missing:
        raise PublicReleaseValidationError(
            "required public source paths are missing: " + ", ".join(missing)
        )
    _validate_relative_paths(selected, policy)
    return tuple(sorted(set(selected), key=lambda path: path.as_posix()))


def _validate_candidate_paths(candidate_root: Path, policy: Mapping[str, Any]) -> None:
    paths = [path.relative_to(candidate_root) for path in _candidate_files(candidate_root)]
    _validate_relative_paths(paths, policy)
    allowlist = policy.get("public_path_allowlist")
    if not isinstance(allowlist, list) or not all(isinstance(item, str) for item in allowlist):
        raise PublicReleaseValidationError("candidate publication allowlist is invalid")
    for path in paths:
        text = path.as_posix()
        if not any(fnmatch.fnmatchcase(text, pattern) for pattern in allowlist):
            raise PublicReleaseValidationError(f"candidate path is not allowlisted: {text}")


def _validate_reference_mapping_allowlist(policy: Mapping[str, Any]) -> None:
    allowlist = policy.get("public_path_allowlist")
    if not isinstance(allowlist, list) or not all(isinstance(item, str) for item in allowlist):
        raise PublicReleaseValidationError("publication allowlist is invalid")
    reference_paths = {item for item in allowlist if item.startswith("reference_mapping_graph/")}
    if reference_paths != _PUBLIC_REFERENCE_MAPPING_PATHS:
        raise PublicReleaseValidationError(
            "reference mapping publication paths must match the exact approved set"
        )


def _validate_relative_paths(paths: Sequence[Path], policy: Mapping[str, Any]) -> None:
    forbidden_segments = policy.get("forbidden_path_segments")
    forbidden_suffixes = policy.get("forbidden_suffixes")
    forbidden_name_patterns = policy.get("forbidden_name_patterns")
    if not isinstance(forbidden_segments, list) or not all(
        isinstance(value, str) for value in forbidden_segments
    ):
        raise PublicReleaseValidationError("forbidden path segments are invalid")
    if not isinstance(forbidden_suffixes, list) or not all(
        isinstance(value, str) for value in forbidden_suffixes
    ):
        raise PublicReleaseValidationError("forbidden suffixes are invalid")
    if not isinstance(forbidden_name_patterns, list) or not all(
        isinstance(value, str) for value in forbidden_name_patterns
    ):
        raise PublicReleaseValidationError("forbidden name patterns are invalid")
    segments = {value.casefold() for value in (*forbidden_segments, *_TRANSIENT_DIRECTORIES)}
    suffixes = tuple(value.casefold() for value in forbidden_suffixes)
    name_patterns = tuple(value.casefold() for value in forbidden_name_patterns)
    for path in paths:
        relative = PurePosixPath(path.as_posix())
        if relative.is_absolute() or ".." in relative.parts:
            raise PublicReleaseValidationError(f"candidate path escapes root: {path}")
        if any(part.casefold() in segments for part in relative.parts):
            raise PublicReleaseValidationError(f"candidate uses forbidden path segment: {path}")
        if relative.name.casefold().endswith(suffixes):
            raise PublicReleaseValidationError(f"candidate uses forbidden file suffix: {path}")
        if any(fnmatch.fnmatchcase(relative.name.casefold(), pattern) for pattern in name_patterns):
            raise PublicReleaseValidationError(f"candidate uses forbidden file name: {path}")


def _scan_candidate_content(
    candidate_root: Path,
    *,
    registered_asset_fingerprints: Sequence[tuple[str, str, int]] = (),
    private_work_identifiers: Sequence[str] = (),
) -> None:
    private_key_header = re.compile(
        r"-----BEGIN (?:(?:OPENSSH |RSA |EC |DSA |ENCRYPTED )?PRIVATE KEY|"
        r"PGP PRIVATE KEY BLOCK)-----"
    )
    local_path_markers = (
        "/" + "Users/",
        "/" + "home/",
        "/" + "root/",
        "/" + "tmp/",
        "/" + "var/lib/",
        "/" + "var/" + "tmp/",
        "/" + "var/run/",
        "/" + "private/" + "var/folders/",
        "file" + "://",
    )
    basecamp_markers = (
        "app.base" + "camp.com/",
        "3.base" + "camp.com/",
        "base" + "camp.com/",
        "basecampapi." + "com/",
    )
    token_patterns = (
        re.compile("gh" + r"[pousr]_[A-Za-z0-9]{30,}"),
        re.compile("github" + r"_pat_[A-Za-z0-9_]{20,}"),
        re.compile("sk" + r"-[A-Za-z0-9_-]{20,}"),
        re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}"),
        re.compile(r"glpat-[A-Za-z0-9_-]{20,}"),
        re.compile(r"xox[baprs]-[A-Za-z0-9-]{20,}"),
        re.compile(r"(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}"),
        re.compile(r"hf_[A-Za-z0-9]{20,}"),
        re.compile(r"AIza[0-9A-Za-z_-]{35}"),
        re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
        re.compile("AGE-" + r"SECRET-KEY-1[0-9A-Z]{20,}", re.IGNORECASE),
    )
    credential_assignment = re.compile(
        r"(?i)\b(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
        r"client[_-]?secret|private[_-]?key|database_url|db_url|auth[_-]?token)\b"
        r"\s*(?::|=)\s*(?:[\"'][^\r\n\"']{8,}[\"']|[^\s#\"']{8,}|"
        r"[A-Za-z0-9_-]{4,}(?:[ \t]+[A-Za-z0-9_-]{4,})+)"
    )
    multiline_credential_assignment = re.compile(
        r"(?im)^[ \t]*(?:-[ \t]+)?(?:password|passwd|pwd|secret|token|"
        r"api[_-]?key|access[_-]?key|client[_-]?secret|private[_-]?key|database_url|"
        r"db_url|auth[_-]?token)\b[ \t]*(?::|=)[ \t]*(?:"
        r"(?:!![A-Za-z0-9_:.-]+[ \t]+)?[>|][+-]?[0-9]*[ \t]*(?:\r?\n|$)"
        r"|(?:\"\"\"|'''))"
    )
    tagged_credential_assignment = re.compile(
        r"(?im)^[ \t]*(?:-[ \t]+)?(?:password|passwd|pwd|secret|token|"
        r"api[_-]?key|access[_-]?key|client[_-]?secret|private[_-]?key|database_url|"
        r"db_url|auth[_-]?token)\b[ \t]*:[ \t]*!![A-Za-z0-9_:.-]+[ \t]+[^\s#]{8,}"
    )
    prefixed_credential_assignment = re.compile(
        r"(?im)^[ \t]*(?:-[ \t]+)?(?:[A-Z][A-Z0-9_]*_)?"
        r"(?:PASSWORD|PASSWD|PWD|SECRET|TOKEN|"
        r"API_KEY|ACCESS_KEY|CLIENT_SECRET|PRIVATE_KEY|DATABASE_URL|DB_URL|AUTH_TOKEN)"
        r"[ \t]*=[ \t]*(?:[\"'][^\r\n\"']{8,}[\"']|[^\s#]{8,})"
    )
    json_credential_assignment = re.compile(
        r"(?i)[\"'](?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
        r"client[_-]?secret|private[_-]?key|database_url|db_url|auth[_-]?token|"
        r"[a-z0-9_]*(?:_token|_secret|_key))[\"'][ \t]*:[ \t]*"
        r"[\"'][^\r\n\"']{8,}[\"']"
    )
    credential_uri = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^@\s/]+@[^\s/]+")
    windows_path = re.compile(r"(?i)(?<![A-Za-z0-9])[A-Z]:[\\/]")
    portable_home_path = re.compile(
        r"(?i)(?<![A-Za-z0-9_])(?:~|\$(?:HOME|USERPROFILE)|"
        r"\$\{(?:HOME|USERPROFILE)\}|%(?:USERPROFILE|HOMEPATH)%)[\\/]"
    )
    absolute_posix_path = re.compile(
        r"(?<![A-Za-z0-9._:/!~<}*\-])/(?!/|dev/null(?:\b|$))"
        r"[A-Za-z0-9.~+%-][A-Za-z0-9._~+%-]*(?:/[A-Za-z0-9._~+%-]+)*"
    )
    unc_path = re.compile(r"(?<!:)(?:\\\\|//)[A-Za-z0-9][A-Za-z0-9._$-]*[\\/][A-Za-z0-9._$-]+")
    private_repository = re.compile(
        r"(?i)(?:"
        r"https?://(?:www\.)?github\.com/Liaust/superdeterminism"
        r"|git@github\.com:Liaust/superdeterminism"
        r"|ssh://git@github\.com/Liaust/superdeterminism"
        r")(?:\.git)?(?![A-Za-z0-9._-])"
    )
    registered_assets = {
        (sha256, size_bytes) for _path, sha256, size_bytes in registered_asset_fingerprints
    }

    def assert_safe_text(text: str, relative: str) -> None:
        path_scan_text = (
            _mask_approved_gitignore_root_anchors(text) if relative == ".gitignore" else text
        )
        if private_key_header.search(text):
            raise PublicReleaseValidationError(f"candidate contains a private key: {relative}")
        if (
            any(marker in path_scan_text for marker in local_path_markers)
            or windows_path.search(path_scan_text)
            or portable_home_path.search(path_scan_text)
            or absolute_posix_path.search(path_scan_text)
            or unc_path.search(path_scan_text)
        ):
            raise PublicReleaseValidationError(
                f"candidate contains a machine-local path: {relative}"
            )
        if (
            any(marker in text.casefold() for marker in basecamp_markers)
            or private_repository.search(text)
            or any(
                _contains_private_work_identifier(text, identifier)
                for identifier in private_work_identifiers
            )
        ):
            raise PublicReleaseValidationError(
                f"candidate contains a private work URL or identifier: {relative}"
            )
        if (
            any(pattern.search(text) for pattern in token_patterns)
            or credential_assignment.search(text)
            or prefixed_credential_assignment.search(text)
            or json_credential_assignment.search(text)
            or multiline_credential_assignment.search(text)
            or tagged_credential_assignment.search(text)
            or credential_uri.search(text)
        ):
            raise PublicReleaseValidationError(
                f"candidate contains a credential pattern: {relative}"
            )

    for path in _candidate_files(candidate_root):
        relative = path.relative_to(candidate_root).as_posix()
        if path.is_symlink():
            raise PublicReleaseValidationError(f"candidate contains a symbolic link: {relative}")
        raw = path.read_bytes()
        raw_fingerprint = (hashlib.sha256(raw).hexdigest(), len(raw))
        if raw_fingerprint in registered_assets:
            raise PublicReleaseValidationError(f"candidate contains a source asset: {relative}")
        if _PDF_HEADER in raw:
            raise PublicReleaseValidationError(f"candidate contains PDF bytes: {relative}")
        if raw.startswith(_SQLITE_HEADER):
            raise PublicReleaseValidationError(f"candidate contains a SQLite database: {relative}")
        if _NUL_BYTE in raw:
            raise PublicReleaseValidationError(
                f"candidate contains unexplained binary data: {relative}"
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise PublicReleaseValidationError(
                f"candidate contains non-UTF-8 data: {relative}"
            ) from error
        scan_text = relative + "\n" + text
        decoded_payloads = _decoded_candidate_payloads(scan_text)
        for text_view in _candidate_text_views(scan_text, decoded_payloads=decoded_payloads):
            assert_safe_text(text_view, relative)
        if path.suffix.casefold() == ".py":
            string_literals, byte_literals = _python_literal_payloads(text, relative=relative)
            for string_literal in string_literals:
                if any(
                    _contains_private_work_identifier(string_literal, identifier)
                    for identifier in private_work_identifiers
                ):
                    raise PublicReleaseValidationError(
                        f"candidate contains a private work URL or identifier: {relative}"
                    )
            for byte_literal in byte_literals:
                literal_fingerprint = (
                    hashlib.sha256(byte_literal).hexdigest(),
                    len(byte_literal),
                )
                if _PDF_HEADER in byte_literal or literal_fingerprint in registered_assets:
                    raise PublicReleaseValidationError(
                        f"candidate contains an encoded source asset: {relative}"
                    )
                if _NUL_BYTE in byte_literal or byte_literal.startswith(
                    (_SQLITE_HEADER, *_BINARY_SIGNATURES)
                ):
                    raise PublicReleaseValidationError(
                        f"candidate contains encoded unexplained binary data: {relative}"
                    )
                try:
                    literal_text = byte_literal.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise PublicReleaseValidationError(
                        f"candidate contains encoded unexplained binary data: {relative}"
                    ) from error
                if any(
                    _contains_private_work_identifier(literal_text, identifier)
                    for identifier in private_work_identifiers
                ):
                    raise PublicReleaseValidationError(
                        f"candidate contains a private work URL or identifier: {relative}"
                    )
        for decoded in decoded_payloads:
            decoded_fingerprint = (hashlib.sha256(decoded).hexdigest(), len(decoded))
            if _PDF_HEADER in decoded or decoded_fingerprint in registered_assets:
                raise PublicReleaseValidationError(
                    f"candidate contains an encoded source asset: {relative}"
                )
            if decoded.startswith(_SQLITE_HEADER):
                raise PublicReleaseValidationError(
                    f"candidate contains an encoded SQLite database: {relative}"
                )
            if decoded.startswith(_BINARY_SIGNATURES):
                raise PublicReleaseValidationError(
                    f"candidate contains encoded unexplained binary data: {relative}"
                )
        for decoded in _explicit_encoded_payloads(text):
            if _NUL_BYTE in decoded:
                raise PublicReleaseValidationError(
                    f"candidate contains encoded unexplained binary data: {relative}"
                )
            try:
                decoded.decode("utf-8")
            except UnicodeDecodeError as error:
                raise PublicReleaseValidationError(
                    f"candidate contains encoded unexplained binary data: {relative}"
                ) from error


def _python_literal_payloads(
    text: str,
    *,
    relative: str,
    concatenations_only: bool = False,
) -> tuple[tuple[str, ...], tuple[bytes, ...]]:
    """Return literal payloads, including implicit and additive concatenations."""

    try:
        tree = ast.parse(text)
    except SyntaxError as error:
        raise PublicReleaseValidationError(
            f"candidate contains invalid Python source: {relative}"
        ) from error

    def literal_value(node: ast.AST) -> str | bytes | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
            return node.value
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = literal_value(node.left)
            right = literal_value(node.right)
            if isinstance(left, str) and isinstance(right, str):
                combined: str | bytes = left + right
            elif isinstance(left, bytes) and isinstance(right, bytes):
                combined = left + right
            else:
                return None
            if len(combined) > _MAX_ENCODED_PAYLOAD_CHARACTERS:
                raise PublicReleaseValidationError(
                    f"candidate contains an oversized literal concatenation: {relative}"
                )
            return combined
        return None

    values = {
        value
        for node in ast.walk(tree)
        if (
            not concatenations_only
            and isinstance(node, ast.Constant)
            and isinstance(node.value, (str, bytes))
            or isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Add)
        )
        and (value := literal_value(node)) is not None
    }
    strings = tuple(sorted(value for value in values if isinstance(value, str)))
    byte_values = tuple(sorted(value for value in values if isinstance(value, bytes)))
    return strings, byte_values


def _mask_approved_gitignore_root_anchors(text: str) -> str:
    """Distinguish approved repo-root ignore rules from machine-local paths."""

    lines: list[str] = []
    for line in text.splitlines(keepends=True):
        match = re.match(r"/(?P<anchor>[A-Za-z0-9._-]+)(?=/|$)", line)
        if match is not None and match.group("anchor") in _PUBLIC_GITIGNORE_ROOT_ANCHORS:
            line = line[1:]
        lines.append(line)
    return "".join(lines)


def _decode_candidate_payloads_once(text: str) -> tuple[bytes, ...]:
    """Decode one layer of bounded reversible text tokens."""

    decoded_payloads: set[bytes] = set()
    tokens = re.finditer(
        r"(?<![A-Za-z0-9+_-])[A-Za-z0-9+/_-]{16,}={0,2}(?![A-Za-z0-9+/_=-])",
        text,
    )
    for match in tokens:
        encoded_text = match.group(0)
        if len(encoded_text) > _MAX_ENCODED_PAYLOAD_CHARACTERS:
            raise PublicReleaseValidationError("candidate contains an oversized encoded payload")
        padded = encoded_text + "=" * ((4 - len(encoded_text) % 4) % 4)
        for altchars in (None, b"-_"):
            try:
                decoded = base64.b64decode(padded, altchars=altchars, validate=True)
            except (binascii.Error, ValueError):
                continue
            if len(decoded) >= 8:
                decoded_payloads.add(decoded)
    delimited_tokens = re.finditer(
        r"(?<=[/=:;])[A-Za-z0-9+_-]{16,}={0,2}(?![A-Za-z0-9+/_=-])",
        text,
    )
    for match in delimited_tokens:
        encoded_text = match.group(0)
        if len(encoded_text) > _MAX_ENCODED_PAYLOAD_CHARACTERS:
            raise PublicReleaseValidationError("candidate contains an oversized encoded payload")
        padded = encoded_text + "=" * ((4 - len(encoded_text) % 4) % 4)
        for altchars in (None, b"-_"):
            try:
                decoded = base64.b64decode(padded, altchars=altchars, validate=True)
            except (binascii.Error, ValueError):
                continue
            if len(decoded) >= 8:
                decoded_payloads.add(decoded)
    wrapped_base64 = re.finditer(
        r"(?<![A-Za-z0-9+_-])[A-Za-z0-9+/_-]{8,}={0,2}"
        r"(?:(?:\\r\\n|\\n|\r?\n)[ \t]*[A-Za-z0-9+/_-]{8,}={0,2})+"
        r"(?![A-Za-z0-9+/_=-])",
        text,
    )
    for match in wrapped_base64:
        encoded_text = re.sub(r"(?:\\r\\n|\\n|\r?\n)[ \t]*", "", match.group(0))
        if len(encoded_text) > _MAX_ENCODED_PAYLOAD_CHARACTERS:
            raise PublicReleaseValidationError("candidate contains an oversized encoded payload")
        padded = encoded_text + "=" * ((4 - len(encoded_text) % 4) % 4)
        for altchars in (None, b"-_"):
            try:
                decoded = base64.b64decode(padded, altchars=altchars, validate=True)
            except (binascii.Error, ValueError):
                continue
            if len(decoded) >= 8:
                decoded_payloads.add(decoded)
    hex_tokens = re.finditer(
        r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{16,}(?![0-9A-Fa-f])",
        text,
    )
    for match in hex_tokens:
        encoded_text = match.group(0)
        if len(encoded_text) > _MAX_ENCODED_PAYLOAD_CHARACTERS:
            raise PublicReleaseValidationError("candidate contains an oversized encoded payload")
        if len(encoded_text) % 2:
            continue
        try:
            decoded = bytes.fromhex(encoded_text)
        except ValueError:
            continue
        if len(decoded) >= 8:
            decoded_payloads.add(decoded)
    wrapped_hex = re.finditer(
        r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{8,}"
        r"(?:(?:\\r\\n|\\n|\r?\n)[ \t]*[0-9A-Fa-f]{8,})+"
        r"(?![0-9A-Fa-f])",
        text,
    )
    for match in wrapped_hex:
        encoded_text = re.sub(r"(?:\\r\\n|\\n|\r?\n)[ \t]*", "", match.group(0))
        if len(encoded_text) > _MAX_ENCODED_PAYLOAD_CHARACTERS:
            raise PublicReleaseValidationError("candidate contains an oversized encoded payload")
        if len(encoded_text) % 2:
            continue
        try:
            decoded = bytes.fromhex(encoded_text)
        except ValueError:
            continue
        if len(decoded) >= 8:
            decoded_payloads.add(decoded)
    return tuple(decoded_payloads)


def _decoded_candidate_payloads(text: str) -> tuple[bytes, ...]:
    """Decode supported reversible text transformations to bounded closure."""

    decoded_payloads: set[bytes] = set()
    expanded_initial_views = _expanded_candidate_text_views(text)
    initial_views = tuple(
        {
            *expanded_initial_views,
            *(
                joined
                for text_view in expanded_initial_views
                for joined in _adjacent_encoded_scalar_views(text_view)
            ),
        }
    )
    text_frontier = list(initial_views)
    seen_texts = set(initial_views)
    total_bytes = 0
    while text_frontier:
        next_frontier: list[str] = []
        for text_view in text_frontier:
            explicit_payloads = {
                decoded
                for carrier in _explicit_base64_carriers(text_view)
                for decoded in _decode_base64_carrier(carrier, minimum_decoded_bytes=1)
            }
            for decoded in (*_decode_candidate_payloads_once(text_view), *explicit_payloads):
                if decoded in decoded_payloads:
                    continue
                decoded_payloads.add(decoded)
                total_bytes += len(decoded)
                if (
                    len(decoded_payloads) > _MAX_DECODED_PAYLOADS_PER_FILE
                    or total_bytes > _MAX_DECODED_PAYLOAD_BYTES_PER_FILE
                ):
                    raise PublicReleaseValidationError(
                        "candidate contains too many nested encoded payloads"
                    )
                try:
                    decoded_text = decoded.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                decoded_views = _expanded_candidate_text_views(decoded_text)
                for expanded_text in (
                    *decoded_views,
                    *(
                        joined
                        for decoded_view in decoded_views
                        for joined in _adjacent_encoded_scalar_views(decoded_view)
                    ),
                ):
                    if expanded_text not in seen_texts:
                        seen_texts.add(expanded_text)
                        next_frontier.append(expanded_text)
        text_frontier = next_frontier
    return tuple(sorted(decoded_payloads))


def _adjacent_encoded_scalar_views(text: str) -> tuple[str, ...]:
    """Reassemble short Base64 scalars in JSON, Python, or YAML sequences."""

    joined_views: set[str] = set()
    encoded_chunk = r"[A-Za-z0-9+/_-]{2,64}={0,2}"
    scalar = rf'(?:"{encoded_chunk}"|\'{encoded_chunk}\')'
    inline_sequences = re.finditer(
        rf"(?:\[(?P<bracket>(?:\s*{scalar}\s*,){{1,}}\s*{scalar}\s*,?\s*)\]"
        rf"|\((?P<tuple>(?:\s*{scalar}\s*,){{1,}}\s*{scalar}\s*,?\s*)\))",
        text,
        re.DOTALL,
    )
    yaml_sequences = re.finditer(
        rf"(?m)(?:^[ \t]*-[ \t]*(?:{scalar}|{encoded_chunk})[ \t]*(?:\r?\n|$)){{2,}}",
        text,
    )
    for match in inline_sequences:
        matched = match.group(0)
        if len(matched) > _MAX_ENCODED_PAYLOAD_CHARACTERS * 2:
            raise PublicReleaseValidationError("candidate contains an oversized encoded payload")
        chunks = re.findall(r'["\']([A-Za-z0-9+/_-]{2,64}={0,2})["\']', matched)
        _add_reassembled_encoded_chunks(joined_views, chunks)
    for match in yaml_sequences:
        matched = match.group(0)
        if len(matched) > _MAX_ENCODED_PAYLOAD_CHARACTERS * 2:
            raise PublicReleaseValidationError("candidate contains an oversized encoded payload")
        chunks = [
            line.split("-", 1)[1].strip().strip("\"'")
            for line in matched.splitlines()
            if "-" in line
        ]
        _add_reassembled_encoded_chunks(joined_views, chunks)
    return tuple(sorted(joined_views))


def _add_reassembled_encoded_chunks(joined_views: set[str], chunks: Sequence[str]) -> None:
    """Validate and add one adjacent sequence of encoded scalar chunks."""

    if len(chunks) < 2 or any(
        not 4 <= len(chunk) <= 66 or not re.fullmatch(r"[A-Za-z0-9+/_-]{2,64}={0,2}", chunk)
        for chunk in chunks
    ):
        return
    joined = "".join(chunks)
    if len(joined) < 16:
        return
    if len(joined) > _MAX_ENCODED_PAYLOAD_CHARACTERS:
        raise PublicReleaseValidationError("candidate contains an oversized encoded payload")
    joined_views.add(joined)


def _has_base64_signal(value: str) -> bool:
    return any(character in "+=" for character in value) or (
        any(character.isupper() for character in value)
        and any(character.islower() for character in value)
        and any(character.isdigit() for character in value)
    )


def _explicit_base64_carriers(text: str) -> tuple[str, ...]:
    """Return payloads whose surrounding label explicitly declares Base64 data."""

    return tuple(
        match.group(1)
        for match in re.finditer(
            r"(?im)\b(?:base64|b64)\s*(?::|=)\s*[\"']?"
            r"([A-Za-z0-9+/_-]{4,}={0,2})(?![A-Za-z0-9+/_=-])",
            text,
        )
    )


def _decode_base64_carrier(
    encoded_text: str,
    *,
    minimum_decoded_bytes: int,
) -> tuple[bytes, ...]:
    """Decode one bounded Base64 carrier with standard and URL-safe alphabets."""

    if len(encoded_text) > _MAX_ENCODED_PAYLOAD_CHARACTERS:
        raise PublicReleaseValidationError("candidate contains an oversized encoded payload")
    padded = encoded_text + "=" * ((4 - len(encoded_text) % 4) % 4)
    decoded_payloads: set[bytes] = set()
    for altchars in (None, b"-_"):
        try:
            decoded = base64.b64decode(padded, altchars=altchars, validate=True)
        except (binascii.Error, ValueError):
            continue
        if len(decoded) >= minimum_decoded_bytes:
            decoded_payloads.add(decoded)
    return tuple(sorted(decoded_payloads))


def _explicit_encoded_payloads(text: str) -> tuple[bytes, ...]:
    """Decode strong encoded tokens and complete scalar or wrapped carriers."""

    declared_base64_carriers = set(_explicit_base64_carriers(text))
    carriers = {text.strip()}
    carriers.update(line.strip() for line in text.splitlines())
    carriers.update(_adjacent_encoded_scalar_views(text))
    carriers.update(declared_base64_carriers)
    carriers.update(
        match.group(2)
        for match in re.finditer(
            r"(?im)\b([A-Za-z][A-Za-z0-9_.-]{0,63})\s*(?::|=)\s*[\"']?"
            r"([A-Za-z0-9+/_-]{16,}={0,2})(?![A-Za-z0-9+/_=-])",
            text,
        )
        if any(character in "+=" for character in match.group(2))
        or (
            match.group(1).casefold() in {"base64", "blob", "content", "data", "encoded", "payload"}
            and _has_base64_signal(match.group(2))
        )
    )
    payloads: set[bytes] = set()
    for carrier in carriers:
        if not carrier:
            continue
        if _PRIVATE_TOPOLOGY_TOKEN.fullmatch(carrier) or re.fullmatch(
            r"xref_(?:batch|candidate|job|rel|shard)_[0-9A-Za-z]+",
            carrier,
        ):
            continue
        has_base64_signal = _has_base64_signal(carrier)
        is_base64 = has_base64_signal and bool(
            re.fullmatch(
                r"[A-Za-z0-9+/_-]{8,}={0,2}"
                r"(?:(?:\\r\\n|\\n|\r?\n)[ \t]*[A-Za-z0-9+/_-]{8,}={0,2})*",
                carrier,
            )
        )
        is_hex = bool(
            len(carrier) % 2 == 0
            and re.fullmatch(
                r"[0-9A-Fa-f]{16,}"
                r"(?:(?:\\r\\n|\\n|\r?\n)[ \t]*[0-9A-Fa-f]{8,})*",
                carrier,
            )
        )
        if is_base64 or is_hex:
            if carrier in declared_base64_carriers:
                payloads.update(_decode_base64_carrier(carrier, minimum_decoded_bytes=1))
            else:
                payloads.update(_decoded_candidate_payloads(carrier))
    return tuple(sorted(payloads))


def _candidate_text_views(
    text: str,
    *,
    decoded_payloads: Sequence[bytes],
) -> tuple[str, ...]:
    """Return direct, decoded-payload, and structured text views."""

    views = set(_expanded_candidate_text_views(text))
    for decoded in decoded_payloads:
        try:
            decoded_text = decoded.decode("utf-8")
        except UnicodeDecodeError:
            continue
        views.update(_expanded_candidate_text_views(decoded_text))
    return tuple(sorted(views))


def _expanded_candidate_text_views(text: str) -> tuple[str, ...]:
    """Expand structured and reversible text escapes until no new view appears."""

    views = {text}
    frontier = [text]
    while frontier:
        text_view = frontier.pop()
        transformed = text_view
        for _ in range(64):
            next_value = _strip_lightweight_markup(
                _strip_markup_tags(
                    _strip_default_ignorables(
                        html.unescape(
                            _decode_percent_escapes(
                                _decode_text_escapes(_decode_json_unicode_escapes(transformed))
                            )
                        )
                    )
                )
            )
            if next_value == transformed:
                break
            transformed = next_value
        else:
            raise PublicReleaseValidationError(
                "candidate contains excessively nested reversible text escapes"
            )
        expanded = {transformed} if transformed != text_view else set()
        for structured_text in (text_view, transformed):
            try:
                json_value = json.loads(structured_text)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            expanded.update(_json_string_values(json_value))
        for value in expanded:
            if value not in views:
                views.add(value)
                frontier.append(value)
    return tuple(sorted(views))


def _strip_markup_tags(text: str) -> str:
    return re.sub(r"</?[A-Za-z][^<>\r\n]{0,255}>", "", text)


def _strip_lightweight_markup(text: str) -> str:
    """Expose text hidden behind common Markdown emphasis delimiters."""

    return re.sub(
        r"(?<!\\)(?:"
        r"\*{1,3}(?=[A-Za-z0-9])|(?<=[A-Za-z0-9])\*{1,3}"
        r"|~{2,}(?=[A-Za-z0-9])|(?<=[A-Za-z0-9])~{2,}"
        r"|_{2,}(?=[A-Za-z0-9])|(?<=[A-Za-z0-9])_{2,}"
        r")",
        "",
        text,
    )


def _is_default_ignorable(character: str) -> bool:
    codepoint = ord(character)
    return (
        unicodedata.category(character) == "Cf"
        or codepoint == 0x034F
        or 0x115F <= codepoint <= 0x1160
        or 0x17B4 <= codepoint <= 0x17B5
        or 0x180B <= codepoint <= 0x180F
        or 0x2060 <= codepoint <= 0x206F
        or codepoint == 0x3164
        or 0xFE00 <= codepoint <= 0xFE0F
        or codepoint == 0xFFA0
        or 0xFFF0 <= codepoint <= 0xFFF8
        or 0x1BCA0 <= codepoint <= 0x1BCA3
        or 0x1D173 <= codepoint <= 0x1D17A
        or 0xE0000 <= codepoint <= 0xE0FFF
    )


def _strip_default_ignorables(text: str) -> str:
    return "".join(character for character in text if not _is_default_ignorable(character))


def _decode_json_unicode_escapes(text: str) -> str:
    def decode_match(match: re.Match[str]) -> str:
        try:
            decoded = json.loads(f'"{match.group(0)}"')
            decoded.encode("utf-8")
        except (json.JSONDecodeError, UnicodeEncodeError):
            return match.group(0)
        return decoded if isinstance(decoded, str) else match.group(0)

    return _JSON_UNICODE_ESCAPE_RUN.sub(decode_match, text)


def _decode_text_escapes(text: str) -> str:
    replacements = {r"\t": "\t", r"\n": "\n", r"\r": "\r"}

    def decode_match(match: re.Match[str]) -> str:
        value = match.group(0)
        if value in replacements:
            return replacements[value]
        return chr(int(value[2:], 16))

    return _TEXT_ESCAPE.sub(decode_match, text)


def _decode_percent_escapes(text: str) -> str:
    if not _PERCENT_ESCAPE.search(text):
        return text
    decoded_bytes = urllib.parse.unquote_to_bytes(text)
    if _NUL_BYTE in decoded_bytes:
        raise PublicReleaseValidationError(
            "candidate contains percent-encoded unexplained binary data"
        )
    try:
        return decoded_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PublicReleaseValidationError(
            "candidate contains percent-encoded unexplained binary data"
        ) from error


def _json_string_values(value: object) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, Mapping):
        return {
            text
            for key, child in value.items()
            for text in (*_json_string_values(key), *_json_string_values(child))
        }
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return {text for child in value for text in _json_string_values(child)}
    return set()


def _scan_for_private_semantic_text(
    candidate_root: Path,
    *,
    graph: Mapping[str, Any],
    graph_manifest: Mapping[str, Any],
    graph_manifest_path: Path,
    private_report: Mapping[str, Any],
    public_catalog: Sequence[Mapping[str, Any]],
    withheld_registration_strings: Sequence[str],
    canonical_vault_strings: Sequence[str],
) -> tuple[str, ...]:
    needles = _semantic_strings(graph, semantic_context=True)
    needles.update(_semantic_strings(graph_manifest, semantic_context=False))
    needles.update(_semantic_strings(private_report, semantic_context=False))
    needles.update(withheld_registration_strings)
    needles.update(canonical_vault_strings)
    needles.update(
        _manifest_bound_semantic_strings(
            graph_manifest=graph_manifest,
            graph_manifest_path=graph_manifest_path,
        )
    )
    safe_fragments = {_normalize_text(value).casefold() for value in _SAFE_OPERATIONAL_PRIVATE_TEXT}
    needles = {
        needle for needle in needles if _normalize_text(needle).casefold() not in safe_fragments
    }
    allowed_metadata = {
        _normalize_text(value).casefold()
        for source in public_catalog
        for key in ("source_id", "title", "type", "language")
        if isinstance((value := source.get(key)), str)
    }
    allowed_metadata.update(
        _normalize_text(creator).casefold()
        for source in public_catalog
        for creator in source.get("creators", [])
        if isinstance(creator, str)
    )
    filtered_needles: set[str] = set()
    for needle in needles:
        normalized_needle = _normalize_text(needle).casefold()
        if not any(normalized_needle in value for value in allowed_metadata):
            filtered_needles.add(needle)
    needles = filtered_needles
    normalized_needles = tuple(sorted(_normalize_text(needle).casefold() for needle in needles))
    _scan_candidate_text_against_private_needles(
        candidate_root,
        needles=normalized_needles,
    )
    return normalized_needles


def _scan_candidate_text_against_private_needles(
    candidate_root: Path,
    *,
    needles: Sequence[str],
) -> None:
    candidate_texts: dict[str, str] = {}
    for path in _candidate_files(candidate_root):
        relative = path.relative_to(candidate_root).as_posix()
        text = path.read_text(encoding="utf-8")
        scan_text = relative + "\n" + text
        decoded_payloads = _decoded_candidate_payloads(scan_text)
        views = [
            _normalize_text(text_view).casefold()
            for text_view in _candidate_text_views(scan_text, decoded_payloads=decoded_payloads)
        ]
        if path.suffix.casefold() == ".py":
            string_literals, byte_literals = _python_literal_payloads(
                text,
                relative=relative,
                concatenations_only=True,
            )
            views.extend(
                _normalize_text(text_view).casefold()
                for literal in string_literals
                for text_view in _candidate_text_views(literal, decoded_payloads=())
            )
            for literal in byte_literals:
                try:
                    literal_text = literal.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                views.extend(
                    _normalize_text(text_view).casefold()
                    for text_view in _candidate_text_views(literal_text, decoded_payloads=())
                )
        candidate_texts[relative] = "\n".join(views)
    substring_needles: dict[str, str] = {}
    bounded_substring_patterns: set[str] = set()
    for needle in needles:
        ascii_escaped_needle = _normalize_text(json.dumps(needle, ensure_ascii=True)[1:-1])
        substring_needles.setdefault(needle, needle)
        substring_needles.setdefault(ascii_escaped_needle, needle)
        if len(needle.split()) in {1, 4}:
            bounded_substring_patterns.update((needle, ascii_escaped_needle))
    matcher = _SubstringMatcher(
        substring_needles,
        bounded_patterns=bounded_substring_patterns,
    )
    for matching_path, candidate_text in candidate_texts.items():
        if substring_match := matcher.find(candidate_text):
            _raise_private_text_match(substring_match, matching_path)


class _SubstringMatcher:
    """A bounded Aho-Corasick matcher for one-pass private-needle scanning."""

    def __init__(
        self,
        patterns: Mapping[str, str],
        *,
        bounded_patterns: set[str] | None = None,
    ) -> None:
        self.transitions: list[dict[str, int]] = [{}]
        self.failures = [0]
        self.outputs: list[list[tuple[str, str]]] = [[]]
        self.bounded_patterns = bounded_patterns or set()
        for pattern, original in patterns.items():
            if not pattern:
                continue
            state = 0
            for character in pattern:
                next_state = self.transitions[state].get(character)
                if next_state is None:
                    next_state = len(self.transitions)
                    self.transitions[state][character] = next_state
                    self.transitions.append({})
                    self.failures.append(0)
                    self.outputs.append([])
                state = next_state
            self.outputs[state].append((pattern, original))
        pending: deque[int] = deque()
        for state in self.transitions[0].values():
            pending.append(state)
        while pending:
            state = pending.popleft()
            for character, next_state in self.transitions[state].items():
                pending.append(next_state)
                failure = self.failures[state]
                while failure and character not in self.transitions[failure]:
                    failure = self.failures[failure]
                self.failures[next_state] = self.transitions[failure].get(character, 0)
                self.outputs[next_state].extend(self.outputs[self.failures[next_state]])

    def find(self, text: str) -> str | None:
        state = 0
        for index, character in enumerate(text):
            while state and character not in self.transitions[state]:
                state = self.failures[state]
            state = self.transitions[state].get(character, 0)
            for pattern, original in self.outputs[state]:
                if pattern in self.bounded_patterns:
                    start = index - len(pattern) + 1
                    before = text[start - 1] if start > 0 else ""
                    after = text[index + 1] if index + 1 < len(text) else ""
                    if before.isalnum() or after.isalnum():
                        continue
                return original
        return None


def _raise_private_text_match(needle: str, matching_path: str) -> None:
    needle_sha256 = hashlib.sha256(needle.encode()).hexdigest()
    raise PublicReleaseValidationError(
        "candidate contains exact or source-derived private graph text "
        f"(fingerprint {needle_sha256}, path {matching_path})"
    )


def _semantic_strings(
    value: object,
    *,
    semantic_context: bool,
    key: str | None = None,
) -> set[str]:
    if isinstance(value, str):
        if value in _SAFE_OPERATIONAL_PRIVATE_TEXT:
            return set()
        protected_strings = _protected_text_fragments(
            value,
            exact=key in _PARTIAL_SOURCE_TEXT_KEYS,
            include_four_word_windows=key in _FOUR_WORD_SOURCE_TEXT_KEYS,
            minimum_length=32 if key in _LOW_ENTROPY_CONTROLLED_TEXT_KEYS else 12,
        )
        if key in _ATOMIC_PRIVATE_TEXT_KEYS and (normalized := _normalize_text(value)):
            protected_strings.add(normalized)
        protected_strings.update(_PRIVATE_TOPOLOGY_TOKEN.findall(value))
        return protected_strings
    if isinstance(value, Mapping):
        strings: set[str] = set()
        for child_key, child in value.items():
            strings.update(
                _semantic_strings(
                    child,
                    semantic_context=semantic_context,
                    key=str(child_key),
                )
            )
        return strings
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        strings = set()
        if (
            len(value) == 2
            and all(isinstance(child, str) for child in value)
            and all(re.fullmatch(r"LIB-[0-9]{3,}", child) for child in value)
        ):
            left, right = sorted(cast(str, child) for child in value)
            strings.update(
                {
                    f"{left}--{right}",
                    f"{left} ↔ {right}",
                    json.dumps([left, right], separators=(",", ":")),
                }
            )
        for child in value:
            strings.update(_semantic_strings(child, semantic_context=semantic_context, key=key))
        return strings
    return set()


def _manifest_bound_semantic_strings(
    *,
    graph_manifest: Mapping[str, Any],
    graph_manifest_path: Path,
) -> set[str]:
    strings: set[str] = set()
    for relative, path, artifact in _manifest_bound_input_paths(
        graph_manifest=graph_manifest,
        graph_manifest_path=graph_manifest_path,
    ):
        observed = fingerprint(path)
        if observed.sha256 != artifact.get("sha256") or observed.size_bytes != artifact.get(
            "size_bytes"
        ):
            raise PublicReleaseValidationError(
                f"manifest-bound private input fingerprint differs: {relative}"
            )
        suffix = relative.suffix.casefold()
        if suffix == ".md":
            try:
                document = load_relationship_markdown(path)
            except (ValueError, UnicodeError) as error:
                raise PublicReleaseValidationError(
                    f"manifest-bound relationship input is invalid: {relative}"
                ) from error
            for relationship in document.relationships:
                strings.update(_semantic_strings(relationship.to_dict(), semantic_context=False))
            continue
        if suffix in {".json", ".yaml", ".yml"}:
            try:
                text = path.read_text(encoding="utf-8")
                value = json.loads(text) if suffix == ".json" else yaml.safe_load(text)
            except (json.JSONDecodeError, UnicodeError, yaml.YAMLError) as error:
                raise PublicReleaseValidationError(
                    f"manifest-bound structured input is invalid: {relative}"
                ) from error
            strings.update(_semantic_strings(value, semantic_context=False))
            continue
        raise PublicReleaseValidationError(
            f"manifest-bound private input type is unsupported: {relative}"
        )
    return strings


def _manifest_bound_input_fingerprints(
    *,
    graph_manifest: Mapping[str, Any],
    graph_manifest_path: Path,
    validate_descriptors: bool,
) -> tuple[tuple[str, str, int], ...]:
    fingerprints: list[tuple[str, str, int]] = []
    for relative, path, artifact in _manifest_bound_input_paths(
        graph_manifest=graph_manifest,
        graph_manifest_path=graph_manifest_path,
    ):
        observed = fingerprint(path)
        if validate_descriptors and (
            observed.sha256 != artifact.get("sha256")
            or observed.size_bytes != artifact.get("size_bytes")
        ):
            raise PublicReleaseValidationError(
                f"manifest-bound private input fingerprint differs: {relative}"
            )
        fingerprints.append((relative.as_posix(), observed.sha256, observed.size_bytes))
    return tuple(fingerprints)


def _manifest_bound_input_paths(
    *,
    graph_manifest: Mapping[str, Any],
    graph_manifest_path: Path,
) -> tuple[tuple[PurePosixPath, Path, Mapping[str, Any]], ...]:
    inputs = _sequence(graph_manifest.get("inputs", []), label="graph manifest inputs")
    if not inputs:
        return ()
    graph_artifact = _mapping(
        graph_manifest.get("corpus_graph"), label="graph manifest corpus graph"
    )
    graph_relative = _safe_manifest_path(
        graph_artifact.get("path"), label="graph manifest corpus graph path"
    )
    namespace_root = graph_manifest_path.resolve()
    for _ in graph_relative.parts:
        namespace_root = namespace_root.parent
    if (namespace_root / graph_relative).resolve() != graph_manifest_path.with_name(
        "graph.json"
    ).resolve():
        raise PublicReleaseValidationError("graph manifest namespace is inconsistent")
    resolved_inputs: list[tuple[PurePosixPath, Path, Mapping[str, Any]]] = []
    for input_value in inputs:
        artifact = _mapping(input_value, label="graph manifest input")
        relative = _safe_manifest_path(artifact.get("path"), label="graph manifest input path")
        path = namespace_root / relative
        resolved = path.resolve()
        if not resolved.is_relative_to(namespace_root) or path.is_symlink() or not path.is_file():
            raise PublicReleaseValidationError(
                f"manifest-bound private input is missing or unsafe: {relative}"
            )
        resolved_inputs.append((relative, resolved, artifact))
    return tuple(resolved_inputs)


def _safe_manifest_path(value: object, *, label: str) -> PurePosixPath:
    if not isinstance(value, str):
        raise PublicReleaseValidationError(f"{label} must be a string")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise PublicReleaseValidationError(f"{label} is not a safe relative path")
    return path


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    without_format_controls = _strip_default_ignorables(normalized)
    return " ".join(without_format_controls.replace("\\n", " ").split())


def _protected_text_fragments(
    value: str,
    *,
    exact: bool,
    include_four_word_windows: bool = False,
    minimum_length: int = 12,
) -> set[str]:
    normalized = _normalize_text(value)
    raw_fragments = re.split(r"(?:\r?\n)+|(?<=[.!?])\s+", value.replace("\\n", "\n"))
    candidates = {normalized}
    candidates.update(_normalize_text(fragment) for fragment in raw_fragments)
    if exact:
        protected = {candidate for candidate in candidates if candidate}
        for candidate in tuple(protected):
            if include_four_word_windows:
                protected.update(
                    fragment for fragment in _word_windows(candidate, size=4) if len(fragment) >= 28
                )
            protected.update(
                fragment for fragment in _word_windows(candidate, size=5) if len(fragment) >= 24
            )
        return protected
    protected = {
        candidate
        for candidate in candidates
        if len(candidate) >= minimum_length and len(candidate.split()) >= 2
    }
    return protected


def _word_windows(value: str, *, size: int) -> tuple[str, ...]:
    words = value.split()
    if len(words) < size:
        return ()
    return tuple(" ".join(words[index : index + size]) for index in range(len(words) - size + 1))


def _looks_like_equation(value: str) -> bool:
    return bool(
        re.search(r"[=<>+*/^]|\\[A-Za-z]+", value)
        or any(unicodedata.category(character) == "Sm" for character in value)
    )


def _source_registration_withheld_strings(payload: Mapping[str, Any]) -> set[str]:
    admitted_paths = {
        ("library_id",),
        ("identity", "title"),
        ("identity", "creators"),
        ("identity", "year"),
        ("identity", "type"),
        ("identity", "language"),
    }

    def collect(value: object, path: tuple[str, ...]) -> set[str]:
        if path in admitted_paths:
            return set()
        if isinstance(value, Mapping):
            strings: set[str] = set()
            for child_key, child in value.items():
                strings.update(collect(child, (*path, str(child_key))))
            return strings
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            strings = set()
            for child in value:
                strings.update(collect(child, path))
            return strings
        if isinstance(value, str):
            protected = _protected_text_fragments(value, exact=path[-1] == "path")
            normalized = _normalize_text(value)
            if _INTERNAL_IDENTIFIER.fullmatch(normalized):
                protected.add(normalized)
            return protected
        return set()

    return collect(payload, ())


def _validate_rights_release_posture(category: str, recorded: Mapping[str, Any]) -> None:
    expected = _RIGHTS_RELEASE_POSTURES.get(category)
    if expected is None or recorded.get("release_posture") != expected:
        raise PublicReleaseValidationError(
            f"rights inventory release posture is unsafe: {category}"
        )


def _validate_public_data_artifacts(
    candidate_root: Path,
    *,
    policy: Mapping[str, Any],
    input_lock: Mapping[str, Any],
    manifest: Mapping[str, Any],
    report: Mapping[str, Any],
) -> None:
    admitted = _mapping(report.get("admitted"), label="publication report admitted fields")
    withheld = _mapping(report.get("withheld"), label="publication report withheld fields")
    if admitted.get("source_fields") != policy.get("admitted_source_fields"):
        raise PublicReleaseValidationError("publication report source fields differ from policy")
    if admitted.get("graph_fields") != policy.get("admitted_graph_fields"):
        raise PublicReleaseValidationError("publication report graph fields differ from policy")
    if withheld.get("graph_fields") != policy.get("withheld_graph_fields"):
        raise PublicReleaseValidationError("publication report withheld fields differ from policy")
    counts = _mapping(input_lock.get("counts"), label="input-lock counts")
    if withheld.get("source_assets") != counts.get("registered_assets"):
        raise PublicReleaseValidationError(
            "publication report withheld asset count differs from the input lock"
        )
    manifest_files = _sequence(manifest.get("files"), label="public manifest files")
    if admitted.get("file_count") != len(manifest_files) + 1:
        raise PublicReleaseValidationError("publication report file count differs from manifest")

    baseline = _mapping(input_lock.get("baseline"), label="input-lock baseline")
    graph_sha256 = _mapping(baseline.get("graph"), label="input-lock graph").get("sha256")
    metrics = _load_json_mapping(candidate_root / METRICS_PATH, label="aggregate metrics")
    if set(metrics) != {
        "schema_version",
        "baseline_id",
        "private_graph_sha256",
        "scope",
        "counts",
        "source_quality_labels",
        "experimental_status",
        "qualification",
    }:
        raise PublicReleaseValidationError("aggregate metrics contain unexpected fields")
    if any(metrics.get(key) != value for key, value in _AGGREGATE_METRICS_FIXED_FIELDS.items()):
        raise PublicReleaseValidationError("aggregate metrics contain unsafe fixed metadata")
    if metrics.get("private_graph_sha256") != graph_sha256 or metrics.get("counts") != counts:
        raise PublicReleaseValidationError("aggregate metrics differ from the input lock")
    quality_labels = _mapping(
        metrics.get("source_quality_labels"), label="aggregate source quality labels"
    )
    if not set(quality_labels).issubset(_SOURCE_QUALITY_LABELS):
        raise PublicReleaseValidationError("aggregate source quality labels use an unknown label")
    if sum(
        _nonnegative_integer(value, label="source-quality count")
        for value in quality_labels.values()
    ) != _integer(counts.get("sources_in_baseline"), label="input-lock baseline source count"):
        raise PublicReleaseValidationError("aggregate source quality labels are incomplete")

    summary = _load_json_mapping(
        candidate_root / RELATIONSHIP_SUMMARY_PATH,
        label="aggregate relationship summary",
    )
    if set(summary) != {
        "schema_version",
        "baseline_id",
        "private_graph_sha256",
        "scope",
        "relationship_count",
        "relationship_kind_counts",
        "inspected_source_pairs",
        "unresolved_source_pairs",
        "withheld_fields",
    }:
        raise PublicReleaseValidationError("relationship summary contains unexpected fields")
    if any(summary.get(key) != value for key, value in _RELATIONSHIP_SUMMARY_FIXED_FIELDS.items()):
        raise PublicReleaseValidationError("relationship summary contains unsafe fixed metadata")
    relationship_count = _integer(
        counts.get("cross_source_relationships"), label="input-lock relationship count"
    )
    relationship_kinds = _mapping(
        summary.get("relationship_kind_counts"), label="relationship kind counts"
    )
    if not set(relationship_kinds).issubset(RELATIONSHIP_TYPES):
        raise PublicReleaseValidationError("relationship summary uses an unknown kind")
    if (
        summary.get("private_graph_sha256") != graph_sha256
        or summary.get("relationship_count") != relationship_count
        or summary.get("inspected_source_pairs") != counts.get("inspected_source_pairs")
        or summary.get("unresolved_source_pairs") != counts.get("unresolved_source_pairs")
        or sum(
            _nonnegative_integer(value, label="relationship-kind count")
            for value in relationship_kinds.values()
        )
        != relationship_count
    ):
        raise PublicReleaseValidationError("relationship summary differs from the input lock")
    expected_withheld = [
        "relationship_ids",
        "source_pairs",
        "endpoints",
        "statements",
        "evidence_spans",
        "rationales",
        "scopes",
        "qualifications",
    ]
    if summary.get("withheld_fields") != expected_withheld:
        raise PublicReleaseValidationError("relationship summary withheld fields are incomplete")

    inventory = _load_yaml_mapping(
        candidate_root / RIGHTS_INVENTORY_PATH,
        label="public rights inventory",
    )
    categories = _mapping(inventory.get("categories"), label="public rights categories")
    expected_inventory_fields = {
        "schema_version",
        "inventory_id",
        "source_count",
        "asset_count",
        "categories",
        "notes",
    }
    if set(inventory) != expected_inventory_fields:
        raise PublicReleaseValidationError("rights inventory contains unexpected fields")
    if inventory.get("source_count") != counts.get("registered_sources") or inventory.get(
        "asset_count"
    ) != counts.get("registered_assets"):
        raise PublicReleaseValidationError("rights inventory counts differ from the input lock")

    field_map = {
        "library_id": "source_id",
        "identity.title": "title",
        "identity.creators": "creators",
        "identity.year": "year",
        "identity.type": "type",
        "identity.language": "language",
        "normalized_rights_category": "rights_category",
    }
    admitted_source_fields = _sequence(
        policy.get("admitted_source_fields"), label="policy admitted source fields"
    )
    try:
        public_source_fields = {field_map[str(field)] for field in admitted_source_fields}
    except KeyError as error:
        raise PublicReleaseValidationError(
            f"policy admits an unsupported source field: {error.args[0]}"
        ) from error
    catalog = _load_yaml_mapping(
        candidate_root / SOURCE_CATALOG_PATH, label="public source catalog"
    )
    if set(catalog) != {"schema_version", "catalog_id", "scope", "sources"}:
        raise PublicReleaseValidationError("source catalog contains unexpected fields")
    sources = _sequence(catalog.get("sources"), label="public source catalog sources")
    if len(sources) != counts.get("registered_sources"):
        raise PublicReleaseValidationError("source catalog count differs from the input lock")
    catalog_categories: dict[str, list[str]] = {str(key): [] for key in categories}
    seen_source_ids: set[str] = set()
    for source_value in sources:
        source = _mapping(source_value, label="public source catalog entry")
        if set(source) != public_source_fields:
            raise PublicReleaseValidationError("source catalog entry violates the field allowlist")
        _validate_catalog_entry_values(source)
        source_id = source.get("source_id")
        category = source.get("rights_category")
        if not isinstance(source_id, str) or source_id in seen_source_ids:
            raise PublicReleaseValidationError("source catalog has an invalid or duplicate ID")
        if not isinstance(category, str) or category not in catalog_categories:
            raise PublicReleaseValidationError("source catalog has an invalid rights category")
        seen_source_ids.add(source_id)
        catalog_categories[category].append(source_id)
    for category, source_ids in catalog_categories.items():
        recorded = _mapping(categories.get(category), label=f"public rights category {category}")
        if set(recorded) != {"source_count", "source_ids", "release_posture"}:
            raise PublicReleaseValidationError("rights category contains unexpected fields")
        _validate_rights_release_posture(category, recorded)
        if recorded.get("source_ids") != sorted(source_ids) or recorded.get("source_count") != len(
            source_ids
        ):
            raise PublicReleaseValidationError("source catalog differs from the rights inventory")


def _validate_catalog_entry_values(source: Mapping[str, Any]) -> None:
    source_id = source.get("source_id")
    if not isinstance(source_id, str) or not re.fullmatch(r"LIB-[0-9]{3}", source_id):
        raise PublicReleaseValidationError("source catalog has an invalid source ID")
    for key in ("title", "type", "language"):
        value = source.get(key)
        if not isinstance(value, str) or not value.strip():
            raise PublicReleaseValidationError(f"source catalog has an invalid {key}")
    creators = source.get("creators")
    if (
        not isinstance(creators, list)
        or not creators
        or not all(isinstance(creator, str) and creator.strip() for creator in creators)
    ):
        raise PublicReleaseValidationError("source catalog has invalid creators")
    year = source.get("year")
    if not isinstance(year, int) or isinstance(year, bool):
        raise PublicReleaseValidationError("source catalog has an invalid year")


def _validate_public_surface(candidate_root: Path) -> None:
    missing = sorted(
        path.as_posix() for path in _PUBLIC_REQUIRED_PATHS if not (candidate_root / path).is_file()
    )
    if missing:
        raise PublicReleaseValidationError(
            "candidate public surface is incomplete: " + ", ".join(missing)
        )


def _validate_command_catalog(catalog: Mapping[str, Any]) -> None:
    commands = _sequence(catalog.get("commands"), label="public command catalog commands")
    observed: dict[str, Mapping[str, Any]] = {}
    for value in commands:
        command = _mapping(value, label="public command catalog command")
        name = command.get("name")
        if not isinstance(name, str) or name in observed:
            raise PublicReleaseValidationError(
                "public command catalog contains an invalid or duplicate command name"
            )
        observed[name] = command
    if set(observed) != set(_EXPECTED_PUBLIC_COMMANDS):
        raise PublicReleaseValidationError(
            "public command catalog is missing one or more documented commands"
        )
    for name, (argv, model_calls, mutates, requires_private) in _EXPECTED_PUBLIC_COMMANDS.items():
        command = observed[name]
        if (
            command.get("argv") != list(argv)
            or command.get("model_calls") is not model_calls
            or command.get("mutates_research_state") is not mutates
            or command.get("requires_private_inputs") is not requires_private
        ):
            raise PublicReleaseValidationError(
                f"public command catalog contract is invalid: {name}"
            )


def _verify_markdown_links(candidate_root: Path) -> None:
    pattern = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
    for path in _candidate_files(candidate_root):
        if path.suffix.casefold() != ".md":
            continue
        for target in pattern.findall(path.read_text(encoding="utf-8")):
            cleaned = target.strip().split(maxsplit=1)[0].strip("<>")
            if not cleaned or cleaned.startswith(("#", "https://", "http://", "mailto:")):
                continue
            relative_target = cleaned.split("#", 1)[0]
            if not relative_target:
                continue
            target_path = Path(relative_target)
            if target_path.is_absolute():
                raise PublicReleaseValidationError(
                    f"candidate Markdown link is absolute: {path.relative_to(candidate_root)}"
                )
            resolved = (path.parent / target_path).resolve()
            try:
                resolved.relative_to(candidate_root)
            except ValueError as error:
                raise PublicReleaseValidationError(
                    f"candidate Markdown link escapes the root: {path.relative_to(candidate_root)}"
                ) from error
            if not resolved.exists():
                raise PublicReleaseValidationError(
                    "candidate Markdown link is broken: "
                    f"{path.relative_to(candidate_root)} -> {relative_target}"
                )


def _synthetic_graph() -> tuple[dict[str, Any], Relationship]:
    asset_a = hashlib.sha256(b"synthetic-source-a").hexdigest()
    asset_b = hashlib.sha256(b"synthetic-source-b").hexdigest()
    records = (
        Record.from_mapping(
            {
                "schema_version": 1,
                "record_type": "evidence",
                "id": "LIB-901:evidence:synthetic-observation",
                "revision": 1,
                "source_id": "LIB-901",
                "asset_sha256": asset_a,
                "page_start": 1,
                "page_end": 1,
                "locator": "Invented source A, synthetic observation",
                "exact_span": (
                    "In the invented apparatus, the blue indicator activates after "
                    "the control pulse."
                ),
            }
        ),
        Record.from_mapping(
            {
                "schema_version": 1,
                "record_type": "atom",
                "id": "LIB-901:atom:indicator-response",
                "revision": 1,
                "source_id": "LIB-901",
                "kind": "observation",
                "label": "Indicator response",
                "statement": "The fictional blue indicator follows the control pulse.",
                "evidence_ids": ["LIB-901:evidence:synthetic-observation"],
                "scope": "Invented apparatus used only for the public demonstration.",
                "attribution": "Synthetic source A",
                "epistemic_posture": "Invented observation, not an empirical claim.",
                "qualifications": ["No real paper, apparatus, or result is represented."],
                "connectable": True,
                "standalone_context": "A pulse precedes an indicator response in a toy system.",
                "type_gap": None,
                "fidelity": None,
            }
        ),
        Record.from_mapping(
            {
                "schema_version": 1,
                "record_type": "evidence",
                "id": "LIB-902:evidence:synthetic-model",
                "revision": 1,
                "source_id": "LIB-902",
                "asset_sha256": asset_b,
                "page_start": 1,
                "page_end": 1,
                "locator": "Invented source B, synthetic model",
                "exact_span": (
                    "The invented delay model predicts an indicator response one step "
                    "after a pulse."
                ),
            }
        ),
        Record.from_mapping(
            {
                "schema_version": 1,
                "record_type": "atom",
                "id": "LIB-902:atom:delay-model",
                "revision": 1,
                "source_id": "LIB-902",
                "kind": "model",
                "label": "One-step delay model",
                "statement": "A fictional one-step delay model predicts the indicator response.",
                "evidence_ids": ["LIB-902:evidence:synthetic-model"],
                "scope": "Invented discrete-time system used only for the public demonstration.",
                "attribution": "Synthetic source B",
                "epistemic_posture": "Invented model, not a scientific conclusion.",
                "qualifications": ["The model is illustrative and has no external source."],
                "connectable": True,
                "standalone_context": "A toy model links a pulse to a later indicator response.",
                "type_gap": None,
                "fidelity": None,
            }
        ),
    )
    by_id = {record.id: record for record in records}
    left = EndpointRef.from_record(by_id["LIB-902:atom:delay-model"])
    right = EndpointRef.from_record(by_id["LIB-901:atom:indicator-response"])
    batch_id = "xref_batch_000000000000000000000000"
    candidate_id = cross_reference_candidate_id(
        batch_id,
        (left.record_id, left.revision, left.record_sha256),
        (right.record_id, right.revision, right.record_sha256),
    )
    relationship_id = cross_reference_relationship_id(
        (left.record_id, left.revision, left.record_sha256),
        (right.record_id, right.revision, right.record_sha256),
        relation_type="potential_support",
        direction="left_to_right",
    )
    relationship = Relationship.from_mapping(
        {
            "schema_version": 1,
            "relationship_id": relationship_id,
            "revision": 1,
            "candidate_id": candidate_id,
            "batch_id": batch_id,
            "relation_type": "potential_support",
            "direction": "left_to_right",
            "left_endpoint": left.to_dict(),
            "right_endpoint": right.to_dict(),
            "left_evidence_ids": ["LIB-902:evidence:synthetic-model"],
            "right_evidence_ids": ["LIB-901:evidence:synthetic-observation"],
            "comparison_surface": "Predicted response compared with an observed response.",
            "scope_alignment": "Both fictional entries use one toy pulse-response scope.",
            "assumption_alignment": "The example assumes a shared one-step time convention.",
            "rationale": (
                "The toy model is represented as potentially supporting the toy observation."
            ),
            "qualifications": [
                "This edge illustrates mapper output; it is not scientific evidence."
            ],
            "provenance": {
                "inspection_job_id": "xref_job_000000000000000000000000",
                "model": "none-synthetic-fixture",
                "protocol_sha256": hashlib.sha256(b"synthetic-protocol-v1").hexdigest(),
                "schema_sha256": hashlib.sha256(b"synthetic-schema-v1").hexdigest(),
                "input_sha256": hashlib.sha256(b"synthetic-input-v1").hexdigest(),
            },
        }
    )
    nodes = []
    for record in records:
        payload = record.to_dict()
        for key in ("id", "record_type", "revision"):
            payload.pop(key)
        nodes.append(
            {
                "id": record.id,
                "source_id": record.source_id,
                "record_type": record.record_type,
                "revision": record.revision,
                "payload": payload,
            }
        )
    graph = {
        "schema_version": 1,
        "batch_id": batch_id,
        "sources": ["LIB-901", "LIB-902"],
        "nodes": nodes,
        "edges": [
            {
                "source": "LIB-901:evidence:synthetic-observation",
                "target": "LIB-901:atom:indicator-response",
                "kind": "supports",
                "origin": "source_local",
                "relationship_id": None,
            },
            {
                "source": "LIB-902:evidence:synthetic-model",
                "target": "LIB-902:atom:delay-model",
                "kind": "supports",
                "origin": "source_local",
                "relationship_id": None,
            },
            {
                "source": "LIB-902:atom:delay-model",
                "target": "LIB-901:atom:indicator-response",
                "kind": "potential_support",
                "origin": "cross_source",
                "relationship_id": relationship.relationship_id,
            },
        ],
    }
    assert record_fingerprint(by_id["LIB-902:atom:delay-model"].payload) == left.record_sha256
    return graph, relationship


def _write_synthetic_example(staging: Path) -> None:
    graph, relationship = _synthetic_graph()
    relationship_path = staging / SYNTHETIC_RELATIONSHIP_PATH
    _write_bytes_new(
        relationship_path,
        render_relationship_markdown(("LIB-901", "LIB-902"), (relationship,)).encode(),
    )
    graph_path = staging / SYNTHETIC_GRAPH_PATH
    _write_json_new(graph_path, graph)
    graph_fingerprint = fingerprint(graph_path)
    relationship_fingerprint = fingerprint(relationship_path)
    manifest = {
        "schema_version": "1.0",
        "kind": "synthetic_public_graph_manifest",
        "batch_id": graph["batch_id"],
        "sources": graph["sources"],
        "inputs": [
            {
                "kind": "canonical_relationship_markdown",
                "path": "relationships/LIB-901--LIB-902/relationships.md",
                "sha256": relationship_fingerprint.sha256,
                "size_bytes": relationship_fingerprint.size_bytes,
            }
        ],
        "corpus_graph": {
            "kind": "corpus_graph",
            "path": "graph.json",
            "sha256": graph_fingerprint.sha256,
            "size_bytes": graph_fingerprint.size_bytes,
        },
    }
    _write_json_new(staging / SYNTHETIC_MANIFEST_PATH, manifest)


def _copy_public_file(
    *,
    repository_root: Path,
    revision: str,
    relative_path: Path,
    destination: Path,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise PublicReleaseValidationError(f"public candidate path collision: {destination.name}")
    data, mode = _git_blob(repository_root, revision=revision, relative_path=relative_path)
    destination.write_bytes(data)
    destination.chmod(0o755 if mode == "100755" else 0o644)


def _verify_tracked_private_worktree(repository_root: Path, *, revision: str) -> None:
    listing = _run_git(
        repository_root,
        ("ls-tree", "-r", "--name-only", "-z", revision, "--", "sources", "vault"),
    )
    for text in listing.stdout.split("\0"):
        if not text:
            continue
        relative = PurePosixPath(text)
        if relative.is_absolute() or ".." in relative.parts:
            raise PublicReleaseValidationError("tracked private Git path is unsafe")
        path = repository_root.joinpath(*relative.parts)
        expected, _mode = _git_blob(
            repository_root,
            revision=revision,
            relative_path=Path(*relative.parts),
        )
        if path.is_symlink() or not path.is_file() or path.read_bytes() != expected:
            raise PublicReleaseValidationError(
                "tracked private source or canonical vault worktree bytes differ "
                "from the frozen commit"
            )


def _git_blob(
    repository_root: Path,
    *,
    revision: str,
    relative_path: Path,
) -> tuple[bytes, str]:
    relative = PurePosixPath(relative_path.as_posix())
    if relative.is_absolute() or ".." in relative.parts:
        raise PublicReleaseValidationError("projected Git path is unsafe")
    entry = _run_git(
        repository_root,
        ("ls-tree", "-z", revision, "--", relative.as_posix()),
    ).stdout.rstrip("\0")
    try:
        metadata, observed_path = entry.split("\t", 1)
        mode, object_type, object_id = metadata.split(" ", 2)
    except ValueError as error:
        raise PublicReleaseValidationError(f"projected Git path is missing: {relative}") from error
    if (
        observed_path != relative.as_posix()
        or object_type != "blob"
        or mode not in {"100644", "100755"}
        or not re.fullmatch(r"[0-9a-f]{40,64}", object_id)
    ):
        raise PublicReleaseValidationError(f"projected Git path is not a regular file: {relative}")
    try:
        completed = subprocess.run(
            ("git", "-C", str(repository_root), "cat-file", "blob", object_id),
            check=False,
            capture_output=True,
        )
    except OSError as error:
        raise PublicReleaseValidationError(f"Git is unavailable: {error}") from error
    if completed.returncode != 0:
        detail = (
            completed.stderr.decode("utf-8", errors="replace").strip() or "Git blob read failed"
        )
        raise PublicReleaseValidationError(detail)
    return completed.stdout, mode


def _write_json_new(path: Path, value: object) -> None:
    _write_bytes_new(path, canonical_json_bytes(value))


def _write_yaml_new(path: Path, value: object) -> None:
    content = yaml.safe_dump(
        value,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=100,
    )
    _write_bytes_new(path, content.encode("utf-8"))


def _write_bytes_new(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise PublicReleaseValidationError(f"generated public path already exists: {path.name}")
    path.write_bytes(value)
    path.chmod(0o644)


def _candidate_files(candidate_root: Path) -> tuple[Path, ...]:
    _validate_root_git_metadata(candidate_root)
    files: list[Path] = []
    for current_root, directory_names, file_names in os.walk(candidate_root):
        current = Path(current_root)
        relative_current = current.relative_to(candidate_root)
        for directory_name in directory_names:
            directory = current / directory_name
            if directory.is_symlink():
                relative = directory.relative_to(candidate_root).as_posix()
                raise PublicReleaseValidationError(
                    f"candidate contains a symbolic link: {relative}"
                )
        directory_names[:] = sorted(
            name
            for name in directory_names
            if not (relative_current == Path(".") and name == ".git")
        )
        for file_name in sorted(file_names):
            if relative_current == Path(".") and file_name == ".git":
                continue
            path = current / file_name
            files.append(path)
    return tuple(sorted(files, key=lambda path: path.relative_to(candidate_root).as_posix()))


def _validate_root_git_metadata(candidate_root: Path) -> None:
    metadata = candidate_root / ".git"
    if not metadata.exists() and not metadata.is_symlink():
        return
    if metadata.is_symlink():
        raise PublicReleaseValidationError("candidate contains unverified Git metadata")
    if metadata.is_file():
        try:
            content = metadata.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise PublicReleaseValidationError(
                "candidate contains unverified Git metadata"
            ) from error
        match = re.fullmatch(r"gitdir: ([^\r\n]+)\r?\n?", content)
        if match is None:
            raise PublicReleaseValidationError("candidate contains unverified Git metadata")
        git_directory = Path(match.group(1))
        if not git_directory.is_absolute():
            git_directory = candidate_root / git_directory
        if not git_directory.resolve().is_dir():
            raise PublicReleaseValidationError("candidate contains unverified Git metadata")
    elif not metadata.is_dir():
        raise PublicReleaseValidationError("candidate contains unverified Git metadata")
    try:
        top_level = _run_git(candidate_root, ("rev-parse", "--show-toplevel")).stdout.strip()
    except PublicReleaseValidationError as error:
        raise PublicReleaseValidationError("candidate contains unverified Git metadata") from error
    if Path(top_level).resolve() != candidate_root.resolve():
        raise PublicReleaseValidationError("candidate contains unverified Git metadata")
    try:
        commit_count = _run_git(candidate_root, ("rev-list", "--all", "--count")).stdout.strip()
        parent_line = _run_git(candidate_root, ("rev-list", "--parents", "-n", "1", "HEAD")).stdout
    except PublicReleaseValidationError as error:
        raise PublicReleaseValidationError("candidate contains unverified Git history") from error
    if commit_count != "1" or len(parent_line.split()) != 1:
        raise PublicReleaseValidationError(
            "candidate Git history must contain exactly one root commit"
        )


def _candidate_descriptors(candidate_root: Path, *, exclude: set[Path]) -> list[dict[str, Any]]:
    descriptors: list[dict[str, Any]] = []
    for path in _candidate_files(candidate_root):
        relative = path.relative_to(candidate_root)
        if relative in exclude:
            continue
        value = fingerprint(path, relative_to=candidate_root)
        mode = "100755" if path.stat().st_mode & stat.S_IXUSR else "100644"
        descriptors.append({**value.to_dict(), "mode": mode})
    return descriptors


def _tree_sha256(descriptors: Sequence[Mapping[str, Any]]) -> str:
    return hashlib.sha256(canonical_json_bytes(list(descriptors))).hexdigest()


def _validate_schema_instance(
    instance: object,
    schema: Mapping[str, Any],
    *,
    label: str,
    failure_message: str | None = None,
) -> None:
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.Draft202012Validator(schema).validate(instance)
    except Exception as error:
        detail = getattr(error, "message", None) or str(error) or type(error).__name__
        message = failure_message or f"{label} does not validate"
        raise PublicReleaseValidationError(f"{message}: {detail}") from error


def _private_work_identifiers(repository_root: Path) -> tuple[str, ...]:
    config_path = repository_root / ".basecamp" / "config.json"
    identifiers: set[str] = set()
    if config_path.exists():
        if config_path.is_symlink() or not config_path.is_file():
            raise PublicReleaseValidationError("private Basecamp configuration is unsafe")
        config = _load_json_mapping(config_path, label="private Basecamp configuration")
        for key in ("account_id", "project_id"):
            value = config.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                value = str(value)
            if not isinstance(value, str) or not re.fullmatch(r"[0-9]{6,}", value):
                raise PublicReleaseValidationError(
                    f"private Basecamp configuration has an invalid {key}"
                )
            identifiers.add(value)

    try:
        remotes = subprocess.run(
            (
                "git",
                "-C",
                str(repository_root),
                "config",
                "--get-regexp",
                r"^remote\..*\.url$",
            ),
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise PublicReleaseValidationError(f"Git is unavailable: {error}") from error
    if remotes.returncode not in {0, 1}:
        detail = remotes.stderr.strip() or remotes.stdout.strip() or "Git command failed"
        raise PublicReleaseValidationError(detail)
    for line in remotes.stdout.splitlines():
        _key, separator, remote_url = line.partition(" ")
        if separator and remote_url.strip():
            identifiers.update(_private_remote_aliases(remote_url.strip()))

    project_root = repository_root / ".project"
    if project_root.exists():
        if project_root.is_symlink() or not project_root.is_dir():
            raise PublicReleaseValidationError("private project control plane is unsafe")
        for path in sorted(project_root.rglob("*")):
            if path.is_symlink():
                raise PublicReleaseValidationError("private project control plane is unsafe")
            if not path.is_file():
                continue
            try:
                private_text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as error:
                raise PublicReleaseValidationError(
                    f"cannot read private project record: {path}"
                ) from error
            identifiers.update(_PRIVATE_TASK_IDENTIFIER.findall(private_text))
            for url in re.findall(
                r"https?://(?:3\.)?(?:app\.)?basecamp\.com/[^\s<>()\[\]\"']+",
                private_text,
                flags=re.IGNORECASE,
            ):
                identifiers.update(re.findall(r"(?<![0-9])[0-9]{6,}(?![0-9])", url))
    return tuple(sorted(identifiers))


def _private_remote_aliases(remote_url: str) -> set[str]:
    """Return common spellings for one configured private Git remote."""

    aliases = {remote_url, remote_url.removesuffix(".git")}
    github_match = re.fullmatch(
        r"(?:https?://github\.com/|git@github\.com:|ssh://git@github\.com/)"
        r"(?P<slug>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?",
        remote_url,
        re.IGNORECASE,
    )
    if github_match is not None:
        slug = github_match.group("slug")
        aliases.update(
            {
                f"https://github.com/{slug}",
                f"https://github.com/{slug}.git",
                f"git@github.com:{slug}",
                f"git@github.com:{slug}.git",
                f"ssh://git@github.com/{slug}",
                f"ssh://git@github.com/{slug}.git",
            }
        )
    return aliases


def _contains_private_work_identifier(text: str, identifier: str) -> bool:
    """Match one private identifier without treating a longer public slug as equal."""

    start = 0
    while (index := text.find(identifier, start)) >= 0:
        before = text[index - 1] if index else ""
        end = index + len(identifier)
        after = text[end] if end < len(text) else ""
        if identifier.isdigit():
            if not before.isdigit() and not after.isdigit():
                return True
        elif not (before and (before.isalnum() or before in "._-")) and not (
            after and (after.isalnum() or after in "._-")
        ):
            return True
        start = index + 1
    return False


def _load_json_mapping(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PublicReleaseValidationError(f"cannot read {label}: {path}") from error
    return dict(_mapping(value, label=label))


def _load_yaml_mapping(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise PublicReleaseValidationError(f"cannot read {label}: {path}") from error
    return dict(_mapping(value, label=label))


def _mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise PublicReleaseValidationError(f"{label} must be an object")
    return value


def _sequence(value: object, *, label: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise PublicReleaseValidationError(f"{label} must be an array")
    return value


def _integer(value: object, *, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise PublicReleaseValidationError(f"{label} must be an integer")
    return value


def _nonnegative_integer(value: object, *, label: str) -> int:
    result = _integer(value, label=label)
    if result < 0:
        raise PublicReleaseValidationError(f"{label} must be nonnegative")
    return result


def _run_git(repository_root: Path, arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            ("git", "-C", str(repository_root), *arguments),
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise PublicReleaseValidationError(f"Git is unavailable: {error}") from error
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "Git command failed"
        raise PublicReleaseValidationError(detail)
    return completed
