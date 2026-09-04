"""Read-only quality and coverage benchmarking for frozen cross-reference maps."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from research_map.codex import CodexCapabilityError, CodexJob, CodexRunner
from research_map.cross_reference import (
    DIRECTED_RELATIONSHIP_TYPES,
    RELATIONSHIP_DIRECTIONS,
    RELATIONSHIP_TYPES,
    SYMMETRIC_RELATIONSHIP_TYPES,
    Relationship,
    record_fingerprint,
)
from research_map.markdown import MarkdownRecordError, load_markdown
from research_map.receipts import canonical_json_bytes, fingerprint, read_receipt
from research_map.records import Dossier, Record
from research_map.relationship_markdown import RelationshipMarkdownError, load_relationship_markdown
from research_map.workspace import CrossReferenceWorkspaceBuilder, WorkspaceError, WorkspaceInput

BENCHMARK_SCHEMA_VERSION = "1.0"
BENCHMARK_CONTRACT_VERSION = "cross-reference-quality-v1"
DEFAULT_AUDIT_SAMPLE_SIZE = 30
RARE_STRATUM_MAXIMUM = 3
AUDIT_SCORE_FIELDS = (
    "evidence_grounding",
    "relation_type_fit",
    "scope_assumption_fit",
    "research_utility",
    "argumentative_importance",
    "distinctiveness",
)
AUDIT_DISPOSITIONS = frozenset({"strong", "usable", "weak", "misleading", "manual_review"})
RELATION_TYPE_ASSESSMENTS = frozenset({"fit", "adjacent", "wrong", "uncertain"})
MATCH_KINDS = frozenset({"exact_endpoint", "semantic_near", "partial", "none"})
STAGE_DEFINITIONS = {
    "edge-audit": (
        "cross_reference_benchmark_audit",
        "cross-reference-edge-audit-benchmark.schema.json",
        "cross-reference-edge-audit-benchmark.md",
    ),
    "blind-probe": (
        "cross_reference_benchmark_blind_probe",
        "cross-reference-blind-probe-benchmark.schema.json",
        "cross-reference-blind-probe-benchmark.md",
    ),
    "probe-comparison": (
        "cross_reference_benchmark_comparison",
        "cross-reference-probe-comparison-benchmark.schema.json",
        "cross-reference-probe-comparison-benchmark.md",
    ),
}
_ADJUDICATIVE_PHRASES = (
    " is correct",
    " is incorrect",
    " is wrong",
    "proves that",
    "disproves",
    "must be rejected",
    "the correct theory",
    "the true account",
    "fills the missing premise",
)
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SOURCE_ID = re.compile(r"^LIB-[0-9]{3}$")


class CrossReferenceBenchmarkError(ValueError):
    """Raised when frozen benchmark inputs or semantic outputs are invalid."""


class CrossReferenceBenchmarkExecutionError(RuntimeError):
    """Raised when a bounded Codex benchmark stage fails terminally."""


@dataclass(frozen=True, slots=True)
class FrozenBenchmarkCorpus:
    batch_id: str
    source_ids: tuple[str, ...]
    dossiers: Mapping[str, Dossier]
    relationships: tuple[Relationship, ...]
    record_index: Mapping[str, Record]
    evidence_ids_by_source: Mapping[str, frozenset[str]]
    graph_path: Path
    input_fingerprints: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class AuditSample:
    benchmark_id: str
    sample_size_requested: int
    independent_relationships: tuple[Relationship, ...]
    family_relationships: tuple[Relationship, ...]
    selected: tuple[Relationship, ...]
    population_by_type: Mapping[str, int]
    sample_by_type: Mapping[str, int]
    source_families: tuple[tuple[str, ...], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": BENCHMARK_SCHEMA_VERSION,
            "contract_version": BENCHMARK_CONTRACT_VERSION,
            "benchmark_id": self.benchmark_id,
            "sample_size_requested": self.sample_size_requested,
            "population_size": len(self.independent_relationships),
            "family_relationship_count": len(self.family_relationships),
            "source_families": [list(group) for group in self.source_families],
            "population_by_type": dict(sorted(self.population_by_type.items())),
            "sample_by_type": dict(sorted(self.sample_by_type.items())),
            "selected_relationship_ids": [item.relationship_id for item in self.selected],
        }


@dataclass(frozen=True, slots=True)
class CrossReferenceBenchmarkResult:
    benchmark_id: str
    batch_id: str
    root: Path
    metrics_path: Path
    report_path: Path
    semantic_calls: int
    reused_stages: tuple[str, ...]
    input_fingerprint_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "benchmark_id": self.benchmark_id,
            "batch_id": self.batch_id,
            "root": str(self.root),
            "metrics_path": str(self.metrics_path),
            "report_path": str(self.report_path),
            "semantic_calls": self.semantic_calls,
            "reused_stages": list(self.reused_stages),
            "input_fingerprint_sha256": self.input_fingerprint_sha256,
        }


def load_frozen_benchmark_corpus(calibration_root: Path, *, batch_id: str) -> FrozenBenchmarkCorpus:
    """Load canonical dossiers and relationships without writing to the input root."""

    _require_safe_identifier(batch_id, label="batch ID")
    root = calibration_root.resolve()
    graph_path = root / "build" / "corpus" / batch_id / "graph.json"
    try:
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CrossReferenceBenchmarkError(
            f"benchmark graph is unreadable: {graph_path}"
        ) from error
    if not isinstance(graph, dict) or graph.get("batch_id") != batch_id:
        raise CrossReferenceBenchmarkError("benchmark graph batch identity does not match")
    raw_source_ids = graph.get("sources")
    if (
        not isinstance(raw_source_ids, list)
        or len(raw_source_ids) < 2
        or not all(isinstance(item, str) and _SOURCE_ID.fullmatch(item) for item in raw_source_ids)
    ):
        raise CrossReferenceBenchmarkError("benchmark graph source inventory is invalid")
    source_ids = tuple(sorted(set(raw_source_ids)))
    if list(source_ids) != raw_source_ids:
        raise CrossReferenceBenchmarkError("benchmark graph sources are not canonical")

    dossiers: dict[str, Dossier] = {}
    records: dict[str, Record] = {}
    evidence_ids: dict[str, frozenset[str]] = {}
    fingerprints: list[dict[str, Any]] = [fingerprint(graph_path, relative_to=root).to_dict()]
    for source_id in source_ids:
        dossier_path = root / "vault" / "sources" / source_id / "paper.md"
        try:
            dossier_document = load_markdown(dossier_path)
        except MarkdownRecordError as error:
            raise CrossReferenceBenchmarkError(
                f"benchmark dossier is invalid: {source_id}: {error}"
            ) from error
        if dossier_document.dossier.source_id != source_id:
            raise CrossReferenceBenchmarkError(f"dossier source identity changed: {source_id}")
        dossiers[source_id] = dossier_document.dossier
        for record in dossier_document.dossier.records:
            if record.id in records:
                raise CrossReferenceBenchmarkError(f"duplicate corpus record ID: {record.id}")
            records[record.id] = record
        evidence_ids[source_id] = frozenset(
            item.id for item in dossier_document.dossier.records if item.record_type == "evidence"
        )
        fingerprints.append(fingerprint(dossier_path, relative_to=root).to_dict())

    relationships: dict[str, Relationship] = {}
    relationship_root = root / "vault" / "relationships"
    for relationship_path in sorted(relationship_root.glob("*/relationships.md")):
        try:
            relationship_document = load_relationship_markdown(relationship_path)
        except RelationshipMarkdownError as error:
            raise CrossReferenceBenchmarkError(
                f"benchmark relationship Markdown is invalid: {relationship_path}: {error}"
            ) from error
        fingerprints.append(fingerprint(relationship_path, relative_to=root).to_dict())
        for relationship in relationship_document.relationships:
            if relationship.batch_id != batch_id:
                continue
            if relationship.relationship_id in relationships:
                raise CrossReferenceBenchmarkError(
                    f"duplicate benchmark relationship ID: {relationship.relationship_id}"
                )
            _validate_relationship_records(
                relationship,
                records=records,
                evidence_ids_by_source=evidence_ids,
            )
            relationships[relationship.relationship_id] = relationship

    raw_edges = graph.get("edges")
    if not isinstance(raw_edges, list):
        raise CrossReferenceBenchmarkError("benchmark graph edge inventory is invalid")
    graph_cross_edges: dict[str, dict[str, Any]] = {}
    for edge in raw_edges:
        if not isinstance(edge, dict) or edge.get("relationship_id") is None:
            continue
        relationship_id = str(edge["relationship_id"])
        if relationship_id in graph_cross_edges:
            raise CrossReferenceBenchmarkError(
                f"duplicate compiled relationship edge: {relationship_id}"
            )
        graph_cross_edges[relationship_id] = {
            "source": edge.get("source"),
            "target": edge.get("target"),
            "kind": edge.get("kind"),
            "origin": edge.get("origin"),
            "relationship_id": relationship_id,
        }
    expected_graph_edges: dict[str, dict[str, Any]] = {}
    for relationship in relationships.values():
        left_id = relationship.payload["left_endpoint"]["record_id"]
        right_id = relationship.payload["right_endpoint"]["record_id"]
        source, target = (
            (right_id, left_id)
            if relationship.payload["direction"] == "right_to_left"
            else (left_id, right_id)
        )
        expected_graph_edges[relationship.relationship_id] = {
            "source": source,
            "target": target,
            "kind": relationship.relation_type,
            "origin": "cross_source",
            "relationship_id": relationship.relationship_id,
        }
    if graph_cross_edges != expected_graph_edges:
        raise CrossReferenceBenchmarkError(
            "canonical relationship Markdown and compiled graph projections differ"
        )
    if not relationships:
        raise CrossReferenceBenchmarkError("benchmark corpus contains no relationships")
    return FrozenBenchmarkCorpus(
        batch_id=batch_id,
        source_ids=source_ids,
        dossiers=dossiers,
        relationships=tuple(relationships[key] for key in sorted(relationships)),
        record_index=records,
        evidence_ids_by_source=evidence_ids,
        graph_path=graph_path,
        input_fingerprints=tuple(sorted(fingerprints, key=lambda item: str(item["path"]))),
    )


class CrossReferenceBenchmarkCoordinator:
    """Coordinate three bounded read-only semantic evaluations over one frozen map."""

    def __init__(
        self,
        *,
        repository_root: Path,
        calibration_root: Path,
        output_root: Path,
        codex_executable: str = "codex",
        timeout_seconds: float = 5400,
    ) -> None:
        self.repository_root = repository_root.resolve()
        self.calibration_root = calibration_root.resolve()
        self.output_root = output_root.resolve()
        self.codex_executable = codex_executable
        self.timeout_seconds = timeout_seconds
        self.schema_directory = self.repository_root / "schemas" / "research-map" / "v1"
        self.protocol_directory = self.repository_root / "protocols" / "research-map" / "v1"
        self._codex_runner: CodexRunner | None = None

    def run(
        self,
        *,
        batch_id: str,
        benchmark_id: str,
        model: str,
        coverage_source_ids: Sequence[str],
        source_families: Sequence[Sequence[str]],
        generation_effective_tokens: int,
        audit_sample_size: int = DEFAULT_AUDIT_SAMPLE_SIZE,
    ) -> CrossReferenceBenchmarkResult:
        if not model.strip():
            raise CrossReferenceBenchmarkError("benchmark model is required")
        if self.timeout_seconds <= 0:
            raise CrossReferenceBenchmarkError("benchmark timeout must be positive")
        corpus = load_frozen_benchmark_corpus(self.calibration_root, batch_id=batch_id)
        coverage_sources = tuple(coverage_source_ids)
        if (
            len(coverage_sources) != 5
            or tuple(sorted(set(coverage_sources))) != coverage_sources
            or set(coverage_sources) - set(corpus.source_ids)
        ):
            raise CrossReferenceBenchmarkError(
                "coverage probe requires exactly five unique canonical corpus sources"
            )
        sample = select_audit_sample(
            corpus.relationships,
            benchmark_id=benchmark_id,
            source_families=source_families,
            sample_size=audit_sample_size,
        )
        root = self.output_root / benchmark_id
        root.mkdir(parents=True, exist_ok=True)
        manifest = self._manifest(
            corpus=corpus,
            sample=sample,
            benchmark_id=benchmark_id,
            model=model,
            coverage_source_ids=coverage_sources,
            generation_effective_tokens=generation_effective_tokens,
        )
        manifest_path = root / "manifest.json"
        _write_or_verify_manifest(manifest_path, manifest)
        manifest_sha256 = fingerprint(manifest_path).sha256

        semantic_calls = 0
        reused_stages: list[str] = []
        audit_payload, reused = self._run_stage(
            root=root,
            stage="edge-audit",
            benchmark_id=benchmark_id,
            model=model,
            source_ids=corpus.source_ids,
            inputs=self._audit_inputs(corpus=corpus, sample=sample, benchmark_id=benchmark_id),
        )
        semantic_calls += int(not reused)
        if reused:
            reused_stages.append("edge-audit")
        audit_items = validate_audit_output(
            audit_payload,
            benchmark_id=benchmark_id,
            expected_relationship_ids=[item.relationship_id for item in sample.selected],
        )
        self._record_stage_validation(
            root=root,
            stage="edge-audit",
            output_path=root / "jobs" / "edge-audit" / "output" / "result.json",
            validated_ids=[str(item["relationship_id"]) for item in audit_items],
        )

        probe_payload, reused = self._run_stage(
            root=root,
            stage="blind-probe",
            benchmark_id=benchmark_id,
            model=model,
            source_ids=coverage_sources,
            inputs=self._blind_probe_inputs(
                corpus=corpus,
                benchmark_id=benchmark_id,
                coverage_source_ids=coverage_sources,
            ),
        )
        semantic_calls += int(not reused)
        if reused:
            reused_stages.append("blind-probe")
        probe_candidates = validate_blind_probe_output(
            probe_payload,
            benchmark_id=benchmark_id,
            coverage_source_ids=coverage_sources,
            records=corpus.record_index,
            evidence_ids_by_source=corpus.evidence_ids_by_source,
        )
        self._record_stage_validation(
            root=root,
            stage="blind-probe",
            output_path=root / "jobs" / "blind-probe" / "output" / "result.json",
            validated_ids=[str(item["candidate_key"]) for item in probe_candidates],
        )

        current_cohort_relationships = cohort_relationships(
            sample.independent_relationships, source_ids=coverage_sources
        )
        comparison_payload, reused = self._run_stage(
            root=root,
            stage="probe-comparison",
            benchmark_id=benchmark_id,
            model=model,
            source_ids=coverage_sources,
            inputs=self._comparison_inputs(
                corpus=corpus,
                benchmark_id=benchmark_id,
                coverage_source_ids=coverage_sources,
                probe_payload=probe_payload,
                relationships=current_cohort_relationships,
            ),
        )
        semantic_calls += int(not reused)
        if reused:
            reused_stages.append("probe-comparison")
        comparison_items = validate_probe_comparison_output(
            comparison_payload,
            benchmark_id=benchmark_id,
            expected_candidate_keys=[str(item["candidate_key"]) for item in probe_candidates],
            allowed_relationship_ids=[
                item.relationship_id for item in current_cohort_relationships
            ],
        )
        self._record_stage_validation(
            root=root,
            stage="probe-comparison",
            output_path=root / "jobs" / "probe-comparison" / "output" / "result.json",
            validated_ids=[str(item["candidate_key"]) for item in comparison_items],
        )

        after = load_frozen_benchmark_corpus(self.calibration_root, batch_id=batch_id)
        if after.input_fingerprints != corpus.input_fingerprints:
            raise CrossReferenceBenchmarkError("frozen benchmark inputs changed during evaluation")
        metrics = compile_benchmark_metrics(
            benchmark_id=benchmark_id,
            sample=sample,
            audit_items=audit_items,
            probe_candidates=probe_candidates,
            comparison_items=comparison_items,
            generation_effective_tokens=generation_effective_tokens,
        )
        metrics["cost"]["evaluation_usage"] = self._evaluation_usage(root)
        metrics["reproducibility"] = {
            "batch_id": batch_id,
            "model": model,
            "manifest_sha256": manifest_sha256,
            "input_fingerprint_sha256": _fingerprint_inventory(corpus.input_fingerprints),
            "stage_output_sha256": {
                stage: fingerprint(root / "jobs" / stage / "output" / "result.json").sha256
                for stage in STAGE_DEFINITIONS
            },
            "stage_receipt_sha256": {
                stage: fingerprint(root / "receipts" / f"{stage}.json").sha256
                for stage in STAGE_DEFINITIONS
            },
            "stage_validation_sha256": {
                stage: fingerprint(root / "validations" / f"{stage}.json").sha256
                for stage in STAGE_DEFINITIONS
            },
        }
        metrics_path = root / "metrics.json"
        _validate_json_schema(
            metrics,
            schema_path=self.schema_directory / "cross-reference-benchmark-report.schema.json",
        )
        _write_or_verify_json(metrics_path, metrics)
        report_path = root / "report.md"
        _write_or_verify_bytes(
            report_path,
            render_benchmark_report(
                metrics, sample=sample, coverage_sources=coverage_sources
            ).encode(),
        )
        _write_or_verify_portable_json(
            root / "benchmark-receipt.json",
            {
                "schema_version": BENCHMARK_SCHEMA_VERSION,
                "kind": "cross_reference_quality_benchmark",
                "contract_version": BENCHMARK_CONTRACT_VERSION,
                "benchmark_id": benchmark_id,
                "batch_id": batch_id,
                "manifest": fingerprint(manifest_path, relative_to=root).to_dict(),
                "metrics": fingerprint(metrics_path, relative_to=root).to_dict(),
                "report": fingerprint(report_path, relative_to=root).to_dict(),
                "frozen_inputs_unchanged": True,
                "semantic_stage_count": len(STAGE_DEFINITIONS),
                "map_mutation_authorized": False,
            },
        )
        return CrossReferenceBenchmarkResult(
            benchmark_id=benchmark_id,
            batch_id=batch_id,
            root=root,
            metrics_path=metrics_path,
            report_path=report_path,
            semantic_calls=semantic_calls,
            reused_stages=tuple(reused_stages),
            input_fingerprint_sha256=_fingerprint_inventory(corpus.input_fingerprints),
        )

    def _manifest(
        self,
        *,
        corpus: FrozenBenchmarkCorpus,
        sample: AuditSample,
        benchmark_id: str,
        model: str,
        coverage_source_ids: tuple[str, ...],
        generation_effective_tokens: int,
    ) -> dict[str, Any]:
        stage_contracts = {}
        for stage, (_, schema_name, protocol_name) in STAGE_DEFINITIONS.items():
            stage_contracts[stage] = {
                "schema": fingerprint(
                    self.schema_directory / schema_name,
                    relative_to=self.repository_root,
                ).to_dict(),
                "protocol": fingerprint(
                    self.protocol_directory / protocol_name,
                    relative_to=self.repository_root,
                ).to_dict(),
            }
        return {
            "schema_version": BENCHMARK_SCHEMA_VERSION,
            "contract_version": BENCHMARK_CONTRACT_VERSION,
            "benchmark_id": benchmark_id,
            "batch_id": corpus.batch_id,
            "model": model,
            "source_ids": list(corpus.source_ids),
            "coverage_source_ids": list(coverage_source_ids),
            "generation_effective_tokens": generation_effective_tokens,
            "audit_sample": sample.to_dict(),
            "input_fingerprints": list(corpus.input_fingerprints),
            "input_fingerprint_sha256": _fingerprint_inventory(corpus.input_fingerprints),
            "stage_contracts": stage_contracts,
            "semantic_stage_order": list(STAGE_DEFINITIONS),
            "map_mutation_authorized": False,
        }

    def _audit_inputs(
        self,
        *,
        corpus: FrozenBenchmarkCorpus,
        sample: AuditSample,
        benchmark_id: str,
    ) -> tuple[WorkspaceInput, ...]:
        task = {
            "schema_version": BENCHMARK_SCHEMA_VERSION,
            "stage": "cross-reference-edge-quality-audit",
            "benchmark_id": benchmark_id,
            "batch_id": corpus.batch_id,
            "source_ids": list(corpus.source_ids),
            "selected_relationship_ids": [item.relationship_id for item in sample.selected],
            "rubric_score_range": [0, 4],
            "score_fields": list(AUDIT_SCORE_FIELDS),
            "map_mutation_authorized": False,
        }
        inputs = [
            self._protocol_input(benchmark_id, "edge-audit"),
            WorkspaceInput(
                source_id=benchmark_id,
                destination="task.json",
                kind="benchmark_task",
                content=canonical_json_bytes(task),
            ),
            WorkspaceInput(
                source_id=benchmark_id,
                destination="audit-sample.json",
                kind="benchmark_audit_sample",
                content=canonical_json_bytes(
                    {
                        **sample.to_dict(),
                        "relationships": [item.to_dict() for item in sample.selected],
                    }
                ),
            ),
        ]
        inputs.extend(self._dossier_inputs(corpus, corpus.source_ids, benchmark_id=benchmark_id))
        return tuple(inputs)

    def _blind_probe_inputs(
        self,
        *,
        corpus: FrozenBenchmarkCorpus,
        benchmark_id: str,
        coverage_source_ids: tuple[str, ...],
    ) -> tuple[WorkspaceInput, ...]:
        task = {
            "schema_version": BENCHMARK_SCHEMA_VERSION,
            "stage": "cross-reference-blind-coverage-probe",
            "benchmark_id": benchmark_id,
            "batch_id": corpus.batch_id,
            "source_ids": list(coverage_source_ids),
            "current_relationships_available": False,
            "map_mutation_authorized": False,
        }
        return (
            self._protocol_input(benchmark_id, "blind-probe"),
            WorkspaceInput(
                source_id=benchmark_id,
                destination="task.json",
                kind="benchmark_task",
                content=canonical_json_bytes(task),
            ),
            *self._dossier_inputs(corpus, coverage_source_ids, benchmark_id=benchmark_id),
        )

    def _comparison_inputs(
        self,
        *,
        corpus: FrozenBenchmarkCorpus,
        benchmark_id: str,
        coverage_source_ids: tuple[str, ...],
        probe_payload: Mapping[str, Any],
        relationships: Sequence[Relationship],
    ) -> tuple[WorkspaceInput, ...]:
        task = {
            "schema_version": BENCHMARK_SCHEMA_VERSION,
            "stage": "cross-reference-probe-graph-comparison",
            "benchmark_id": benchmark_id,
            "batch_id": corpus.batch_id,
            "source_ids": list(coverage_source_ids),
            "candidate_keys": [
                str(item["candidate_key"])
                for item in probe_payload.get("candidates", [])
                if isinstance(item, dict)
            ],
            "map_mutation_authorized": False,
        }
        return (
            self._protocol_input(benchmark_id, "probe-comparison"),
            WorkspaceInput(
                source_id=benchmark_id,
                destination="task.json",
                kind="benchmark_task",
                content=canonical_json_bytes(task),
            ),
            WorkspaceInput(
                source_id=benchmark_id,
                destination="probe-candidates.json",
                kind="blind_probe_output",
                content=canonical_json_bytes(probe_payload),
            ),
            WorkspaceInput(
                source_id=benchmark_id,
                destination="current-relationships.json",
                kind="benchmark_current_relationships",
                content=canonical_json_bytes(
                    {
                        "schema_version": BENCHMARK_SCHEMA_VERSION,
                        "relationships": [item.to_dict() for item in relationships],
                    }
                ),
            ),
            *self._dossier_inputs(corpus, coverage_source_ids, benchmark_id=benchmark_id),
        )

    def _dossier_inputs(
        self,
        corpus: FrozenBenchmarkCorpus,
        source_ids: Sequence[str],
        *,
        benchmark_id: str,
    ) -> tuple[WorkspaceInput, ...]:
        return tuple(
            WorkspaceInput(
                source_id=benchmark_id,
                destination=f"dossiers/{source_id}.md",
                kind="canonical_source_dossier",
                source_path=self.calibration_root / "vault" / "sources" / source_id / "paper.md",
            )
            for source_id in source_ids
        )

    def _protocol_input(self, benchmark_id: str, stage: str) -> WorkspaceInput:
        protocol_name = STAGE_DEFINITIONS[stage][2]
        return WorkspaceInput(
            source_id=benchmark_id,
            destination="AGENTS.md",
            kind="benchmark_protocol",
            area="root",
            source_path=self.protocol_directory / protocol_name,
        )

    def _run_stage(
        self,
        *,
        root: Path,
        stage: str,
        benchmark_id: str,
        model: str,
        source_ids: tuple[str, ...],
        inputs: tuple[WorkspaceInput, ...],
    ) -> tuple[dict[str, Any], bool]:
        workspace_kind, schema_name, _ = STAGE_DEFINITIONS[stage]
        try:
            workspace = CrossReferenceWorkspaceBuilder(
                root / "jobs" / stage,
                batch_id=benchmark_id,
                job_id=_stable_job_id(benchmark_id, stage),
                source_ids=source_ids,
            ).build(kind=workspace_kind, inputs=inputs)
        except WorkspaceError as error:
            raise CrossReferenceBenchmarkError(
                f"benchmark stage {stage} workspace is invalid: {error}"
            ) from error
        output_path = workspace.path / "output" / "result.json"
        events_path = root / "events" / f"{stage}.jsonl"
        receipt_path = root / "receipts" / f"{stage}.json"
        schema_path = self.schema_directory / schema_name
        existing = (output_path.exists(), events_path.exists(), receipt_path.exists())
        if any(existing):
            if not all(existing):
                raise CrossReferenceBenchmarkError(
                    f"retained {stage} stage is incomplete; refusing semantic replacement"
                )
            payload = self._validate_retained_stage(
                stage=stage,
                workspace_manifest=workspace.manifest_path,
                output_path=output_path,
                events_path=events_path,
                receipt_path=receipt_path,
                schema_path=schema_path,
                model=model,
            )
            return payload, True

        if self._codex_runner is None:
            try:
                self._codex_runner = CodexRunner(self.codex_executable)
            except CodexCapabilityError as error:
                raise CrossReferenceBenchmarkExecutionError(str(error)) from error
        result = self._codex_runner.run(
            CodexJob(
                job_id=_stable_job_id(benchmark_id, stage),
                source_id=benchmark_id,
                model=model,
                prompt=(
                    "Read AGENTS.md and input/task.json. Inspect every declared input needed "
                    "for this benchmark stage. Return exactly the schema-constrained result."
                ),
                workspace=workspace.path,
                output_schema=schema_path,
                events_path=events_path,
                last_message_path=output_path,
                receipt_path=receipt_path,
                timeout_seconds=self.timeout_seconds,
            )
        )
        if not result.output_valid:
            raise CrossReferenceBenchmarkExecutionError(
                f"benchmark stage {stage} failed: {result.failure_class}; "
                f"errors={list(result.validation_errors)}"
            )
        payload = _read_json_object(output_path, label=f"{stage} output")
        _validate_json_schema(payload, schema_path=schema_path)
        return payload, False

    def _validate_retained_stage(
        self,
        *,
        stage: str,
        workspace_manifest: Path,
        output_path: Path,
        events_path: Path,
        receipt_path: Path,
        schema_path: Path,
        model: str,
    ) -> dict[str, Any]:
        try:
            receipt = read_receipt(receipt_path)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise CrossReferenceBenchmarkError(f"retained {stage} receipt is unreadable") from error
        if receipt.get("kind") != "codex_job" or receipt.get("model") != model:
            raise CrossReferenceBenchmarkError(f"retained {stage} receipt identity changed")
        validation = receipt.get("validation")
        if not isinstance(validation, dict) or validation.get("valid") is not True:
            raise CrossReferenceBenchmarkError(f"retained {stage} receipt is not valid")
        if receipt.get("workspace_manifest_sha256") != fingerprint(workspace_manifest).sha256:
            raise CrossReferenceBenchmarkError(f"retained {stage} workspace lineage changed")
        schema_receipt = receipt.get("output_schema")
        if (
            not isinstance(schema_receipt, dict)
            or schema_receipt.get("sha256") != fingerprint(schema_path).sha256
        ):
            raise CrossReferenceBenchmarkError(f"retained {stage} schema lineage changed")
        observed_outputs = {
            (str(item.get("sha256")), int(item.get("size_bytes", -1)))
            for item in receipt.get("outputs", [])
            if isinstance(item, dict)
        }
        for path in (events_path, output_path):
            observed = fingerprint(path)
            if (observed.sha256, observed.size_bytes) not in observed_outputs:
                raise CrossReferenceBenchmarkError(f"retained {stage} output fingerprint changed")
        payload = _read_json_object(output_path, label=f"retained {stage} output")
        _validate_json_schema(payload, schema_path=schema_path)
        return payload

    def _evaluation_usage(self, root: Path) -> dict[str, Any]:
        stages: dict[str, dict[str, Any]] = {}
        totals: dict[str, int | float] = {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "uncached_input_tokens": 0,
            "output_tokens": 0,
            "effective_tokens": 0,
            "duration_seconds": 0.0,
        }
        for stage in STAGE_DEFINITIONS:
            receipt = read_receipt(root / "receipts" / f"{stage}.json")
            events = receipt.get("events")
            last_event = events.get("last_event", {}) if isinstance(events, dict) else {}
            usage = last_event.get("usage", {}) if isinstance(last_event, dict) else {}
            process = receipt.get("process", {})
            input_tokens = _nonnegative_int(usage.get("input_tokens"))
            cached_tokens = _nonnegative_int(usage.get("cached_input_tokens"))
            output_tokens = _nonnegative_int(usage.get("output_tokens"))
            uncached_tokens = max(0, input_tokens - cached_tokens)
            duration = (
                float(process.get("duration_seconds", 0.0)) if isinstance(process, dict) else 0.0
            )
            stage_usage = {
                "input_tokens": input_tokens,
                "cached_input_tokens": cached_tokens,
                "uncached_input_tokens": uncached_tokens,
                "output_tokens": output_tokens,
                "effective_tokens": uncached_tokens + output_tokens,
                "duration_seconds": _rounded(duration),
            }
            stages[stage] = stage_usage
            for field in (
                "input_tokens",
                "cached_input_tokens",
                "uncached_input_tokens",
                "output_tokens",
                "effective_tokens",
            ):
                totals[field] = int(totals[field]) + int(stage_usage[field])
            totals["duration_seconds"] = float(totals["duration_seconds"]) + duration
        totals["duration_seconds"] = _rounded(float(totals["duration_seconds"]))
        return {"semantic_call_count": len(STAGE_DEFINITIONS), "stages": stages, "totals": totals}

    def _record_stage_validation(
        self, *, root: Path, stage: str, output_path: Path, validated_ids: Sequence[str]
    ) -> None:
        _write_or_verify_portable_json(
            root / "validations" / f"{stage}.json",
            {
                "schema_version": BENCHMARK_SCHEMA_VERSION,
                "kind": "cross_reference_benchmark_stage_validation",
                "contract_version": BENCHMARK_CONTRACT_VERSION,
                "stage": stage,
                "output": fingerprint(output_path, relative_to=root).to_dict(),
                "semantic_validation": "passed",
                "validated_item_count": len(validated_ids),
                "validated_ids": list(validated_ids),
                "map_mutation_authorized": False,
            },
        )


def normalize_source_families(
    groups: Sequence[Sequence[str]], *, source_ids: Sequence[str]
) -> tuple[tuple[str, ...], ...]:
    known = set(source_ids)
    normalized: list[tuple[str, ...]] = []
    seen: set[str] = set()
    for raw_group in groups:
        group = tuple(sorted(set(raw_group)))
        if len(group) < 2 or len(group) != len(raw_group):
            raise CrossReferenceBenchmarkError(
                "each source-family group must contain at least two unique sources"
            )
        unknown = sorted(set(group) - known)
        if unknown:
            raise CrossReferenceBenchmarkError(
                f"source-family group contains unknown sources: {unknown}"
            )
        overlap = sorted(set(group) & seen)
        if overlap:
            raise CrossReferenceBenchmarkError(f"source-family groups overlap: {overlap}")
        seen.update(group)
        normalized.append(group)
    return tuple(sorted(normalized))


def select_audit_sample(
    relationships: Sequence[Relationship],
    *,
    benchmark_id: str,
    source_families: Sequence[Sequence[str]] = (),
    sample_size: int = DEFAULT_AUDIT_SAMPLE_SIZE,
) -> AuditSample:
    _require_safe_identifier(benchmark_id, label="benchmark ID")
    if sample_size < 1:
        raise CrossReferenceBenchmarkError("audit sample size must be positive")
    source_ids = sorted(
        {
            str(endpoint["source_id"])
            for relationship in relationships
            for endpoint in (
                relationship.payload["left_endpoint"],
                relationship.payload["right_endpoint"],
            )
        }
    )
    families = normalize_source_families(source_families, source_ids=source_ids)
    family_index = {source_id: group for group in families for source_id in group}
    family: list[Relationship] = []
    independent: list[Relationship] = []
    for relationship in relationships:
        left_source = str(relationship.payload["left_endpoint"]["source_id"])
        right_source = str(relationship.payload["right_endpoint"]["source_id"])
        if family_index.get(left_source) is not None and family_index.get(
            left_source
        ) == family_index.get(right_source):
            family.append(relationship)
        else:
            independent.append(relationship)
    if not independent:
        raise CrossReferenceBenchmarkError("benchmark contains no independent-paper relationships")

    by_type: dict[str, list[Relationship]] = defaultdict(list)
    for relationship in independent:
        by_type[relationship.relation_type].append(relationship)
    target = min(sample_size, len(independent))
    rare_total = sum(len(items) for items in by_type.values() if len(items) <= RARE_STRATUM_MAXIMUM)
    if target < rare_total:
        raise CrossReferenceBenchmarkError(
            "audit sample is too small to include every relationship in rare strata"
        )
    quotas = {
        relation_type: (len(items) if len(items) <= RARE_STRATUM_MAXIMUM else 0)
        for relation_type, items in by_type.items()
    }
    remaining = target - sum(quotas.values())
    common = {
        relation_type: len(items)
        for relation_type, items in by_type.items()
        if len(items) > RARE_STRATUM_MAXIMUM
    }
    if remaining and common:
        total_common = sum(common.values())
        fractional: list[tuple[float, str]] = []
        for relation_type, count in common.items():
            ideal = remaining * count / total_common
            allocation = min(count, math.floor(ideal))
            quotas[relation_type] = allocation
            fractional.append((ideal - allocation, relation_type))
        unallocated = target - sum(quotas.values())
        ordered = [
            relation_type
            for _, relation_type in sorted(fractional, key=lambda item: (-item[0], item[1]))
        ]
        while unallocated:
            progressed = False
            for relation_type in ordered:
                if quotas[relation_type] >= common[relation_type]:
                    continue
                quotas[relation_type] += 1
                unallocated -= 1
                progressed = True
                if unallocated == 0:
                    break
            if not progressed:
                raise CrossReferenceBenchmarkError("audit sample allocation could not be completed")
    if any(quotas[relation_type] == 0 for relation_type in common):
        raise CrossReferenceBenchmarkError(
            "audit sample is too small to include every populated relationship type"
        )

    selected: list[Relationship] = []
    for relation_type, items in sorted(by_type.items()):
        ranked = sorted(
            items,
            key=lambda item: (
                hashlib.sha256(f"{benchmark_id}:{item.relationship_id}".encode()).hexdigest(),
                item.relationship_id,
            ),
        )
        selected.extend(ranked[: quotas[relation_type]])
    selected.sort(key=lambda item: item.relationship_id)
    if len(selected) != target:
        raise CrossReferenceBenchmarkError("audit sample selection produced the wrong size")
    return AuditSample(
        benchmark_id=benchmark_id,
        sample_size_requested=sample_size,
        independent_relationships=tuple(sorted(independent, key=lambda item: item.relationship_id)),
        family_relationships=tuple(sorted(family, key=lambda item: item.relationship_id)),
        selected=tuple(selected),
        population_by_type=dict(Counter(item.relation_type for item in independent)),
        sample_by_type=dict(Counter(item.relation_type for item in selected)),
        source_families=families,
    )


def validate_audit_output(
    payload: Mapping[str, Any], *, benchmark_id: str, expected_relationship_ids: Sequence[str]
) -> tuple[dict[str, Any], ...]:
    _require_identity(payload, benchmark_id=benchmark_id)
    raw_items = payload.get("audited_relationships")
    if not isinstance(raw_items, list):
        raise CrossReferenceBenchmarkError("audit output requires audited_relationships")
    items = tuple(_mapping(item, label="audit item") for item in raw_items)
    observed_ids = [str(item.get("relationship_id", "")) for item in items]
    _require_exact_ids(observed_ids, expected_relationship_ids, label="audit relationship")
    for item in items:
        scores = _mapping(item.get("scores"), label="audit scores")
        if set(scores) != set(AUDIT_SCORE_FIELDS):
            raise CrossReferenceBenchmarkError("audit scores do not match the fixed rubric")
        for field in AUDIT_SCORE_FIELDS:
            _score(scores[field], label=field)
        if item.get("disposition") not in AUDIT_DISPOSITIONS:
            raise CrossReferenceBenchmarkError("audit disposition is invalid")
        if item.get("relation_type_assessment") not in RELATION_TYPE_ASSESSMENTS:
            raise CrossReferenceBenchmarkError("audit relation-type assessment is invalid")
        _nonempty_text(item.get("rationale"), label="audit rationale")
    return items


def validate_blind_probe_output(
    payload: Mapping[str, Any],
    *,
    benchmark_id: str,
    coverage_source_ids: Sequence[str],
    records: Mapping[str, Record],
    evidence_ids_by_source: Mapping[str, frozenset[str]],
) -> tuple[dict[str, Any], ...]:
    _require_identity(payload, benchmark_id=benchmark_id)
    if payload.get("source_ids") != list(coverage_source_ids):
        raise CrossReferenceBenchmarkError("blind probe source identities do not match")
    raw_candidates = payload.get("candidates")
    if not isinstance(raw_candidates, list):
        raise CrossReferenceBenchmarkError("blind probe requires candidates")
    candidates = tuple(_mapping(item, label="blind probe candidate") for item in raw_candidates)
    keys = [str(item.get("candidate_key", "")) for item in candidates]
    if any(not key.strip() for key in keys) or len(set(keys)) != len(keys):
        raise CrossReferenceBenchmarkError(
            "blind probe candidate keys must be unique and non-empty"
        )
    allowed_sources = set(coverage_source_ids)
    semantic_keys: set[tuple[str, str, str, str]] = set()
    for candidate in candidates:
        left_id = str(candidate.get("left_record_id", ""))
        right_id = str(candidate.get("right_record_id", ""))
        left = records.get(left_id)
        right = records.get(right_id)
        if left is None or right is None:
            raise CrossReferenceBenchmarkError("blind probe endpoint does not resolve")
        if left.record_type != "atom" or right.record_type != "atom":
            raise CrossReferenceBenchmarkError("blind probe endpoints must be atoms")
        if (
            left.payload.get("connectable") is not True
            or right.payload.get("connectable") is not True
        ):
            raise CrossReferenceBenchmarkError("blind probe endpoint is not connectable")
        if left.source_id not in allowed_sources or right.source_id not in allowed_sources:
            raise CrossReferenceBenchmarkError(
                "blind probe endpoint is outside the coverage cohort"
            )
        if left.source_id == right.source_id:
            raise CrossReferenceBenchmarkError("blind probe endpoints must be cross-source")
        relation_type = candidate.get("proposed_relation_type")
        direction = candidate.get("direction")
        if relation_type not in RELATIONSHIP_TYPES or direction not in RELATIONSHIP_DIRECTIONS:
            raise CrossReferenceBenchmarkError("blind probe relationship posture is invalid")
        if relation_type in SYMMETRIC_RELATIONSHIP_TYPES and direction != "symmetric":
            raise CrossReferenceBenchmarkError("blind probe symmetric type has invalid direction")
        if relation_type in DIRECTED_RELATIONSHIP_TYPES and direction == "symmetric":
            raise CrossReferenceBenchmarkError("blind probe directed type requires a direction")
        _validate_probe_evidence(
            candidate.get("left_evidence_ids"),
            source_id=left.source_id,
            known=evidence_ids_by_source,
        )
        _validate_probe_evidence(
            candidate.get("right_evidence_ids"),
            source_id=right.source_id,
            known=evidence_ids_by_source,
        )
        for field in (
            "comparison_surface",
            "scope_alignment",
            "assumption_alignment",
            "rationale",
        ):
            _nonempty_text(candidate.get(field), label=f"blind probe {field}")
        _reject_adjudicative_text(candidate)
        importance = candidate.get("importance")
        if (
            not isinstance(importance, int)
            or isinstance(importance, bool)
            or not 1 <= importance <= 4
        ):
            raise CrossReferenceBenchmarkError(
                "blind probe importance must be an integer from 1 to 4"
            )
        semantic_key = _probe_semantic_key(
            left_id,
            right_id,
            relation_type=str(relation_type),
            direction=str(direction),
        )
        if semantic_key in semantic_keys:
            raise CrossReferenceBenchmarkError(
                "blind probe contains a duplicate semantic candidate"
            )
        semantic_keys.add(semantic_key)
    return candidates


def validate_probe_comparison_output(
    payload: Mapping[str, Any],
    *,
    benchmark_id: str,
    expected_candidate_keys: Sequence[str],
    allowed_relationship_ids: Sequence[str],
) -> tuple[dict[str, Any], ...]:
    _require_identity(payload, benchmark_id=benchmark_id)
    raw_matches = payload.get("matches")
    if not isinstance(raw_matches, list):
        raise CrossReferenceBenchmarkError("probe comparison requires matches")
    matches = tuple(_mapping(item, label="probe comparison item") for item in raw_matches)
    keys = [str(item.get("candidate_key", "")) for item in matches]
    _require_exact_ids(keys, expected_candidate_keys, label="probe candidate")
    allowed = set(allowed_relationship_ids)
    for item in matches:
        for field in ("candidate_validity", "candidate_utility", "match_quality"):
            _score(item.get(field), label=field)
        if item.get("match_kind") not in MATCH_KINDS:
            raise CrossReferenceBenchmarkError("probe comparison match kind is invalid")
        raw_ids = item.get("matched_relationship_ids")
        if not isinstance(raw_ids, list) or not all(isinstance(value, str) for value in raw_ids):
            raise CrossReferenceBenchmarkError("probe comparison matched IDs are invalid")
        if len(set(raw_ids)) != len(raw_ids) or set(raw_ids) - allowed:
            raise CrossReferenceBenchmarkError(
                "probe comparison cites unknown or duplicate relationships"
            )
        if item.get("match_kind") == "none" and raw_ids:
            raise CrossReferenceBenchmarkError(
                "unmatched probe candidate cannot cite relationships"
            )
        if item.get("match_kind") != "none" and not raw_ids:
            raise CrossReferenceBenchmarkError("matched probe candidate must cite a relationship")
        _nonempty_text(item.get("rationale"), label="probe comparison rationale")
    return matches


def compile_benchmark_metrics(
    *,
    benchmark_id: str,
    sample: AuditSample,
    audit_items: Sequence[Mapping[str, Any]],
    probe_candidates: Sequence[Mapping[str, Any]],
    comparison_items: Sequence[Mapping[str, Any]],
    generation_effective_tokens: int,
) -> dict[str, Any]:
    if generation_effective_tokens < 0:
        raise CrossReferenceBenchmarkError("generation effective tokens must not be negative")
    audit_by_id = {str(item["relationship_id"]): item for item in audit_items}
    if set(audit_by_id) != {item.relationship_id for item in sample.selected}:
        raise CrossReferenceBenchmarkError("audit metrics input does not match the frozen sample")
    strata: dict[str, dict[str, Any]] = {}
    estimate = 0.0
    estimate_low = 0.0
    estimate_high = 0.0
    for relation_type, population_count in sorted(sample.population_by_type.items()):
        sampled = [item for item in sample.selected if item.relation_type == relation_type]
        useful_count = sum(
            _audit_item_is_useful(audit_by_id[item.relationship_id]) for item in sampled
        )
        sample_count = len(sampled)
        rate = useful_count / sample_count
        low, high = _wilson_interval(useful_count, sample_count)
        stratum_estimate = population_count * rate
        estimate += stratum_estimate
        estimate_low += population_count * low
        estimate_high += population_count * high
        strata[relation_type] = {
            "population_count": population_count,
            "sample_count": sample_count,
            "useful_sample_count": useful_count,
            "useful_sample_rate": _rounded(rate),
            "useful_rate_wilson_95_low": _rounded(low),
            "useful_rate_wilson_95_high": _rounded(high),
            "estimated_useful_edges": _rounded(stratum_estimate),
        }

    sample_score_means = {
        field: _rounded(
            sum(int(_mapping(item["scores"], label="audit scores")[field]) for item in audit_items)
            / len(audit_items)
        )
        for field in AUDIT_SCORE_FIELDS
    }
    candidate_by_key = {str(item["candidate_key"]): item for item in probe_candidates}
    comparison_by_key = {str(item["candidate_key"]): item for item in comparison_items}
    if set(candidate_by_key) != set(comparison_by_key):
        raise CrossReferenceBenchmarkError("probe and comparison metrics inputs do not match")
    eligible_keys = [
        key
        for key, item in comparison_by_key.items()
        if int(item["candidate_validity"]) >= 3 and int(item["candidate_utility"]) >= 2
    ]
    coverage_denominator = sum(int(candidate_by_key[key]["importance"]) for key in eligible_keys)
    coverage_numerator = sum(
        int(candidate_by_key[key]["importance"]) * int(comparison_by_key[key]["match_quality"]) / 4
        for key in eligible_keys
    )
    coverage = (
        None if coverage_denominator == 0 else _rounded(coverage_numerator / coverage_denominator)
    )
    pair_count = len({_relationship_pair(item) for item in sample.independent_relationships})
    family_pair_count = len({_relationship_pair(item) for item in sample.family_relationships})
    tokens_per_useful = None if estimate == 0 else _rounded(generation_effective_tokens / estimate)
    return {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "contract_version": BENCHMARK_CONTRACT_VERSION,
        "benchmark_id": benchmark_id,
        "classification": "model_assisted_calibration_evidence",
        "admission": {
            "admitted_relationship_count": len(sample.independent_relationships)
            + len(sample.family_relationships),
            "independent_relationship_count": len(sample.independent_relationships),
            "source_family_relationship_count": len(sample.family_relationships),
            "independent_source_pair_count": pair_count,
            "source_family_pair_count": family_pair_count,
            "source_families": [list(group) for group in sample.source_families],
        },
        "edge_quality_audit": {
            "sample_size": len(sample.selected),
            "useful_sample_count": sum(_audit_item_is_useful(item) for item in audit_items),
            "score_means": sample_score_means,
            "strata": strata,
            "estimated_useful_independent_edges": _rounded(estimate),
            "estimated_useful_independent_edges_wilson_95_low": _rounded(estimate_low),
            "estimated_useful_independent_edges_wilson_95_high": _rounded(estimate_high),
        },
        "coverage_probe": {
            "candidate_count": len(probe_candidates),
            "eligible_candidate_count": len(eligible_keys),
            "importance_weight_denominator": coverage_denominator,
            "weighted_match_numerator": _rounded(coverage_numerator),
            "weighted_probe_coverage": coverage,
            "interpretation": "model-assisted bounded probe; not objective recall",
        },
        "cost": {
            "generation_effective_tokens": generation_effective_tokens,
            "effective_tokens_per_estimated_useful_independent_edge": tokens_per_useful,
            "evaluation_usage": None,
        },
    }


def cohort_relationships(
    relationships: Sequence[Relationship], *, source_ids: Sequence[str]
) -> tuple[Relationship, ...]:
    allowed = set(source_ids)
    return tuple(
        sorted(
            (item for item in relationships if set(_relationship_pair(item)).issubset(allowed)),
            key=lambda item: item.relationship_id,
        )
    )


def _validate_relationship_records(
    relationship: Relationship,
    *,
    records: Mapping[str, Record],
    evidence_ids_by_source: Mapping[str, frozenset[str]],
) -> None:
    for side in ("left", "right"):
        endpoint = relationship.payload[f"{side}_endpoint"]
        record_id = str(endpoint["record_id"])
        record = records.get(record_id)
        if record is None or record.record_type != "atom":
            raise CrossReferenceBenchmarkError(
                f"relationship endpoint does not resolve: {record_id}"
            )
        if record.source_id != endpoint["source_id"] or record.revision != endpoint["revision"]:
            raise CrossReferenceBenchmarkError(
                f"relationship endpoint identity changed: {record_id}"
            )
        if record_fingerprint(record.payload) != endpoint["record_sha256"]:
            raise CrossReferenceBenchmarkError(
                f"relationship endpoint fingerprint changed: {record_id}"
            )
        known_evidence = evidence_ids_by_source.get(record.source_id, frozenset())
        cited = relationship.payload.get(f"{side}_evidence_ids")
        if not isinstance(cited, list) or not cited or set(cited) - set(known_evidence):
            raise CrossReferenceBenchmarkError(
                f"relationship evidence does not resolve: {record_id}"
            )


def _validate_probe_evidence(
    value: Any, *, source_id: str, known: Mapping[str, frozenset[str]]
) -> None:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise CrossReferenceBenchmarkError("blind probe evidence lists must be non-empty")
    if set(value) - set(known.get(source_id, frozenset())):
        raise CrossReferenceBenchmarkError("blind probe evidence does not resolve")


def _require_identity(payload: Mapping[str, Any], *, benchmark_id: str) -> None:
    if payload.get("schema_version") != BENCHMARK_SCHEMA_VERSION:
        raise CrossReferenceBenchmarkError("benchmark output schema version is invalid")
    if payload.get("benchmark_id") != benchmark_id:
        raise CrossReferenceBenchmarkError("benchmark output identity does not match")


def _require_safe_identifier(value: str, *, label: str) -> None:
    if not _SAFE_IDENTIFIER.fullmatch(value):
        raise CrossReferenceBenchmarkError(
            f"{label} must be a safe path component of at most 128 characters"
        )


def _probe_semantic_key(
    left_id: str, right_id: str, *, relation_type: str, direction: str
) -> tuple[str, str, str, str]:
    if direction == "symmetric":
        first, second = sorted((left_id, right_id))
        return first, second, relation_type, direction
    if direction == "left_to_right":
        return left_id, right_id, relation_type, "directed"
    return right_id, left_id, relation_type, "directed"


def _require_exact_ids(observed: Sequence[str], expected: Sequence[str], *, label: str) -> None:
    if len(observed) != len(set(observed)):
        raise CrossReferenceBenchmarkError(f"{label} IDs are not unique")
    if set(observed) != set(expected):
        missing = sorted(set(expected) - set(observed))
        unexpected = sorted(set(observed) - set(expected))
        raise CrossReferenceBenchmarkError(
            f"{label} IDs do not match; missing={missing}, unexpected={unexpected}"
        )


def _audit_item_is_useful(item: Mapping[str, Any]) -> bool:
    scores = _mapping(item["scores"], label="audit scores")
    return bool(
        item["disposition"] in {"strong", "usable"}
        and int(scores["evidence_grounding"]) >= 3
        and int(scores["relation_type_fit"]) >= 3
        and int(scores["scope_assumption_fit"]) >= 3
        and int(scores["research_utility"]) >= 2
        and int(scores["distinctiveness"]) >= 2
    )


def _wilson_interval(successes: int, sample_size: int) -> tuple[float, float]:
    if sample_size <= 0:
        raise CrossReferenceBenchmarkError("Wilson interval requires a non-empty sample")
    z = 1.959963984540054
    rate = successes / sample_size
    denominator = 1 + z * z / sample_size
    center = (rate + z * z / (2 * sample_size)) / denominator
    margin = (
        z * math.sqrt(rate * (1 - rate) / sample_size + z * z / (4 * sample_size**2)) / denominator
    )
    return max(0.0, center - margin), min(1.0, center + margin)


def _relationship_pair(relationship: Relationship) -> tuple[str, str]:
    return tuple(
        sorted(
            (
                str(relationship.payload["left_endpoint"]["source_id"]),
                str(relationship.payload["right_endpoint"]["source_id"]),
            )
        )
    )  # type: ignore[return-value]


def _mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CrossReferenceBenchmarkError(f"{label} must be an object")
    return value


def _score(value: Any, *, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 4:
        raise CrossReferenceBenchmarkError(f"{label} must be an integer from 0 to 4")
    return value


def _nonempty_text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CrossReferenceBenchmarkError(f"{label} must be non-empty")
    return value.strip()


def _reject_adjudicative_text(value: Mapping[str, Any]) -> None:
    text = json.dumps(value, ensure_ascii=False).lower()
    matched = next((phrase for phrase in _ADJUDICATIVE_PHRASES if phrase in text), None)
    if matched is not None:
        raise CrossReferenceBenchmarkError(
            f"benchmark proposal crosses the non-adjudication boundary: {matched.strip()}"
        )


def render_benchmark_report(
    metrics: Mapping[str, Any], *, sample: AuditSample, coverage_sources: Sequence[str]
) -> str:
    admission = _mapping(metrics["admission"], label="admission metrics")
    audit = _mapping(metrics["edge_quality_audit"], label="audit metrics")
    coverage = _mapping(metrics["coverage_probe"], label="coverage metrics")
    cost = _mapping(metrics["cost"], label="cost metrics")
    usage = _mapping(cost["evaluation_usage"], label="evaluation usage")
    totals = _mapping(usage["totals"], label="evaluation totals")
    lines = [
        "# Cross-reference quality benchmark",
        "",
        f"Benchmark: `{metrics['benchmark_id']}`",
        "",
        "This is model-assisted calibration evidence. It does not adjudicate scientific "
        "truth, change the map, or establish objective recall.",
        "",
        "## Outcome",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Admitted relationships | {admission['admitted_relationship_count']} |",
        f"| Independent-paper relationships | {admission['independent_relationship_count']} |",
        "| Declared source-family relationships | "
        f"{admission['source_family_relationship_count']} |",
        f"| Audit sample | {audit['sample_size']} |",
        f"| Useful audited edges | {audit['useful_sample_count']} |",
        f"| Estimated useful independent edges | {audit['estimated_useful_independent_edges']} |",
        "| Estimated useful edges, Wilson 95% interval | "
        f"{audit['estimated_useful_independent_edges_wilson_95_low']}–"
        f"{audit['estimated_useful_independent_edges_wilson_95_high']} |",
        f"| Blind-probe candidates | {coverage['candidate_count']} |",
        f"| Eligible blind-probe candidates | {coverage['eligible_candidate_count']} |",
        f"| Weighted probe coverage | {_display_metric(coverage['weighted_probe_coverage'])} |",
        f"| Generation effective tokens | {cost['generation_effective_tokens']} |",
        "| Generation tokens per estimated useful edge | "
        f"{_display_metric(cost['effective_tokens_per_estimated_useful_independent_edge'])} |",
        f"| Evaluation effective tokens | {totals['effective_tokens']} |",
        f"| Evaluation duration | {totals['duration_seconds']} s |",
        "",
        "## Frozen design",
        "",
        f"- Coverage cohort: {', '.join(coverage_sources)}",
        f"- Declared source families: {_display_source_families(sample.source_families)}",
        "- Audit selection: relation-type stratified, with all strata of at most three "
        "edges included before proportional stable-hash sampling.",
        "- Useful-edge gate: grounding/type/scope at least 3; utility and distinctiveness "
        "at least 2; disposition strong or usable.",
        "- Probe coverage: importance-weighted match quality over candidates with validity "
        "at least 3 and utility at least 2.",
        "",
        "## Relation-type audit",
        "",
        "| Relation type | Population | Sample | Useful | Rate | Estimated useful |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    strata = _mapping(audit["strata"], label="audit strata")
    for relation_type, raw in sorted(strata.items()):
        item = _mapping(raw, label=f"{relation_type} stratum")
        lines.append(
            f"| `{relation_type}` | {item['population_count']} | {item['sample_count']} | "
            f"{item['useful_sample_count']} | {item['useful_sample_rate']} | "
            f"{item['estimated_useful_edges']} |"
        )
    lines.extend(
        (
            "",
            "## Limitations",
            "",
            "- The audit is a model review of a deterministic sample, not human scientific "
            "endorsement.",
            "- The coverage result is a bounded five-source probe, not corpus-wide recall.",
            "- Wilson intervals express audit sample uncertainty, not reviewer-bias uncertainty.",
            "- Generation and evaluation costs are reported separately.",
            "",
        )
    )
    return "\n".join(lines)


def _stable_job_id(benchmark_id: str, stage: str) -> str:
    digest = hashlib.sha256(
        f"{BENCHMARK_CONTRACT_VERSION}:{benchmark_id}:{stage}".encode()
    ).hexdigest()
    return f"xref_benchmark_job_{digest[:24]}"


def _fingerprint_inventory(items: Sequence[Mapping[str, Any]]) -> str:
    return hashlib.sha256(canonical_json_bytes(list(items))).hexdigest()


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CrossReferenceBenchmarkError(f"{label} is unreadable: {path}") from error
    if not isinstance(value, dict):
        raise CrossReferenceBenchmarkError(f"{label} must be an object")
    return value


def _validate_json_schema(payload: Mapping[str, Any], *, schema_path: Path) -> None:
    schema = _read_json_object(schema_path, label="benchmark schema")
    errors = sorted(
        Draft202012Validator(schema).iter_errors(dict(payload)), key=lambda item: list(item.path)
    )
    if errors:
        location = ".".join(str(part) for part in errors[0].path) or "record"
        raise CrossReferenceBenchmarkError(f"{location}: {errors[0].message}")


def _write_or_verify_json(path: Path, value: Mapping[str, Any]) -> None:
    _write_or_verify_bytes(path, canonical_json_bytes(value))


def _write_or_verify_manifest(path: Path, value: Mapping[str, Any]) -> None:
    """Write portable manifests while accepting hash-identical legacy absolute paths."""

    _write_or_verify_portable_json(path, value)


def _write_or_verify_portable_json(path: Path, value: Mapping[str, Any]) -> None:
    """Verify retained JSON after normalizing only fingerprint path prefixes."""

    if not path.exists():
        _write_or_verify_json(path, value)
        return
    retained = _read_json_object(path, label="retained benchmark manifest")
    normalized = _normalize_legacy_fingerprint_paths(retained, value)
    if canonical_json_bytes(normalized) != canonical_json_bytes(value):
        raise CrossReferenceBenchmarkError(f"retained benchmark artifact changed: {path}")


def _normalize_legacy_fingerprint_paths(retained: Any, expected: Any) -> Any:
    if isinstance(retained, dict) and isinstance(expected, Mapping):
        normalized = {
            key: _normalize_legacy_fingerprint_paths(item, expected.get(key))
            for key, item in retained.items()
        }
        fingerprint_fields = {"path", "sha256", "size_bytes"}
        if fingerprint_fields.issubset(expected) and fingerprint_fields.issubset(normalized):
            expected_path = expected.get("path")
            retained_path = normalized.get("path")
            if (
                isinstance(expected_path, str)
                and isinstance(retained_path, str)
                and (retained_path == expected_path or retained_path.endswith(f"/{expected_path}"))
            ):
                normalized["path"] = expected_path
        return normalized
    if isinstance(retained, list) and isinstance(expected, Sequence):
        return [
            _normalize_legacy_fingerprint_paths(
                item, expected[index] if index < len(expected) else None
            )
            for index, item in enumerate(retained)
        ]
    return retained


def _write_or_verify_bytes(path: Path, data: bytes) -> None:
    if path.exists():
        if path.read_bytes() != data:
            raise CrossReferenceBenchmarkError(f"retained benchmark artifact changed: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def _nonnegative_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _display_metric(value: Any) -> str:
    return "undefined" if value is None else str(value)


def _display_source_families(groups: Sequence[Sequence[str]]) -> str:
    return "; ".join(",".join(group) for group in groups) if groups else "none declared"


def _rounded(value: float) -> float:
    return round(value, 6)
