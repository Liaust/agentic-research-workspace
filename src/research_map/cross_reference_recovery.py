"""Bounded, immutable-input recovery for retained failed mapper shards."""

from __future__ import annotations

import copy
import itertools
import json
import threading
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from research_map.codex import CodexJob, CodexRunner
from research_map.cross_reference import (
    CrossReferenceValidationError,
    Relationship,
    validate_relationship_against_records,
    validate_schema,
)
from research_map.cross_reference_admission import compile_holistic_admission
from research_map.exploration import GraphExplorer
from research_map.ids import stable_id
from research_map.multi_surface_contract import (
    MultiSurfaceValidationReport,
    diagnose_multi_surface_proposal,
    multi_surface_granular_admission_projection,
)
from research_map.receipts import canonical_json_bytes, fingerprint, read_receipt, write_receipt
from research_map.records import Dossier, Record, RecordError
from research_map.relationship_markdown import materialize_relationship_markdown
from research_map.workspace import CrossReferenceWorkspaceBuilder, WorkspaceInput

RECOVERY_COORDINATOR_VERSION = "1.0.4"
MANIFEST_SCHEMA = "cross-reference-recovery-manifest.schema.json"
LAUNCH_PLAN_SCHEMA = "cross-reference-recovery-launch-plan.schema.json"
STATUS_SCHEMA = "cross-reference-recovery-status.schema.json"
SHARD_RECEIPT_SCHEMA = "cross-reference-recovery-shard-receipt.schema.json"
CORRECTED_PROPOSAL_SCHEMA = "failed-shard-corrected-proposal.schema.json"
VALIDATION_REPORT_SCHEMA = "multi-surface-validation-report.schema.json"
PROTOCOL_NAME = "failed-shard-correction.md"
SEMANTIC_SCHEMA_CONTRACTS = (
    MANIFEST_SCHEMA,
    LAUNCH_PLAN_SCHEMA,
    STATUS_SCHEMA,
    SHARD_RECEIPT_SCHEMA,
    CORRECTED_PROPOSAL_SCHEMA,
    VALIDATION_REPORT_SCHEMA,
    "holistic-multi-surface-proposal.schema.json",
    "relationship.schema.json",
    "discovery-candidate.schema.json",
    "inspection-outcome.schema.json",
    "cross-reference-admission-receipt.schema.json",
    "corpus-graph.schema.json",
    "cross-reference-recovery-graph-manifest.schema.json",
    "cross-reference-recovery-report.schema.json",
    "workspace-manifest.schema.json",
)


class CrossReferenceRecoveryValidationError(ValueError):
    """Raised when immutable recovery identity or retained inputs are invalid."""


class CrossReferenceRecoveryExecutionError(RuntimeError):
    """Raised when an approved bounded correction run cannot continue."""


