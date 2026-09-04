"""Deterministic downstream completion for one frozen provisional corpus."""

from __future__ import annotations

import hashlib
import itertools
import json
import re
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from research_map.audit import AuditCoordinator, AuditValidationError
from research_map.compiler import CompilationCoordinator, CompilationError
from research_map.cross_reference import CrossReferenceValidationError
from research_map.cross_reference_loop import (
    CrossReferenceCoordinator,
    CrossReferenceExecutionError,
    OwnedPairShard,
    _aggregate_shard_telemetry,
    _cross_reference_job_telemetry,
)
from research_map.exploration import GraphExplorationError, GraphExplorer
from research_map.ids import (
    canonical_source_pair,
    holistic_cross_reference_job_id,
    job_id,
    stable_id,
)
from research_map.proposals import ManualReviewRequired, ProposalValidationError
from research_map.reading import (
    ReadingValidationError,
    validate_source_wip,
)
from research_map.receipts import canonical_json_bytes, fingerprint, read_receipt, write_receipt
from research_map.source import RegisteredSource, resolve_source, verify_asset
from research_map.state import IdentityMismatch, SnapshotImportOutcome, StateRepository
from research_map.validation import DossierValidationError

COMPLETION_COORDINATOR_VERSION = "1.0.0"
MANIFEST_SCHEMA = Path("schemas/research-map/v1/corpus-completion-manifest.schema.json")
TRANSFER_SCHEMA = Path("schemas/research-map/v1/corpus-transfer-receipt.schema.json")
SELECTION_SCHEMA = Path("schemas/research-map/v1/corpus-source-selection.schema.json")
QUALITY_MANIFEST_SCHEMA = Path("schemas/research-map/v1/corpus-source-quality-manifest.schema.json")
ELIGIBILITY_SCHEMA = Path("schemas/research-map/v1/corpus-eligibility-manifest.schema.json")
STATUS_SCHEMA = Path("schemas/research-map/v1/corpus-completion-status.schema.json")
CROSS_REFERENCE_PLAN_SCHEMA = Path(
    "schemas/research-map/v1/corpus-cross-reference-plan.schema.json"
)
REPORT_SCHEMA = Path("schemas/research-map/v1/corpus-completion-report.schema.json")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class CorpusCompletionValidationError(ValueError):
    """Raised when a completion manifest or frozen origin is invalid."""


class CorpusCompletionExecutionError(RuntimeError):
    """Raised when approved completion work cannot proceed safely."""


@dataclass(frozen=True, slots=True)
class CorpusCompletionManifest:
    path: Path
    payload: dict[str, Any]
    run_id: str
    source_run_id: str
    source_run_root: Path
    destination_run_root: Path
    source_run_terminal_summary_sha256: str
    source_model: str
    audit_model: str
    cross_reference_model: str
    canary_source_id: str
    source_ids: tuple[str, ...]
    excluded_source_ids: tuple[str, ...]
    audit_workers: int
    mapper_workers: int
    semantic_attempts_per_job: int
    automatic_retries: int
    max_sources_per_block: int
    max_dossier_bytes_per_block: int
    max_mapper_input_bytes: int
    selection_policy: str | None = None
    prior_completion_run_id: str | None = None
    prior_completion_run_root: Path | None = None
    prior_completion_launch_plan_sha256: str | None = None

    @property
    def quality_labelled(self) -> bool:
        return self.selection_policy == "quality_labelled_best_valid"


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    source_id: str
    metadata: dict[str, Any]
    assets: tuple[dict[str, Any], ...]
    records: tuple[dict[str, Any], ...]
    coverage: tuple[dict[str, Any], ...]
    transfer_receipt: dict[str, Any]


@dataclass(frozen=True, slots=True)
class QualifiedSourceSelection:
    source_id: str
    snapshot: SourceSnapshot
    quality_label: str
    selected_origin: str
    findings: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...]
    audit_telemetry: dict[str, Any]
    receipt: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AuditSourceResult:
    source_id: str
    outcome: str
    state: str
    duration_seconds: float
    semantic_jobs_started: int
    worker_events_emitted: int = 0
    token_usage: dict[str, int] | None = None
    warnings: tuple[str, ...] = ()
    failure_class: str | None = None
    failure_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "outcome": self.outcome,
            "state": self.state,
            "duration_seconds": self.duration_seconds,
            "semantic_jobs_started": self.semantic_jobs_started,
            "worker_events_emitted": self.worker_events_emitted,
            "token_usage": self.token_usage or _empty_token_usage(),
            "warnings": list(self.warnings),
            "failure_class": self.failure_class,
            "failure_reason": self.failure_reason,
        }


@dataclass(frozen=True, slots=True)
class CorpusCompletionResult:
    run_id: str
    root: Path
    state: str
    launch_plan_path: Path
    launch_plan_sha256: str
    preflight_receipt_path: Path
    source_count: int
    semantic_jobs_started: int = 0
    worker_events_emitted: int = 0
    duration_seconds: float = 0.0
    token_usage: dict[str, int] | None = None
    cross_reference_semantic_jobs_started: int = 0
    cross_reference_duration_seconds: float = 0.0
    cross_reference_worker_events_emitted: int = 0
    cross_reference_token_usage: dict[str, int] | None = None
    imported_source_ids: tuple[str, ...] = ()
    reused_source_ids: tuple[str, ...] = ()
    eligible_source_ids: tuple[str, ...] = ()
    excluded_source_ids: tuple[str, ...] = ()
    peak_audit_concurrency: int = 0
    eligibility_manifest_path: Path | None = None
    cross_reference_plan_path: Path | None = None
    cross_reference_batch_id: str | None = None
    pair_count: int = 0
    uncovered_pairs: tuple[tuple[str, str], ...] = ()
    peak_mapper_concurrency: int = 0
    corpus_graph_path: Path | None = None
    selection_plan_path: Path | None = None
    quality_manifest_path: Path | None = None
    quality_label_counts: dict[str, int] | None = None
    selected_origin_counts: dict[str, int] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "run_id": self.run_id,
            "root": str(self.root),
            "state": self.state,
            "launch_plan": {
                "path": str(self.launch_plan_path),
                "sha256": self.launch_plan_sha256,
            },
            "preflight_receipt": str(self.preflight_receipt_path),
            "source_count": self.source_count,
            "semantic_jobs_started": self.semantic_jobs_started,
            "worker_events_emitted": self.worker_events_emitted,
            "duration_seconds": self.duration_seconds,
            "token_usage": self.token_usage or _empty_token_usage(),
            "cross_reference_telemetry": {
                "semantic_jobs_started": self.cross_reference_semantic_jobs_started,
                "duration_seconds": self.cross_reference_duration_seconds,
                "worker_events_emitted": self.cross_reference_worker_events_emitted,
                "token_usage": self.cross_reference_token_usage or _empty_token_usage(),
            },
            "imported_source_ids": list(self.imported_source_ids),
            "reused_source_ids": list(self.reused_source_ids),
            "eligible_source_ids": list(self.eligible_source_ids),
            "excluded_source_ids": list(self.excluded_source_ids),
            "peak_audit_concurrency": self.peak_audit_concurrency,
            "eligibility_manifest": (
                None
                if self.eligibility_manifest_path is None
                else str(self.eligibility_manifest_path)
            ),
            "cross_reference_plan": (
                None
                if self.cross_reference_plan_path is None
                else str(self.cross_reference_plan_path)
            ),
            "cross_reference_batch_id": self.cross_reference_batch_id,
            "pair_count": self.pair_count,
            "uncovered_pairs": [list(pair) for pair in self.uncovered_pairs],
            "peak_mapper_concurrency": self.peak_mapper_concurrency,
            "corpus_graph": (
                None if self.corpus_graph_path is None else str(self.corpus_graph_path)
            ),
        }
        if self.selection_plan_path is not None or self.quality_manifest_path is not None:
            payload.update(
                {
                    "selection_plan": (
                        None if self.selection_plan_path is None else str(self.selection_plan_path)
                    ),
                    "quality_manifest": (
                        None
                        if self.quality_manifest_path is None
                        else str(self.quality_manifest_path)
                    ),
                    "quality_label_counts": self.quality_label_counts
                    or {label: 0 for label in _QUALITY_LABELS},
                    "selected_origin_counts": self.selected_origin_counts
                    or {
                        "prior_completion_wip": 0,
                        "source_run_provisional": 0,
                    },
                }
            )
        return payload


def plan_corpus_cross_reference(
    eligibility_manifest: Mapping[str, Any],
    *,
    max_sources_per_block: int = 8,
    max_dossier_bytes_per_block: int = 900_000,
    max_mapper_input_bytes: int = 1_800_000,
) -> dict[str, Any]:
    """Pack frozen dossiers and assign every viable unordered pair exactly once."""

    if max_sources_per_block < 1:
        raise CorpusCompletionValidationError("cross-reference blocks require a source limit")
    if max_dossier_bytes_per_block < 1 or max_mapper_input_bytes < max_dossier_bytes_per_block:
        raise CorpusCompletionValidationError("cross-reference byte limits are invalid")
    manifest_kind = eligibility_manifest.get("kind")
    source_key = (
        "connectable_sources"
        if manifest_kind == "corpus_source_quality_manifest"
        else "eligible_sources"
    )
    raw_sources = eligibility_manifest.get(source_key)
    if not isinstance(raw_sources, list):
        raise CorpusCompletionValidationError("source manifest lacks connectable mapper inputs")
    entries: list[dict[str, Any]] = []
    for raw in raw_sources:
        if not isinstance(raw, dict):
            raise CorpusCompletionValidationError("eligible source entry must be an object")
        source_id = str(raw.get("source_id", ""))
        dossier_bytes = raw.get("dossier_bytes")
        dossier_sha256 = str(raw.get("dossier_sha256", ""))
        dossier_path = str(raw.get("dossier_path", ""))
        if (
            re.fullmatch(r"LIB-[0-9]{3}", source_id) is None
            or not isinstance(dossier_bytes, int)
            or dossier_bytes < 1
            or _SHA256.fullmatch(dossier_sha256) is None
            or not dossier_path
        ):
            raise CorpusCompletionValidationError(
                f"eligible source planning metadata is invalid: {source_id or '<missing>'}"
            )
        entries.append(
            {
                "source_id": source_id,
                "dossier_bytes": dossier_bytes,
                "dossier_sha256": dossier_sha256,
                "dossier_path": dossier_path,
            }
        )
    source_ids = tuple(sorted(str(item["source_id"]) for item in entries))
    if len(source_ids) < 2 or len(set(source_ids)) != len(source_ids):
        raise CorpusCompletionValidationError(
            "cross-reference planning requires at least two unique eligible sources"
        )

    planning_failures: list[dict[str, Any]] = []
    packable: list[dict[str, Any]] = []
    oversized: set[str] = set()
    for entry in sorted(
        entries, key=lambda item: (-int(item["dossier_bytes"]), str(item["source_id"]))
    ):
        if int(entry["dossier_bytes"]) > max_mapper_input_bytes:
            source_id = str(entry["source_id"])
            oversized.add(source_id)
            planning_failures.append(
                {
                    "failure_class": "source_mapper_input_ceiling",
                    "source_ids": [source_id],
                    "shard_id": None,
                    "reason": (
                        f"dossier bytes {entry['dossier_bytes']} exceed mapper input ceiling "
                        f"{max_mapper_input_bytes}"
                    ),
                }
            )
        else:
            packable.append(entry)

    mutable_blocks: list[list[dict[str, Any]]] = []
    for entry in packable:
        entry_bytes = int(entry["dossier_bytes"])
        placed = False
        if entry_bytes <= max_dossier_bytes_per_block:
            for block in mutable_blocks:
                if (
                    len(block) < max_sources_per_block
                    and sum(int(item["dossier_bytes"]) for item in block) + entry_bytes
                    <= max_dossier_bytes_per_block
                ):
                    block.append(entry)
                    placed = True
                    break
        if not placed:
            mutable_blocks.append([entry])

    blocks: list[dict[str, Any]] = []
    for block in mutable_blocks:
        members = tuple(sorted(str(item["source_id"]) for item in block))
        entries_by_id = {str(item["source_id"]): item for item in block}
        dossier_bytes = sum(int(item["dossier_bytes"]) for item in block)
        block_id = stable_id(
            "xref_block",
            tuple(
                f"{source_id}:{entries_by_id[source_id]['dossier_sha256']}" for source_id in members
            ),
        )
        blocks.append(
            {
                "block_id": block_id,
                "source_ids": list(members),
                "dossier_bytes": dossier_bytes,
                "source_count": len(members),
            }
        )

    shards: list[dict[str, Any]] = []
    assigned_pairs: list[tuple[str, str]] = []
    successful_pairs: set[tuple[str, str]] = set()
    for left_index, left in enumerate(blocks):
        for right_index in range(left_index, len(blocks)):
            right = blocks[right_index]
            left_sources = tuple(str(item) for item in left["source_ids"])
            right_sources = tuple(str(item) for item in right["source_ids"])
            if left_index == right_index:
                owned_pairs = tuple(itertools.combinations(left_sources, 2))
                input_bytes = int(left["dossier_bytes"])
            else:
                owned_pairs = tuple(
                    sorted(
                        canonical_source_pair(left_source, right_source)
                        for left_source in left_sources
                        for right_source in right_sources
                    )
                )
                input_bytes = int(left["dossier_bytes"]) + int(right["dossier_bytes"])
            assigned_pairs.extend(owned_pairs)
            shard_sources = (
                left_sources
                if left_index == right_index
                else tuple(sorted((*left_sources, *right_sources)))
            )
            shard_id = stable_id(
                "xref_shard",
                (
                    str(left["block_id"]),
                    str(right["block_id"]),
                    *(f"{pair[0]}:{pair[1]}" for pair in owned_pairs),
                ),
            )
            planning_failure: str | None = None
            if input_bytes > max_mapper_input_bytes:
                planning_failure = "mapper_input_ceiling"
                planning_failures.append(
                    {
                        "failure_class": planning_failure,
                        "source_ids": list(shard_sources),
                        "shard_id": shard_id,
                        "reason": (
                            f"shard input bytes {input_bytes} exceed mapper input ceiling "
                            f"{max_mapper_input_bytes}"
                        ),
                    }
                )
            elif owned_pairs:
                successful_pairs.update(owned_pairs)
            shards.append(
                {
                    "shard_id": shard_id,
                    "left_block_id": left["block_id"],
                    "right_block_id": right["block_id"],
                    "source_ids": list(shard_sources),
                    "dossier_bytes": input_bytes,
                    "owned_pairs": [
                        {
                            "pair_key": _source_pair_key(*pair),
                            "source_ids": list(pair),
                        }
                        for pair in owned_pairs
                    ],
                    "owned_pair_count": len(owned_pairs),
                    "no_op": not owned_pairs,
                    "planning_failure": planning_failure,
                }
            )

    expected_pairs = tuple(itertools.combinations(source_ids, 2))
    if len(assigned_pairs) != len(set(assigned_pairs)):
        raise CorpusCompletionValidationError("cross-reference plan assigned a pair twice")
    assigned_set = set(assigned_pairs)
    assigned_non_oversized = {
        pair for pair in expected_pairs if not set(pair).intersection(oversized)
    }
    if assigned_set != assigned_non_oversized:
        raise CorpusCompletionValidationError(
            "cross-reference plan omitted or added a viable source pair"
        )
    uncovered_pairs = tuple(pair for pair in expected_pairs if pair not in successful_pairs)
    payload = {
        "schema_version": "1.0",
        "kind": "corpus_cross_reference_plan",
        "run_id": str(eligibility_manifest.get("run_id", "")),
        "source_ids": list(source_ids),
        "source_count": len(source_ids),
        "limits": {
            "max_sources_per_block": max_sources_per_block,
            "max_dossier_bytes_per_block": max_dossier_bytes_per_block,
            "max_mapper_input_bytes": max_mapper_input_bytes,
            "mapper_workers": 2,
            "semantic_attempts_per_job": 1,
            "automatic_retries": 0,
        },
        "blocks": blocks,
        "shards": shards,
        "pair_count": len(expected_pairs),
        "owned_pair_count": len(assigned_pairs),
        "planning_failures": planning_failures,
        "uncovered_pairs": [
            {"pair_key": _source_pair_key(*pair), "source_ids": list(pair)}
            for pair in uncovered_pairs
        ],
    }
    if manifest_kind == "corpus_source_quality_manifest":
        payload.update(
            {
                "source_manifest_kind": "corpus_source_quality_manifest",
                "quality_manifest_sha256": _digest(eligibility_manifest),
                "source_inputs": [
                    {
                        "source_id": str(item["source_id"]),
                        "quality_label": str(item["quality_label"]),
                        "selection_receipt_sha256": str(item["selection_receipt_sha256"]),
                        "dossier_sha256": str(item["dossier_sha256"]),
                        "graph_sha256": str(item["graph_sha256"]),
                        "findings_sha256": str(item["findings_sha256"]),
                    }
                    for item in sorted(raw_sources, key=lambda value: str(value["source_id"]))
                ],
            }
        )
    else:
        payload["eligibility_manifest_sha256"] = _digest(eligibility_manifest)
    return payload