@dataclass(frozen=True, slots=True)
class RecoveryArtifact:
    path: Path
    sha256: str

    def to_dict(self, *, relative_to: Path | None = None) -> dict[str, Any]:
        display = self.path
        if relative_to is not None and self.path.is_relative_to(relative_to):
            display = self.path.relative_to(relative_to)
        return {"path": display.as_posix(), "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class RecoveryShard:
    shard_id: str
    job_id: str
    batch_id: str
    source_ids: tuple[str, ...]
    owned_pairs: tuple[tuple[str, str], ...]
    original_receipt: RecoveryArtifact
    original_proposal: RecoveryArtifact
    frozen_records: RecoveryArtifact


@dataclass(frozen=True, slots=True)
class RecoverySuccessfulShard:
    shard_id: str
    job_id: str
    admission_receipt: RecoveryArtifact


@dataclass(frozen=True, slots=True)
class CrossReferenceRecoveryManifest:
    path: Path
    recovery_id: str
    origin_run_id: str
    origin_run_root: Path
    destination_run_root: Path
    model: str
    max_workers: int
    attempts_per_shard: int
    terminal_report: RecoveryArtifact
    corpus_graph: RecoveryArtifact
    graph_manifest: RecoveryArtifact
    pair_coverage: RecoveryArtifact
    successful_shards: tuple[RecoverySuccessfulShard, ...]
    failed_shards: tuple[RecoveryShard, ...]
    expected_inventory: dict[str, int]


@dataclass(frozen=True, slots=True)
class RecoveryWorkerResult:
    succeeded: bool
    duration_seconds: float
    event_count: int
    token_usage: dict[str, int]
    failure_reason: str | None = None


@dataclass(frozen=True, slots=True)
class RecoveryCorrectionJob:
    recovery_id: str
    shard_id: str
    job_id: str
    batch_id: str
    model: str
    workspace: Path
    output_path: Path
    output_schema_path: Path
    events_path: Path
    worker_receipt_path: Path


class RecoveryCorrectionRunner(Protocol):
    def run(self, job: RecoveryCorrectionJob) -> RecoveryWorkerResult: ...


@dataclass(frozen=True, slots=True)
class CrossReferenceRecoveryResult:
    recovery_id: str
    root: Path
    state: str
    launch_plan_path: Path
    launch_plan_sha256: str
    semantic_jobs_started: int
    peak_concurrency: int
    shard_results: tuple[dict[str, Any], ...]
    graph_path: Path | None = None
    report_path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        telemetry = _aggregate_telemetry(self.shard_results)
        return {
            "recovery_id": self.recovery_id,
            "root": str(self.root),
            "state": self.state,
            "launch_plan": {
                "path": str(self.launch_plan_path),
                "sha256": self.launch_plan_sha256,
            },
            "semantic_jobs_started": self.semantic_jobs_started,
            "peak_concurrency": self.peak_concurrency,
            "shards": list(self.shard_results),
            "telemetry": telemetry,
            "graph_path": None if self.graph_path is None else str(self.graph_path),
            "report_path": None if self.report_path is None else str(self.report_path),
        }


class CodexRecoveryRunner:
    """Production correction runner; construction itself starts no semantic job."""

    def __init__(self, executable: str = "codex", *, timeout_seconds: float = 5400) -> None:
        self.runner = CodexRunner(executable)
        self.timeout_seconds = timeout_seconds

    def run(self, job: RecoveryCorrectionJob) -> RecoveryWorkerResult:
        started = time.monotonic()
        result = self.runner.run(
            CodexJob(
                job_id=job.job_id,
                source_id=job.batch_id,
                model=job.model,
                prompt=(
                    "Follow AGENTS.md. Correct the retained proposal once using only the "
                    "declared immutable workspace inputs. Return the complete JSON artifact."
                ),
                workspace=job.workspace,
                output_schema=job.output_schema_path,
                events_path=job.events_path,
                last_message_path=job.output_path,
                receipt_path=job.worker_receipt_path,
                timeout_seconds=self.timeout_seconds,
            )
        )
        return RecoveryWorkerResult(
            succeeded=result.returncode == 0 and result.output_valid,
            duration_seconds=time.monotonic() - started,
            event_count=result.event_count,
            token_usage=_event_token_usage(job.events_path),
            failure_reason=(
                None
                if result.returncode == 0 and result.output_valid
                else "; ".join(result.validation_errors)
            ),
        )


class CrossReferenceRecoveryCoordinator:
    """Preflight, execute once, and retain failed-shard correction outcomes."""

    def __init__(
        self,
        *,
        repository_root: Path,
        manifest_path: Path,
        origin_run_root: Path | None = None,
        destination_run_root: Path | None = None,
        runner: RecoveryCorrectionRunner | None = None,
    ) -> None:
        self.repository_root = repository_root.resolve()
        self.schema_directory = self.repository_root / "schemas" / "research-map" / "v1"
        self.protocol_directory = self.repository_root / "protocols" / "research-map" / "v1"
        self.manifest = load_recovery_manifest(
            manifest_path,
            repository_root=self.repository_root,
        )
        if (
            origin_run_root is not None
            and origin_run_root.resolve() != self.manifest.origin_run_root
        ):
            raise CrossReferenceRecoveryValidationError(
                "explicit origin root differs from the recovery manifest"
            )
        if (
            destination_run_root is not None
            and destination_run_root.resolve() != self.manifest.destination_run_root
        ):
            raise CrossReferenceRecoveryValidationError(
                "explicit destination root differs from the recovery manifest"
            )
        self.root = self.manifest.destination_run_root
        self.launch_plan_path = self.root / "launch-plan.json"
        self.preflight_receipt_path = self.root / "preflight.json"
        self.status_path = self.root / "status.json"
        self.report_path = self.root / "report.json"
        self.graph_path = self.root / "graph" / "graph.json"
        self.graph_manifest_path = self.graph_path.with_name("manifest.json")
        self.shard_receipt_root = self.root / "shards"
        self.workspace_root = self.root / "workspaces"
        self.runner = runner
        self._concurrency_lock = threading.Lock()
        self._active_workers = 0
        self._peak_workers = 0

    def preflight(self) -> CrossReferenceRecoveryResult:
        """Verify and freeze exact retained inputs without starting a correction job."""

        artifacts = self._verify_origin()
        semantic_contracts = self._semantic_contract_descriptors()
        shard_plans: list[dict[str, Any]] = []
        surface_count = 0
        relationship_count = 0
        pair_count = 0
        for shard in self.manifest.failed_shards:
            proposal = _load_object(shard.original_proposal.path)
            records, evidence = _load_frozen_records(
                shard.frozen_records.path,
                batch_id=shard.batch_id,
                source_ids=shard.source_ids,
            )
            report = diagnose_multi_surface_proposal(
                proposal,
                batch_id=shard.batch_id,
                source_ids=shard.source_ids,
                records=records,
                evidence_ids_by_source=evidence,
                expected_pairs=shard.owned_pairs,
            )
            validate_schema(
                report.to_dict(),
                schema_directory=self.schema_directory,
                schema_name=VALIDATION_REPORT_SCHEMA,
            )
            diagnostic_path = self.root / "preflight" / "diagnostics" / f"{shard.shard_id}.json"
            _write_stable_receipt(
                diagnostic_path,
                {key: value for key, value in report.to_dict().items() if key != "schema_version"},
            )
            surface_count += len(proposal.get("comparison_surfaces", []))
            relationship_count += len(proposal.get("relationships", []))
            pair_count += len(shard.owned_pairs)
            shard_plans.append(
                {
                    "shard_id": shard.shard_id,
                    "job_id": shard.job_id,
                    "batch_id": shard.batch_id,
                    "source_ids": list(shard.source_ids),
                    "owned_pairs": [list(pair) for pair in shard.owned_pairs],
                    "original_receipt": shard.original_receipt.to_dict(),
                    "original_proposal": shard.original_proposal.to_dict(),
                    "frozen_records": shard.frozen_records.to_dict(),
                    "diagnostics": fingerprint(diagnostic_path).to_dict(),
                    "finding_count": len(report.findings),
                }
            )
        observed = {
            "failed_shards": len(self.manifest.failed_shards),
            "successful_shards": len(self.manifest.successful_shards),
            "failed_pairs": pair_count,
            "retained_surfaces": surface_count,
            "retained_relationships": relationship_count,
        }
        if observed != self.manifest.expected_inventory:
            raise CrossReferenceRecoveryValidationError(
                f"recovery inventory changed: expected {self.manifest.expected_inventory}; "
                f"observed {observed}"
            )
        launch_payload = {
            "kind": "cross_reference_recovery_launch_plan",
            "coordinator_version": RECOVERY_COORDINATOR_VERSION,
            "recovery_id": self.manifest.recovery_id,
            "origin_run_id": self.manifest.origin_run_id,
            "origin_run_root": str(self.manifest.origin_run_root),
            "destination_run_root": str(self.root),
            "model": self.manifest.model,
            "policy": {
                "max_workers": self.manifest.max_workers,
                "attempts_per_shard": self.manifest.attempts_per_shard,
                "automatic_retries": 0,
            },
            "inventory": observed,
            "semantic_contracts": semantic_contracts,
            "origin_artifacts": artifacts,
            "successful_shards": [
                {
                    "shard_id": item.shard_id,
                    "job_id": item.job_id,
                    "admission_receipt": item.admission_receipt.to_dict(),
                }
                for item in self.manifest.successful_shards
            ],
            "failed_shards": shard_plans,
        }
        _write_stable_receipt(self.launch_plan_path, launch_payload)
        validate_schema(
            read_receipt(self.launch_plan_path),
            schema_directory=self.schema_directory,
            schema_name=LAUNCH_PLAN_SCHEMA,
        )
        launch_sha256 = fingerprint(self.launch_plan_path).sha256
        _write_stable_receipt(
            self.preflight_receipt_path,
            {
                "kind": "cross_reference_recovery_preflight",
                "recovery_id": self.manifest.recovery_id,
                "launch_plan_sha256": launch_sha256,
                "semantic_jobs_started": 0,
                "inventory": observed,
            },
        )
        return CrossReferenceRecoveryResult(
            recovery_id=self.manifest.recovery_id,
            root=self.root,
            state="ready_for_approval",
            launch_plan_path=self.launch_plan_path,
            launch_plan_sha256=launch_sha256,
            semantic_jobs_started=0,
            peak_concurrency=0,
            shard_results=(),
        )

    def run(self, *, approved_launch_plan_sha256: str) -> CrossReferenceRecoveryResult:
        """Execute each retained failed shard once after exact plan approval."""

        preflight = self.preflight()
        if approved_launch_plan_sha256 != preflight.launch_plan_sha256:
            raise CrossReferenceRecoveryValidationError(
                "approved recovery launch-plan hash does not match stable preflight"
            )
        retained_status: dict[str, Any] | None = None
        if self.status_path.is_file():
            retained = read_receipt(self.status_path)
            if retained.get("launch_plan_sha256") != approved_launch_plan_sha256:
                raise CrossReferenceRecoveryValidationError(
                    "terminal recovery status belongs to another launch plan"
                )
            if retained.get("state") in {"complete", "partial"}:
                validate_schema(
                    retained,
                    schema_directory=self.schema_directory,
                    schema_name=STATUS_SCHEMA,
                )
                self._verified_receipts_from_status(retained)
                self.report()
                return self._result_from_status(retained, semantic_jobs_started=0)
            if retained.get("state") == "shards_complete":
                validate_schema(
                    retained,
                    schema_directory=self.schema_directory,
                    schema_name=STATUS_SCHEMA,
                )
                retained_status = retained
        if self.runner is None:
            self.runner = CodexRecoveryRunner()
        receipts: list[dict[str, Any]] = []
        jobs_started = 0
        pending: list[RecoveryShard] = []
        for shard in self.manifest.failed_shards:
            receipt_path = self._shard_receipt_path(shard.shard_id)
            if receipt_path.is_file():
                receipt = read_receipt(receipt_path)
                self._verify_shard_receipt(receipt, shard=shard)
                receipts.append(receipt)
            else:
                pending.append(shard)
        if retained_status is not None and pending:
            raise CrossReferenceRecoveryValidationError(
                "shards_complete status is missing a terminal shard receipt"
            )
        if pending:
            jobs_started = len(pending)
            with ThreadPoolExecutor(max_workers=self.manifest.max_workers) as executor:
                futures = {executor.submit(self._recover_shard, shard): shard for shard in pending}
                for future in as_completed(futures):
                    receipts.append(future.result())
        receipts.sort(key=lambda item: str(item["shard_id"]))
        if retained_status is None:
            self._write_status(receipts, launch_plan_sha256=approved_launch_plan_sha256)
        status = self._finalize_recovery(
            receipts,
            launch_plan_sha256=approved_launch_plan_sha256,
        )
        return self._result_from_status(status, semantic_jobs_started=jobs_started)

    def status(self) -> dict[str, Any]:
        if self.status_path.is_file():
            status = read_receipt(self.status_path)
            validate_schema(
                status,
                schema_directory=self.schema_directory,
                schema_name=STATUS_SCHEMA,
            )
            return status
        if self.preflight_receipt_path.is_file():
            return {
                "schema_version": "1.0",
                "recovery_id": self.manifest.recovery_id,
                "state": "ready_for_approval",
                "semantic_jobs_started": 0,
                "preflight": read_receipt(self.preflight_receipt_path),
            }
        return {
            "schema_version": "1.0",
            "recovery_id": self.manifest.recovery_id,
            "state": "not_preflighted",
            "semantic_jobs_started": 0,
        }

    def report(self) -> dict[str, Any]:
        """Return a terminal schema- and manifest-verified report without a model call."""

        if not self.report_path.is_file() or not self.graph_path.is_file():
            raise CrossReferenceRecoveryValidationError("recovery report is not terminal")
        report = read_receipt(self.report_path)
        validate_schema(
            report,
            schema_directory=self.schema_directory,
            schema_name="cross-reference-recovery-report.schema.json",
        )
        explorer = GraphExplorer(
            graph_path=self.graph_path,
            schema_directory=self.schema_directory,
        )
        graph = _object(report.get("graph"), "report graph")
        if (
            not explorer.graph_identity["manifest_verified"]
            or graph.get("sha256") != fingerprint(self.graph_path).sha256
        ):
            raise CrossReferenceRecoveryValidationError(
                "recovery report graph manifest did not verify"
            )
        return report

    def _verify_origin(self) -> list[dict[str, Any]]:
        if self.root == self.manifest.origin_run_root or self.root.is_relative_to(
            self.manifest.origin_run_root
        ):
            raise CrossReferenceRecoveryValidationError(
                "recovery destination must be separate from the immutable origin"
            )
        artifacts = (
            self.manifest.terminal_report,
            self.manifest.corpus_graph,
            self.manifest.graph_manifest,
            self.manifest.pair_coverage,
            *(item.admission_receipt for item in self.manifest.successful_shards),
            *(
                item
                for shard in self.manifest.failed_shards
                for item in (
                    shard.original_receipt,
                    shard.original_proposal,
                    shard.frozen_records,
                )
            ),
        )
        descriptors: list[dict[str, Any]] = []
        for artifact in artifacts:
            if not artifact.path.is_file() or fingerprint(artifact.path).sha256 != artifact.sha256:
                raise CrossReferenceRecoveryValidationError(
                    f"retained recovery input is missing or changed: {artifact.path}"
                )
            descriptors.append(fingerprint(artifact.path).to_dict())
        report = _load_object(self.manifest.terminal_report.path)
        if (
            report.get("state") not in {"partial", "complete"}
            or report.get("run_id") != self.manifest.origin_run_id
        ):
            raise CrossReferenceRecoveryValidationError("origin completion report is not terminal")
        graph = _load_object(self.manifest.corpus_graph.path)
        _load_object(self.manifest.graph_manifest.path)
        coverage = _load_object(self.manifest.pair_coverage.path)
        graph_sources = tuple(str(item) for item in _array(graph.get("sources"), "graph sources"))
        coverage_sources = tuple(
            str(item) for item in _array(coverage.get("source_ids"), "coverage sources")
        )
        if (
            graph.get("batch_id") != coverage.get("batch_id")
            or report.get("cross_reference_batch_id") != coverage.get("batch_id")
            or graph_sources != coverage_sources
        ):
            raise CrossReferenceRecoveryValidationError(
                "origin report, graph, and pair coverage identities differ"
            )
        self._verify_terminal_report_artifacts(report)
        if self.manifest.graph_manifest.path != self.manifest.corpus_graph.path.with_name(
            "manifest.json"
        ):
            raise CrossReferenceRecoveryValidationError(
                "origin graph manifest must be beside the explicit corpus graph"
            )
        explorer = GraphExplorer(
            graph_path=self.manifest.corpus_graph.path,
            schema_directory=self.schema_directory,
        )
        if (
            not explorer.graph_identity["manifest_verified"]
            or explorer.manifest_fingerprint is None
            or explorer.manifest_fingerprint.sha256 != self.manifest.graph_manifest.sha256
            or explorer.namespace_root is None
            or explorer.manifest is None
        ):
            raise CrossReferenceRecoveryValidationError(
                "origin graph manifest and relationship inputs did not verify"
            )
        for item in _array(explorer.manifest.get("inputs"), "origin graph inputs"):
            input_artifact = _object(item, "origin graph input")
            recorded_path = Path(str(input_artifact.get("path", "")))
            path = (
                recorded_path.resolve()
                if recorded_path.is_absolute()
                else (explorer.namespace_root / recorded_path).resolve()
            )
            descriptors.append(fingerprint(path).to_dict())
        self._verify_pair_coverage(coverage, descriptors=descriptors)
        return sorted(descriptors, key=lambda item: str(item["path"]))

    def _verify_terminal_report_artifacts(self, report: Mapping[str, Any]) -> None:
        artifacts = _list_of_objects(report.get("artifacts"), "origin report artifacts")
        by_kind: dict[str, Mapping[str, Any]] = {}
        for item in artifacts:
            kind = str(item.get("kind", ""))
            if not kind or kind in by_kind:
                raise CrossReferenceRecoveryValidationError(
                    "origin terminal report artifact inventory is ambiguous"
                )
            by_kind[kind] = item
        expected = {
            "corpus_graph": self.manifest.corpus_graph,
            "corpus_graph_manifest": self.manifest.graph_manifest,
            "cross_reference_coverage": self.manifest.pair_coverage,
        }
        for kind, artifact in expected.items():
            report_artifact = by_kind.get(kind)
            if report_artifact is None:
                raise CrossReferenceRecoveryValidationError(f"origin terminal report lacks {kind}")
            recorded_path = Path(str(report_artifact.get("path", "")))
            resolved = (
                recorded_path.resolve()
                if recorded_path.is_absolute()
                else (self.repository_root / recorded_path).resolve()
            )
            if resolved != artifact.path or report_artifact.get("sha256") != artifact.sha256:
                raise CrossReferenceRecoveryValidationError(
                    f"origin terminal report {kind} descriptor differs from recovery manifest"
                )

    def _semantic_contract_descriptors(self) -> list[dict[str, Any]]:
        contracts = [
            ("recovery_manifest", self.manifest.path),
            ("correction_protocol", self.protocol_directory / PROTOCOL_NAME),
            *(
                (
                    schema_name.removesuffix(".schema.json").replace("-", "_"),
                    self.schema_directory / schema_name,
                )
                for schema_name in SEMANTIC_SCHEMA_CONTRACTS
            ),
        ]
        descriptors = []
        for kind, path in contracts:
            if not path.is_file():
                raise CrossReferenceRecoveryValidationError(
                    f"recovery semantic contract is missing: {path}"
                )
            descriptors.append({"kind": kind, **fingerprint(path).to_dict()})
        return sorted(descriptors, key=lambda item: (str(item["kind"]), str(item["path"])))

    def _verify_pair_coverage(
        self,
        coverage: Mapping[str, Any],
        *,
        descriptors: list[dict[str, Any]],
    ) -> None:
        source_ids = tuple(
            str(item) for item in _array(coverage.get("source_ids"), "coverage sources")
        )
        if (
            coverage.get("kind") != "owned_pair_cross_reference_coverage"
            or tuple(sorted(set(source_ids))) != source_ids
            or not source_ids
        ):
            raise CrossReferenceRecoveryValidationError("origin pair coverage identity is invalid")
        expected_pairs = set(itertools.combinations(source_ids, 2))
        if coverage.get("pair_count") != len(expected_pairs):
            raise CrossReferenceRecoveryValidationError(
                "origin pair coverage pair count is invalid"
            )
        successful_pairs = {
            _canonical_pair(item, source_ids=source_ids)
            for item in _array(coverage.get("successful_pairs"), "successful coverage pairs")
        }
        uncovered_pairs = {
            _canonical_pair(item, source_ids=source_ids)
            for item in _array(coverage.get("uncovered_pairs"), "uncovered coverage pairs")
        }
        if (
            successful_pairs & uncovered_pairs
            or successful_pairs | uncovered_pairs != expected_pairs
            or len(uncovered_pairs) != self.manifest.expected_inventory["failed_pairs"]
        ):
            raise CrossReferenceRecoveryValidationError(
                "origin pair coverage partitions are not exhaustive and disjoint"
            )
        raw_shards = _list_of_objects(coverage.get("shards"), "coverage shards")
        expected_total = (
            self.manifest.expected_inventory["successful_shards"]
            + self.manifest.expected_inventory["failed_shards"]
        )
        if len(raw_shards) != expected_total:
            raise CrossReferenceRecoveryValidationError(
                "origin pair coverage shard inventory changed"
            )
        ledger_by_id: dict[str, Mapping[str, Any]] = {}
        ledger_job_ids: set[str] = set()
        derived_successful: set[tuple[str, str]] = set()
        derived_uncovered: set[tuple[str, str]] = set()
        status_counts = {"succeeded": 0, "failed": 0}
        coverage_batch_id = str(coverage.get("batch_id", ""))
        if not coverage_batch_id:
            raise CrossReferenceRecoveryValidationError("origin pair coverage lacks batch scope")
        for ledger in raw_shards:
            shard_id = str(ledger.get("shard_id", ""))
            job_id = str(ledger.get("job_id", ""))
            ledger_sources = tuple(
                str(item) for item in _array(ledger.get("source_ids"), "ledger source IDs")
            )
            if (
                not shard_id
                or not job_id
                or shard_id in ledger_by_id
                or job_id in ledger_job_ids
                or tuple(sorted(set(ledger_sources))) != ledger_sources
                or not set(ledger_sources).issubset(source_ids)
            ):
                raise CrossReferenceRecoveryValidationError(
                    "origin pair coverage shard identity is invalid"
                )
            owned_pairs = tuple(
                _canonical_pair(item, source_ids=ledger_sources)
                for item in _array(ledger.get("owned_pairs"), "ledger owned pairs")
            )
            if not owned_pairs or len(set(owned_pairs)) != len(owned_pairs):
                raise CrossReferenceRecoveryValidationError(
                    "origin pair coverage shard pairs are invalid"
                )
            status = str(ledger.get("status", ""))
            if status not in status_counts:
                raise CrossReferenceRecoveryValidationError(
                    "origin pair coverage shard status is invalid"
                )
            status_counts[status] += 1
            (derived_successful if status == "succeeded" else derived_uncovered).update(owned_pairs)
            ledger_by_id[shard_id] = ledger
            ledger_job_ids.add(job_id)
        if (
            status_counts["succeeded"] != self.manifest.expected_inventory["successful_shards"]
            or status_counts["failed"] != self.manifest.expected_inventory["failed_shards"]
            or derived_successful != successful_pairs
            or derived_uncovered != uncovered_pairs
        ):
            raise CrossReferenceRecoveryValidationError(
                "origin pair coverage shard outcomes differ from pair partitions"
            )

        failed_ids = {item.shard_id for item in self.manifest.failed_shards}
        successful_ids = {item.shard_id for item in self.manifest.successful_shards}
        if failed_ids & successful_ids or failed_ids | successful_ids != set(ledger_by_id):
            raise CrossReferenceRecoveryValidationError(
                "recovery manifest shard inventory differs from pair coverage"
            )
        for shard in self.manifest.failed_shards:
            ledger = ledger_by_id[shard.shard_id]
            if (
                coverage_batch_id != shard.batch_id
                or ledger.get("status") != "failed"
                or ledger.get("job_id") != shard.job_id
                or ledger.get("source_ids") != list(shard.source_ids)
                or ledger.get("owned_pairs") != [list(pair) for pair in shard.owned_pairs]
            ):
                raise CrossReferenceRecoveryValidationError(
                    f"failed recovery shard differs from pair coverage: {shard.shard_id}"
                )
            self._verify_failed_codex_receipt(shard)
        for successful_shard in self.manifest.successful_shards:
            ledger = ledger_by_id[successful_shard.shard_id]
            if (
                ledger.get("status") != "succeeded"
                or ledger.get("job_id") != successful_shard.job_id
                or ledger.get("source_ids") is None
                or ledger.get("owned_pairs") is None
            ):
                raise CrossReferenceRecoveryValidationError(
                    "successful recovery shard differs from pair coverage: "
                    f"{successful_shard.shard_id}"
                )
            admission = read_receipt(successful_shard.admission_receipt.path)
            try:
                validate_schema(
                    admission,
                    schema_directory=self.schema_directory,
                    schema_name="cross-reference-admission-receipt.schema.json",
                )
            except CrossReferenceValidationError as error:
                raise CrossReferenceRecoveryValidationError(
                    f"successful admission receipt is invalid: {successful_shard.shard_id}: {error}"
                ) from error
            if (
                admission.get("batch_id") != coverage_batch_id
                or admission.get("job_id") != successful_shard.job_id
            ):
                raise CrossReferenceRecoveryValidationError(
                    f"successful admission identity changed: {successful_shard.shard_id}"
                )
            raw_proposal = _object(admission.get("raw_proposal"), "successful raw proposal")
            proposal_path = self._origin_bound_path(str(raw_proposal.get("path", "")))
            if not proposal_path.is_file() or fingerprint(proposal_path).sha256 != raw_proposal.get(
                "sha256"
            ):
                raise CrossReferenceRecoveryValidationError(
                    f"successful admission proposal changed: {successful_shard.shard_id}"
                )
            descriptors.append(fingerprint(proposal_path).to_dict())

    def _verify_failed_codex_receipt(self, shard: RecoveryShard) -> None:
        receipt = read_receipt(shard.original_receipt.path)
        process = _object(receipt.get("process"), "failed Codex process")
        validation = _object(receipt.get("validation"), "failed Codex validation")
        output_contract = _object(receipt.get("output_contract"), "failed Codex output contract")
        if (
            receipt.get("kind") != "codex_job"
            or receipt.get("job_id") != shard.job_id
            or receipt.get("source_id") != shard.batch_id
            or receipt.get("model") != self.manifest.model
            or process.get("returncode") != 0
            or validation.get("valid") is not True
            or output_contract.get("mode") != "structured_last_message"
        ):
            raise CrossReferenceRecoveryValidationError(
                f"failed shard Codex execution identity changed: {shard.shard_id}"
            )
        output_path = self._origin_bound_path(str(output_contract.get("path", "")))
        if output_path != shard.original_proposal.path:
            raise CrossReferenceRecoveryValidationError(
                f"failed shard output proposal path changed: {shard.shard_id}"
            )
        outputs = _list_of_objects(receipt.get("outputs"), "failed Codex outputs")
        proposal_fingerprint = fingerprint(shard.original_proposal.path)
        output_matches = [
            item
            for item in outputs
            if self._origin_bound_path(str(item.get("path", ""))) == shard.original_proposal.path
            and item.get("sha256") == shard.original_proposal.sha256
            and item.get("size_bytes") == proposal_fingerprint.size_bytes
        ]
        inputs = _list_of_objects(receipt.get("inputs"), "failed Codex inputs")
        records_fingerprint = fingerprint(shard.frozen_records.path)
        frozen_matches = [
            item
            for item in inputs
            if Path(str(item.get("path", ""))).as_posix() == "input/corpus-records.json"
            and item.get("sha256") == shard.frozen_records.sha256
            and item.get("size_bytes") == records_fingerprint.size_bytes
        ]
        if len(output_matches) != 1 or len(frozen_matches) != 1:
            raise CrossReferenceRecoveryValidationError(
                f"failed shard Codex input/output bindings changed: {shard.shard_id}"
            )
        if receipt.get("proposal") != _load_object(shard.original_proposal.path):
            raise CrossReferenceRecoveryValidationError(
                f"failed shard embedded proposal changed: {shard.shard_id}"
            )

    def _origin_bound_path(self, value: str) -> Path:
        raw = Path(value)
        path = (raw if raw.is_absolute() else self.manifest.origin_run_root / raw).resolve()
        if not path.is_relative_to(self.manifest.origin_run_root):
            raise CrossReferenceRecoveryValidationError(
                "retained recovery descriptor escapes the immutable origin"
            )
        return path

    def _recover_shard(self, shard: RecoveryShard) -> dict[str, Any]:
        receipt_path = self._shard_receipt_path(shard.shard_id)
        if receipt_path.is_file():
            retained = read_receipt(receipt_path)
            self._verify_shard_receipt(retained, shard=shard)
            return retained
        attempt_start_path = self.root / "attempt-started" / f"{shard.shard_id}.json"
        if attempt_start_path.is_file():
            raise CrossReferenceRecoveryExecutionError(
                "recovery attempt already started without a terminal shard receipt: "
                f"{shard.shard_id}"
            )
        with self._concurrency_lock:
            self._active_workers += 1
            self._peak_workers = max(self._peak_workers, self._active_workers)
        started = time.monotonic()
        attempt_started = False
        report: MultiSurfaceValidationReport | None = None
        worker: RecoveryWorkerResult | None = None
        try:
            workspace, report = self._correction_workspace(shard)
            job = RecoveryCorrectionJob(
                recovery_id=self.manifest.recovery_id,
                shard_id=shard.shard_id,
                job_id=_correction_job_id(self.manifest.recovery_id, shard.shard_id),
                batch_id=shard.batch_id,
                model=self.manifest.model,
                workspace=workspace,
                output_path=workspace / "output" / "corrected-proposal.json",
                output_schema_path=workspace / "templates" / "corrected-proposal.schema.json",
                events_path=self.root / "events" / f"{shard.shard_id}.jsonl",
                worker_receipt_path=self.root / "worker-receipts" / f"{shard.shard_id}.json",
            )
            assert self.runner is not None
            _write_stable_receipt(
                attempt_start_path,
                {
                    "kind": "cross_reference_recovery_attempt_started",
                    "recovery_id": self.manifest.recovery_id,
                    "shard_id": shard.shard_id,
                    "attempt": 1,
                    "workspace_manifest_sha256": fingerprint(workspace / "manifest.json").sha256,
                    "semantic_call_limit": 1,
                },
            )
            attempt_started = True
            worker = self.runner.run(job)
            if not worker.succeeded or not job.output_path.is_file():
                return self._write_shard_receipt(
                    shard,
                    outcome="worker_failed",
                    corrected_proposal=None,
                    original_report=report,
                    corrected_report=None,
                    admission=None,
                    pair_outcomes=_failed_pair_outcomes(shard.owned_pairs),
                    worker=worker,
                    duration_seconds=time.monotonic() - started,
                    failure_reason=worker.failure_reason or "correction worker produced no output",
                )
            corrected = _load_object(job.output_path)
            validate_schema(
                corrected,
                schema_directory=self.schema_directory,
                schema_name=CORRECTED_PROPOSAL_SCHEMA,
            )
            records, evidence = _load_frozen_records(
                shard.frozen_records.path,
                batch_id=shard.batch_id,
                source_ids=shard.source_ids,
            )
            corrected_report = diagnose_multi_surface_proposal(
                corrected,
                batch_id=shard.batch_id,
                source_ids=shard.source_ids,
                records=records,
                evidence_ids_by_source=evidence,
                expected_pairs=shard.owned_pairs,
            )
            if corrected_report.has_fatal_findings:
                return self._write_shard_receipt(
                    shard,
                    outcome="validation_failed",
                    corrected_proposal=job.output_path,
                    original_report=report,
                    corrected_report=corrected_report,
                    admission=None,
                    pair_outcomes=_pair_outcomes(
                        corrected,
                        corrected_report,
                        owned_pairs=shard.owned_pairs,
                    ),
                    worker=worker,
                    duration_seconds=time.monotonic() - started,
                    failure_reason="corrected proposal retained fatal validation findings",
                )
            return self._write_shard_receipt(
                shard,
                outcome="succeeded",
                corrected_proposal=job.output_path,
                original_report=report,
                corrected_report=corrected_report,
                admission=self._compile_recovery_admission_payload(
                    shard,
                    corrected=corrected,
                    corrected_report=corrected_report,
                    corrected_proposal_path=job.output_path,
                ),
                pair_outcomes=_pair_outcomes(
                    corrected,
                    corrected_report,
                    owned_pairs=shard.owned_pairs,
                ),
                worker=worker,
                duration_seconds=time.monotonic() - started,
                failure_reason=None,
            )
        except Exception as error:
            if not attempt_started:
                raise
            if worker is None:
                worker = RecoveryWorkerResult(
                    succeeded=False,
                    duration_seconds=time.monotonic() - started,
                    event_count=0,
                    token_usage=_empty_token_usage(),
                    failure_reason=str(error),
                )
            return self._write_shard_receipt(
                shard,
                outcome="validation_failed",
                corrected_proposal=None,
                original_report=report,
                corrected_report=None,
                admission=None,
                pair_outcomes=_failed_pair_outcomes(shard.owned_pairs),
                worker=worker,
                duration_seconds=time.monotonic() - started,
                failure_reason=str(error),
            )
        finally:
            with self._concurrency_lock:
                self._active_workers -= 1

    def _compile_recovery_admission_payload(
        self,
        shard: RecoveryShard,
        *,
        corrected: Mapping[str, Any],
        corrected_report: MultiSurfaceValidationReport,
        corrected_proposal_path: Path,
    ) -> dict[str, Any]:
        records, evidence = _load_frozen_records(
            shard.frozen_records.path,
            batch_id=shard.batch_id,
            source_ids=shard.source_ids,
        )
        existing_relationships = self._origin_relationships()
        projection = multi_surface_granular_admission_projection(
            corrected,
            validation_report=corrected_report,
            existing_relationship_ids=tuple(
                relationship.relationship_id for relationship in existing_relationships
            ),
        )
        job_id = _correction_job_id(self.manifest.recovery_id, shard.shard_id)
        admission = compile_holistic_admission(
            projection,
            batch_id=shard.batch_id,
            job_id=job_id,
            raw_proposal_sha256=fingerprint(corrected_proposal_path).sha256,
            records=records,
            evidence_ids_by_source=evidence,
            existing_relationships=existing_relationships,
            owned_pairs=frozenset(shard.owned_pairs),
            model=self.manifest.model,
            input_sha256=shard.frozen_records.sha256,
            protocol_sha256=fingerprint(self.protocol_directory / PROTOCOL_NAME).sha256,
            relationship_schema_sha256=fingerprint(
                self.schema_directory / "relationship.schema.json"
            ).sha256,
            schema_directory=self.schema_directory,
        )
        return {
            **admission.receipt_payload(raw_proposal_path=str(corrected_proposal_path)),
            "admitted_relationships": [
                item.relationship.to_dict()
                for item in admission.admitted
                if item.relationship is not None
            ],
        }

    def _correction_workspace(
        self, shard: RecoveryShard
    ) -> tuple[Path, MultiSurfaceValidationReport]:
        proposal = _load_object(shard.original_proposal.path)
        records, evidence = _load_frozen_records(
            shard.frozen_records.path,
            batch_id=shard.batch_id,
            source_ids=shard.source_ids,
        )
        report = diagnose_multi_surface_proposal(
            proposal,
            batch_id=shard.batch_id,
            source_ids=shard.source_ids,
            records=records,
            evidence_ids_by_source=evidence,
            expected_pairs=shard.owned_pairs,
        )
        task = {
            "schema_version": "1.0",
            "kind": "failed_shard_correction",
            "recovery_id": self.manifest.recovery_id,
            "shard_id": shard.shard_id,
            "original_job_id": shard.job_id,
            "batch_id": shard.batch_id,
            "source_ids": list(shard.source_ids),
            "owned_pairs": [list(pair) for pair in shard.owned_pairs],
            "attempt": 1,
            "semantic_call_limit": 1,
            "authorized_inputs": [
                "input/task.json",
                "input/corpus-records.json",
                "input/original-proposal.json",
                "input/validation-report.json",
            ],
            "forbidden_inputs": [
                "PDFs",
                "source readers",
                "network access",
                "new scientific nodes or evidence",
            ],
        }
        workspace = CrossReferenceWorkspaceBuilder(
            self.workspace_root / shard.shard_id,
            batch_id=shard.batch_id,
            job_id=_correction_job_id(self.manifest.recovery_id, shard.shard_id),
            source_ids=shard.source_ids,
        ).build(
            kind="cross_reference_recovery",
            inputs=(
                WorkspaceInput(
                    source_id=shard.batch_id,
                    destination="task.json",
                    kind="recovery_task",
                    content=canonical_json_bytes(task),
                ),
                WorkspaceInput(
                    source_id=shard.batch_id,
                    destination="corpus-records.json",
                    kind="frozen_corpus_records",
                    source_path=shard.frozen_records.path,
                ),
                WorkspaceInput(
                    source_id=shard.batch_id,
                    destination="original-proposal.json",
                    kind="immutable_original_proposal",
                    source_path=shard.original_proposal.path,
                ),
                WorkspaceInput(
                    source_id=shard.batch_id,
                    destination="validation-report.json",
                    kind="structured_validation_findings",
                    content=canonical_json_bytes(report.to_dict()),
                ),
                WorkspaceInput(
                    source_id=shard.batch_id,
                    destination="corrected-proposal.schema.json",
                    kind="corrected_proposal_schema",
                    area="templates",
                    source_path=self.schema_directory / CORRECTED_PROPOSAL_SCHEMA,
                ),
                WorkspaceInput(
                    source_id=shard.batch_id,
                    destination="AGENTS.md",
                    kind="correction_protocol",
                    area="root",
                    source_path=self.protocol_directory / PROTOCOL_NAME,
                ),
            ),
        )
        return workspace.path, report

    def _write_shard_receipt(
        self,
        shard: RecoveryShard,
        *,
        outcome: str,
        corrected_proposal: Path | None,
        original_report: MultiSurfaceValidationReport | None,
        corrected_report: MultiSurfaceValidationReport | None,
        admission: dict[str, Any] | None,
        pair_outcomes: Sequence[Mapping[str, Any]],
        worker: RecoveryWorkerResult,
        duration_seconds: float,
        failure_reason: str | None,
    ) -> dict[str, Any]:
        receipt_path = self._shard_receipt_path(shard.shard_id)
        payload = {
            "kind": "cross_reference_recovery_shard",
            "recovery_id": self.manifest.recovery_id,
            "shard_id": shard.shard_id,
            "original_job_id": shard.job_id,
            "batch_id": shard.batch_id,
            "source_ids": list(shard.source_ids),
            "owned_pairs": [list(pair) for pair in shard.owned_pairs],
            "attempt": 1,
            "outcome": outcome,
            "original": {
                "receipt": shard.original_receipt.to_dict(),
                "proposal": shard.original_proposal.to_dict(),
                "validation_report": (
                    None if original_report is None else original_report.to_dict()
                ),
            },
            "correction": {
                "proposal": (
                    None
                    if corrected_proposal is None or not corrected_proposal.is_file()
                    else fingerprint(corrected_proposal).to_dict()
                ),
                "validation_report": (
                    None if corrected_report is None else corrected_report.to_dict()
                ),
            },
            "admission": admission,
            "pair_outcomes": [dict(item) for item in pair_outcomes],
            "telemetry": {
                "semantic_job_started": True,
                "duration_seconds": duration_seconds,
                "worker_events_emitted": worker.event_count,
                "token_usage": dict(worker.token_usage),
            },
            "failure_reason": failure_reason,
        }
        _write_stable_receipt(receipt_path, payload)
        receipt = read_receipt(receipt_path)
        validate_schema(
            receipt,
            schema_directory=self.schema_directory,
            schema_name=SHARD_RECEIPT_SCHEMA,
        )
        return receipt

    def _write_status(
        self, receipts: Sequence[Mapping[str, Any]], *, launch_plan_sha256: str
    ) -> dict[str, Any]:
        outcomes: dict[str, int] = {}
        for receipt in receipts:
            outcome = str(receipt["outcome"])
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
        pair_outcomes = [
            dict(pair)
            for receipt in receipts
            for pair in _list_of_objects(receipt.get("pair_outcomes"), "pair outcomes")
        ]
        payload = {
            "kind": "cross_reference_recovery_status",
            "recovery_id": self.manifest.recovery_id,
            "origin_run_id": self.manifest.origin_run_id,
            "state": "shards_complete",
            "launch_plan_sha256": launch_plan_sha256,
            "policy": {
                "max_workers": self.manifest.max_workers,
                "attempts_per_shard": self.manifest.attempts_per_shard,
                "automatic_retries": 0,
            },
            "shard_counts": {
                "total": len(receipts),
                "succeeded": outcomes.get("succeeded", 0),
                "validation_failed": outcomes.get("validation_failed", 0),
                "worker_failed": outcomes.get("worker_failed", 0),
            },
            "pair_counts": {
                "total": len(pair_outcomes),
                "inspected": sum(item.get("status") == "inspected" for item in pair_outcomes),
                "unresolved": sum(item.get("status") == "unresolved" for item in pair_outcomes),
                "shard_failed": sum(item.get("status") == "shard_failed" for item in pair_outcomes),
            },
            "peak_concurrency": self._peak_workers,
            "shards": [
                {
                    "shard_id": str(receipt["shard_id"]),
                    "outcome": str(receipt["outcome"]),
                    "receipt": fingerprint(
                        self._shard_receipt_path(str(receipt["shard_id"]))
                    ).to_dict(),
                    "telemetry": dict(receipt["telemetry"]),
                }
                for receipt in receipts
            ],
        }
        write_receipt(self.status_path, payload)
        status = read_receipt(self.status_path)
        validate_schema(
            status,
            schema_directory=self.schema_directory,
            schema_name=STATUS_SCHEMA,
        )
        return status

    def _finalize_recovery(
        self,
        receipts: Sequence[Mapping[str, Any]],
        *,
        launch_plan_sha256: str,
    ) -> dict[str, Any]:
        origin_graph = _load_object(self.manifest.corpus_graph.path)
        origin_manifest = _load_object(self.manifest.graph_manifest.path)
        origin_relationships = self._origin_relationships()
        origin_by_id = {
            relationship.relationship_id: relationship for relationship in origin_relationships
        }
        origin_cross_edges = {
            str(edge["relationship_id"]): dict(edge)
            for edge in _list_of_objects(origin_graph.get("edges"), "origin graph edges")
            if edge.get("origin") == "cross_source"
        }
        if set(origin_cross_edges) != set(origin_by_id):
            raise CrossReferenceRecoveryValidationError(
                "origin graph cross-source edges differ from retained relationships"
            )
        recovered_by_id: dict[str, Relationship] = {}
        for receipt in receipts:
            admission = receipt.get("admission")
            if not isinstance(admission, dict):
                continue
            for value in _array(
                admission.get("admitted_relationships", []),
                "admitted recovered relationships",
            ):
                relationship = Relationship.from_mapping(
                    _object(value, "admitted recovered relationship")
                )
                existing = origin_by_id.get(relationship.relationship_id)
                if existing is not None:
                    if existing.to_dict() != relationship.to_dict():
                        raise CrossReferenceRecoveryValidationError(
                            "recovered relationship attempts to replace an origin relationship"
                        )
                    continue
                prior = recovered_by_id.get(relationship.relationship_id)
                if prior is not None and prior.to_dict() != relationship.to_dict():
                    raise CrossReferenceRecoveryValidationError(
                        "recovered relationship identity has conflicting payloads"
                    )
                recovered_by_id[relationship.relationship_id] = relationship
        all_relationships = {**origin_by_id, **recovered_by_id}
        records, evidence = _graph_records(origin_graph)
        for relationship in all_relationships.values():
            validate_relationship_against_records(
                relationship,
                records=records,
                evidence_ids_by_source=evidence,
            )
        graph = copy.deepcopy(origin_graph)
        edges = _array(graph.get("edges"), "recovery graph edges")
        for relationship in sorted(recovered_by_id.values(), key=lambda item: item.relationship_id):
            left = relationship.payload["left_endpoint"]
            right = relationship.payload["right_endpoint"]
            source, target = (
                (str(right["record_id"]), str(left["record_id"]))
                if relationship.payload["direction"] == "right_to_left"
                else (str(left["record_id"]), str(right["record_id"]))
            )
            edges.append(
                {
                    "source": source,
                    "target": target,
                    "kind": relationship.relation_type,
                    "origin": "cross_source",
                    "relationship_id": relationship.relationship_id,
                }
            )
        if graph.get("nodes") != origin_graph.get("nodes") or edges[
            : len(origin_graph["edges"])
        ] != list(origin_graph["edges"]):
            raise CrossReferenceRecoveryValidationError(
                "recovery graph changed an origin node or edge"
            )
        validate_schema(
            graph,
            schema_directory=self.schema_directory,
            schema_name="corpus-graph.schema.json",
        )
        self.graph_path.parent.mkdir(parents=True, exist_ok=True)
        _write_bytes_stable(self.graph_path, canonical_json_bytes(graph))

        relationship_paths = _materialize_relationship_projection(
            self.root / "relationships",
            tuple(all_relationships.values()),
        )
        lineage_inputs = self._materialize_lineage_copies()
        manifest_inputs = (
            [
                _artifact_descriptor("recovery_lineage", path, relative_to=self.root)
                for path in lineage_inputs
            ]
            + [
                _artifact_descriptor(
                    "cross_reference_recovery_shard_receipt",
                    self._shard_receipt_path(str(receipt["shard_id"])),
                    relative_to=self.root,
                )
                for receipt in receipts
            ]
            + [
                _artifact_descriptor("canonical_relationship_markdown", path, relative_to=self.root)
                for path in relationship_paths
            ]
        )
        graph_manifest = {
            "schema_version": "1.0",
            "kind": "cross_reference_recovery_graph_manifest",
            "recovery_id": self.manifest.recovery_id,
            "origin_run_id": self.manifest.origin_run_id,
            "batch_id": str(graph["batch_id"]),
            "sources": list(graph["sources"]),
            "launch_plan_sha256": launch_plan_sha256,
            "origin_graph_sha256": self.manifest.corpus_graph.sha256,
            "inputs": manifest_inputs,
            "corpus_graph": _artifact_descriptor(
                "corpus_graph", self.graph_path, relative_to=self.root
            ),
            "node_count": len(graph["nodes"]),
            "source_local_edge_count": sum(
                edge["origin"] == "source_local" for edge in graph["edges"]
            ),
            "origin_relationship_count": len(origin_by_id),
            "recovered_relationship_count": len(recovered_by_id),
            "cross_source_edge_count": sum(
                edge["origin"] == "cross_source" for edge in graph["edges"]
            ),
            "source_quality": origin_manifest.get("source_quality", []),
            "quality_label_counts": origin_manifest.get("quality_label_counts", {}),
            "selected_origin_counts": origin_manifest.get("selected_origin_counts", {}),
            "structurally_invalid_sources": origin_manifest.get("structurally_invalid_sources", []),
        }
        _write_bytes_stable(self.graph_manifest_path, canonical_json_bytes(graph_manifest))
        validate_schema(
            graph_manifest,
            schema_directory=self.schema_directory,
            schema_name="cross-reference-recovery-graph-manifest.schema.json",
        )
        explorer = GraphExplorer(
            graph_path=self.graph_path,
            schema_directory=self.schema_directory,
        )
        if not explorer.graph_identity["manifest_verified"] or set(explorer.relationships) != set(
            all_relationships
        ):
            raise CrossReferenceRecoveryValidationError(
                "recovery graph search manifest did not verify the full relationship union"
            )

        pair_partitions = self._pair_partitions(receipts)
        telemetry = _aggregate_telemetry(receipts)
        report = {
            "kind": "cross_reference_recovery_report",
            "recovery_id": self.manifest.recovery_id,
            "origin_run_id": self.manifest.origin_run_id,
            "state": "complete" if not pair_partitions["remaining_unresolved"] else "partial",
            "launch_plan_sha256": launch_plan_sha256,
            "counts": {
                "records": len(graph["nodes"]),
                "source_local_edges": sum(
                    edge["origin"] == "source_local" for edge in graph["edges"]
                ),
                "origin_relationships": len(origin_by_id),
                "recovered_relationships": len(recovered_by_id),
                "relationships": len(all_relationships),
                "pairs": sum(len(value) for value in pair_partitions.values()),
            },
            "pair_partitions": pair_partitions,
            "shard_outcomes": [
                {
                    "shard_id": str(receipt["shard_id"]),
                    "outcome": str(receipt["outcome"]),
                    "receipt": _artifact_descriptor(
                        "cross_reference_recovery_shard_receipt",
                        self._shard_receipt_path(str(receipt["shard_id"])),
                        relative_to=self.root,
                    ),
                }
                for receipt in receipts
            ],
            "telemetry": telemetry,
            "graph": _artifact_descriptor("corpus_graph", self.graph_path, relative_to=self.root),
            "graph_manifest": _artifact_descriptor(
                "cross_reference_recovery_graph_manifest",
                self.graph_manifest_path,
                relative_to=self.root,
            ),
            "retrieval": {
                "graph_path": str(self.graph_path),
                "search_command": f"research-map search --graph {self.graph_path}",
                "explore_command": f"research-map explore --graph {self.graph_path}",
                "manifest_verified": True,
            },
        }
        _write_stable_receipt(self.report_path, report)
        report_receipt = read_receipt(self.report_path)
        validate_schema(
            report_receipt,
            schema_directory=self.schema_directory,
            schema_name="cross-reference-recovery-report.schema.json",
        )
        existing_status = read_receipt(self.status_path) if self.status_path.is_file() else None
        final_status = (
            existing_status
            if existing_status is not None
            and existing_status.get("state") == "shards_complete"
            and existing_status.get("launch_plan_sha256") == launch_plan_sha256
            else self._write_status(receipts, launch_plan_sha256=launch_plan_sha256)
        )
        final_status.update(
            {
                "state": report["state"],
                "graph": _artifact_descriptor(
                    "corpus_graph", self.graph_path, relative_to=self.root
                ),
                "report": _artifact_descriptor(
                    "cross_reference_recovery_report",
                    self.report_path,
                    relative_to=self.root,
                ),
                "pair_partitions": pair_partitions,
                "counts": report["counts"],
                "telemetry": telemetry,
            }
        )
        _write_receipt_document(self.status_path, final_status)
        validate_schema(
            final_status,
            schema_directory=self.schema_directory,
            schema_name=STATUS_SCHEMA,
        )
        return final_status

    def _materialize_lineage_copies(self) -> tuple[Path, ...]:
        lineage_root = self.root / "lineage"
        artifacts = {
            "origin-terminal-report.json": self.manifest.terminal_report.path,
            "origin-corpus-graph.json": self.manifest.corpus_graph.path,
            "origin-graph-manifest.json": self.manifest.graph_manifest.path,
            "origin-pair-coverage.json": self.manifest.pair_coverage.path,
            "recovery-launch-plan.json": self.launch_plan_path,
        }
        paths = []
        for name, source in artifacts.items():
            destination = lineage_root / name
            _write_bytes_stable(destination, source.read_bytes())
            paths.append(destination)
        return tuple(paths)

    def _pair_partitions(self, receipts: Sequence[Mapping[str, Any]]) -> dict[str, list[list[str]]]:
        coverage = _load_object(self.manifest.pair_coverage.path)
        original_successful = {
            _canonical_pair(pair, source_ids=coverage.get("source_ids", []))
            for pair in _array(coverage.get("successful_pairs"), "successful pairs")
        }
        original_uncovered = {
            _canonical_pair(pair, source_ids=coverage.get("source_ids", []))
            for pair in _array(coverage.get("uncovered_pairs"), "uncovered pairs")
        }
        expected = set(original_successful) | set(original_uncovered)
        source_ids = tuple(str(value) for value in coverage.get("source_ids", []))
        if (
            tuple(sorted(set(source_ids))) != source_ids
            or expected != set(itertools.combinations(source_ids, 2))
            or original_successful & original_uncovered
        ):
            raise CrossReferenceRecoveryValidationError(
                "origin pair coverage is not exhaustive and disjoint"
            )
        manifest_failed_pairs = {
            pair for shard in self.manifest.failed_shards for pair in shard.owned_pairs
        }
        if manifest_failed_pairs != original_uncovered:
            raise CrossReferenceRecoveryValidationError(
                "retained failed shard pairs differ from origin uncovered pairs"
            )
        recovered: set[tuple[str, str]] = set()
        unresolved: set[tuple[str, str]] = set()
        failed: set[tuple[str, str]] = set()
        for receipt in receipts:
            for item in _list_of_objects(receipt.get("pair_outcomes"), "pair outcomes"):
                pair = _canonical_pair(item.get("source_ids"), source_ids=coverage["source_ids"])
                status = item.get("status")
                target = (
                    recovered
                    if status == "inspected"
                    else unresolved
                    if status == "unresolved"
                    else failed
                )
                target.add(pair)
        if (
            recovered & unresolved
            or recovered & failed
            or unresolved & failed
            or recovered | unresolved | failed != original_uncovered
            or original_successful | recovered | unresolved | failed != expected
        ):
            raise CrossReferenceRecoveryValidationError(
                "recovery pair outcome partitions are not exhaustive and disjoint"
            )
        return {
            "original_successful": [list(pair) for pair in sorted(original_successful)],
            "recovered_inspected": [list(pair) for pair in sorted(recovered)],
            "remaining_unresolved": [list(pair) for pair in sorted(unresolved | failed)],
        }

    def _origin_relationships(self) -> tuple[Relationship, ...]:
        explorer = GraphExplorer(
            graph_path=self.manifest.corpus_graph.path,
            schema_directory=self.schema_directory,
        )
        if not explorer.graph_identity["manifest_verified"]:
            raise CrossReferenceRecoveryValidationError(
                "origin relationship graph manifest did not verify"
            )
        return tuple(
            Relationship.from_mapping(value)
            for _relationship_id, value in sorted(explorer.relationships.items())
        )

    def _result_from_status(
        self, status: Mapping[str, Any], *, semantic_jobs_started: int
    ) -> CrossReferenceRecoveryResult:
        launch_sha = str(status["launch_plan_sha256"])
        shard_results = self._verified_receipts_from_status(status)
        return CrossReferenceRecoveryResult(
            recovery_id=self.manifest.recovery_id,
            root=self.root,
            state=str(status["state"]),
            launch_plan_path=self.launch_plan_path,
            launch_plan_sha256=launch_sha,
            semantic_jobs_started=semantic_jobs_started,
            peak_concurrency=int(status["peak_concurrency"]) if semantic_jobs_started else 0,
            shard_results=shard_results,
            graph_path=(self.graph_path if self.graph_path.is_file() else None),
            report_path=(self.report_path if self.report_path.is_file() else None),
        )

    def _verified_receipts_from_status(
        self, status: Mapping[str, Any]
    ) -> tuple[dict[str, Any], ...]:
        status_items = _list_of_objects(status.get("shards"), "recovery status shards")
        by_shard = {shard.shard_id: shard for shard in self.manifest.failed_shards}
        if len(status_items) != len(by_shard):
            raise CrossReferenceRecoveryValidationError(
                "retained recovery status shard inventory changed"
            )
        receipts: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in status_items:
            shard_id = str(item.get("shard_id", ""))
            shard = by_shard.get(shard_id)
            if shard is None or shard_id in seen:
                raise CrossReferenceRecoveryValidationError(
                    "retained recovery status shard identity changed"
                )
            expected_path = self._shard_receipt_path(shard_id)
            descriptor = _object(item.get("receipt"), "recovery status shard receipt")
            if not expected_path.is_file() or descriptor != fingerprint(expected_path).to_dict():
                raise CrossReferenceRecoveryValidationError(
                    f"retained recovery status receipt binding changed: {shard_id}"
                )
            receipt = read_receipt(expected_path)
            self._verify_shard_receipt(receipt, shard=shard)
            if item.get("outcome") != receipt.get("outcome") or item.get(
                "telemetry"
            ) != receipt.get("telemetry"):
                raise CrossReferenceRecoveryValidationError(
                    f"retained recovery status outcome changed: {shard_id}"
                )
            receipts.append(receipt)
            seen.add(shard_id)
        return tuple(sorted(receipts, key=lambda value: str(value["shard_id"])))

    def _verify_shard_receipt(self, receipt: Mapping[str, Any], *, shard: RecoveryShard) -> None:
        try:
            validate_schema(
                receipt,
                schema_directory=self.schema_directory,
                schema_name=SHARD_RECEIPT_SCHEMA,
            )
        except CrossReferenceValidationError as error:
            raise CrossReferenceRecoveryValidationError(
                f"retained recovery shard receipt schema changed: {shard.shard_id}: {error}"
            ) from error
        if (
            receipt.get("recovery_id") != self.manifest.recovery_id
            or receipt.get("shard_id") != shard.shard_id
            or receipt.get("attempt") != 1
            or receipt.get("original_job_id") != shard.job_id
            or receipt.get("batch_id") != shard.batch_id
            or receipt.get("source_ids") != list(shard.source_ids)
            or receipt.get("owned_pairs") != [list(pair) for pair in shard.owned_pairs]
        ):
            raise CrossReferenceRecoveryValidationError(
                f"retained recovery shard receipt identity changed: {shard.shard_id}"
            )
        original = _object(receipt.get("original"), "retained recovery original")
        if (
            original.get("receipt") != shard.original_receipt.to_dict()
            or original.get("proposal") != shard.original_proposal.to_dict()
        ):
            raise CrossReferenceRecoveryValidationError(
                f"retained recovery original lineage changed: {shard.shard_id}"
            )
        original_proposal = _load_object(shard.original_proposal.path)
        records, evidence = _load_frozen_records(
            shard.frozen_records.path,
            batch_id=shard.batch_id,
            source_ids=shard.source_ids,
        )
        original_report = diagnose_multi_surface_proposal(
            original_proposal,
            batch_id=shard.batch_id,
            source_ids=shard.source_ids,
            records=records,
            evidence_ids_by_source=evidence,
            expected_pairs=shard.owned_pairs,
        )
        if original.get("validation_report") != original_report.to_dict():
            raise CrossReferenceRecoveryValidationError(
                f"retained recovery original validation changed: {shard.shard_id}"
            )

        correction = _object(receipt.get("correction"), "retained recovery correction")
        corrected_descriptor = correction.get("proposal")
        corrected_report_value = correction.get("validation_report")
        outcome = str(receipt.get("outcome"))
        expected_pair_outcomes: list[dict[str, Any]]
        expected_admission: dict[str, Any] | None = None
        if corrected_descriptor is None:
            if corrected_report_value is not None or outcome not in {
                "worker_failed",
                "validation_failed",
            }:
                raise CrossReferenceRecoveryValidationError(
                    f"retained recovery correction outcome changed: {shard.shard_id}"
                )
            expected_pair_outcomes = _failed_pair_outcomes(shard.owned_pairs)
        else:
            descriptor = _object(corrected_descriptor, "retained corrected proposal")
            corrected_path = (
                self.workspace_root / shard.shard_id / "output" / "corrected-proposal.json"
            ).resolve()
            expected_descriptor = (
                fingerprint(corrected_path).to_dict() if corrected_path.is_file() else None
            )
            if descriptor != expected_descriptor:
                raise CrossReferenceRecoveryValidationError(
                    f"retained recovery corrected proposal changed: {shard.shard_id}"
                )
            corrected = _load_object(corrected_path)
            try:
                validate_schema(
                    corrected,
                    schema_directory=self.schema_directory,
                    schema_name=CORRECTED_PROPOSAL_SCHEMA,
                )
            except CrossReferenceValidationError as error:
                raise CrossReferenceRecoveryValidationError(
                    f"retained corrected proposal schema changed: {shard.shard_id}: {error}"
                ) from error
            corrected_report = diagnose_multi_surface_proposal(
                corrected,
                batch_id=shard.batch_id,
                source_ids=shard.source_ids,
                records=records,
                evidence_ids_by_source=evidence,
                expected_pairs=shard.owned_pairs,
            )
            if corrected_report_value != corrected_report.to_dict():
                raise CrossReferenceRecoveryValidationError(
                    f"retained recovery corrected validation changed: {shard.shard_id}"
                )
            expected_pair_outcomes = _pair_outcomes(
                corrected,
                corrected_report,
                owned_pairs=shard.owned_pairs,
            )
            if corrected_report.has_fatal_findings:
                if outcome != "validation_failed":
                    raise CrossReferenceRecoveryValidationError(
                        f"retained recovery fatal outcome changed: {shard.shard_id}"
                    )
            else:
                if outcome != "succeeded":
                    raise CrossReferenceRecoveryValidationError(
                        f"retained recovery admitted outcome changed: {shard.shard_id}"
                    )
                expected_admission = self._compile_recovery_admission_payload(
                    shard,
                    corrected=corrected,
                    corrected_report=corrected_report,
                    corrected_proposal_path=corrected_path,
                )
        if receipt.get("pair_outcomes") != expected_pair_outcomes:
            raise CrossReferenceRecoveryValidationError(
                f"retained recovery pair outcomes changed: {shard.shard_id}"
            )
        if receipt.get("admission") != expected_admission:
            raise CrossReferenceRecoveryValidationError(
                f"retained recovery admission changed: {shard.shard_id}"
            )

    def _shard_receipt_path(self, shard_id: str) -> Path:
        return self.shard_receipt_root / f"{shard_id}.json"


def load_recovery_manifest(path: Path, *, repository_root: Path) -> CrossReferenceRecoveryManifest:
    manifest_path = path if path.is_absolute() else repository_root / path
    payload = _load_object(manifest_path.resolve())
    validate_schema(
        payload,
        schema_directory=repository_root / "schemas" / "research-map" / "v1",
        schema_name=MANIFEST_SCHEMA,
    )
    origin_root = _resolve_root(payload["origin_run_root"], repository_root=repository_root)
    destination_root = _resolve_root(
        payload["destination_run_root"], repository_root=repository_root
    )

    def artifact(value: Mapping[str, Any]) -> RecoveryArtifact:
        relative = Path(str(value["path"]))
        if relative.is_absolute() or ".." in relative.parts:
            raise CrossReferenceRecoveryValidationError(
                "recovery origin artifact paths must be safe origin-relative paths"
            )
        resolved = (origin_root / relative).resolve()
        if not resolved.is_relative_to(origin_root):
            raise CrossReferenceRecoveryValidationError("recovery artifact escapes origin root")
        return RecoveryArtifact(resolved, str(value["sha256"]))

    origin = _object(payload["origin_artifacts"], "origin artifacts")
    successful: list[RecoverySuccessfulShard] = []
    for item in _array(payload["successful_shards"], "successful shards"):
        shard = _object(item, "successful shard")
        successful.append(
            RecoverySuccessfulShard(
                shard_id=str(shard["shard_id"]),
                job_id=str(shard["job_id"]),
                admission_receipt=artifact(
                    _object(shard["admission_receipt"], "successful admission receipt")
                ),
            )
        )
    failed: list[RecoveryShard] = []
    for item in _array(payload["failed_shards"], "failed shards"):
        shard = _object(item, "failed shard")
        source_ids = tuple(str(value) for value in _array(shard["source_ids"], "source IDs"))
        if tuple(sorted(set(source_ids))) != source_ids:
            raise CrossReferenceRecoveryValidationError(
                "failed shard source IDs must be unique and canonical"
            )
        owned_pairs = tuple(
            _canonical_pair(value, source_ids=source_ids)
            for value in _array(shard["owned_pairs"], "owned pairs")
        )
        if len(set(owned_pairs)) != len(owned_pairs):
            raise CrossReferenceRecoveryValidationError("failed shard owned pairs repeat")
        failed.append(
            RecoveryShard(
                shard_id=str(shard["shard_id"]),
                job_id=str(shard["job_id"]),
                batch_id=str(shard["batch_id"]),
                source_ids=source_ids,
                owned_pairs=tuple(sorted(owned_pairs)),
                original_receipt=artifact(_object(shard["original_receipt"], "receipt")),
                original_proposal=artifact(_object(shard["original_proposal"], "proposal")),
                frozen_records=artifact(_object(shard["frozen_records"], "records")),
            )
        )
    if len({item.shard_id for item in failed}) != len(failed):
        raise CrossReferenceRecoveryValidationError("failed shard IDs must be unique")
    if len({item.shard_id for item in successful}) != len(successful):
        raise CrossReferenceRecoveryValidationError("successful shard IDs must be unique")
    if len({item.job_id for item in successful}) != len(successful):
        raise CrossReferenceRecoveryValidationError("successful shard job IDs must be unique")
    if {item.shard_id for item in successful} & {item.shard_id for item in failed}:
        raise CrossReferenceRecoveryValidationError(
            "successful and failed shard IDs must be disjoint"
        )
    if {item.job_id for item in successful} & {item.job_id for item in failed}:
        raise CrossReferenceRecoveryValidationError(
            "successful and failed shard job IDs must be disjoint"
        )
    flattened_pairs = [pair for shard in failed for pair in shard.owned_pairs]
    if len(flattened_pairs) != len(set(flattened_pairs)):
        raise CrossReferenceRecoveryValidationError(
            "failed shard owned pairs must be globally disjoint"
        )
    policy = _object(payload["policy"], "recovery policy")
    expected = {
        str(key): int(value)
        for key, value in _object(payload["expected_inventory"], "expected inventory").items()
    }
    return CrossReferenceRecoveryManifest(
        path=manifest_path.resolve(),
        recovery_id=str(payload["recovery_id"]),
        origin_run_id=str(payload["origin_run_id"]),
        origin_run_root=origin_root,
        destination_run_root=destination_root,
        model=str(payload["model"]),
        max_workers=int(policy["max_workers"]),
        attempts_per_shard=int(policy["attempts_per_shard"]),
        terminal_report=artifact(_object(origin["terminal_report"], "terminal report")),
        corpus_graph=artifact(_object(origin["corpus_graph"], "corpus graph")),
        graph_manifest=artifact(_object(origin["graph_manifest"], "graph manifest")),
        pair_coverage=artifact(_object(origin["pair_coverage"], "pair coverage")),
        successful_shards=tuple(successful),
        failed_shards=tuple(failed),
        expected_inventory=expected,
    )


def _load_frozen_records(
    path: Path, *, batch_id: str, source_ids: Sequence[str]
) -> tuple[dict[str, Record], dict[str, frozenset[str]]]:
    payload = _load_object(path)
    if payload.get("schema_version") != 1 or payload.get("batch_id") != batch_id:
        raise CrossReferenceRecoveryValidationError(
            "frozen recovery record identity differs from the retained shard"
        )
    expected_sources = tuple(source_ids)
    if tuple(sorted(set(expected_sources))) != expected_sources or not expected_sources:
        raise CrossReferenceRecoveryValidationError(
            "retained shard source scope must be non-empty, unique, and canonical"
        )
    if "sources" in payload:
        expected_keys = {"schema_version", "batch_id", "strategy", "sources"}
        if set(payload) != expected_keys:
            raise CrossReferenceRecoveryValidationError(
                "nested frozen recovery record envelope changed"
            )
        strategy = payload.get("strategy")
        raw_sources = _list_of_objects(payload.get("sources"), "frozen dossier sources")
        observed_sources = tuple(str(item.get("source_id", "")) for item in raw_sources)
        if (
            not isinstance(strategy, str)
            or not strategy.strip()
            or observed_sources != expected_sources
        ):
            raise CrossReferenceRecoveryValidationError(
                "frozen recovery dossier source order differs from the retained shard"
            )
        dossiers: list[Dossier] = []
        fingerprints: set[str] = set()
        for source_id, source in zip(expected_sources, raw_sources, strict=True):
            if set(source) != {"source_id", "input_fingerprint", "dossier"}:
                raise CrossReferenceRecoveryValidationError(
                    f"frozen recovery dossier envelope changed: {source_id}"
                )
            input_fingerprint = source.get("input_fingerprint")
            if (
                not isinstance(input_fingerprint, str)
                or len(input_fingerprint) != 64
                or any(character not in "0123456789abcdef" for character in input_fingerprint)
                or input_fingerprint in fingerprints
            ):
                raise CrossReferenceRecoveryValidationError(
                    f"frozen recovery input fingerprint is invalid: {source_id}"
                )
            dossier_value = _object(source.get("dossier"), "frozen source dossier")
            if set(dossier_value) != {
                "schema_version",
                "source_id",
                "title",
                "summary",
                "records",
            }:
                raise CrossReferenceRecoveryValidationError(
                    f"frozen recovery dossier shape changed: {source_id}"
                )
            try:
                dossier = Dossier.from_mapping(dossier_value)
            except RecordError as error:
                raise CrossReferenceRecoveryValidationError(
                    f"frozen recovery dossier is invalid: {source_id}: {error}"
                ) from error
            if source.get("source_id") != source_id or dossier.source_id != source_id:
                raise CrossReferenceRecoveryValidationError(
                    f"frozen recovery dossier identity changed: {source_id}"
                )
            dossiers.append(dossier)
            fingerprints.add(input_fingerprint)
        raw_records: Sequence[Mapping[str, Any]] = tuple(
            record.to_dict() for dossier in dossiers for record in dossier.records
        )
    else:
        expected_keys = {"schema_version", "batch_id", "source_ids", "records"}
        if set(payload) != expected_keys or payload.get("source_ids") != list(expected_sources):
            raise CrossReferenceRecoveryValidationError(
                "flat frozen recovery record identity differs from the retained shard"
            )
        raw_records = tuple(
            _object(value, "frozen record")
            for value in _array(payload.get("records"), "frozen records")
        )
    records: dict[str, Record] = {}
    evidence: dict[str, set[str]] = {source_id: set() for source_id in expected_sources}
    for value in raw_records:
        try:
            record = Record.from_mapping(_object(value, "frozen record"))
        except RecordError as error:
            raise CrossReferenceRecoveryValidationError(
                f"frozen recovery record is invalid: {error}"
            ) from error
        if record.source_id not in expected_sources or record.id in records:
            raise CrossReferenceRecoveryValidationError(
                "frozen records contain changed scope or duplicate identifiers"
            )
        records[record.id] = record
        if record.record_type == "evidence":
            evidence[record.source_id].add(record.id)
    for record in records.values():
        raw_evidence_ids = record.payload.get("evidence_ids")
        if raw_evidence_ids is None:
            continue
        if (
            not isinstance(raw_evidence_ids, list)
            or not all(isinstance(item, str) and item for item in raw_evidence_ids)
            or len(set(raw_evidence_ids)) != len(raw_evidence_ids)
            or not set(raw_evidence_ids).issubset(evidence[record.source_id])
        ):
            raise CrossReferenceRecoveryValidationError(
                f"frozen record evidence bindings are invalid: {record.id}"
            )
    return records, {source_id: frozenset(values) for source_id, values in evidence.items()}


def _graph_records(
    graph: Mapping[str, Any],
) -> tuple[dict[str, Record], dict[str, frozenset[str]]]:
    records: dict[str, Record] = {}
    evidence: dict[str, set[str]] = {
        str(source_id): set() for source_id in _array(graph.get("sources"), "graph sources")
    }
    for value in _list_of_objects(graph.get("nodes"), "graph nodes"):
        payload = _object(value.get("payload"), "graph node payload")
        record = Record.from_mapping(
            {
                "id": value.get("id"),
                "record_type": value.get("record_type"),
                "revision": value.get("revision"),
                **dict(payload),
            }
        )
        if value.get("source_id") != record.source_id or record.id in records:
            raise CrossReferenceRecoveryValidationError(
                "origin graph contains changed or duplicate records"
            )
        records[record.id] = record
        if record.record_type == "evidence":
            evidence[record.source_id].add(record.id)
    return records, {source_id: frozenset(values) for source_id, values in evidence.items()}


def _materialize_relationship_projection(
    root: Path, relationships: tuple[Relationship, ...]
) -> tuple[Path, ...]:
    grouped: dict[tuple[str, str], list[Relationship]] = {}
    for relationship in relationships:
        sources = sorted(
            (
                str(relationship.payload["left_endpoint"]["source_id"]),
                str(relationship.payload["right_endpoint"]["source_id"]),
            )
        )
        canonical = (sources[0], sources[1])
        grouped.setdefault(canonical, []).append(relationship)
    paths = []
    for pair, values in sorted(grouped.items()):
        path = root / f"{pair[0]}--{pair[1]}" / "relationships.md"
        materialize_relationship_markdown(
            path,
            source_pair=pair,
            relationships=tuple(values),
        )
        paths.append(path)
    return tuple(paths)


def _artifact_descriptor(kind: str, path: Path, *, relative_to: Path) -> dict[str, Any]:
    value = fingerprint(path, relative_to=relative_to)
    return {"kind": kind, **value.to_dict()}


def _write_bytes_stable(path: Path, data: bytes) -> None:
    if path.exists():
        if path.is_file() and path.read_bytes() == data:
            return
        raise CrossReferenceRecoveryValidationError(
            f"existing recovery artifact differs from deterministic rebuild: {path}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _write_stable_receipt(path: Path, payload: Mapping[str, Any]) -> None:
    document = {"schema_version": "1.0", **dict(payload)}
    data = canonical_json_bytes(document)
    if path.exists() and path.read_bytes() != data:
        raise CrossReferenceRecoveryValidationError(
            f"existing recovery receipt differs from deterministic rebuild: {path}"
        )
    _write_bytes_stable(path, data)


def _write_receipt_document(path: Path, payload: Mapping[str, Any]) -> None:
    if payload.get("schema_version") != "1.0":
        raise CrossReferenceRecoveryValidationError("receipt schema version changed")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(payload))


def _pair_outcomes(
    proposal: Mapping[str, Any],
    report: MultiSurfaceValidationReport,
    *,
    owned_pairs: Sequence[tuple[str, str]],
) -> list[dict[str, Any]]:
    surface_pair: dict[str, str] = {}
    ledgers = _array(proposal.get("pair_coverage"), "pair coverage")
    ledger_by_key: dict[str, Mapping[str, Any]] = {}
    for value in ledgers:
        ledger = _object(value, "pair ledger")
        pair_key = str(ledger.get("pair_key", ""))
        ledger_by_key[pair_key] = ledger
        for surface_key in ledger.get("comparison_surface_keys", []):
            if isinstance(surface_key, str):
                surface_pair[surface_key] = pair_key
    unresolved_pairs: set[str] = set()
    for finding in report.findings:
        if finding.scope == "fatal":
            unresolved_pairs.update(_pair_key(*pair) for pair in owned_pairs)
        elif finding.pair_key is not None:
            unresolved_pairs.add(finding.pair_key)
        elif finding.surface_key is not None and finding.surface_key in surface_pair:
            unresolved_pairs.add(surface_pair[finding.surface_key])
    outcomes = []
    for pair in owned_pairs:
        pair_key = _pair_key(*pair)
        ledger = ledger_by_key.get(pair_key, {})
        outcomes.append(
            {
                "pair_key": pair_key,
                "source_ids": list(pair),
                "status": "unresolved" if pair_key in unresolved_pairs else "inspected",
                "ledger_disposition": ledger.get("disposition"),
                "comparison_surface_keys": list(
                    ledger.get("comparison_surface_keys", [])
                    if isinstance(ledger.get("comparison_surface_keys", []), list)
                    else []
                ),
            }
        )
    return outcomes


def _failed_pair_outcomes(owned_pairs: Sequence[tuple[str, str]]) -> list[dict[str, Any]]:
    return [
        {
            "pair_key": _pair_key(*pair),
            "source_ids": list(pair),
            "status": "shard_failed",
            "ledger_disposition": None,
            "comparison_surface_keys": [],
        }
        for pair in owned_pairs
    ]


def _aggregate_telemetry(shards: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    usage = _empty_token_usage()
    duration = 0.0
    events = 0
    for shard in shards:
        telemetry = _object(shard.get("telemetry"), "shard telemetry")
        duration += float(telemetry.get("duration_seconds", 0.0))
        events += int(telemetry.get("worker_events_emitted", 0))
        shard_usage = _object(telemetry.get("token_usage"), "token usage")
        for key in usage:
            usage[key] += int(shard_usage.get(key, 0))
    return {
        "duration_seconds": duration,
        "worker_events_emitted": events,
        "token_usage": usage,
    }


def _event_token_usage(path: Path) -> dict[str, int]:
    usage = _empty_token_usage()
    if not path.is_file():
        return usage
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        raw = event.get("usage") if isinstance(event, dict) else None
        if not isinstance(raw, dict):
            continue
        for key in usage:
            value = raw.get(key, 0)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                usage[key] += value
    return usage


def _empty_token_usage() -> dict[str, int]:
    return {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}


def _resolve_root(value: Any, *, repository_root: Path) -> Path:
    path = Path(str(value))
    return (path if path.is_absolute() else repository_root / path).resolve()


def _canonical_pair(value: Any, *, source_ids: Sequence[str]) -> tuple[str, str]:
    raw = _array(value, "source pair")
    if len(raw) != 2 or not all(isinstance(item, str) for item in raw):
        raise CrossReferenceRecoveryValidationError("source pair must contain two source IDs")
    pair = (str(raw[0]), str(raw[1]))
    if pair[0] >= pair[1] or not set(pair).issubset(source_ids):
        raise CrossReferenceRecoveryValidationError(
            "source pair must be canonical and inside the shard scope"
        )
    return pair


def _pair_key(left: str, right: str) -> str:
    return f"pair-{left.lower()}-{right.lower()}"


def _correction_job_id(recovery_id: str, shard_id: str) -> str:
    return stable_id("xref_job", (recovery_id, shard_id, "correction", "1"))


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CrossReferenceRecoveryValidationError(f"cannot read JSON object: {path}") from error
    return dict(_object(value, str(path)))


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise CrossReferenceRecoveryValidationError(f"{label} must be an object")
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise CrossReferenceRecoveryValidationError(f"{label} must be an array")
    return value


def _list_of_objects(value: Any, label: str) -> list[Mapping[str, Any]]:
    return [_object(item, label) for item in _array(value, label)]