def quality_source_summaries(
    quality_manifest: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Project workflow-quality context without changing scientific records."""

    summaries: list[dict[str, Any]] = []
    for item in quality_manifest["connectable_sources"]:
        findings = [dict(value) for value in item["findings"]]
        summaries.append(
            {
                "source_id": str(item["source_id"]),
                "quality_label": str(item["quality_label"]),
                "selected_origin": str(item["selected_origin"]),
                "selection_receipt_sha256": str(item["selection_receipt_sha256"]),
                "finding_count": len(findings),
                "open_finding_count": sum(value.get("status") == "open" for value in findings),
                "findings_sha256": str(item["findings_sha256"]),
                "findings": findings,
                "warnings": [str(value) for value in item["warnings"]],
            }
        )
    return summaries


def load_corpus_completion_manifest(
    path: Path, *, repository_root: Path
) -> CorpusCompletionManifest:
    manifest_path = path.resolve()
    payload = _read_object(manifest_path, label="corpus-completion manifest")
    _validate_payload(
        payload,
        schema_path=repository_root / MANIFEST_SCHEMA,
        label="corpus-completion manifest",
    )
    source_ids = tuple(str(value) for value in payload["source_ids"])
    excluded = tuple(str(value) for value in payload["excluded_source_ids"])
    if payload["canary_source_id"] != "LIB-043":
        raise CorpusCompletionValidationError("completion canary must be LIB-043")
    if source_ids[0] != payload["canary_source_id"]:
        raise CorpusCompletionValidationError("completion canary must be first in source order")
    if set(source_ids).intersection(excluded):
        raise CorpusCompletionValidationError("included and excluded completion sources overlap")
    if "LIB-042" not in excluded or "LIB-042" in source_ids:
        raise CorpusCompletionValidationError("LIB-042 must remain explicitly excluded")
    source_root = _resolve_run_root(
        repository_root, str(payload["source_run_root"]), label="source"
    )
    destination_root = _resolve_run_root(
        repository_root, str(payload["destination_run_root"]), label="destination"
    )
    if source_root == destination_root:
        raise CorpusCompletionValidationError("source and destination run roots must differ")
    if source_root.name != payload["source_run_id"]:
        raise CorpusCompletionValidationError("source run root and source run ID differ")
    if destination_root.name != payload["run_id"]:
        raise CorpusCompletionValidationError("destination run root and run ID differ")
    selection_policy = payload.get("selection_policy")
    prior_completion_run_id = payload.get("prior_completion_run_id")
    prior_completion_root: Path | None = None
    prior_completion_launch_plan_sha256 = payload.get("prior_completion_launch_plan_sha256")
    if selection_policy == "quality_labelled_best_valid":
        prior_completion_root = _resolve_run_root(
            repository_root,
            str(payload["prior_completion_run_root"]),
            label="prior completion",
        )
        if prior_completion_root.name != prior_completion_run_id:
            raise CorpusCompletionValidationError("prior completion run root and run ID differ")
        if prior_completion_root in {source_root, destination_root}:
            raise CorpusCompletionValidationError(
                "source, prior completion, and destination run roots must differ"
            )
    return CorpusCompletionManifest(
        path=manifest_path,
        payload=payload,
        run_id=str(payload["run_id"]),
        source_run_id=str(payload["source_run_id"]),
        source_run_root=source_root,
        destination_run_root=destination_root,
        source_run_terminal_summary_sha256=str(payload["source_run_terminal_summary_sha256"]),
        source_model=str(payload["source_model"]),
        audit_model=str(payload["audit_model"]),
        cross_reference_model=str(payload["cross_reference_model"]),
        canary_source_id=str(payload["canary_source_id"]),
        source_ids=source_ids,
        excluded_source_ids=excluded,
        audit_workers=int(payload["audit_workers"]),
        mapper_workers=int(payload["mapper_workers"]),
        semantic_attempts_per_job=int(payload["semantic_attempts_per_job"]),
        automatic_retries=int(payload["automatic_retries"]),
        max_sources_per_block=int(payload["max_sources_per_block"]),
        max_dossier_bytes_per_block=int(payload["max_dossier_bytes_per_block"]),
        max_mapper_input_bytes=int(payload["max_mapper_input_bytes"]),
        selection_policy=(None if selection_policy is None else str(selection_policy)),
        prior_completion_run_id=(
            None if prior_completion_run_id is None else str(prior_completion_run_id)
        ),
        prior_completion_run_root=prior_completion_root,
        prior_completion_launch_plan_sha256=(
            None
            if prior_completion_launch_plan_sha256 is None
            else str(prior_completion_launch_plan_sha256)
        ),
    )


class CorpusCompletionCoordinator:
    """Verify, transfer, audit, and freeze one provisional corpus."""

    def __init__(
        self,
        *,
        repository_root: Path,
        manifest_path: Path,
        audit_runner: Callable[[str, str], AuditSourceResult] | None = None,
        cross_reference_codex_executable: str = "codex",
    ) -> None:
        self.repository_root = repository_root.resolve()
        self.manifest = load_corpus_completion_manifest(
            manifest_path, repository_root=self.repository_root
        )
        self.root = self.manifest.destination_run_root
        self.state_path = self.root / "state.sqlite3"
        self.cache_root = self.root / "cache"
        self.vault_root = self.root / "vault" / "sources"
        self.relationship_root = self.root / "vault" / "relationships"
        self.build_root = self.root / "build"
        self.launch_plan_path = self.root / "launch-plan.json"
        self.preflight_receipt_path = self.root / "preflight.json"
        self.transfer_root = self.root / "transfers"
        self.selection_root = self.root / "selections"
        self.selection_plan_path = self.root / "selection-plan.json"
        self.audit_start_root = self.root / "audit-starts"
        self.audit_resume_root = self.root / "audit-resumes"
        self.audit_result_root = self.root / "audit-results"
        self.eligibility_manifest_path = self.root / "eligibility-manifest.json"
        self.quality_manifest_path = self.root / "quality-manifest.json"
        self.cross_reference_plan_path = self.root / "cross-reference-plan.json"
        self.report_path = self.root / "completion-report.json"
        self.audit_runner = audit_runner or self._run_audit_source
        self.cross_reference_codex_executable = cross_reference_codex_executable

    def preflight(self) -> CorpusCompletionResult:
        if self.manifest.quality_labelled:
            return self._quality_preflight()
        snapshots = tuple(
            self._verify_source_snapshot(source_id) for source_id in self.manifest.source_ids
        )
        launch_plan = {
            "kind": "corpus_completion_launch_plan",
            "contract_version": COMPLETION_COORDINATOR_VERSION,
            "run_id": self.manifest.run_id,
            "source_run_id": self.manifest.source_run_id,
            "source_run_root": _display_path(
                self.manifest.source_run_root, repository_root=self.repository_root
            ),
            "destination_run_root": _display_path(self.root, repository_root=self.repository_root),
            "source_run_terminal_summary_sha256": (
                self.manifest.source_run_terminal_summary_sha256
            ),
            "source_ids": list(self.manifest.source_ids),
            "excluded_source_ids": list(self.manifest.excluded_source_ids),
            "canary_source_id": self.manifest.canary_source_id,
            "source_model": self.manifest.source_model,
            "audit_model": self.manifest.audit_model,
            "cross_reference_model": self.manifest.cross_reference_model,
            "audit_workers": self.manifest.audit_workers,
            "mapper_workers": self.manifest.mapper_workers,
            "semantic_attempts_per_job": self.manifest.semantic_attempts_per_job,
            "automatic_retries": self.manifest.automatic_retries,
            "max_sources_per_block": self.manifest.max_sources_per_block,
            "max_dossier_bytes_per_block": self.manifest.max_dossier_bytes_per_block,
            "max_mapper_input_bytes": self.manifest.max_mapper_input_bytes,
            "manifest": fingerprint(
                self.manifest.path,
                relative_to=(
                    self.repository_root
                    if self.manifest.path.is_relative_to(self.repository_root)
                    else None
                ),
            ).to_dict(),
            "source_snapshots": [snapshot.transfer_receipt for snapshot in snapshots],
            "source_snapshot_composite_sha256": _digest(
                [snapshot.transfer_receipt for snapshot in snapshots]
            ),
            "semantic_jobs_started": 0,
            "worker_events_emitted": 0,
        }
        launch_sha256 = self._write_or_verify_receipt(self.launch_plan_path, launch_plan)
        self._write_or_verify_receipt(
            self.preflight_receipt_path,
            {
                "kind": "corpus_completion_preflight",
                "run_id": self.manifest.run_id,
                "status": "ready_for_approval",
                "launch_plan_sha256": launch_sha256,
                "source_count": len(snapshots),
                "semantic_jobs_started": 0,
                "worker_events_emitted": 0,
                "errors": [],
            },
        )
        return CorpusCompletionResult(
            run_id=self.manifest.run_id,
            root=self.root,
            state="ready_for_approval",
            launch_plan_path=self.launch_plan_path,
            launch_plan_sha256=launch_sha256,
            preflight_receipt_path=self.preflight_receipt_path,
            source_count=len(snapshots),
        )

    def _quality_preflight(self) -> CorpusCompletionResult:
        """Freeze a zero-call latest-valid selection plan over both retained runs."""

        source_before = _directory_composite(self.manifest.source_run_root)
        prior_identity = self._verify_prior_completion_identity()
        prior_before = _directory_composite(self._prior_completion_root())
        selections = tuple(
            self._select_quality_snapshot(source_id) for source_id in self.manifest.source_ids
        )
        prior_after = _directory_composite(self._prior_completion_root())
        source_after = _directory_composite(self.manifest.source_run_root)
        if source_after != source_before:
            raise CorpusCompletionValidationError("source run namespace changed during selection")
        if prior_after != prior_before:
            raise CorpusCompletionValidationError(
                "prior completion namespace changed during selection"
            )
        selection_artifacts: list[dict[str, Any]] = []
        for selection in selections:
            path = self.selection_root / f"{selection.source_id}.json"
            self._write_or_verify_receipt(
                path,
                {key: value for key, value in selection.receipt.items() if key != "schema_version"},
                schema_path=self.repository_root / SELECTION_SCHEMA,
            )
            selection_artifacts.append(
                {
                    "source_id": selection.source_id,
                    **fingerprint(path, relative_to=self.repository_root).to_dict(),
                }
            )
        label_counts = {
            label: sum(selection.quality_label == label for selection in selections)
            for label in _QUALITY_LABELS
        }
        origin_counts = {
            origin: sum(selection.selected_origin == origin for selection in selections)
            for origin in ("prior_completion_wip", "source_run_provisional")
        }
        selection_plan = {
            "kind": "corpus_source_selection_plan",
            "contract_version": "quality-labelled-completion-v2",
            "run_id": self.manifest.run_id,
            "selection_policy": self.manifest.selection_policy,
            "source_run_id": self.manifest.source_run_id,
            "prior_completion_run_id": self.manifest.prior_completion_run_id,
            "requested_source_ids": list(self.manifest.source_ids),
            "excluded_source_ids": list(self.manifest.excluded_source_ids),
            "source_snapshot_composite_sha256": _digest(
                [selection.receipt["origin_snapshot"] for selection in selections]
            ),
            "prior_completion_namespace": prior_before,
            "prior_completion_identity": prior_identity,
            "quality_label_counts": label_counts,
            "selected_origin_counts": origin_counts,
            "selections": selection_artifacts,
            "semantic_jobs_started": 0,
            "worker_events_emitted": 0,
            "duration_seconds": 0.0,
            "token_usage": _empty_token_usage(),
        }
        self._write_or_verify_receipt(self.selection_plan_path, selection_plan)
        selection_plan_fingerprint = fingerprint(
            self.selection_plan_path, relative_to=self.repository_root
        )
        launch_plan = {
            "kind": "corpus_completion_launch_plan",
            "contract_version": "quality-labelled-completion-v2",
            "run_id": self.manifest.run_id,
            "selection_policy": self.manifest.selection_policy,
            "source_run_id": self.manifest.source_run_id,
            "source_run_root": _display_path(
                self.manifest.source_run_root, repository_root=self.repository_root
            ),
            "prior_completion_run_id": self.manifest.prior_completion_run_id,
            "prior_completion_run_root": _display_path(
                self._prior_completion_root(), repository_root=self.repository_root
            ),
            "destination_run_root": _display_path(self.root, repository_root=self.repository_root),
            "source_run_terminal_summary_sha256": (
                self.manifest.source_run_terminal_summary_sha256
            ),
            "prior_completion_launch_plan_sha256": (
                self.manifest.prior_completion_launch_plan_sha256
            ),
            "source_ids": list(self.manifest.source_ids),
            "excluded_source_ids": list(self.manifest.excluded_source_ids),
            "source_model": self.manifest.source_model,
            "audit_model": self.manifest.audit_model,
            "cross_reference_model": self.manifest.cross_reference_model,
            "audit_workers": self.manifest.audit_workers,
            "mapper_workers": self.manifest.mapper_workers,
            "semantic_attempts_per_job": self.manifest.semantic_attempts_per_job,
            "automatic_retries": self.manifest.automatic_retries,
            "max_sources_per_block": self.manifest.max_sources_per_block,
            "max_dossier_bytes_per_block": self.manifest.max_dossier_bytes_per_block,
            "max_mapper_input_bytes": self.manifest.max_mapper_input_bytes,
            "manifest": fingerprint(
                self.manifest.path,
                relative_to=(
                    self.repository_root
                    if self.manifest.path.is_relative_to(self.repository_root)
                    else None
                ),
            ).to_dict(),
            "selection_plan": selection_plan_fingerprint.to_dict(),
            "quality_label_counts": label_counts,
            "selected_origin_counts": origin_counts,
            "semantic_jobs_started": 0,
            "worker_events_emitted": 0,
        }
        launch_sha256 = self._write_or_verify_receipt(self.launch_plan_path, launch_plan)
        self._write_or_verify_receipt(
            self.preflight_receipt_path,
            {
                "kind": "corpus_completion_preflight",
                "run_id": self.manifest.run_id,
                "status": "ready_for_approval",
                "launch_plan_sha256": launch_sha256,
                "source_count": len(selections),
                "selection_policy": self.manifest.selection_policy,
                "quality_label_counts": label_counts,
                "semantic_jobs_started": 0,
                "worker_events_emitted": 0,
                "errors": [],
            },
        )
        return CorpusCompletionResult(
            run_id=self.manifest.run_id,
            root=self.root,
            state="ready_for_approval",
            launch_plan_path=self.launch_plan_path,
            launch_plan_sha256=launch_sha256,
            preflight_receipt_path=self.preflight_receipt_path,
            source_count=len(selections),
            selection_plan_path=self.selection_plan_path,
            quality_label_counts=label_counts,
            selected_origin_counts=origin_counts,
        )

    def _verify_prior_completion_identity(self) -> dict[str, Any]:
        root = self._prior_completion_root()
        launch_path = root / "launch-plan.json"
        if not launch_path.is_file():
            raise CorpusCompletionValidationError("prior completion launch plan is missing")
        observed_sha256 = fingerprint(launch_path).sha256
        if observed_sha256 != self.manifest.prior_completion_launch_plan_sha256:
            raise CorpusCompletionValidationError(
                "prior completion launch-plan fingerprint changed"
            )
        launch = _read_object(launch_path, label="prior completion launch plan")
        if (
            launch.get("kind") != "corpus_completion_launch_plan"
            or launch.get("run_id") != self.manifest.prior_completion_run_id
            or launch.get("source_run_id") != self.manifest.source_run_id
            or launch.get("source_run_terminal_summary_sha256")
            != self.manifest.source_run_terminal_summary_sha256
            or tuple(launch.get("source_ids", ())) != self.manifest.source_ids
            or tuple(launch.get("excluded_source_ids", ())) != self.manifest.excluded_source_ids
            or launch.get("destination_run_root")
            != _display_path(root, repository_root=self.repository_root)
        ):
            raise CorpusCompletionValidationError("prior completion launch-plan identity changed")
        manifest_value = launch.get("manifest")
        if not isinstance(manifest_value, dict):
            raise CorpusCompletionValidationError(
                "prior completion launch plan lacks its tracked manifest"
            )
        manifest_path = (self.repository_root / str(manifest_value.get("path", ""))).resolve()
        if not manifest_path.is_relative_to(self.repository_root) or not manifest_path.is_file():
            raise CorpusCompletionValidationError(
                "prior completion tracked manifest escapes or is missing"
            )
        manifest_fingerprint = fingerprint(manifest_path, relative_to=self.repository_root)
        if manifest_fingerprint.sha256 != manifest_value.get(
            "sha256"
        ) or manifest_fingerprint.size_bytes != manifest_value.get("size_bytes"):
            raise CorpusCompletionValidationError(
                "prior completion tracked manifest fingerprint changed"
            )
        prior_state = StateRepository(root / "state.sqlite3")
        if not prior_state.path.is_file() or prior_state.schema_versions() != (1, 2, 3):
            raise CorpusCompletionValidationError(
                "prior completion state store is missing or incompatible"
            )
        for receipt_root in (root / "audit-starts", root / "audit-results"):
            unexpected = sorted(
                path.stem
                for path in receipt_root.glob("*.json")
                if path.stem not in self.manifest.source_ids
            )
            if unexpected:
                raise CorpusCompletionValidationError(
                    "prior completion contains foreign audit receipts: " + ", ".join(unexpected)
                )
        return {
            "run_id": self.manifest.prior_completion_run_id,
            "launch_plan": fingerprint(launch_path, relative_to=self.repository_root).to_dict(),
            "tracked_manifest": manifest_fingerprint.to_dict(),
            "state_schema_versions": list(prior_state.schema_versions()),
        }

    def _select_quality_snapshot(self, source_id: str) -> QualifiedSourceSelection:
        origin = self._verify_source_snapshot(source_id)
        prior_root = self._prior_completion_root()
        repository = StateRepository(prior_root / "state.sqlite3")
        source = resolve_source(source_id, repository_root=self.repository_root)
        prior_source = repository.source_record(source_id)
        if prior_source is None:
            raise CorpusCompletionValidationError(
                f"prior completion source is missing: {source_id}"
            )
        if (
            prior_source["metadata"] != origin.metadata
            or repository.assets_for(source_id) != origin.assets
        ):
            raise CorpusCompletionValidationError(
                f"prior completion source identity differs: {source_id}"
            )
        revisions = repository.wip_record_revisions(source_id)
        candidate_records = repository.latest_wip_records(source_id)
        candidate_coverage = _origin_coverage(repository.coverage_for(source_id))
        if not revisions or not candidate_records or not candidate_coverage:
            raise CorpusCompletionValidationError(
                f"prior completion source snapshot is incomplete: {source_id}"
            )
        if any(record.get("source_id") != source_id for record in revisions):
            raise CorpusCompletionValidationError(
                f"prior completion WIP contains a foreign source: {source_id}"
            )
        candidate_error: str | None = None
        try:
            validate_source_wip(
                repository,
                source,
                schema_directory=self.repository_root / "schemas/research-map/v1",
            )
        except (ReadingValidationError, DossierValidationError, ProposalValidationError) as error:
            candidate_error = f"{type(error).__name__}: {error}"
        audit = self._prior_audit_evidence(source_id, repository=repository)
        same_as_origin = (
            _digest(candidate_records) == origin.transfer_receipt["wip_sha256"]
            and _digest(candidate_coverage) == origin.transfer_receipt["coverage_sha256"]
        )
        use_candidate = candidate_error is None and not same_as_origin
        selected_origin = "prior_completion_wip" if use_candidate else "source_run_provisional"
        selected_snapshot = (
            SourceSnapshot(
                source_id=source_id,
                metadata=origin.metadata,
                assets=origin.assets,
                records=candidate_records,
                coverage=candidate_coverage,
                transfer_receipt=origin.transfer_receipt,
            )
            if use_candidate
            else origin
        )
        fallback_reason = candidate_error
        if same_as_origin:
            fallback_reason = "prior completion WIP is byte-equivalent to the source origin"
        candidate = {
            "run_id": self.manifest.prior_completion_run_id,
            "source_state": prior_source["state"],
            "wip_sha256": _digest(candidate_records),
            "wip_revision_history_sha256": _digest(revisions),
            "coverage_sha256": _digest(candidate_coverage),
            "record_count": len(candidate_records),
            "coverage_entry_count": len(candidate_coverage),
            "deterministically_valid": candidate_error is None,
            "validation_error": candidate_error,
        }
        selected = {
            "origin": selected_origin,
            "run_id": (
                self.manifest.prior_completion_run_id
                if use_candidate
                else self.manifest.source_run_id
            ),
            "source_state": (
                prior_source["state"] if use_candidate else "calibration_graph_inserted"
            ),
            "wip_sha256": _digest(selected_snapshot.records),
            "coverage_sha256": _digest(selected_snapshot.coverage),
            "record_count": len(selected_snapshot.records),
            "coverage_entry_count": len(selected_snapshot.coverage),
        }
        receipt = {
            "schema_version": "1.0",
            "kind": "corpus_source_selection",
            "run_id": self.manifest.run_id,
            "source_id": source_id,
            "selection_policy": self.manifest.selection_policy,
            "quality_label": audit["quality_label"],
            "selected_origin": selected_origin,
            "selected_snapshot": selected,
            "origin_snapshot": origin.transfer_receipt,
            "prior_candidate": candidate,
            "fallback_reason": fallback_reason,
            "audit": audit,
            "semantic_jobs_started": 0,
        }
        _validate_payload(
            receipt,
            schema_path=self.repository_root / SELECTION_SCHEMA,
            label=f"source selection for {source_id}",
        )
        return QualifiedSourceSelection(
            source_id=source_id,
            snapshot=selected_snapshot,
            quality_label=str(audit["quality_label"]),
            selected_origin=selected_origin,
            findings=tuple(dict(item) for item in audit["findings"]),
            warnings=tuple(str(item) for item in audit["warnings"]),
            audit_telemetry=dict(audit["telemetry"]),
            receipt=receipt,
        )

    def _prior_audit_evidence(
        self, source_id: str, *, repository: StateRepository
    ) -> dict[str, Any]:
        root = self._prior_completion_root()
        start_path = root / "audit-starts" / f"{source_id}.json"
        result_path = root / "audit-results" / f"{source_id}.json"
        if result_path.is_file() and not start_path.is_file():
            raise CorpusCompletionValidationError(
                f"prior audit result lacks its start receipt: {source_id}"
            )
        start: dict[str, Any] | None = None
        if start_path.is_file():
            start = read_receipt(start_path)
            if (
                start.get("kind") != "corpus_audit_source_start"
                or start.get("run_id") != self.manifest.prior_completion_run_id
                or start.get("launch_plan_sha256")
                != self.manifest.prior_completion_launch_plan_sha256
                or start.get("source_id") != source_id
                or start.get("coordinator_attempt") != 1
                or start.get("model") != self.manifest.audit_model
            ):
                raise CorpusCompletionValidationError(
                    f"prior audit start identity differs: {source_id}"
                )
        result: dict[str, Any] | None = None
        if result_path.is_file():
            result = _read_object(result_path, label=f"prior audit result for {source_id}")
            usage = result.get("token_usage")
            if (
                result.get("schema_version") != "1.0"
                or result.get("kind") != "corpus_audit_source_result"
                or result.get("run_id") != self.manifest.prior_completion_run_id
                or result.get("source_id") != source_id
                or result.get("outcome") not in {"eligible", "excluded"}
                or not isinstance(result.get("duration_seconds"), (int, float))
                or isinstance(result.get("duration_seconds"), bool)
                or float(result["duration_seconds"]) < 0
                or not isinstance(result.get("semantic_jobs_started"), int)
                or isinstance(result.get("semantic_jobs_started"), bool)
                or int(result["semantic_jobs_started"]) < 0
                or not isinstance(result.get("worker_events_emitted"), int)
                or isinstance(result.get("worker_events_emitted"), bool)
                or int(result["worker_events_emitted"]) < 0
                or not isinstance(usage, dict)
                or set(usage) != set(_empty_token_usage())
                or any(
                    not isinstance(value, int) or isinstance(value, bool) or value < 0
                    for value in usage.values()
                )
            ):
                raise CorpusCompletionValidationError(
                    f"prior audit result identity or telemetry differs: {source_id}"
                )
            if result["outcome"] == "eligible" and result.get("state") != "graph_inserted":
                raise CorpusCompletionValidationError(
                    f"prior eligible audit result is not graph_inserted: {source_id}"
                )
        findings = tuple(repository.findings_for(source_id))
        job_receipts = _retained_source_job_receipts(
            repository,
            source_id=source_id,
            run_root=root,
            repository_root=self.repository_root,
        )
        if result is not None:
            telemetry = {
                "semantic_jobs_started": int(result["semantic_jobs_started"]),
                "duration_seconds": float(result["duration_seconds"]),
                "worker_events_emitted": int(result["worker_events_emitted"]),
                "token_usage": dict(result["token_usage"]),
            }
            label = "audited_passed" if result["outcome"] == "eligible" else "audited_with_findings"
        elif start is not None:
            telemetry = _retained_job_telemetry(job_receipts)
            label = "audit_interrupted"
        else:
            telemetry = {
                "semantic_jobs_started": 0,
                "duration_seconds": 0.0,
                "worker_events_emitted": 0,
                "token_usage": _empty_token_usage(),
            }
            label = "unaudited_provisional"
        warnings = [] if result is None else [str(item) for item in result.get("warnings", ())]
        return {
            "quality_label": label,
            "started": start is not None,
            "terminal": result is not None,
            "outcome": None if result is None else result["outcome"],
            "terminal_state": None if result is None else result.get("state"),
            "failure_class": None if result is None else result.get("failure_class"),
            "failure_reason": None if result is None else result.get("failure_reason"),
            "findings": [dict(item) for item in findings],
            "findings_sha256": _digest(findings),
            "warnings": warnings,
            "start_receipt": (
                None
                if start is None
                else fingerprint(start_path, relative_to=self.repository_root).to_dict()
            ),
            "result_receipt": (
                None
                if result is None
                else fingerprint(result_path, relative_to=self.repository_root).to_dict()
            ),
            "job_receipts": list(job_receipts),
            "telemetry": telemetry,
        }

    def _prior_completion_root(self) -> Path:
        root = self.manifest.prior_completion_run_root
        if root is None:
            raise CorpusCompletionValidationError(
                "quality-labelled completion requires a prior completion run"
            )
        return root

    def run_through_quality(self, *, approved_launch_plan_sha256: str) -> CorpusCompletionResult:
        """Import and provisionally compile every selected structurally valid source."""

        if not self.manifest.quality_labelled:
            raise CorpusCompletionValidationError(
                "source-quality freeze requires the v2 selection policy"
            )
        if _SHA256.fullmatch(approved_launch_plan_sha256) is None:
            raise CorpusCompletionValidationError("approved launch-plan SHA-256 is malformed")
        preflight = self.preflight()
        if approved_launch_plan_sha256 != preflight.launch_plan_sha256:
            raise CorpusCompletionValidationError(
                "approved launch-plan SHA-256 does not match the current completion plan"
            )
        if self.quality_manifest_path.is_file():
            quality = self.cross_reference_quality()
            connectable_ids = tuple(
                str(item["source_id"]) for item in quality["connectable_sources"]
            )
            invalid_ids = tuple(
                str(item["source_id"]) for item in quality["structurally_invalid_sources"]
            )
            return CorpusCompletionResult(
                run_id=self.manifest.run_id,
                root=self.root,
                state="source_quality_frozen",
                launch_plan_path=self.launch_plan_path,
                launch_plan_sha256=preflight.launch_plan_sha256,
                preflight_receipt_path=self.preflight_receipt_path,
                source_count=len(self.manifest.source_ids),
                reused_source_ids=connectable_ids,
                eligible_source_ids=connectable_ids,
                excluded_source_ids=invalid_ids,
                selection_plan_path=self.selection_plan_path,
                quality_manifest_path=self.quality_manifest_path,
                quality_label_counts=dict(quality["quality_label_counts"]),
                selected_origin_counts=dict(quality["selected_origin_counts"]),
            )

        selections = tuple(
            self._select_quality_snapshot(source_id) for source_id in self.manifest.source_ids
        )
        imported: list[str] = []
        reused: list[str] = []
        connectable: list[dict[str, Any]] = []
        structurally_invalid: list[dict[str, Any]] = []
        for selection in selections:
            selected = selection.snapshot
            effective_origin = selection.selected_origin
            runtime_fallback_reason: str | None = None
            selected_error = self._snapshot_compilation_error(
                selected, selection_receipt=selection.receipt
            )
            if selected_error is not None and effective_origin == "prior_completion_wip":
                selected = self._verify_source_snapshot(selection.source_id)
                effective_origin = "source_run_provisional"
                runtime_fallback_reason = selected_error
                selected_error = self._snapshot_compilation_error(
                    selected, selection_receipt=selection.receipt
                )
            if selected_error is not None:
                structurally_invalid.append(
                    {
                        "source_id": selection.source_id,
                        "quality_label": selection.quality_label,
                        "failure_class": "SourceSnapshotCompilationError",
                        "failure_reason": selected_error,
                        "selected_origin": effective_origin,
                        "selection_receipt_sha256": fingerprint(
                            self.selection_root / f"{selection.source_id}.json"
                        ).sha256,
                        "warnings": list(selection.warnings),
                    }
                )
                continue
            applied, compilation = self._import_and_compile_quality_snapshot(
                selected,
                selection_receipt=selection.receipt,
                effective_origin=effective_origin,
                runtime_fallback_reason=runtime_fallback_reason,
            )
            (imported if applied else reused).append(selection.source_id)
            dossier_path = self.vault_root / selection.source_id / "paper.md"
            graph_path = self.build_root / selection.source_id / "graph.json"
            graph_manifest_path = self.build_root / selection.source_id / "manifest.json"
            graph_manifest = _read_object(
                graph_manifest_path,
                label=f"quality graph manifest for {selection.source_id}",
            )
            dossier = fingerprint(dossier_path, relative_to=self.repository_root)
            graph = fingerprint(graph_path, relative_to=self.repository_root)
            selection_receipt = fingerprint(
                self.selection_root / f"{selection.source_id}.json",
                relative_to=self.repository_root,
            )
            connectable.append(
                {
                    "source_id": selection.source_id,
                    "quality_label": selection.quality_label,
                    "selected_origin": effective_origin,
                    "runtime_fallback_reason": runtime_fallback_reason,
                    "selection_receipt_path": selection_receipt.path,
                    "selection_receipt_sha256": selection_receipt.sha256,
                    "source_state": compilation.state,
                    "dossier_path": dossier.path,
                    "dossier_sha256": dossier.sha256,
                    "dossier_bytes": dossier.size_bytes,
                    "graph_path": graph.path,
                    "graph_sha256": graph.sha256,
                    "record_count": int(graph_manifest["record_count"]),
                    "edge_count": int(graph_manifest["edge_count"]),
                    "findings": [dict(item) for item in selection.findings],
                    "findings_sha256": _digest(selection.findings),
                    "warnings": [*selection.warnings, *compilation.warnings],
                    "audit_telemetry": selection.audit_telemetry,
                    "source_semantic_jobs_started": 0,
                }
            )
        label_counts = {
            label: sum(selection.quality_label == label for selection in selections)
            for label in _QUALITY_LABELS
        }
        selected_origin_counts = {
            origin: sum(
                item["selected_origin"] == origin for item in [*connectable, *structurally_invalid]
            )
            for origin in ("prior_completion_wip", "source_run_provisional")
        }
        payload = {
            "schema_version": "1.0",
            "kind": "corpus_source_quality_manifest",
            "run_id": self.manifest.run_id,
            "launch_plan_sha256": preflight.launch_plan_sha256,
            "selection_plan_sha256": fingerprint(self.selection_plan_path).sha256,
            "selection_policy": self.manifest.selection_policy,
            "requested_source_ids": list(self.manifest.source_ids),
            "connectable_sources": connectable,
            "structurally_invalid_sources": structurally_invalid,
            "source_count": len(self.manifest.source_ids),
            "connectable_source_count": len(connectable),
            "structurally_invalid_source_count": len(structurally_invalid),
            "quality_label_counts": label_counts,
            "selected_origin_counts": selected_origin_counts,
            "record_count": sum(int(item["record_count"]) for item in connectable),
            "edge_count": sum(int(item["edge_count"]) for item in connectable),
            "source_telemetry": {
                "semantic_jobs_started": 0,
                "duration_seconds": 0.0,
                "worker_events_emitted": 0,
                "token_usage": _empty_token_usage(),
            },
        }
        self._write_or_verify_receipt(
            self.quality_manifest_path,
            {key: value for key, value in payload.items() if key != "schema_version"},
            schema_path=self.repository_root / QUALITY_MANIFEST_SCHEMA,
        )
        return CorpusCompletionResult(
            run_id=self.manifest.run_id,
            root=self.root,
            state="source_quality_frozen",
            launch_plan_path=self.launch_plan_path,
            launch_plan_sha256=preflight.launch_plan_sha256,
            preflight_receipt_path=self.preflight_receipt_path,
            source_count=len(self.manifest.source_ids),
            imported_source_ids=tuple(imported),
            reused_source_ids=tuple(reused),
            eligible_source_ids=tuple(str(item["source_id"]) for item in connectable),
            excluded_source_ids=tuple(str(item["source_id"]) for item in structurally_invalid),
            selection_plan_path=self.selection_plan_path,
            quality_manifest_path=self.quality_manifest_path,
            quality_label_counts=label_counts,
            selected_origin_counts=selected_origin_counts,
        )

    def cross_reference_quality(self) -> dict[str, Any]:
        """Load and verify the v2 structural-only mapper input boundary."""

        if not self.quality_manifest_path.is_file():
            raise CorpusCompletionValidationError(
                "cross-reference requires a frozen source-quality manifest"
            )
        payload = _read_object(self.quality_manifest_path, label="source-quality manifest")
        _validate_payload(
            payload,
            schema_path=self.repository_root / QUALITY_MANIFEST_SCHEMA,
            label="source-quality manifest",
        )
        if (
            payload["run_id"] != self.manifest.run_id
            or payload["launch_plan_sha256"] != fingerprint(self.launch_plan_path).sha256
            or payload["selection_plan_sha256"] != fingerprint(self.selection_plan_path).sha256
            or tuple(payload["requested_source_ids"]) != self.manifest.source_ids
            or payload["selection_policy"] != self.manifest.selection_policy
        ):
            raise CorpusCompletionValidationError(
                "source-quality manifest identity binding is invalid"
            )
        connectable = {str(item["source_id"]) for item in payload["connectable_sources"]}
        invalid = {str(item["source_id"]) for item in payload["structurally_invalid_sources"]}
        source_entries = [
            *payload["connectable_sources"],
            *payload["structurally_invalid_sources"],
        ]
        expected_label_counts = {
            label: sum(item["quality_label"] == label for item in source_entries)
            for label in _QUALITY_LABELS
        }
        expected_origin_counts = {
            origin: sum(item["selected_origin"] == origin for item in source_entries)
            for origin in ("prior_completion_wip", "source_run_provisional")
        }
        if (
            connectable.intersection(invalid)
            or connectable.union(invalid) != set(self.manifest.source_ids)
            or payload["connectable_source_count"] != len(connectable)
            or payload["structurally_invalid_source_count"] != len(invalid)
            or payload["source_count"] != len(self.manifest.source_ids)
            or payload["quality_label_counts"] != expected_label_counts
            or payload["selected_origin_counts"] != expected_origin_counts
        ):
            raise CorpusCompletionValidationError("source-quality membership is not exhaustive")
        repository = StateRepository(self.state_path)
        for item in payload["connectable_sources"]:
            source_id = str(item["source_id"])
            if repository.source_state(source_id) != "calibration_graph_inserted":
                raise CorpusCompletionValidationError(
                    f"connectable source is not provisionally compiled: {source_id}"
                )
            self._verify_quality_artifact(item, "selection_receipt")
            self._verify_quality_artifact(item, "dossier")
            self._verify_quality_artifact(item, "graph")
            selection = read_receipt(
                (self.repository_root / str(item["selection_receipt_path"])).resolve()
            )
            if (
                selection.get("source_id") != source_id
                or selection.get("quality_label") != item["quality_label"]
            ):
                raise CorpusCompletionValidationError(
                    f"source-quality selection identity changed: {source_id}"
                )
        return payload

    def _snapshot_compilation_error(
        self,
        snapshot: SourceSnapshot,
        *,
        selection_receipt: Mapping[str, Any],
    ) -> str | None:
        try:
            with tempfile.TemporaryDirectory(prefix="research-map-quality-") as value:
                root = Path(value)
                state = StateRepository(root / "state.sqlite3")
                state.initialize()
                state.import_source_snapshot(
                    snapshot.source_id,
                    metadata=snapshot.metadata,
                    assets=snapshot.assets,
                    records=snapshot.records,
                    coverage=snapshot.coverage,
                    run_identifier=stable_id(
                        "run", (self.manifest.run_id, snapshot.source_id, "trial_import")
                    ),
                    job_identifier=stable_id(
                        "job", (self.manifest.run_id, snapshot.source_id, "trial_import")
                    ),
                    receipt_path=str(root / "trial-import.json"),
                    origin_receipt=selection_receipt,
                )
                CompilationCoordinator(
                    repository_root=self.repository_root,
                    state_path=root / "state.sqlite3",
                    cache_root=root / "cache",
                    vault_root=root / "vault/sources",
                    build_root=root / "build",
                ).run_through_calibration_graph_inserted(
                    snapshot.source_id, model=self.manifest.source_model
                )
        except (
            CompilationError,
            ReadingValidationError,
            DossierValidationError,
            ProposalValidationError,
            IdentityMismatch,
            OSError,
        ) as error:
            return f"{type(error).__name__}: {error}"
        return None

    def _import_and_compile_quality_snapshot(
        self,
        snapshot: SourceSnapshot,
        *,
        selection_receipt: Mapping[str, Any],
        effective_origin: str,
        runtime_fallback_reason: str | None,
    ) -> tuple[bool, Any]:
        repository = StateRepository(self.state_path)
        repository.initialize()
        run_identifier = stable_id(
            "run", (self.manifest.run_id, snapshot.source_id, "quality_snapshot_import")
        )
        job_identifier = stable_id("job", (run_identifier, "quality_snapshot_import", "1"))
        transfer_path = self.transfer_root / f"{snapshot.source_id}.json"
        origin_receipt = {
            "kind": "quality_labelled_snapshot_import",
            "selection": dict(selection_receipt),
            "effective_origin": effective_origin,
            "runtime_fallback_reason": runtime_fallback_reason,
            "semantic_jobs_started": 0,
        }
        self._write_or_verify_receipt(transfer_path, origin_receipt)
        current = repository.source_state(snapshot.source_id)
        if current is None:
            outcome = repository.import_source_snapshot(
                snapshot.source_id,
                metadata=snapshot.metadata,
                assets=snapshot.assets,
                records=snapshot.records,
                coverage=snapshot.coverage,
                run_identifier=run_identifier,
                job_identifier=job_identifier,
                receipt_path=str(transfer_path),
                origin_receipt=origin_receipt,
            )
            applied = outcome.applied
        else:
            destination_source = repository.source_record(snapshot.source_id)
            if (
                current != "calibration_graph_inserted"
                or destination_source is None
                or destination_source["metadata"] != snapshot.metadata
                or repository.assets_for(snapshot.source_id) != snapshot.assets
                or repository.latest_wip_records(snapshot.source_id) != snapshot.records
                or _origin_coverage(repository.coverage_for(snapshot.source_id))
                != snapshot.coverage
            ):
                raise CorpusCompletionExecutionError(
                    f"quality snapshot replay differs: {snapshot.source_id}"
                )
            applied = False
        compilation = CompilationCoordinator(
            repository_root=self.repository_root,
            state_path=self.state_path,
            cache_root=self.cache_root,
            vault_root=self.vault_root,
            build_root=self.build_root,
        ).run_through_calibration_graph_inserted(
            snapshot.source_id, model=self.manifest.source_model
        )
        return applied, compilation

    def _verify_quality_artifact(self, item: Mapping[str, Any], artifact_kind: str) -> None:
        path = (self.repository_root / str(item[f"{artifact_kind}_path"])).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise CorpusCompletionValidationError(
                f"source-quality {artifact_kind} escapes or is missing: {item['source_id']}"
            )
        observed = fingerprint(path)
        if observed.sha256 != item[f"{artifact_kind}_sha256"] or (
            artifact_kind == "dossier" and observed.size_bytes != item["dossier_bytes"]
        ):
            raise CorpusCompletionValidationError(
                f"source-quality {artifact_kind} fingerprint changed: {item['source_id']}"
            )

    def transfer(self, *, approved_launch_plan_sha256: str) -> CorpusCompletionResult:
        if self.manifest.quality_labelled:
            return self.run_through_quality(approved_launch_plan_sha256=approved_launch_plan_sha256)
        if _SHA256.fullmatch(approved_launch_plan_sha256) is None:
            raise CorpusCompletionValidationError("approved launch-plan SHA-256 is malformed")
        preflight = self.preflight()
        if approved_launch_plan_sha256 != preflight.launch_plan_sha256:
            raise CorpusCompletionValidationError(
                "approved launch-plan SHA-256 does not match the current completion plan"
            )
        destination = StateRepository(self.state_path)
        destination.initialize()
        imported: list[str] = []
        reused: list[str] = []
        expected_receipts: dict[str, dict[str, Any]] = {}
        for source_id in self.manifest.source_ids:
            snapshot = self._verify_source_snapshot(source_id)
            receipt_path = self.transfer_root / f"{source_id}.json"
            expected_receipt = snapshot.transfer_receipt
            expected_receipts[source_id] = expected_receipt
            self._write_or_verify_receipt(
                receipt_path,
                {key: value for key, value in expected_receipt.items() if key != "schema_version"},
                schema_path=self.repository_root / TRANSFER_SCHEMA,
            )
            run_identifier = stable_id(
                "run", (self.manifest.run_id, source_id, "corpus_snapshot_import")
            )
            job_identifier = stable_id("job", (run_identifier, "corpus_snapshot_import", "1"))
            current_state = destination.source_state(source_id)
            audit_started = (self.audit_start_root / f"{source_id}.json").is_file()
            terminal_result_path = self.audit_result_root / f"{source_id}.json"
            terminal_result = (
                self._load_audit_result(source_id, terminal_result_path)
                if terminal_result_path.is_file()
                else None
            )
            if (
                terminal_result is not None
                or audit_started
                or current_state
                in {
                    "audit_failed",
                    "audit_passed",
                    "graph_inserted",
                }
            ):
                if current_state is None:
                    raise CorpusCompletionExecutionError(
                        f"terminal audit source disappeared: {source_id}"
                    )
                self._verify_destination_snapshot(
                    destination,
                    snapshot=snapshot,
                    run_identifier=run_identifier,
                    job_identifier=job_identifier,
                    receipt_path=receipt_path,
                )
                outcome = SnapshotImportOutcome(
                    source_id=source_id,
                    state=current_state,
                    applied=False,
                    transition_count=len(destination.transitions_for("source", source_id)),
                )
            else:
                try:
                    outcome = destination.import_source_snapshot(
                        source_id,
                        metadata=snapshot.metadata,
                        assets=snapshot.assets,
                        records=snapshot.records,
                        coverage=snapshot.coverage,
                        run_identifier=run_identifier,
                        job_identifier=job_identifier,
                        receipt_path=str(receipt_path),
                        origin_receipt=expected_receipt,
                    )
                except IdentityMismatch as error:
                    raise CorpusCompletionExecutionError(str(error)) from error
            self._verify_import(
                destination,
                snapshot=snapshot,
                outcome=outcome,
                terminal_result=terminal_result,
                audit_started=audit_started,
            )
            (imported if outcome.applied else reused).append(source_id)

        replayed = {
            source_id: self._verify_source_snapshot(source_id).transfer_receipt
            for source_id in self.manifest.source_ids
        }
        if replayed != expected_receipts:
            raise CorpusCompletionExecutionError("source run changed during snapshot transfer")
        return CorpusCompletionResult(
            run_id=self.manifest.run_id,
            root=self.root,
            state="source_snapshots_transferred",
            launch_plan_path=self.launch_plan_path,
            launch_plan_sha256=preflight.launch_plan_sha256,
            preflight_receipt_path=self.preflight_receipt_path,
            source_count=len(self.manifest.source_ids),
            imported_source_ids=tuple(imported),
            reused_source_ids=tuple(reused),
        )

    def run_through_audit(
        self,
        *,
        approved_launch_plan_sha256: str,
        resume_started_audits: bool = False,
    ) -> CorpusCompletionResult:
        """Run the canary and bounded audit queue, then freeze eligibility."""

        if self.manifest.quality_labelled:
            if resume_started_audits:
                raise CorpusCompletionValidationError(
                    "quality-labelled completion never resumes source audits"
                )
            return self.run_through_quality(approved_launch_plan_sha256=approved_launch_plan_sha256)

        transfer = self.transfer(approved_launch_plan_sha256=approved_launch_plan_sha256)
        prior = self._audit_results()
        semantic_jobs_started = 0
        worker_events_emitted = 0
        duration_seconds = 0.0
        token_usage = _empty_token_usage()
        active = 0
        peak = 0
        lock = threading.Lock()
        resumable_started: frozenset[str] = frozenset()

        def execute(source_id: str) -> AuditSourceResult:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                return self._execute_audit_source(
                    source_id,
                    launch_plan_sha256=transfer.launch_plan_sha256,
                    resume_started=source_id in resumable_started,
                )
            finally:
                with lock:
                    active -= 1

        canary = self.manifest.canary_source_id
        if canary not in prior:
            try:
                canary_result = execute(canary)
            except Exception as error:
                raise CorpusCompletionExecutionError(
                    f"audit canary coordinator failed: {type(error).__name__}: {error}"
                ) from error
            prior[canary] = canary_result
            semantic_jobs_started += canary_result.semantic_jobs_started
            worker_events_emitted += canary_result.worker_events_emitted
            duration_seconds += canary_result.duration_seconds
            _add_token_usage(token_usage, canary_result.token_usage or _empty_token_usage())

        remaining = [
            source_id
            for source_id in self.manifest.source_ids
            if source_id != canary and source_id not in prior
        ]
        blocked = [
            source_id
            for source_id in remaining
            if (self.audit_start_root / f"{source_id}.json").exists()
        ]
        if blocked and not resume_started_audits:
            raise CorpusCompletionExecutionError(
                "audit resume found started sources without terminal receipts: "
                + ", ".join(blocked)
            )
        resumable_started = frozenset(blocked)
        for source_id in blocked:
            self._prepare_started_audit_resume(
                source_id,
                launch_plan_sha256=transfer.launch_plan_sha256,
            )

        system_error: Exception | None = None
        with ThreadPoolExecutor(max_workers=self.manifest.audit_workers) as executor:
            futures: dict[Future[AuditSourceResult], str] = {}
            pending = iter(remaining)

            def submit_next() -> bool:
                try:
                    source_id = next(pending)
                except StopIteration:
                    return False
                futures[executor.submit(execute, source_id)] = source_id
                return True

            for _ in range(self.manifest.audit_workers):
                if not submit_next():
                    break
            while futures:
                done, _ = wait(tuple(futures), return_when=FIRST_COMPLETED)
                completed_successes = 0
                for future in sorted(done, key=lambda item: futures[item]):
                    source_id = futures.pop(future)
                    try:
                        result = future.result()
                    except Exception as error:
                        system_error = error
                    else:
                        prior[source_id] = result
                        semantic_jobs_started += result.semantic_jobs_started
                        worker_events_emitted += result.worker_events_emitted
                        duration_seconds += result.duration_seconds
                        _add_token_usage(token_usage, result.token_usage or _empty_token_usage())
                        completed_successes += 1
                if system_error is None:
                    for _ in range(completed_successes):
                        submit_next()
                if system_error is not None:
                    for future in tuple(futures):
                        try:
                            result = future.result()
                        except Exception:
                            pass
                        else:
                            source_id = futures[future]
                            prior[source_id] = result
                            semantic_jobs_started += result.semantic_jobs_started
                            worker_events_emitted += result.worker_events_emitted
                            duration_seconds += result.duration_seconds
                            _add_token_usage(
                                token_usage, result.token_usage or _empty_token_usage()
                            )
                    futures.clear()

        if system_error is not None:
            if isinstance(system_error, (CorpusCompletionExecutionError, OSError)):
                raise CorpusCompletionExecutionError(str(system_error)) from system_error
            raise CorpusCompletionExecutionError(
                f"audit coordinator failed: {type(system_error).__name__}: {system_error}"
            ) from system_error

        if set(prior) != set(self.manifest.source_ids):
            missing = sorted(set(self.manifest.source_ids).difference(prior))
            raise CorpusCompletionExecutionError(
                "audit queue ended before every source became terminal: " + ", ".join(missing)
            )
        manifest = self._freeze_eligibility(
            prior,
            launch_plan_sha256=transfer.launch_plan_sha256,
            peak_audit_concurrency=peak,
        )
        return CorpusCompletionResult(
            run_id=self.manifest.run_id,
            root=self.root,
            state="audit_complete",
            launch_plan_path=self.launch_plan_path,
            launch_plan_sha256=transfer.launch_plan_sha256,
            preflight_receipt_path=self.preflight_receipt_path,
            source_count=len(self.manifest.source_ids),
            semantic_jobs_started=semantic_jobs_started,
            worker_events_emitted=worker_events_emitted,
            duration_seconds=duration_seconds,
            token_usage=token_usage,
            imported_source_ids=transfer.imported_source_ids,
            reused_source_ids=transfer.reused_source_ids,
            eligible_source_ids=tuple(
                str(item["source_id"]) for item in manifest["eligible_sources"]
            ),
            excluded_source_ids=tuple(
                str(item["source_id"]) for item in manifest["excluded_sources"]
            ),
            peak_audit_concurrency=int(manifest["peak_audit_concurrency"]),
            eligibility_manifest_path=self.eligibility_manifest_path,
        )

    def status(self) -> dict[str, Any]:
        """Return schema-validated, read-only completion status."""

        if self.manifest.quality_labelled:
            return self._quality_status()

        repository = StateRepository(self.state_path)
        source_states = {
            source_id: repository.source_state(source_id)
            for source_id in self.manifest.source_ids
            if self.state_path.is_file()
        }
        results = self._audit_results()
        eligibility = (
            self.cross_reference_eligibility() if self.eligibility_manifest_path.is_file() else None
        )
        coverage = self._cross_reference_coverage()
        source_failures = [] if eligibility is None else list(eligibility["excluded_sources"])
        shard_failures = (
            []
            if coverage is None
            else [
                {
                    "shard_id": str(item["shard_id"]),
                    "failure_class": str(item["failure_class"]),
                    "failure_reason": str(item["failure_reason"]),
                    "owned_pairs": list(item["owned_pairs"]),
                    "semantic_job_started": bool(item["semantic_job_started"]),
                    "duration_seconds": float(item["duration_seconds"]),
                    "worker_events_emitted": int(item["worker_events_emitted"]),
                    "token_usage": dict(item["token_usage"]),
                }
                for item in coverage["shards"]
                if item["status"] == "failed"
            ]
        )
        if coverage is not None:
            state = (
                "complete" if not source_failures and not coverage["uncovered_pairs"] else "partial"
            )
        elif self.cross_reference_plan_path.is_file():
            state = "cross_reference_in_progress"
        elif eligibility is not None:
            state = "audit_complete"
        elif results or any(self.audit_start_root.glob("*.json")):
            state = "audit_in_progress"
        elif source_states:
            state = "source_snapshots_transferred"
        elif self.preflight_receipt_path.is_file():
            state = "ready_for_approval"
        else:
            state = "not_preflighted"
        eligible_count = 0 if eligibility is None else int(eligibility["eligible_source_count"])
        excluded_count = 0 if eligibility is None else int(eligibility["excluded_source_count"])
        artifacts: list[dict[str, Any]] = []
        for kind, path in (
            ("launch_plan", self.launch_plan_path),
            ("preflight_receipt", self.preflight_receipt_path),
            ("eligibility_manifest", self.eligibility_manifest_path),
            ("cross_reference_plan", self.cross_reference_plan_path),
        ):
            if path.is_file():
                item = fingerprint(path).to_dict()
                artifacts.append({"kind": kind, "path": item["path"], "sha256": item["sha256"]})
        cross_reference_batch_id: str | None = None
        pair_count = 0
        uncovered_pair_count = 0
        cross_reference_jobs = 0
        cross_reference_telemetry: dict[str, Any] = {
            "semantic_jobs_started": 0,
            "duration_seconds": 0.0,
            "worker_events_emitted": 0,
            "token_usage": _empty_token_usage(),
        }
        if coverage is not None:
            cross_reference_batch_id = str(coverage["batch_id"])
            pair_count = int(coverage["pair_count"])
            uncovered_pair_count = len(coverage["uncovered_pairs"])
            cross_reference_jobs = int(coverage["semantic_jobs_started"])
            cross_reference_telemetry = {
                "semantic_jobs_started": cross_reference_jobs,
                "duration_seconds": float(coverage["duration_seconds"]),
                "worker_events_emitted": int(coverage["worker_events_emitted"]),
                "token_usage": dict(coverage["token_usage"]),
            }
            graph_path = self.build_root / "corpus" / cross_reference_batch_id / "graph.json"
            try:
                explorer = GraphExplorer(
                    graph_path=graph_path,
                    schema_directory=self.repository_root / "schemas/research-map/v1",
                )
            except GraphExplorationError as error:
                raise CorpusCompletionValidationError(
                    f"compiled completion graph is invalid: {error}"
                ) from error
            if (
                explorer.graph_kind != "corpus"
                or explorer.graph.get("batch_id") != cross_reference_batch_id
                or explorer.manifest is None
            ):
                raise CorpusCompletionValidationError(
                    "compiled completion graph identity is invalid"
                )
            for kind, path in (
                ("cross_reference_coverage", Path(str(coverage["_path"]))),
                ("corpus_graph", graph_path),
                (
                    "corpus_graph_manifest",
                    self.build_root / "corpus" / cross_reference_batch_id / "manifest.json",
                ),
            ):
                if path.is_file():
                    item = fingerprint(path).to_dict()
                    artifacts.append({"kind": kind, "path": item["path"], "sha256": item["sha256"]})
        audit_duration = sum(result.duration_seconds for result in results.values())
        audit_usage = _empty_token_usage()
        for result in results.values():
            _add_token_usage(audit_usage, result.token_usage or _empty_token_usage())
        total_usage = dict(audit_usage)
        _add_token_usage(total_usage, cross_reference_telemetry["token_usage"])
        payload = {
            "schema_version": "1.0",
            "run_id": self.manifest.run_id,
            "state": state,
            "source_count": len(self.manifest.source_ids),
            "transferred_source_count": sum(value is not None for value in source_states.values()),
            "terminal_audit_source_count": len(results),
            "eligible_source_count": eligible_count,
            "excluded_source_count": excluded_count,
            "cross_reference_batch_id": cross_reference_batch_id,
            "pair_count": pair_count,
            "uncovered_pair_count": uncovered_pair_count,
            "semantic_jobs_started": sum(
                result.semantic_jobs_started for result in results.values()
            )
            + cross_reference_jobs,
            "worker_events_emitted": sum(
                result.worker_events_emitted for result in results.values()
            )
            + cross_reference_telemetry["worker_events_emitted"],
            "duration_seconds": audit_duration + cross_reference_telemetry["duration_seconds"],
            "token_usage": total_usage,
            "cross_reference_telemetry": cross_reference_telemetry,
            "source_failures": source_failures,
            "shard_failures": shard_failures,
            "artifacts": artifacts,
        }
        _validate_payload(
            payload,
            schema_path=self.repository_root / STATUS_SCHEMA,
            label="corpus completion status",
        )
        return payload

    def _quality_status(self) -> dict[str, Any]:
        quality = self.cross_reference_quality() if self.quality_manifest_path.is_file() else None
        selected_origin_counts = (
            {"prior_completion_wip": 0, "source_run_provisional": 0}
            if quality is None and not self.selection_plan_path.is_file()
            else (
                dict(quality["selected_origin_counts"])
                if quality is not None
                else self._selected_origin_counts()
            )
        )
        coverage = self._cross_reference_coverage()
        if coverage is not None and quality is None:
            raise CorpusCompletionValidationError(
                "cross-reference coverage lacks its source-quality manifest"
            )
        quality_payload = {} if quality is None else quality
        if coverage is not None:
            state = (
                "complete"
                if not quality_payload["structurally_invalid_sources"]
                and not coverage["uncovered_pairs"]
                else "partial"
            )
        elif self.cross_reference_plan_path.is_file():
            state = "cross_reference_in_progress"
        elif quality is not None:
            state = "source_quality_frozen"
        elif self.preflight_receipt_path.is_file():
            state = "ready_for_approval"
        else:
            state = "not_preflighted"
        artifacts: list[dict[str, Any]] = []
        for kind, path in (
            ("launch_plan", self.launch_plan_path),
            ("preflight_receipt", self.preflight_receipt_path),
            ("selection_plan", self.selection_plan_path),
            ("quality_manifest", self.quality_manifest_path),
            ("cross_reference_plan", self.cross_reference_plan_path),
        ):
            if path.is_file():
                value = fingerprint(path).to_dict()
                artifacts.append({"kind": kind, "path": value["path"], "sha256": value["sha256"]})
        label_counts = (
            {label: 0 for label in _QUALITY_LABELS}
            if quality is None
            else dict(quality["quality_label_counts"])
        )
        source_telemetry = (
            {
                "semantic_jobs_started": 0,
                "duration_seconds": 0.0,
                "worker_events_emitted": 0,
                "token_usage": _empty_token_usage(),
            }
            if quality is None
            else dict(quality["source_telemetry"])
        )
        cross_reference_telemetry: dict[str, Any] = {
            "semantic_jobs_started": 0,
            "duration_seconds": 0.0,
            "worker_events_emitted": 0,
            "token_usage": _empty_token_usage(),
        }
        cross_reference_batch_id: str | None = None
        pair_count = 0
        uncovered_pair_count = 0
        shard_failures: list[dict[str, Any]] = []
        relationship_count = 0
        if coverage is not None:
            cross_reference_batch_id = str(coverage["batch_id"])
            pair_count = int(coverage["pair_count"])
            uncovered_pair_count = len(coverage["uncovered_pairs"])
            cross_reference_telemetry = {
                "semantic_jobs_started": int(coverage["semantic_jobs_started"]),
                "duration_seconds": float(coverage["duration_seconds"]),
                "worker_events_emitted": int(coverage["worker_events_emitted"]),
                "token_usage": dict(coverage["token_usage"]),
            }
            shard_failures = [
                {
                    "shard_id": str(item["shard_id"]),
                    "failure_class": str(item["failure_class"]),
                    "failure_reason": str(item["failure_reason"]),
                    "owned_pairs": list(item["owned_pairs"]),
                    "semantic_job_started": bool(item["semantic_job_started"]),
                    "duration_seconds": float(item["duration_seconds"]),
                    "worker_events_emitted": int(item["worker_events_emitted"]),
                    "token_usage": dict(item["token_usage"]),
                }
                for item in coverage["shards"]
                if item["status"] == "failed"
            ]
            graph_path = self._quality_graph_path(cross_reference_batch_id)
            try:
                explorer = GraphExplorer(
                    graph_path=graph_path,
                    schema_directory=self.repository_root / "schemas/research-map/v1",
                )
            except GraphExplorationError as error:
                raise CorpusCompletionValidationError(
                    f"quality-labelled completion graph is invalid: {error}"
                ) from error
            manifest = explorer.manifest
            if (
                explorer.graph_kind != "corpus"
                or explorer.graph.get("batch_id") != cross_reference_batch_id
                or manifest is None
                or manifest.get("quality_manifest_sha256")
                != fingerprint(self.quality_manifest_path).sha256
                or manifest.get("quality_label_counts") != quality_payload["quality_label_counts"]
                or manifest.get("selected_origin_counts")
                != quality_payload["selected_origin_counts"]
                or manifest.get("source_quality") != quality_source_summaries(quality_payload)
                or manifest.get("structurally_invalid_sources")
                != quality_payload["structurally_invalid_sources"]
            ):
                raise CorpusCompletionValidationError(
                    "quality-labelled completion graph identity is invalid"
                )
            relationship_count = int(manifest["cross_source_edge_count"])
            for kind, path in (
                ("cross_reference_coverage", Path(str(coverage["_path"]))),
                ("corpus_graph", graph_path),
                ("corpus_graph_manifest", graph_path.with_name("manifest.json")),
            ):
                value = fingerprint(path).to_dict()
                artifacts.append({"kind": kind, "path": value["path"], "sha256": value["sha256"]})
        payload = {
            "schema_version": "1.0",
            "run_id": self.manifest.run_id,
            "selection_policy": self.manifest.selection_policy,
            "state": state,
            "source_count": len(self.manifest.source_ids),
            "transferred_source_count": (
                0 if quality is None else int(quality["connectable_source_count"])
            ),
            "terminal_audit_source_count": 0,
            "eligible_source_count": (
                0 if quality is None else int(quality["connectable_source_count"])
            ),
            "excluded_source_count": (
                0 if quality is None else int(quality["structurally_invalid_source_count"])
            ),
            "quality_label_counts": label_counts,
            "selected_origin_counts": selected_origin_counts,
            "record_count": 0 if quality is None else int(quality["record_count"]),
            "edge_count": 0 if quality is None else int(quality["edge_count"]),
            "relationship_count": relationship_count,
            "source_telemetry": source_telemetry,
            "cross_reference_batch_id": cross_reference_batch_id,
            "pair_count": pair_count,
            "uncovered_pair_count": uncovered_pair_count,
            "semantic_jobs_started": cross_reference_telemetry["semantic_jobs_started"],
            "worker_events_emitted": cross_reference_telemetry["worker_events_emitted"],
            "duration_seconds": cross_reference_telemetry["duration_seconds"],
            "token_usage": dict(cross_reference_telemetry["token_usage"]),
            "cross_reference_telemetry": cross_reference_telemetry,
            "source_failures": (
                [] if quality is None else list(quality["structurally_invalid_sources"])
            ),
            "shard_failures": shard_failures,
            "artifacts": artifacts,
        }
        _validate_payload(
            payload,
            schema_path=self.repository_root / STATUS_SCHEMA,
            label="corpus completion status",
        )
        return payload

    def _quality_graph_path(self, batch_id: str) -> Path:
        return self.build_root / "quality-corpus" / batch_id / "graph.json"

    def _selected_origin_counts(self) -> dict[str, int]:
        selection_plan = _read_object(self.selection_plan_path, label="source selection plan")
        raw_counts = selection_plan.get("selected_origin_counts")
        expected_keys = {"prior_completion_wip", "source_run_provisional"}
        if (
            selection_plan.get("run_id") != self.manifest.run_id
            or selection_plan.get("selection_policy") != self.manifest.selection_policy
            or selection_plan.get("requested_source_ids") != list(self.manifest.source_ids)
            or not isinstance(raw_counts, dict)
            or set(raw_counts) != expected_keys
            or not all(
                isinstance(value, int) and not isinstance(value, bool) and value >= 0
                for value in raw_counts.values()
            )
            or sum(raw_counts.values()) != len(self.manifest.source_ids)
        ):
            raise CorpusCompletionValidationError(
                "source selection plan identity or counts are invalid"
            )
        return {key: int(raw_counts[key]) for key in sorted(expected_keys)}

    def report(self) -> dict[str, Any]:
        """Materialize one immutable terminal report from verified run receipts."""

        status = self.status()
        if status["state"] not in {"complete", "partial"}:
            raise CorpusCompletionValidationError(
                "completion report requires a compiled complete or partial run"
            )
        payload = {
            "schema_version": "1.0",
            "kind": "corpus_completion_report",
            "run_id": self.manifest.run_id,
            "state": status["state"],
            "launch_plan_sha256": fingerprint(self.launch_plan_path).sha256,
            "source_count": status["source_count"],
            "eligible_source_count": status["eligible_source_count"],
            "excluded_source_count": status["excluded_source_count"],
            "cross_reference_batch_id": status["cross_reference_batch_id"],
            "pair_count": status["pair_count"],
            "uncovered_pair_count": status["uncovered_pair_count"],
            "semantic_jobs_started": status["semantic_jobs_started"],
            "worker_events_emitted": status["worker_events_emitted"],
            "duration_seconds": status["duration_seconds"],
            "token_usage": status["token_usage"],
            "cross_reference_telemetry": status["cross_reference_telemetry"],
            "source_failures": status["source_failures"],
            "shard_failures": status["shard_failures"],
            "artifacts": status["artifacts"],
        }
        if self.manifest.quality_labelled:
            payload.update(
                {
                    "selection_policy": self.manifest.selection_policy,
                    "quality_label_counts": status["quality_label_counts"],
                    "selected_origin_counts": status["selected_origin_counts"],
                    "record_count": status["record_count"],
                    "edge_count": status["edge_count"],
                    "relationship_count": status["relationship_count"],
                    "source_telemetry": status["source_telemetry"],
                }
            )
        self._write_or_verify_receipt(
            self.report_path,
            {key: value for key, value in payload.items() if key != "schema_version"},
            schema_path=self.repository_root / REPORT_SCHEMA,
        )
        return payload

    def run_through_cross_reference(
        self,
        *,
        approved_launch_plan_sha256: str,
        resume_started_audits: bool = False,
    ) -> CorpusCompletionResult:
        """Resume through bounded exact-pair mapping and isolated graph compilation."""

        audit = self.run_through_audit(
            approved_launch_plan_sha256=approved_launch_plan_sha256,
            resume_started_audits=resume_started_audits,
        )
        source_manifest = (
            self.cross_reference_quality()
            if self.manifest.quality_labelled
            else self.cross_reference_eligibility()
        )
        plan = plan_corpus_cross_reference(
            source_manifest,
            max_sources_per_block=self.manifest.max_sources_per_block,
            max_dossier_bytes_per_block=self.manifest.max_dossier_bytes_per_block,
            max_mapper_input_bytes=self.manifest.max_mapper_input_bytes,
        )
        _validate_payload(
            plan,
            schema_path=self.repository_root / CROSS_REFERENCE_PLAN_SCHEMA,
            label="corpus cross-reference plan",
        )
        self._write_or_verify_receipt(
            self.cross_reference_plan_path,
            {key: value for key, value in plan.items() if key != "schema_version"},
            schema_path=self.repository_root / CROSS_REFERENCE_PLAN_SCHEMA,
        )
        plan_sha256 = fingerprint(self.cross_reference_plan_path).sha256
        strategy_prefix = (
            "corpus-quality-block-pair-multi-surface-v2"
            if self.manifest.quality_labelled
            else "corpus-block-pair-multi-surface-v1"
        )
        strategy = f"{strategy_prefix}:{plan_sha256}"
        executable: list[OwnedPairShard] = []
        for shard in plan["shards"]:
            if shard["no_op"] or shard["planning_failure"] is not None:
                continue
            executable.append(
                OwnedPairShard(
                    shard_id=str(shard["shard_id"]),
                    source_ids=tuple(str(item) for item in shard["source_ids"]),
                    owned_pairs=tuple(_plan_pair(pair) for pair in shard["owned_pairs"]),
                )
            )
        pre_uncovered = tuple(_plan_pair(pair) for pair in plan["uncovered_pairs"])
        try:
            cross_reference = CrossReferenceCoordinator(
                repository_root=self.repository_root,
                state_path=self.state_path,
                cache_root=self.cache_root,
                vault_root=self.vault_root,
                build_root=self.build_root,
                relationship_root=self.relationship_root,
                codex_executable=self.cross_reference_codex_executable,
                eligible_source_states=(
                    frozenset({"calibration_graph_inserted"})
                    if self.manifest.quality_labelled
                    else None
                ),
            ).run_owned_pair_shards(
                tuple(str(item) for item in plan["source_ids"]),
                shards=tuple(executable),
                model=self.manifest.cross_reference_model,
                strategy=strategy,
                pre_uncovered_pairs=pre_uncovered,
            )
        except CrossReferenceValidationError as error:
            raise CorpusCompletionValidationError(str(error)) from error
        except CrossReferenceExecutionError as error:
            raise CorpusCompletionExecutionError(str(error)) from error
        graph_path = self.build_root / "corpus" / cross_reference.batch_id / "graph.json"
        if not graph_path.is_file():
            raise CorpusCompletionExecutionError(
                "cross-reference compilation did not create the isolated corpus graph"
            )
        if self.manifest.quality_labelled:
            graph_path = self._materialize_quality_graph(
                batch_id=cross_reference.batch_id,
                quality_manifest=source_manifest,
            )
        state = (
            "complete"
            if not audit.excluded_source_ids and not cross_reference.uncovered_pairs
            else "partial"
        )
        token_usage = dict(audit.token_usage or _empty_token_usage())
        _add_token_usage(token_usage, cross_reference.token_usage or _empty_token_usage())
        return CorpusCompletionResult(
            run_id=self.manifest.run_id,
            root=self.root,
            state=state,
            launch_plan_path=self.launch_plan_path,
            launch_plan_sha256=audit.launch_plan_sha256,
            preflight_receipt_path=self.preflight_receipt_path,
            source_count=len(self.manifest.source_ids),
            semantic_jobs_started=(
                audit.semantic_jobs_started + cross_reference.semantic_jobs_started
            ),
            worker_events_emitted=(
                audit.worker_events_emitted + cross_reference.worker_events_emitted
            ),
            duration_seconds=audit.duration_seconds + cross_reference.duration_seconds,
            token_usage=token_usage,
            cross_reference_semantic_jobs_started=cross_reference.semantic_jobs_started,
            cross_reference_duration_seconds=cross_reference.duration_seconds,
            cross_reference_worker_events_emitted=cross_reference.worker_events_emitted,
            cross_reference_token_usage=cross_reference.token_usage,
            imported_source_ids=audit.imported_source_ids,
            reused_source_ids=audit.reused_source_ids,
            eligible_source_ids=audit.eligible_source_ids,
            excluded_source_ids=audit.excluded_source_ids,
            peak_audit_concurrency=audit.peak_audit_concurrency,
            eligibility_manifest_path=(
                None if self.manifest.quality_labelled else self.eligibility_manifest_path
            ),
            cross_reference_plan_path=self.cross_reference_plan_path,
            cross_reference_batch_id=cross_reference.batch_id,
            pair_count=int(plan["pair_count"]),
            uncovered_pairs=cross_reference.uncovered_pairs,
            peak_mapper_concurrency=cross_reference.peak_mapper_concurrency,
            corpus_graph_path=graph_path,
            selection_plan_path=(
                self.selection_plan_path if self.manifest.quality_labelled else None
            ),
            quality_manifest_path=(
                self.quality_manifest_path if self.manifest.quality_labelled else None
            ),
            quality_label_counts=(
                dict(source_manifest["quality_label_counts"])
                if self.manifest.quality_labelled
                else None
            ),
            selected_origin_counts=(
                audit.selected_origin_counts if self.manifest.quality_labelled else None
            ),
        )

    def cross_reference_eligibility(self) -> dict[str, Any]:
        """Load and verify the frozen eligibility gate for cross-reference."""

        if not self.eligibility_manifest_path.is_file():
            raise CorpusCompletionValidationError(
                "cross-reference requires a frozen eligibility manifest"
            )
        payload = _read_object(self.eligibility_manifest_path, label="eligibility manifest")
        _validate_payload(
            payload,
            schema_path=self.repository_root / ELIGIBILITY_SCHEMA,
            label="eligibility manifest",
        )
        if payload["run_id"] != self.manifest.run_id:
            raise CorpusCompletionValidationError(
                "eligibility manifest is bound to a different completion run"
            )
        if payload["launch_plan_sha256"] != fingerprint(self.launch_plan_path).sha256:
            raise CorpusCompletionValidationError(
                "eligibility manifest is bound to a different launch plan"
            )
        if tuple(payload["requested_source_ids"]) != self.manifest.source_ids:
            raise CorpusCompletionValidationError(
                "eligibility source order differs from the completion manifest"
            )
        included = {str(item["source_id"]) for item in payload["eligible_sources"]}
        excluded = {str(item["source_id"]) for item in payload["excluded_sources"]}
        if (
            included.intersection(excluded)
            or included.union(excluded) != set(self.manifest.source_ids)
            or payload["eligible_source_count"] != len(included)
            or payload["excluded_source_count"] != len(excluded)
        ):
            raise CorpusCompletionValidationError("eligibility membership is not exhaustive")
        repository = StateRepository(self.state_path)
        for item in payload["eligible_sources"]:
            source_id = str(item["source_id"])
            if repository.source_state(source_id) != "graph_inserted":
                raise CorpusCompletionValidationError(
                    f"eligible source is not graph_inserted: {source_id}"
                )
            self._verify_eligible_artifact(item, "dossier")
            self._verify_eligible_artifact(item, "graph")
        return payload

    def _materialize_quality_graph(
        self,
        *,
        batch_id: str,
        quality_manifest: Mapping[str, Any],
    ) -> Path:
        base_root = self.build_root / "corpus" / batch_id
        base_graph_path = base_root / "graph.json"
        base_manifest_path = base_root / "manifest.json"
        try:
            GraphExplorer(
                graph_path=base_graph_path,
                schema_directory=self.repository_root / "schemas/research-map/v1",
            )
        except GraphExplorationError as error:
            raise CorpusCompletionValidationError(
                f"base corpus graph is invalid: {error}"
            ) from error
        base_manifest = _read_object(base_manifest_path, label="base corpus graph manifest")
        output_root = self.build_root / "quality-corpus" / batch_id
        graph_path = output_root / "graph.json"
        manifest_path = output_root / "manifest.json"
        graph_bytes = base_graph_path.read_bytes()
        _write_or_verify_bytes(graph_path, graph_bytes)
        graph = fingerprint(graph_path, relative_to=self.repository_root)
        quality = fingerprint(self.quality_manifest_path, relative_to=self.repository_root)
        source_quality = quality_source_summaries(quality_manifest)
        payload = {
            **{
                key: value
                for key, value in base_manifest.items()
                if key not in {"schema_version", "corpus_graph", "inputs"}
            },
            "batch_id": batch_id,
            "sources": [str(item) for item in base_manifest["sources"]],
            "inputs": [
                *[dict(item) for item in base_manifest["inputs"]],
                {"kind": "corpus_source_quality_manifest", **quality.to_dict()},
            ],
            "corpus_graph": {"kind": "corpus_graph", **graph.to_dict()},
            "quality_manifest_sha256": quality.sha256,
            "quality_label_counts": dict(quality_manifest["quality_label_counts"]),
            "selected_origin_counts": dict(quality_manifest["selected_origin_counts"]),
            "source_quality": source_quality,
            "structurally_invalid_sources": [
                dict(item) for item in quality_manifest["structurally_invalid_sources"]
            ],
        }
        self._write_or_verify_receipt(manifest_path, payload)
        try:
            GraphExplorer(
                graph_path=graph_path,
                schema_directory=self.repository_root / "schemas/research-map/v1",
            )
        except GraphExplorationError as error:
            raise CorpusCompletionValidationError(
                f"quality-labelled corpus graph is invalid: {error}"
            ) from error
        return graph_path

    def _execute_audit_source(
        self,
        source_id: str,
        *,
        launch_plan_sha256: str,
        resume_started: bool = False,
    ) -> AuditSourceResult:
        result_path = self.audit_result_root / f"{source_id}.json"
        if result_path.exists():
            return self._load_audit_result(source_id, result_path)
        start_path = self.audit_start_root / f"{source_id}.json"
        if start_path.exists() and not resume_started:
            raise CorpusCompletionExecutionError(
                f"audit source has an ambiguous prior start: {source_id}"
            )
        if not start_path.exists():
            self._write_or_verify_receipt(
                start_path,
                {
                    "kind": "corpus_audit_source_start",
                    "run_id": self.manifest.run_id,
                    "launch_plan_sha256": launch_plan_sha256,
                    "source_id": source_id,
                    "coordinator_attempt": 1,
                    "model": self.manifest.audit_model,
                },
            )
        started = time.monotonic()
        before_jobs = self._source_job_ids(source_id)
        telemetry_baseline = (
            {
                str(job["job_id"])
                for job in StateRepository(self.state_path).jobs_for_source(source_id)
                if str(job["kind"]) == "corpus_snapshot_import"
            }
            if resume_started
            else before_jobs
        )
        try:
            result = self.audit_runner(source_id, self.manifest.audit_model)
        except _SOURCE_LOCAL_AUDIT_ERRORS as error:
            jobs, events, usage = self._source_telemetry(source_id, before_jobs=telemetry_baseline)
            result = AuditSourceResult(
                source_id=source_id,
                outcome="excluded",
                state=StateRepository(self.state_path).source_state(source_id) or "unknown",
                duration_seconds=time.monotonic() - started,
                semantic_jobs_started=jobs,
                worker_events_emitted=events,
                token_usage=usage,
                failure_class=type(error).__name__,
                failure_reason=str(error),
            )
        if resume_started:
            jobs, events, usage = self._source_telemetry(source_id, before_jobs=telemetry_baseline)
            result = AuditSourceResult(
                source_id=result.source_id,
                outcome=result.outcome,
                state=result.state,
                duration_seconds=self._source_worker_duration(
                    source_id, before_jobs=telemetry_baseline
                ),
                semantic_jobs_started=jobs,
                worker_events_emitted=events,
                token_usage=usage,
                warnings=result.warnings,
                failure_class=result.failure_class,
                failure_reason=result.failure_reason,
            )
        if result.source_id != source_id:
            raise CorpusCompletionExecutionError(
                f"audit result source differs from scheduled source: {source_id}"
            )
        self._validate_audit_source_result(result)
        self._write_or_verify_receipt(
            result_path,
            {
                "kind": "corpus_audit_source_result",
                "run_id": self.manifest.run_id,
                **result.to_dict(),
            },
        )
        return result

    def _prepare_started_audit_resume(
        self,
        source_id: str,
        *,
        launch_plan_sha256: str,
    ) -> Path:
        """Authorize one explicit continuation from terminal worker receipts."""

        start_path = self.audit_start_root / f"{source_id}.json"
        expected_start = {
            "schema_version": "1.0",
            "kind": "corpus_audit_source_start",
            "run_id": self.manifest.run_id,
            "launch_plan_sha256": launch_plan_sha256,
            "source_id": source_id,
            "coordinator_attempt": 1,
            "model": self.manifest.audit_model,
        }
        if not start_path.is_file() or read_receipt(start_path) != expected_start:
            raise CorpusCompletionExecutionError(
                f"started audit receipt identity differs: {source_id}"
            )

        repository = StateRepository(self.state_path)
        source_state = repository.source_state(source_id)
        if source_state not in {"source_complete", "audit_failed"}:
            raise CorpusCompletionExecutionError(
                f"started audit source is not resumable: {source_id} ({source_state})"
            )

        jobs = tuple(
            job
            for job in repository.jobs_for_source(source_id)
            if str(job["kind"]) != "corpus_snapshot_import"
        )
        if not jobs:
            raise CorpusCompletionExecutionError(
                f"started audit has no durable semantic jobs: {source_id}"
            )

        terminal_failure_seen = False
        basis: list[dict[str, Any]] = []
        cache_root = self.cache_root.resolve()
        for job in jobs:
            receipt_value = job.get("receipt_path")
            receipt_path: Path | None = None
            receipt_sha256: str | None = None
            terminal_event_type: str | None = None
            process_returncode: int | None = None
            receipt: dict[str, Any] = {}
            if isinstance(receipt_value, str) and receipt_value.strip():
                receipt_path = Path(receipt_value)
                if not receipt_path.is_absolute():
                    receipt_path = self.repository_root / receipt_path
                receipt_path = receipt_path.resolve()
                if not receipt_path.is_file():
                    raise CorpusCompletionExecutionError(
                        f"started audit job receipt is missing: {job['job_id']}"
                    )
                receipt = read_receipt(receipt_path)
                receipt_sha256 = fingerprint(receipt_path).sha256
                process = receipt.get("process")
                events = receipt.get("events")
                if isinstance(process, dict) and isinstance(events, dict):
                    raw_returncode = process.get("returncode")
                    raw_terminal = events.get("terminal_event_type")
                    if isinstance(raw_returncode, int) and not isinstance(raw_returncode, bool):
                        process_returncode = raw_returncode
                    if isinstance(raw_terminal, str):
                        terminal_event_type = raw_terminal

            if str(job["state"]) in {"pending", "running", "failed"} and (
                receipt_path is None
                or not receipt_path.is_relative_to(cache_root)
                or receipt.get("kind") != "codex_job"
                or receipt.get("job_id") != job["job_id"]
                or receipt.get("source_id") != source_id
                or process_returncode is None
                or not isinstance(receipt.get("events"), dict)
                or receipt["events"].get("terminal_event_seen") is not True
                or terminal_event_type not in {"turn.completed", "turn.failed"}
            ):
                raise CorpusCompletionExecutionError(
                    f"started audit may still have a live or ambiguous job: {job['job_id']}"
                )

            if str(job["state"]) == "failed":
                if process_returncode is None or process_returncode == 0:
                    raise CorpusCompletionExecutionError(
                        f"failed audit job lacks a terminal process failure: {job['job_id']}"
                    )
                if terminal_event_type != "turn.failed":
                    raise CorpusCompletionExecutionError(
                        f"failed audit job lacks a terminal failure event: {job['job_id']}"
                    )
                terminal_failure_seen = True

            basis.append(
                {
                    "job_id": str(job["job_id"]),
                    "run_id": str(job["run_id"]),
                    "kind": str(job["kind"]),
                    "attempt": int(job["attempt"]),
                    "state": str(job["state"]),
                    "receipt_sha256": receipt_sha256,
                    "process_returncode": process_returncode,
                    "terminal_event_type": terminal_event_type,
                }
            )

        if not terminal_failure_seen:
            raise CorpusCompletionExecutionError(
                f"started audit has no retained terminal worker failure: {source_id}"
            )

        basis_sha256 = _digest(basis)
        resume_path = self.audit_resume_root / source_id / f"{basis_sha256}.json"
        if resume_path.exists():
            raise CorpusCompletionExecutionError(
                f"started audit resume basis is already consumed: {source_id}"
            )
        self._write_or_verify_receipt(
            resume_path,
            {
                "kind": "corpus_audit_source_resume",
                "run_id": self.manifest.run_id,
                "launch_plan_sha256": launch_plan_sha256,
                "source_id": source_id,
                "model": self.manifest.audit_model,
                "source_state": source_state,
                "audit_start_sha256": fingerprint(start_path).sha256,
                "basis": basis,
                "basis_sha256": basis_sha256,
                "explicit_resume": True,
                "semantic_jobs_started": 0,
            },
        )
        return resume_path

    def _run_audit_source(self, source_id: str, model: str) -> AuditSourceResult:
        before_jobs = self._source_job_ids(source_id)
        started = time.monotonic()
        audit = AuditCoordinator(
            repository_root=self.repository_root,
            state_path=self.state_path,
            cache_root=self.cache_root,
        ).run_through_audit_passed(source_id, model=model)
        compilation = CompilationCoordinator(
            repository_root=self.repository_root,
            state_path=self.state_path,
            cache_root=self.cache_root,
            vault_root=self.vault_root,
            build_root=self.build_root,
        ).run_through_graph_inserted(source_id, model=model)
        jobs, events, usage = self._source_telemetry(source_id, before_jobs=before_jobs)
        return AuditSourceResult(
            source_id=source_id,
            outcome="eligible",
            state=compilation.state,
            duration_seconds=time.monotonic() - started,
            semantic_jobs_started=jobs,
            worker_events_emitted=events,
            token_usage=usage,
            warnings=tuple((*audit.warnings, *compilation.warnings)),
        )

    def _validate_audit_source_result(self, result: AuditSourceResult) -> None:
        if result.outcome not in {"eligible", "excluded"}:
            raise CorpusCompletionExecutionError(
                f"invalid audit outcome for {result.source_id}: {result.outcome}"
            )
        if (
            result.duration_seconds < 0
            or result.semantic_jobs_started < 0
            or result.worker_events_emitted < 0
            or set(result.token_usage or {}) != set(_empty_token_usage())
            or any(value < 0 for value in (result.token_usage or {}).values())
        ):
            raise CorpusCompletionExecutionError(f"invalid audit metrics for {result.source_id}")
        observed_state = StateRepository(self.state_path).source_state(result.source_id)
        if observed_state != result.state:
            raise CorpusCompletionExecutionError(
                f"audit result state differs from destination state: {result.source_id}"
            )
        if result.outcome == "eligible":
            if result.state != "graph_inserted":
                raise CorpusCompletionExecutionError(
                    f"eligible source is not graph_inserted: {result.source_id}"
                )
            if result.failure_class is not None or result.failure_reason is not None:
                raise CorpusCompletionExecutionError(
                    f"eligible source has failure metadata: {result.source_id}"
                )
        elif not result.failure_class or not result.failure_reason:
            raise CorpusCompletionExecutionError(
                f"excluded source lacks failure metadata: {result.source_id}"
            )

    def _audit_results(self) -> dict[str, AuditSourceResult]:
        if not self.audit_result_root.is_dir():
            return {}
        results: dict[str, AuditSourceResult] = {}
        for source_id in self.manifest.source_ids:
            path = self.audit_result_root / f"{source_id}.json"
            if path.is_file():
                results[source_id] = self._load_audit_result(source_id, path)
        unexpected = sorted(
            path.stem
            for path in self.audit_result_root.glob("*.json")
            if path.stem not in self.manifest.source_ids
        )
        if unexpected:
            raise CorpusCompletionExecutionError(
                "unexpected audit result sources: " + ", ".join(unexpected)
            )
        return results

    def _load_audit_result(self, source_id: str, path: Path) -> AuditSourceResult:
        start_path = self.audit_start_root / f"{source_id}.json"
        if not start_path.is_file():
            raise CorpusCompletionExecutionError(
                f"audit result lacks its start receipt: {source_id}"
            )
        start = read_receipt(start_path)
        if (
            start.get("kind") != "corpus_audit_source_start"
            or start.get("run_id") != self.manifest.run_id
            or start.get("launch_plan_sha256") != fingerprint(self.launch_plan_path).sha256
            or start.get("source_id") != source_id
            or start.get("coordinator_attempt") != 1
            or start.get("model") != self.manifest.audit_model
        ):
            raise CorpusCompletionExecutionError(f"audit start identity differs: {source_id}")
        value = _read_object(path, label=f"audit result for {source_id}")
        if (
            value.get("schema_version") != "1.0"
            or value.get("kind") != "corpus_audit_source_result"
            or value.get("run_id") != self.manifest.run_id
            or value.get("source_id") != source_id
        ):
            raise CorpusCompletionExecutionError(f"audit result identity differs: {source_id}")
        usage = value.get("token_usage")
        if not isinstance(usage, dict):
            raise CorpusCompletionExecutionError(
                f"audit result token usage is invalid: {source_id}"
            )
        result = AuditSourceResult(
            source_id=source_id,
            outcome=str(value.get("outcome")),
            state=str(value.get("state")),
            duration_seconds=float(value.get("duration_seconds", -1)),
            semantic_jobs_started=int(value.get("semantic_jobs_started", -1)),
            worker_events_emitted=int(value.get("worker_events_emitted", -1)),
            token_usage={key: int(usage.get(key, -1)) for key in _empty_token_usage()},
            warnings=tuple(str(item) for item in value.get("warnings", ())),
            failure_class=(
                None if value.get("failure_class") is None else str(value["failure_class"])
            ),
            failure_reason=(
                None if value.get("failure_reason") is None else str(value["failure_reason"])
            ),
        )
        if (
            result.duration_seconds < 0
            or result.semantic_jobs_started < 0
            or result.worker_events_emitted < 0
            or any(value < 0 for value in (result.token_usage or {}).values())
        ):
            raise CorpusCompletionExecutionError(f"audit result metrics are invalid: {source_id}")
        self._validate_audit_source_result(result)
        return result

    def _freeze_eligibility(
        self,
        results: Mapping[str, AuditSourceResult],
        *,
        launch_plan_sha256: str,
        peak_audit_concurrency: int,
    ) -> dict[str, Any]:
        existing = (
            _read_object(self.eligibility_manifest_path, label="eligibility manifest")
            if self.eligibility_manifest_path.is_file()
            else None
        )
        effective_peak = (
            int(existing["peak_audit_concurrency"])
            if existing is not None
            else peak_audit_concurrency
        )
        eligible: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        for source_id in self.manifest.source_ids:
            result = results[source_id]
            if result.outcome == "eligible":
                eligible.append(self._eligible_source_entry(result))
            else:
                excluded.append(
                    {
                        "source_id": source_id,
                        "terminal_audit_state": result.state,
                        "failure_class": result.failure_class,
                        "failure_reason": result.failure_reason,
                        "duration_seconds": result.duration_seconds,
                        "token_usage": result.token_usage or _empty_token_usage(),
                        "warnings": list(result.warnings),
                    }
                )
        payload = {
            "schema_version": "1.0",
            "kind": "corpus_eligibility_manifest",
            "run_id": self.manifest.run_id,
            "launch_plan_sha256": launch_plan_sha256,
            "requested_source_ids": list(self.manifest.source_ids),
            "eligible_sources": eligible,
            "excluded_sources": excluded,
            "eligible_source_count": len(eligible),
            "excluded_source_count": len(excluded),
            "semantic_jobs_started": sum(
                result.semantic_jobs_started for result in results.values()
            ),
            "peak_audit_concurrency": effective_peak,
        }
        self._write_or_verify_receipt(
            self.eligibility_manifest_path,
            {key: value for key, value in payload.items() if key != "schema_version"},
            schema_path=self.repository_root / ELIGIBILITY_SCHEMA,
        )
        return payload

    def _eligible_source_entry(self, result: AuditSourceResult) -> dict[str, Any]:
        source_id = result.source_id
        dossier_path = self.vault_root / source_id / "paper.md"
        graph_path = self.build_root / source_id / "graph.json"
        manifest_path = self.build_root / source_id / "manifest.json"
        graph_manifest = _read_object(manifest_path, label=f"graph manifest for {source_id}")
        dossier = fingerprint(dossier_path, relative_to=self.repository_root)
        graph = fingerprint(graph_path, relative_to=self.repository_root)
        return {
            "source_id": source_id,
            "terminal_audit_state": result.state,
            "dossier_path": dossier.path,
            "dossier_sha256": dossier.sha256,
            "dossier_bytes": dossier.size_bytes,
            "graph_path": graph.path,
            "graph_sha256": graph.sha256,
            "record_count": int(graph_manifest["record_count"]),
            "edge_count": int(graph_manifest["edge_count"]),
            "duration_seconds": result.duration_seconds,
            "token_usage": result.token_usage or _empty_token_usage(),
            "warnings": list(result.warnings),
        }

    def _verify_eligible_artifact(self, item: Mapping[str, Any], artifact_kind: str) -> None:
        path = (self.repository_root / str(item[f"{artifact_kind}_path"])).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise CorpusCompletionValidationError(
                f"eligible {artifact_kind} escapes or is missing: {item['source_id']}"
            )
        observed = fingerprint(path)
        if observed.sha256 != item[f"{artifact_kind}_sha256"] or (
            artifact_kind == "dossier" and observed.size_bytes != item["dossier_bytes"]
        ):
            raise CorpusCompletionValidationError(
                f"eligible {artifact_kind} fingerprint changed: {item['source_id']}"
            )

    def _source_job_ids(self, source_id: str) -> set[str]:
        if not self.state_path.is_file():
            return set()
        return {
            str(job["job_id"])
            for job in StateRepository(self.state_path).jobs_for_source(source_id)
        }

    def _source_telemetry(
        self, source_id: str, *, before_jobs: set[str]
    ) -> tuple[int, int, dict[str, int]]:
        repository = StateRepository(self.state_path)
        jobs = tuple(
            job
            for job in repository.jobs_for_source(source_id)
            if str(job["job_id"]) not in before_jobs
            and str(job["kind"]) not in {"graph_compile", "corpus_snapshot_import"}
        )
        event_count = 0
        usage = _empty_token_usage()
        for job in jobs:
            events_path = (
                self.cache_root / "runs" / str(job["run_id"]) / "events" / f"{job['job_id']}.jsonl"
            )
            if not events_path.is_file():
                continue
            for line in events_path.read_text(encoding="utf-8").splitlines():
                event_count += 1
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                raw_usage = event.get("usage")
                if isinstance(raw_usage, dict):
                    for key in usage:
                        value = raw_usage.get(key, 0)
                        if isinstance(value, int) and value >= 0:
                            usage[key] += value
        return len(jobs), event_count, usage

    def _source_worker_duration(self, source_id: str, *, before_jobs: set[str]) -> float:
        duration = 0.0
        for job in StateRepository(self.state_path).jobs_for_source(source_id):
            if str(job["job_id"]) in before_jobs or str(job["kind"]) in {
                "graph_compile",
                "corpus_snapshot_import",
            }:
                continue
            receipt_value = job.get("receipt_path")
            if not isinstance(receipt_value, str) or not receipt_value.strip():
                continue
            receipt_path = Path(receipt_value)
            if not receipt_path.is_absolute():
                receipt_path = self.repository_root / receipt_path
            if not receipt_path.is_file():
                continue
            process = read_receipt(receipt_path).get("process")
            raw_duration = process.get("duration_seconds") if isinstance(process, dict) else None
            if raw_duration is None:
                continue
            if (
                not isinstance(raw_duration, (int, float))
                or isinstance(raw_duration, bool)
                or raw_duration < 0
            ):
                raise CorpusCompletionExecutionError(
                    f"audit worker duration is invalid: {job['job_id']}"
                )
            duration += float(raw_duration)
        return duration

    def _cross_reference_coverage(self) -> dict[str, Any] | None:
        paths = sorted(self.cache_root.glob("cross-reference/*/pair-coverage.json"))
        if not paths:
            return None
        if len(paths) != 1:
            raise CorpusCompletionExecutionError(
                "completion run contains multiple cross-reference coverage receipts"
            )
        path = paths[0]
        try:
            value = read_receipt(path)
        except (OSError, ValueError) as error:
            raise CorpusCompletionValidationError(
                f"cross-reference coverage receipt is invalid: {error}"
            ) from error
        batch_id = path.parent.name
        if not self.cross_reference_plan_path.is_file():
            raise CorpusCompletionValidationError(
                "cross-reference coverage lacks its immutable plan"
            )
        plan = _read_object(self.cross_reference_plan_path, label="cross-reference plan")
        _validate_payload(
            plan,
            schema_path=self.repository_root / CROSS_REFERENCE_PLAN_SCHEMA,
            label="corpus cross-reference plan",
        )
        if self.manifest.quality_labelled:
            eligibility = self.cross_reference_quality()
            source_manifest_sha256 = fingerprint(self.quality_manifest_path).sha256
            eligible_source_ids = tuple(
                sorted(str(item["source_id"]) for item in eligibility["connectable_sources"])
            )
            source_binding_valid = (
                plan.get("source_manifest_kind") == "corpus_source_quality_manifest"
                and plan.get("quality_manifest_sha256") == source_manifest_sha256
                and plan.get("source_inputs")
                == [
                    {
                        "source_id": str(item["source_id"]),
                        "quality_label": str(item["quality_label"]),
                        "selection_receipt_sha256": str(item["selection_receipt_sha256"]),
                        "dossier_sha256": str(item["dossier_sha256"]),
                        "graph_sha256": str(item["graph_sha256"]),
                        "findings_sha256": str(item["findings_sha256"]),
                    }
                    for item in sorted(
                        eligibility["connectable_sources"],
                        key=lambda value: str(value["source_id"]),
                    )
                ]
            )
            strategy_prefix = "corpus-quality-block-pair-multi-surface-v2"
        else:
            eligibility = self.cross_reference_eligibility()
            source_manifest_sha256 = fingerprint(self.eligibility_manifest_path).sha256
            eligible_source_ids = tuple(
                sorted(str(item["source_id"]) for item in eligibility["eligible_sources"])
            )
            source_binding_valid = plan.get("eligibility_manifest_sha256") == source_manifest_sha256
            strategy_prefix = "corpus-block-pair-multi-surface-v1"
        expected_limits = {
            "max_sources_per_block": self.manifest.max_sources_per_block,
            "max_dossier_bytes_per_block": self.manifest.max_dossier_bytes_per_block,
            "max_mapper_input_bytes": self.manifest.max_mapper_input_bytes,
            "mapper_workers": self.manifest.mapper_workers,
            "semantic_attempts_per_job": self.manifest.semantic_attempts_per_job,
            "automatic_retries": self.manifest.automatic_retries,
        }
        if (
            plan["run_id"] != self.manifest.run_id
            or plan["run_id"] != eligibility["run_id"]
            or not source_binding_valid
            or tuple(str(item) for item in plan["source_ids"]) != eligible_source_ids
            or plan["source_count"] != len(eligible_source_ids)
            or plan["limits"] != expected_limits
        ):
            raise CorpusCompletionValidationError(
                "cross-reference plan "
                + (
                    "quality-manifest binding is invalid"
                    if self.manifest.quality_labelled
                    else "eligibility binding is invalid"
                )
            )
        plan_sha256 = fingerprint(self.cross_reference_plan_path).sha256
        raw_sources = value.get("source_ids")
        raw_successful = value.get("successful_pairs")
        raw_uncovered = value.get("uncovered_pairs")
        raw_shards = value.get("shards")
        semantic_jobs = value.get("semantic_jobs_started")
        peak = value.get("peak_mapper_concurrency")
        if (
            value.get("kind") != "owned_pair_cross_reference_coverage"
            or value.get("batch_id") != batch_id
            or not isinstance(value.get("strategy"), str)
            or not str(value["strategy"]).startswith(f"{strategy_prefix}:{plan_sha256}:")
            or not isinstance(raw_sources, list)
            or tuple(str(item) for item in raw_sources)
            != tuple(str(item) for item in plan["source_ids"])
            or not isinstance(raw_successful, list)
            or not isinstance(raw_uncovered, list)
            or not isinstance(raw_shards, list)
            or not all(isinstance(item, dict) for item in raw_shards)
            or not isinstance(value.get("pair_count"), int)
            or not isinstance(semantic_jobs, int)
            or isinstance(semantic_jobs, bool)
            or semantic_jobs < 0
            or not isinstance(peak, int)
            or isinstance(peak, bool)
            or peak < 0
            or peak > self.manifest.mapper_workers
        ):
            raise CorpusCompletionValidationError(
                "cross-reference coverage receipt identity is invalid"
            )
        sources = tuple(str(item) for item in raw_sources)
        expected_pairs = set(itertools.combinations(sources, 2))
        successful_pairs = _coverage_pairs(raw_successful, label="successful pair")
        uncovered_pairs = _coverage_pairs(raw_uncovered, label="uncovered pair")
        if (
            value["pair_count"] != plan["pair_count"]
            or value["pair_count"] != len(expected_pairs)
            or successful_pairs.intersection(uncovered_pairs)
            or successful_pairs.union(uncovered_pairs) != expected_pairs
        ):
            raise CorpusCompletionValidationError("cross-reference coverage pair ledger is invalid")
        expected_shards = [
            shard
            for shard in plan["shards"]
            if not shard["no_op"] and shard["planning_failure"] is None
        ]
        if len(raw_shards) != len(expected_shards) or semantic_jobs > len(expected_shards):
            raise CorpusCompletionValidationError(
                "cross-reference coverage shard ledger is invalid"
            )
        derived_successful: set[tuple[str, str]] = set()
        derived_uncovered = {_plan_pair(pair) for pair in plan["uncovered_pairs"]}
        repository = StateRepository(self.state_path)
        batch = repository.cross_reference_batch_record(batch_id)
        if (
            batch is None
            or batch["state"] != "compiled"
            or batch["requested_through"] != "compiled"
            or batch["model"] != self.manifest.cross_reference_model
            or batch["strategy"] != value["strategy"]
            or tuple(batch["source_ids"]) != eligible_source_ids
            or set(batch["input_fingerprints"]) != set(eligible_source_ids)
        ):
            raise CorpusCompletionValidationError(
                "cross-reference terminal batch identity is invalid"
            )
        observed_job_ids: set[str] = set()
        for observed, expected in zip(raw_shards, expected_shards, strict=True):
            observed_pairs = _coverage_pairs(observed.get("owned_pairs"), label="shard owned pair")
            expected_owned = {_plan_pair(pair) for pair in expected["owned_pairs"]}
            status = observed.get("status")
            warnings = observed.get("warnings")
            expected_job_id = holistic_cross_reference_job_id(
                batch_id, tuple(str(item) for item in expected["source_ids"]), 1
            )
            try:
                expected_telemetry = _cross_reference_job_telemetry(
                    state=repository,
                    cache_root=self.cache_root,
                    batch_id=batch_id,
                    job_id=expected_job_id,
                )
            except CrossReferenceValidationError as error:
                raise CorpusCompletionValidationError(str(error)) from error
            if (
                observed.get("shard_id") != expected["shard_id"]
                or observed.get("job_id") != expected_job_id
                or observed.get("source_ids") != expected["source_ids"]
                or observed_pairs != expected_owned
                or any(observed.get(key) != value for key, value in expected_telemetry.items())
                or status not in {"succeeded", "failed"}
                or not isinstance(warnings, list)
                or not all(isinstance(item, str) for item in warnings)
                or (
                    status == "succeeded"
                    and (
                        observed.get("failure_class") is not None
                        or observed.get("failure_reason") is not None
                    )
                )
                or (
                    status == "failed"
                    and (
                        not isinstance(observed.get("failure_class"), str)
                        or not observed["failure_class"]
                        or not isinstance(observed.get("failure_reason"), str)
                        or not observed["failure_reason"]
                    )
                )
            ):
                raise CorpusCompletionValidationError(
                    "cross-reference coverage shard ledger is invalid"
                )
            if expected_telemetry["semantic_job_started"]:
                observed_job_ids.add(expected_job_id)
                job = repository.cross_reference_job_record(expected_job_id)
                if (
                    job is None
                    or (status == "succeeded" and job["state"] != "succeeded")
                    or (status == "failed" and job["state"] not in {"failed", "validation_failed"})
                ):
                    raise CorpusCompletionValidationError(
                        "cross-reference shard job outcome is invalid"
                    )
            elif status != "failed":
                raise CorpusCompletionValidationError(
                    "successful cross-reference shard lacks a semantic job"
                )
            (derived_successful if status == "succeeded" else derived_uncovered).update(
                observed_pairs
            )
        durable_job_ids = {
            str(job["job_id"]) for job in repository.cross_reference_jobs_for_batch(batch_id)
        }
        try:
            aggregate_telemetry = _aggregate_shard_telemetry(raw_shards)
        except CrossReferenceValidationError as error:
            raise CorpusCompletionValidationError(str(error)) from error
        if (
            durable_job_ids != observed_job_ids
            or semantic_jobs != len(observed_job_ids)
            or value.get("duration_seconds") != aggregate_telemetry["duration_seconds"]
            or value.get("worker_events_emitted") != aggregate_telemetry["worker_events_emitted"]
            or value.get("token_usage") != aggregate_telemetry["token_usage"]
        ):
            raise CorpusCompletionValidationError("cross-reference coverage telemetry is invalid")
        if derived_successful != successful_pairs or derived_uncovered != uncovered_pairs:
            raise CorpusCompletionValidationError(
                "cross-reference coverage outcomes differ from shard receipts"
            )
        return {**value, "_path": str(path)}

    def _verify_source_snapshot(self, source_id: str) -> SourceSnapshot:
        summary_path = self.manifest.source_run_root / "summary.json"
        if fingerprint(summary_path).sha256 != self.manifest.source_run_terminal_summary_sha256:
            raise CorpusCompletionValidationError("source-run terminal summary fingerprint changed")
        summary = read_receipt(summary_path)
        if (
            summary.get("kind") != "provisional_corpus_run"
            or summary.get("run_id") != self.manifest.source_run_id
            or summary.get("status") != "source_maps_complete"
            or tuple(summary.get("terminal_source_ids", ())) != self.manifest.source_ids
            or summary.get("remaining_source_ids") != []
        ):
            raise CorpusCompletionValidationError("source-run terminal summary is not complete")

        source_repository = StateRepository(self.manifest.source_run_root / "state.sqlite3")
        if not source_repository.path.is_file() or source_repository.schema_versions() != (1, 2, 3):
            raise CorpusCompletionValidationError(
                "source-run state store is missing or incompatible"
            )
        source_record = source_repository.source_record(source_id)
        if source_record is None or source_record["state"] != "calibration_graph_inserted":
            raise CorpusCompletionValidationError(
                f"source-run state is not terminal provisional: {source_id}"
            )
        source = resolve_source(source_id, repository_root=self.repository_root)
        for asset in source.assets:
            verify_asset(asset)
        expected_metadata = _registration_metadata(source)
        expected_assets = tuple(asset.to_record() for asset in source.assets)
        if source_record["metadata"] != expected_metadata:
            raise CorpusCompletionValidationError(f"source registration changed: {source_id}")
        if source_repository.assets_for(source_id) != expected_assets:
            raise CorpusCompletionValidationError(f"source assets changed: {source_id}")
        validate_source_wip(
            source_repository,
            source,
            schema_directory=self.repository_root / "schemas" / "research-map" / "v1",
        )
        records = source_repository.latest_wip_records(source_id)
        coverage = source_repository.coverage_for(source_id)
        if not records or not coverage:
            raise CorpusCompletionValidationError(f"source snapshot is incomplete: {source_id}")

        source_result_path = self.manifest.source_run_root / "source-results" / f"{source_id}.json"
        source_result = read_receipt(source_result_path)
        if (
            source_result.get("run_id") != self.manifest.source_run_id
            or source_result.get("source_id") != source_id
            or source_result.get("outcome") != "provisional_map"
            or source_result.get("state") != "calibration_graph_inserted"
            or source_result.get("retry_allowed") is not False
            or source_result.get("record_count") != len(records)
        ):
            raise CorpusCompletionValidationError(f"source result identity changed: {source_id}")

        projection_manifest_path = (
            self.manifest.source_run_root / "build" / source_id / "manifest.json"
        )
        projection_manifest = _read_object(
            projection_manifest_path, label=f"provisional projection manifest for {source_id}"
        )
        if (
            projection_manifest.get("source_id") != source_id
            or projection_manifest.get("projection_status") != "provisional_calibration"
            or projection_manifest.get("canonical_promotion") is not False
            or projection_manifest.get("record_count") != len(records)
            or projection_manifest.get("edge_count") != source_result.get("edge_count")
        ):
            raise CorpusCompletionValidationError(
                f"provisional projection identity changed: {source_id}"
            )
        projection_artifacts = _verify_projection_artifacts(
            projection_manifest,
            repository_root=self.repository_root,
            source_run_root=self.manifest.source_run_root,
        )
        job_receipts = _source_job_receipts(
            source_repository,
            source_id=source_id,
            source_run_root=self.manifest.source_run_root,
            repository_root=self.repository_root,
        )
        transitions = source_repository.transitions_for("source", source_id)
        applied = [row for row in transitions if int(row["applied"]) == 1]
        if not applied or str(applied[-1]["to_state"]) != "calibration_graph_inserted":
            raise CorpusCompletionValidationError(
                f"source terminal receipt is missing: {source_id}"
            )
        terminal_receipt_sha256 = hashlib.sha256(
            str(applied[-1]["receipt_json"]).encode("utf-8")
        ).hexdigest()
        receipt = {
            "schema_version": "1.0",
            "kind": "corpus_snapshot_transfer",
            "completion_run_id": self.manifest.run_id,
            "source_run_id": self.manifest.source_run_id,
            "source_id": source_id,
            "source_state": "calibration_graph_inserted",
            "destination_state": "source_complete",
            "source_run_summary_sha256": self.manifest.source_run_terminal_summary_sha256,
            "source_result_sha256": fingerprint(source_result_path).sha256,
            "source_registration_sha256": _digest(source.to_record()),
            "asset_fingerprints": [
                {
                    "path": asset.relative_path,
                    "sha256": asset.sha256,
                    "size_bytes": asset.size_bytes,
                }
                for asset in source.assets
            ],
            "wip_sha256": _digest(records),
            "coverage_sha256": _digest(_origin_coverage(coverage)),
            "source_terminal_transition_sha256": terminal_receipt_sha256,
            "job_receipts": list(job_receipts),
            "projection_manifest_sha256": fingerprint(projection_manifest_path).sha256,
            "projection_artifacts": list(projection_artifacts),
            "semantic_jobs_started": 0,
        }
        _validate_payload(
            receipt,
            schema_path=self.repository_root / TRANSFER_SCHEMA,
            label="corpus transfer receipt",
        )
        return SourceSnapshot(
            source_id=source_id,
            metadata=expected_metadata,
            assets=expected_assets,
            records=records,
            coverage=_origin_coverage(coverage),
            transfer_receipt=receipt,
        )

    def _verify_import(
        self,
        repository: StateRepository,
        *,
        snapshot: SourceSnapshot,
        outcome: SnapshotImportOutcome,
        terminal_result: AuditSourceResult | None,
        audit_started: bool,
    ) -> None:
        terminal_excluded = terminal_result is not None and terminal_result.outcome == "excluded"
        if (
            (
                outcome.state
                not in {
                    "source_complete",
                    "audit_failed",
                    "audit_passed",
                    "graph_inserted",
                }
                and not terminal_excluded
            )
            or repository.source_state(snapshot.source_id) != outcome.state
            or (terminal_result is not None and terminal_result.state != outcome.state)
        ):
            raise CorpusCompletionExecutionError(
                f"destination snapshot is not reusable: {snapshot.source_id}"
            )
        if not terminal_excluded:
            source = resolve_source(snapshot.source_id, repository_root=self.repository_root)
            validate_source_wip(
                repository,
                source,
                schema_directory=self.repository_root / "schemas" / "research-map" / "v1",
            )
        if (
            outcome.state == "source_complete"
            and terminal_result is None
            and not audit_started
            and repository.latest_wip_records(snapshot.source_id) != snapshot.records
        ):
            raise CorpusCompletionExecutionError(
                f"destination WIP differs from origin: {snapshot.source_id}"
            )

    def _verify_destination_snapshot(
        self,
        repository: StateRepository,
        *,
        snapshot: SourceSnapshot,
        run_identifier: str,
        job_identifier: str,
        receipt_path: Path,
    ) -> None:
        source = repository.source_record(snapshot.source_id)
        if source is None or source["metadata"] != snapshot.metadata:
            raise CorpusCompletionExecutionError(
                f"destination source identity differs from origin: {snapshot.source_id}"
            )
        if repository.assets_for(snapshot.source_id) != snapshot.assets:
            raise CorpusCompletionExecutionError(
                f"destination source assets differ from origin: {snapshot.source_id}"
            )
        retained = {
            (str(record["id"]), int(record["revision"])): record
            for record in repository.wip_record_revisions(snapshot.source_id)
        }
        if any(
            retained.get((str(record["id"]), int(record["revision"]))) != record
            for record in snapshot.records
        ):
            raise CorpusCompletionExecutionError(
                f"destination origin WIP revisions differ: {snapshot.source_id}"
            )
        source_complete_transitions = [
            row
            for row in repository.transitions_for("source", snapshot.source_id)
            if int(row["applied"]) == 1 and row["to_state"] == "source_complete"
        ]
        expected_import_receipt = {
            "kind": "corpus_snapshot_imported",
            "origin": snapshot.transfer_receipt,
            "semantic_jobs_started": 0,
        }
        import_receipts: list[dict[str, Any]] = []
        for transition in source_complete_transitions:
            try:
                receipt = json.loads(str(transition["receipt_json"]))
            except json.JSONDecodeError as error:
                raise CorpusCompletionExecutionError(
                    f"destination source-complete receipt is invalid: {snapshot.source_id}"
                ) from error
            if isinstance(receipt, dict) and receipt.get("kind") == "corpus_snapshot_imported":
                import_receipts.append(receipt)
        if len(import_receipts) != 1:
            raise CorpusCompletionExecutionError(
                f"destination import transition differs: {snapshot.source_id}"
            )
        if import_receipts[0] != expected_import_receipt:
            raise CorpusCompletionExecutionError(
                f"destination import receipt differs: {snapshot.source_id}"
            )
        run = repository.run_record(run_identifier)
        job = repository.job_record(job_identifier)
        if (
            run is None
            or run["source_id"] != snapshot.source_id
            or run["requested_through"] != "source_complete_import"
            or job is None
            or job["source_id"] != snapshot.source_id
            or job["kind"] != "corpus_snapshot_import"
            or job["state"] != "succeeded"
            or job["receipt_path"] != str(receipt_path)
        ):
            raise CorpusCompletionExecutionError(
                f"destination transfer provenance differs: {snapshot.source_id}"
            )

    def _write_or_verify_receipt(
        self,
        path: Path,
        payload: Mapping[str, Any],
        *,
        schema_path: Path | None = None,
    ) -> str:
        expected = {"schema_version": "1.0", **dict(payload)}
        if path.exists():
            if read_receipt(path) != expected:
                raise CorpusCompletionExecutionError(
                    f"immutable completion artifact differs on replay: {path}"
                )
        else:
            write_receipt(path, payload)
        if schema_path is not None:
            _validate_payload(expected, schema_path=schema_path, label=path.name)
        return fingerprint(path).sha256


def _registration_metadata(source: RegisteredSource) -> dict[str, Any]:
    record = source.to_record()
    return {
        "schema_version": source.schema_version,
        "source_yaml_path": record["source_yaml_path"],
        "source_yaml_sha256": source.source_yaml_sha256,
        "metadata": dict(source.metadata),
    }


def _origin_coverage(values: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "scope_id": str(value["scope_id"]),
            "scope_kind": str(value["scope_kind"]),
            "disposition": str(value["disposition"]),
            "details": dict(value["details"]),
        }
        for value in values
    )


def _source_job_receipts(
    repository: StateRepository,
    *,
    source_id: str,
    source_run_root: Path,
    repository_root: Path,
) -> tuple[dict[str, Any], ...]:
    values: list[dict[str, Any]] = []
    for job in repository.jobs_for_source(source_id):
        receipt_value = job.get("receipt_path")
        if not isinstance(receipt_value, str) or not receipt_value:
            raise CorpusCompletionValidationError(f"source job receipt is missing: {job['job_id']}")
        receipt_path = Path(receipt_value)
        if not receipt_path.is_absolute():
            receipt_path = repository_root / receipt_path
        resolved = receipt_path.resolve()
        if not resolved.is_relative_to(source_run_root) or not resolved.is_file():
            raise CorpusCompletionValidationError(
                f"source job receipt escapes or is missing: {job['job_id']}"
            )
        item = fingerprint(resolved, relative_to=repository_root)
        values.append({"kind": str(job["kind"]), **item.to_dict()})
    return tuple(sorted(values, key=lambda item: (str(item["kind"]), str(item["path"]))))


def _verify_projection_artifacts(
    manifest: Mapping[str, Any],
    *,
    repository_root: Path,
    source_run_root: Path,
) -> tuple[dict[str, Any], ...]:
    raw_artifacts = manifest.get("artifacts")
    if not isinstance(raw_artifacts, list) or len(raw_artifacts) != 3:
        raise CorpusCompletionValidationError("provisional projection artifact set is incomplete")
    observed: list[dict[str, Any]] = []
    for raw in raw_artifacts:
        if not isinstance(raw, dict):
            raise CorpusCompletionValidationError("provisional projection artifact is invalid")
        path_value = raw.get("path")
        if not isinstance(path_value, str):
            raise CorpusCompletionValidationError("provisional projection path is invalid")
        path = Path(path_value)
        resolved = (path if path.is_absolute() else repository_root / path).resolve()
        if (
            not resolved.is_relative_to(source_run_root)
            or not resolved.is_file()
            or resolved.is_symlink()
        ):
            raise CorpusCompletionValidationError(
                f"provisional projection artifact escapes or is missing: {path_value}"
            )
        item = fingerprint(resolved, relative_to=repository_root)
        if item.sha256 != raw.get("sha256") or item.size_bytes != raw.get("size_bytes"):
            raise CorpusCompletionValidationError(
                f"provisional projection artifact fingerprint changed: {path_value}"
            )
        observed.append({"kind": str(raw.get("kind", "")), **item.to_dict()})
    return tuple(observed)


def _retained_source_job_receipts(
    repository: StateRepository,
    *,
    source_id: str,
    run_root: Path,
    repository_root: Path,
) -> tuple[dict[str, Any], ...]:
    values: list[dict[str, Any]] = []
    nonsemantic_kinds = {
        "corpus_snapshot_import",
        "graph_compile",
        "calibration_graph_compile",
    }
    for job in repository.jobs_for_source(source_id):
        receipt_value = job.get("receipt_path")
        if not isinstance(receipt_value, str) or not receipt_value.strip():
            evidence = _retained_orientation_transition_evidence(
                repository,
                job=job,
                source_id=source_id,
            )
            values.append(
                {
                    "job_id": str(job["job_id"]),
                    "run_id": str(job["run_id"]),
                    "kind": str(job["kind"]),
                    "state": str(job["state"]),
                    "receipt": None,
                    "durable_transition_evidence": evidence,
                    "semantic_job": False,
                    "duration_seconds": 0.0,
                    "worker_events_emitted": 0,
                    "token_usage": _empty_token_usage(),
                }
            )
            continue
        receipt_path = Path(receipt_value)
        if not receipt_path.is_absolute():
            receipt_path = repository_root / receipt_path
        receipt_path = receipt_path.resolve()
        if not receipt_path.is_relative_to(run_root) or not receipt_path.is_file():
            raise CorpusCompletionValidationError(
                f"prior completion job receipt escapes or is missing: {job['job_id']}"
            )
        receipt = read_receipt(receipt_path)
        if receipt.get("source_id") not in {None, source_id}:
            raise CorpusCompletionValidationError(
                f"prior completion job receipt has a foreign source: {job['job_id']}"
            )
        semantic = str(job["kind"]) not in nonsemantic_kinds
        duration = 0.0
        event_count = 0
        usage = _empty_token_usage()
        process = receipt.get("process")
        events = receipt.get("events")
        if isinstance(process, dict) and process.get("duration_seconds") is not None:
            raw_duration = process["duration_seconds"]
            if (
                not isinstance(raw_duration, (int, float))
                or isinstance(raw_duration, bool)
                or raw_duration < 0
            ):
                raise CorpusCompletionValidationError(
                    f"prior completion job duration is invalid: {job['job_id']}"
                )
            duration = float(raw_duration)
        if isinstance(events, dict):
            raw_count = events.get("count", 0)
            if not isinstance(raw_count, int) or isinstance(raw_count, bool) or raw_count < 0:
                raise CorpusCompletionValidationError(
                    f"prior completion event count is invalid: {job['job_id']}"
                )
            event_count = raw_count
            last_event = events.get("last_event")
            raw_usage = last_event.get("usage") if isinstance(last_event, dict) else None
            if isinstance(raw_usage, dict):
                for key in usage:
                    raw_value = raw_usage.get(key, 0)
                    if (
                        not isinstance(raw_value, int)
                        or isinstance(raw_value, bool)
                        or raw_value < 0
                    ):
                        raise CorpusCompletionValidationError(
                            f"prior completion token usage is invalid: {job['job_id']}"
                        )
                    usage[key] = raw_value
        item = fingerprint(receipt_path, relative_to=repository_root)
        values.append(
            {
                "job_id": str(job["job_id"]),
                "run_id": str(job["run_id"]),
                "kind": str(job["kind"]),
                "state": str(job["state"]),
                "receipt": item.to_dict(),
                "semantic_job": semantic,
                "duration_seconds": duration,
                "worker_events_emitted": event_count,
                "token_usage": usage,
            }
        )
    return tuple(values)


def _retained_orientation_transition_evidence(
    repository: StateRepository,
    *,
    job: Mapping[str, Any],
    source_id: str,
) -> dict[str, Any]:
    """Prove one receiptless orientation was deterministically materialized.

    This is deliberately narrower than a general receipt fallback. The retained
    repair path never invokes Codex and therefore has no worker receipt; its
    durable evidence is the exact state transition trail emitted by
    ``ReadingCoordinator._seed_retained_orientation_for_audit_repair``.
    """

    job_identifier = str(job.get("job_id", ""))

    def invalid() -> NoReturn:
        raise CorpusCompletionValidationError(
            "prior completion receiptless job lacks exact retained orientation "
            f"evidence: {job_identifier}"
        )

    run_identifier = job.get("run_id")
    attempt = job.get("attempt")
    if (
        not job_identifier
        or not isinstance(run_identifier, str)
        or not run_identifier
        or not isinstance(attempt, int)
        or isinstance(attempt, bool)
        or attempt < 1
        or job.get("source_id") != source_id
        or job.get("kind") != "orientation"
        or job.get("state") != "succeeded"
        or job_identifier != job_id(run_identifier, "orientation", attempt)
    ):
        invalid()

    run = repository.run_record(run_identifier)
    if (
        run is None
        or run.get("run_id") != run_identifier
        or run.get("source_id") != source_id
        or run.get("requested_through") != "source_complete"
        or not isinstance(run.get("model"), str)
        or not str(run["model"]).strip()
    ):
        invalid()

    coverage_scope_ids = [str(row["scope_id"]) for row in repository.coverage_for(source_id)]
    if not coverage_scope_ids or len(set(coverage_scope_ids)) != len(coverage_scope_ids):
        invalid()
    expected = (
        (
            None,
            "pending",
            {"kind": "retained_orientation_created", "semantic_call": False},
        ),
        (
            "pending",
            "running",
            {"kind": "retained_orientation_materialized", "semantic_call": False},
        ),
        (
            "running",
            "succeeded",
            {
                "kind": "retained_orientation_applied",
                "semantic_call": False,
                "coverage_scope_ids": coverage_scope_ids,
            },
        ),
    )
    transitions = repository.transitions_for("job", job_identifier)
    if len(transitions) != len(expected):
        invalid()

    durable_job_transitions: list[dict[str, Any]] = []
    for row, (from_state, to_state, expected_receipt) in zip(transitions, expected, strict=True):
        try:
            receipt = json.loads(str(row["receipt_json"]))
        except (json.JSONDecodeError, TypeError, ValueError):
            invalid()
        if (
            row["entity_type"] != "job"
            or row["entity_id"] != job_identifier
            or row["from_state"] != from_state
            or row["to_state"] != to_state
            or int(row["applied"]) != 1
            or row["run_id"] != run_identifier
            or row["job_id"] != job_identifier
            or receipt != expected_receipt
        ):
            invalid()
        durable_job_transitions.append(
            {
                "transition_id": int(row["transition_id"]),
                "from_state": row["from_state"],
                "to_state": str(row["to_state"]),
                "applied": True,
                "receipt": receipt,
            }
        )

    durable_source_transitions: list[dict[str, Any]] = []
    previous_transition_id = int(transitions[-1]["transition_id"])
    active_record_count = len(repository.latest_wip_records(source_id))
    for source_transition in repository.source_transitions_for_job(job_identifier):
        try:
            source_receipt = json.loads(str(source_transition["receipt_json"]))
        except (json.JSONDecodeError, TypeError, ValueError):
            invalid()
        transition_id = int(source_transition["transition_id"])
        common_identity_valid = (
            transition_id > previous_transition_id
            and source_transition["entity_type"] == "source"
            and source_transition["entity_id"] == source_id
            and int(source_transition["applied"]) == 1
            and source_transition["run_id"] == run_identifier
            and source_transition["job_id"] == job_identifier
        )
        reading_started = (
            source_transition["from_state"] == "audit_failed"
            and source_transition["to_state"] == "reading"
            and source_receipt == {"kind": "reading_started", "job_id": job_identifier}
        )
        completion_record_count = (
            source_receipt.get("record_count") if isinstance(source_receipt, dict) else None
        )
        reading_completed = (
            active_record_count > 0
            and source_transition["from_state"] == "reading"
            and source_transition["to_state"] == "source_complete"
            and isinstance(completion_record_count, int)
            and not isinstance(completion_record_count, bool)
            and completion_record_count > 0
            and completion_record_count == active_record_count
            and source_receipt.get("coverage_complete") is True
            and source_receipt
            == {
                "kind": "source_reading_complete",
                "record_count": active_record_count,
                "coverage_complete": True,
            }
        )
        if not common_identity_valid or not (reading_started or reading_completed):
            invalid()
        durable_source_transitions.append(
            {
                "transition_id": transition_id,
                "from_state": str(source_transition["from_state"]),
                "to_state": str(source_transition["to_state"]),
                "applied": True,
                "source_id": source_id,
                "run_id": run_identifier,
                "job_id": job_identifier,
                "receipt": source_receipt,
            }
        )
        previous_transition_id = transition_id

    return {
        "kind": "retained_orientation_transition_evidence",
        "classification": "deterministic_nonsemantic",
        "semantic_call": False,
        "job_identity": {
            "job_id": job_identifier,
            "run_id": run_identifier,
            "source_id": source_id,
            "job_kind": "orientation",
            "attempt": attempt,
            "state": "succeeded",
        },
        "run_identity": {
            "run_id": run_identifier,
            "source_id": source_id,
            "requested_through": "source_complete",
            "model": str(run["model"]),
        },
        "job_transitions": durable_job_transitions,
        "source_transitions": durable_source_transitions,
    }


def _retained_job_telemetry(receipts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    selected = [item for item in receipts if item.get("semantic_job") is True]
    usage = _empty_token_usage()
    for item in selected:
        raw_usage = item.get("token_usage")
        if not isinstance(raw_usage, dict):
            raise CorpusCompletionValidationError("retained job telemetry is invalid")
        _add_token_usage(usage, raw_usage)
    return {
        "semantic_jobs_started": len(selected),
        "duration_seconds": sum(float(item["duration_seconds"]) for item in selected),
        "worker_events_emitted": sum(int(item["worker_events_emitted"]) for item in selected),
        "token_usage": usage,
    }


def _directory_composite(root: Path) -> dict[str, Any]:
    if not root.is_dir():
        raise CorpusCompletionValidationError(f"retained run root is missing: {root}")
    entries: list[dict[str, Any]] = []
    total_bytes = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise CorpusCompletionValidationError(f"retained run contains a symbolic link: {path}")
        if not path.is_file():
            continue
        value = fingerprint(path)
        relative = path.relative_to(root).as_posix()
        entries.append(
            {
                "path": relative,
                "sha256": value.sha256,
                "size_bytes": value.size_bytes,
            }
        )
        total_bytes += value.size_bytes
    if not entries:
        raise CorpusCompletionValidationError(f"retained run root is empty: {root}")
    return {
        "file_count": len(entries),
        "total_bytes": total_bytes,
        "sha256": _digest(entries),
    }


def _resolve_run_root(repository_root: Path, value: str, *, label: str) -> Path:
    path = (repository_root / value).resolve()
    allowed = (repository_root / ".cache" / "research-map" / "corpus-runs").resolve()
    if not path.is_relative_to(allowed) or path == allowed:
        raise CorpusCompletionValidationError(
            f"{label} run root must remain under .cache/research-map/corpus-runs"
        )
    return path


def _display_path(path: Path, *, repository_root: Path) -> str:
    return path.resolve().relative_to(repository_root).as_posix()


def _read_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CorpusCompletionValidationError(f"{label} is unreadable: {error}") from error
    if not isinstance(value, dict):
        raise CorpusCompletionValidationError(f"{label} must be an object")
    return value


def _write_or_verify_bytes(path: Path, value: bytes) -> None:
    if path.exists():
        if not path.is_file() or path.read_bytes() != value:
            raise CorpusCompletionExecutionError(
                f"immutable completion artifact differs on replay: {path}"
            )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)


def _validate_payload(payload: object, *, schema_path: Path, label: str) -> None:
    schema = _read_object(schema_path, label=f"{label} schema")
    errors = sorted(
        Draft202012Validator(schema).iter_errors(payload), key=lambda item: item.json_path
    )
    if errors:
        first = errors[0]
        raise CorpusCompletionValidationError(
            f"{label} violates {first.json_path}: {first.message}"
        )


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _empty_token_usage() -> dict[str, int]:
    return {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
    }


def _add_token_usage(total: dict[str, int], value: Mapping[str, Any]) -> None:
    if set(value) != set(total) or any(
        not isinstance(item, int) or isinstance(item, bool) or item < 0 for item in value.values()
    ):
        raise CorpusCompletionValidationError("token usage is invalid")
    for key in total:
        total[key] += int(value[key])


def _source_pair_key(left: str, right: str) -> str:
    source_left, source_right = canonical_source_pair(left, right)
    return f"pair-{source_left.lower()}-{source_right.lower()}"


def _plan_pair(value: Mapping[str, Any]) -> tuple[str, str]:
    raw = value.get("source_ids")
    if not isinstance(raw, list) or len(raw) != 2:
        raise CorpusCompletionValidationError("cross-reference plan pair is invalid")
    pair = (str(raw[0]), str(raw[1]))
    if pair != canonical_source_pair(*pair):
        raise CorpusCompletionValidationError("cross-reference plan pair is not canonical")
    return pair


def _coverage_pairs(value: object, *, label: str) -> set[tuple[str, str]]:
    if not isinstance(value, list):
        raise CorpusCompletionValidationError(f"cross-reference {label} ledger is invalid")
    pairs: set[tuple[str, str]] = set()
    for raw in value:
        if (
            not isinstance(raw, list)
            or len(raw) != 2
            or not all(isinstance(item, str) for item in raw)
        ):
            raise CorpusCompletionValidationError(f"cross-reference {label} ledger is invalid")
        pair = (raw[0], raw[1])
        if pair != canonical_source_pair(*pair) or pair in pairs:
            raise CorpusCompletionValidationError(f"cross-reference {label} ledger is invalid")
        pairs.add(pair)
    return pairs


_SOURCE_LOCAL_AUDIT_ERRORS = (
    AuditValidationError,
    ReadingValidationError,
    DossierValidationError,
    ProposalValidationError,
    ManualReviewRequired,
)

_QUALITY_LABELS = (
    "audited_passed",
    "audited_with_findings",
    "audit_interrupted",
    "unaudited_provisional",
)
