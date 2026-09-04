"""Read-only focused cross-reference calibration over one frozen five-source cohort."""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import tempfile
from collections import Counter
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
)
from research_map.cross_reference_benchmark import (
    AUDIT_SCORE_FIELDS,
    MATCH_KINDS,
    CrossReferenceBenchmarkError,
    FrozenBenchmarkCorpus,
    load_frozen_benchmark_corpus,
    validate_blind_probe_output,
    validate_probe_comparison_output,
)
from research_map.receipts import canonical_json_bytes, fingerprint, read_receipt
from research_map.records import Record
from research_map.workspace import CrossReferenceWorkspaceBuilder, WorkspaceError, WorkspaceInput

CALIBRATION_SCHEMA_VERSION = "1.0"
CALIBRATION_CONTRACT_VERSION = "focused-cross-reference-v1"
EXISTING_GRAPH_BASELINE = 0.555882
ELIGIBLE_REFERENCE_COUNT = 25
ELIGIBLE_REFERENCE_IMPORTANCE = 85
TYPE_ASSESSMENTS = frozenset({"fit", "adjacent", "wrong", "ontology_gap", "uncertain"})
AUDIT_DISPOSITIONS = frozenset({"strong", "usable", "weak", "misleading", "manual_review"})
RECOMMENDED_TYPES = RELATIONSHIP_TYPES | frozenset(
    {"current_type_is_best", "no_current_type_fits", "uncertain"}
)
STAGE_DEFINITIONS = {
    "focused-mapper": (
        "cross_reference_holistic",
        "holistic-cross-reference-proposal.schema.json",
        "focused-cross-reference-calibration.md",
    ),
    "independent-evaluation": (
        "cross_reference_benchmark_comparison",
        "focused-cross-reference-evaluation.schema.json",
        "focused-cross-reference-evaluation.md",
    ),
}
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SOURCE_ID = re.compile(r"^LIB-[0-9]{3}$")
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


class FocusedCrossReferenceCalibrationError(ValueError):
    """Raised when frozen inputs or semantic calibration outputs are invalid."""


class FocusedCrossReferenceCalibrationExecutionError(RuntimeError):
    """Raised when one bounded Codex calibration stage fails terminally."""


@dataclass(frozen=True, slots=True)
class ReferenceCandidateSet:
    benchmark_id: str
    batch_id: str
    source_ids: tuple[str, ...]
    candidates: tuple[dict[str, Any], ...]
    existing_graph_coverage: float
    importance_weight: int
    input_fingerprints: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class FocusedCalibrationResult:
    calibration_id: str
    batch_id: str
    root: Path
    metrics_path: Path
    report_path: Path
    semantic_calls: int
    reused_stages: tuple[str, ...]
    frozen_input_fingerprint_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "calibration_id": self.calibration_id,
            "batch_id": self.batch_id,
            "root": str(self.root),
            "metrics_path": str(self.metrics_path),
            "report_path": str(self.report_path),
            "semantic_calls": self.semantic_calls,
            "reused_stages": list(self.reused_stages),
            "frozen_input_fingerprint_sha256": self.frozen_input_fingerprint_sha256,
        }


class FocusedCrossReferenceCalibrationCoordinator:
    """Coordinate a graph-blind mapper and one independent evaluator without map writes."""

    contract_version = CALIBRATION_CONTRACT_VERSION
    stage_definitions = STAGE_DEFINITIONS
    receipt_kind = "focused_cross_reference_calibration"
    stage_validation_kind = "focused_cross_reference_stage_validation"

    def __init__(
        self,
        *,
        repository_root: Path,
        calibration_root: Path,
        reference_benchmark_root: Path,
        output_root: Path,
        codex_executable: str = "codex",
        timeout_seconds: float = 5400,
    ) -> None:
        self.repository_root = repository_root.resolve()
        self.calibration_root = calibration_root.resolve()
        self.reference_benchmark_root = reference_benchmark_root.resolve()
        self.output_root = output_root.resolve()
        self.codex_executable = codex_executable
        self.timeout_seconds = timeout_seconds
        self.schema_directory = self.repository_root / "schemas" / "research-map" / "v1"
        self.protocol_directory = self.repository_root / "protocols" / "research-map" / "v1"
        self._codex_runner: CodexRunner | None = None

    def run(
        self,
        *,
        calibration_id: str,
        batch_id: str,
        model: str,
        source_ids: Sequence[str],
    ) -> FocusedCalibrationResult:
        _require_safe_identifier(calibration_id, label="calibration ID")
        _require_safe_identifier(batch_id, label="batch ID")
        if not model.strip():
            raise FocusedCrossReferenceCalibrationError("calibration model is required")
        if self.timeout_seconds <= 0:
            raise FocusedCrossReferenceCalibrationError("calibration timeout must be positive")
        cohort = tuple(source_ids)
        if (
            len(cohort) != 5
            or tuple(sorted(set(cohort))) != cohort
            or not all(_SOURCE_ID.fullmatch(source) for source in cohort)
        ):
            raise FocusedCrossReferenceCalibrationError(
                "focused calibration requires exactly five unique canonical source IDs"
            )

        corpus = _load_frozen_corpus(self.calibration_root, batch_id=batch_id)
        if set(cohort) - set(corpus.source_ids):
            raise FocusedCrossReferenceCalibrationError(
                "focused calibration cohort is outside the frozen corpus"
            )
        reference = load_reference_candidates(
            self.reference_benchmark_root,
            corpus=corpus,
            expected_batch_id=batch_id,
            expected_source_ids=cohort,
        )
        root = self.output_root / calibration_id
        root.mkdir(parents=True, exist_ok=True)
        manifest = self._manifest(
            corpus=corpus,
            reference=reference,
            calibration_id=calibration_id,
            model=model,
            source_ids=cohort,
        )
        manifest_path = root / "manifest.json"
        _write_or_verify_portable_json(manifest_path, manifest)

        mapper_payload, mapper_reused = self._run_stage(
            root=root,
            stage="focused-mapper",
            calibration_id=calibration_id,
            model=model,
            source_ids=cohort,
            inputs=self._mapper_inputs(
                corpus=corpus,
                calibration_id=calibration_id,
                batch_id=batch_id,
                source_ids=cohort,
            ),
        )
        keyed_relationships = self._validate_mapper_output(
            mapper_payload,
            batch_id=batch_id,
            source_ids=cohort,
            records=corpus.record_index,
            evidence_ids_by_source=corpus.evidence_ids_by_source,
        )
        self._record_stage_validation(
            root=root,
            stage="focused-mapper",
            output_path=root / "jobs" / "focused-mapper" / "output" / "result.json",
            validated_ids=[item["proposal_key"] for item in keyed_relationships],
            extra=self._mapper_validation_extra(mapper_payload, keyed_relationships),
        )

        evaluation_payload, evaluation_reused = self._run_stage(
            root=root,
            stage="independent-evaluation",
            calibration_id=calibration_id,
            model=model,
            source_ids=cohort,
            inputs=self._evaluation_inputs(
                corpus=corpus,
                reference=reference,
                calibration_id=calibration_id,
                batch_id=batch_id,
                source_ids=cohort,
                mapper_payload=mapper_payload,
                keyed_relationships=keyed_relationships,
            ),
        )
        proposal_audits, reference_matches = validate_focused_evaluation_output(
            evaluation_payload,
            calibration_id=calibration_id,
            keyed_relationships=keyed_relationships,
            reference_candidates=reference.candidates,
        )
        self._record_stage_validation(
            root=root,
            stage="independent-evaluation",
            output_path=(root / "jobs" / "independent-evaluation" / "output" / "result.json"),
            validated_ids=[item["proposal_key"] for item in proposal_audits]
            + [item["candidate_key"] for item in reference_matches],
            extra={
                "proposal_audit_count": len(proposal_audits),
                "reference_match_count": len(reference_matches),
            },
        )

        after_corpus = _load_frozen_corpus(self.calibration_root, batch_id=batch_id)
        after_reference = _fingerprint_tree(self.reference_benchmark_root)
        if after_corpus.input_fingerprints != corpus.input_fingerprints:
            raise FocusedCrossReferenceCalibrationError(
                "frozen corpus inputs changed during calibration"
            )
        if after_reference != reference.input_fingerprints:
            raise FocusedCrossReferenceCalibrationError(
                "reference benchmark inputs changed during calibration"
            )

        usage = self._stage_usage(root)
        metrics = self._compile_metrics(
            calibration_id=calibration_id,
            batch_id=batch_id,
            source_ids=cohort,
            mapper_payload=mapper_payload,
            keyed_relationships=keyed_relationships,
            proposal_audits=proposal_audits,
            reference=reference,
            reference_matches=reference_matches,
            stage_usage=usage,
        )
        metrics["reproducibility"] = {
            "model": model,
            "manifest_sha256": fingerprint(manifest_path).sha256,
            "frozen_corpus_fingerprint_sha256": _fingerprint_inventory(corpus.input_fingerprints),
            "reference_benchmark_fingerprint_sha256": _fingerprint_inventory(
                reference.input_fingerprints
            ),
            "combined_frozen_input_fingerprint_sha256": _combined_input_fingerprint(
                corpus, reference
            ),
            "stage_output_sha256": {
                stage: fingerprint(root / "jobs" / stage / "output" / "result.json").sha256
                for stage in self.stage_definitions
            },
            "stage_receipt_sha256": {
                stage: fingerprint(root / "receipts" / f"{stage}.json").sha256
                for stage in self.stage_definitions
            },
            "stage_validation_sha256": {
                stage: fingerprint(root / "validations" / f"{stage}.json").sha256
                for stage in self.stage_definitions
            },
            "frozen_inputs_unchanged": True,
            "map_mutation_authorized": False,
        }
        metrics_path = root / "metrics.json"
        _write_or_verify_json(metrics_path, metrics)
        report_path = root / "report.md"
        _write_or_verify_bytes(report_path, self._render_report(metrics).encode())
        _write_or_verify_portable_json(
            root / "calibration-receipt.json",
            {
                "schema_version": CALIBRATION_SCHEMA_VERSION,
                "kind": self.receipt_kind,
                "contract_version": self.contract_version,
                "calibration_id": calibration_id,
                "batch_id": batch_id,
                "manifest": fingerprint(manifest_path, relative_to=root).to_dict(),
                "metrics": fingerprint(metrics_path, relative_to=root).to_dict(),
                "report": fingerprint(report_path, relative_to=root).to_dict(),
                "semantic_stage_count": len(self.stage_definitions),
                "frozen_inputs_unchanged": True,
                "map_mutation_authorized": False,
            },
        )
        reused = tuple(
            stage
            for stage, was_reused in (
                ("focused-mapper", mapper_reused),
                ("independent-evaluation", evaluation_reused),
            )
            if was_reused
        )
        return FocusedCalibrationResult(
            calibration_id=calibration_id,
            batch_id=batch_id,
            root=root,
            metrics_path=metrics_path,
            report_path=report_path,
            semantic_calls=2 - len(reused),
            reused_stages=reused,
            frozen_input_fingerprint_sha256=_combined_input_fingerprint(corpus, reference),
        )

    def _manifest(
        self,
        *,
        corpus: FrozenBenchmarkCorpus,
        reference: ReferenceCandidateSet,
        calibration_id: str,
        model: str,
        source_ids: tuple[str, ...],
    ) -> dict[str, Any]:
        stage_contracts: dict[str, Any] = {}
        for stage, (_, schema_name, protocol_name) in self.stage_definitions.items():
            stage_contracts[stage] = {
                "schema": fingerprint(
                    self.schema_directory / schema_name, relative_to=self.repository_root
                ).to_dict(),
                "protocol": fingerprint(
                    self.protocol_directory / protocol_name, relative_to=self.repository_root
                ).to_dict(),
            }
        return {
            "schema_version": CALIBRATION_SCHEMA_VERSION,
            "contract_version": self.contract_version,
            "calibration_id": calibration_id,
            "batch_id": corpus.batch_id,
            "model": model,
            "source_ids": list(source_ids),
            "owned_source_pairs": [list(pair) for pair in itertools.combinations(source_ids, 2)],
            "reference_benchmark_id": reference.benchmark_id,
            "reference_candidate_count": len(reference.candidates),
            "reference_importance_weight": reference.importance_weight,
            "existing_graph_weighted_coverage": reference.existing_graph_coverage,
            "corpus_input_fingerprints": list(corpus.input_fingerprints),
            "reference_input_fingerprints": list(reference.input_fingerprints),
            "combined_frozen_input_fingerprint_sha256": _combined_input_fingerprint(
                corpus, reference
            ),
            "stage_contracts": stage_contracts,
            "semantic_stage_order": list(self.stage_definitions),
            "fresh_semantic_call_limit": 2,
            "mapper_current_relationships_available": False,
            "mapper_reference_candidates_available": False,
            "map_mutation_authorized": False,
        }

    def _mapper_inputs(
        self,
        *,
        corpus: FrozenBenchmarkCorpus,
        calibration_id: str,
        batch_id: str,
        source_ids: tuple[str, ...],
    ) -> tuple[WorkspaceInput, ...]:
        task = {
            "schema_version": CALIBRATION_SCHEMA_VERSION,
            "stage": "focused-five-source-cross-reference",
            "calibration_id": calibration_id,
            "batch_id": batch_id,
            "source_ids": list(source_ids),
            "owned_source_pairs": [list(pair) for pair in itertools.combinations(source_ids, 2)],
            "required_pair_surface_count": 10,
            "current_relationships_available": False,
            "reference_candidates_available": False,
            "relationship_quota": None,
            "map_mutation_authorized": False,
        }
        return (
            self._protocol_input(calibration_id, "focused-mapper"),
            WorkspaceInput(
                source_id=calibration_id,
                destination="task.json",
                kind="focused_calibration_task",
                content=canonical_json_bytes(task),
            ),
            *self._dossier_inputs(corpus, source_ids, calibration_id=calibration_id),
        )

    def _evaluation_inputs(
        self,
        *,
        corpus: FrozenBenchmarkCorpus,
        reference: ReferenceCandidateSet,
        calibration_id: str,
        batch_id: str,
        source_ids: tuple[str, ...],
        mapper_payload: Mapping[str, Any],
        keyed_relationships: Sequence[Mapping[str, Any]],
    ) -> tuple[WorkspaceInput, ...]:
        proposal_keys = [str(item["proposal_key"]) for item in keyed_relationships]
        reference_keys = [str(item["candidate_key"]) for item in reference.candidates]
        task = {
            "schema_version": CALIBRATION_SCHEMA_VERSION,
            "stage": "focused-cross-reference-independent-evaluation",
            "calibration_id": calibration_id,
            "batch_id": batch_id,
            "source_ids": list(source_ids),
            "proposal_keys": proposal_keys,
            "reference_candidate_keys": reference_keys,
            "rubric_score_range": [0, 4],
            "score_fields": list(AUDIT_SCORE_FIELDS),
            "reference_candidates_are_objective_truth": False,
            "map_mutation_authorized": False,
        }
        focused_projection = {
            "schema_version": CALIBRATION_SCHEMA_VERSION,
            "calibration_id": calibration_id,
            "batch_id": batch_id,
            "source_ids": list(source_ids),
            "raw_proposal_sha256": hashlib.sha256(canonical_json_bytes(mapper_payload)).hexdigest(),
            "coverage_surfaces": mapper_payload["coverage_surfaces"],
            "relationships": list(keyed_relationships),
        }
        reference_projection = {
            "schema_version": CALIBRATION_SCHEMA_VERSION,
            "benchmark_id": reference.benchmark_id,
            "batch_id": batch_id,
            "source_ids": list(source_ids),
            "eligibility_gate": {"candidate_validity_minimum": 3, "candidate_utility_minimum": 2},
            "candidates": list(reference.candidates),
        }
        return (
            self._protocol_input(calibration_id, "independent-evaluation"),
            WorkspaceInput(
                source_id=calibration_id,
                destination="task.json",
                kind="focused_evaluation_task",
                content=canonical_json_bytes(task),
            ),
            WorkspaceInput(
                source_id=calibration_id,
                destination="focused-proposal.json",
                kind="focused_mapper_output",
                content=canonical_json_bytes(focused_projection),
            ),
            WorkspaceInput(
                source_id=calibration_id,
                destination="reference-candidates.json",
                kind="fixed_reference_candidates",
                content=canonical_json_bytes(reference_projection),
            ),
            *self._dossier_inputs(corpus, source_ids, calibration_id=calibration_id),
        )

    def _dossier_inputs(
        self,
        corpus: FrozenBenchmarkCorpus,
        source_ids: Sequence[str],
        *,
        calibration_id: str,
    ) -> tuple[WorkspaceInput, ...]:
        return tuple(
            WorkspaceInput(
                source_id=calibration_id,
                destination=f"dossiers/{source_id}.md",
                kind="canonical_source_dossier",
                source_path=self.calibration_root / "vault" / "sources" / source_id / "paper.md",
            )
            for source_id in source_ids
        )

    def _protocol_input(self, calibration_id: str, stage: str) -> WorkspaceInput:
        return WorkspaceInput(
            source_id=calibration_id,
            destination="AGENTS.md",
            kind="focused_calibration_protocol",
            area="root",
            source_path=self.protocol_directory / self.stage_definitions[stage][2],
        )

    def _run_stage(
        self,
        *,
        root: Path,
        stage: str,
        calibration_id: str,
        model: str,
        source_ids: tuple[str, ...],
        inputs: tuple[WorkspaceInput, ...],
    ) -> tuple[dict[str, Any], bool]:
        workspace_kind, schema_name, _ = self.stage_definitions[stage]
        try:
            workspace = CrossReferenceWorkspaceBuilder(
                root / "jobs" / stage,
                batch_id=calibration_id,
                job_id=_stable_job_id(
                    calibration_id, stage, contract_version=self.contract_version
                ),
                source_ids=source_ids,
            ).build(kind=workspace_kind, inputs=inputs)
        except WorkspaceError as error:
            raise FocusedCrossReferenceCalibrationError(
                f"focused calibration stage {stage} workspace is invalid: {error}"
            ) from error
        output_path = workspace.path / "output" / "result.json"
        events_path = root / "events" / f"{stage}.jsonl"
        receipt_path = root / "receipts" / f"{stage}.json"
        schema_path = self.schema_directory / schema_name
        existing = (output_path.exists(), events_path.exists(), receipt_path.exists())
        if any(existing):
            if not all(existing):
                raise FocusedCrossReferenceCalibrationError(
                    f"retained {stage} stage is incomplete; refusing semantic replacement"
                )
            return (
                self._validate_retained_stage(
                    stage=stage,
                    workspace_manifest=workspace.manifest_path,
                    output_path=output_path,
                    events_path=events_path,
                    receipt_path=receipt_path,
                    schema_path=schema_path,
                    model=model,
                ),
                True,
            )

        if self._codex_runner is None:
            try:
                self._codex_runner = CodexRunner(self.codex_executable)
            except CodexCapabilityError as error:
                raise FocusedCrossReferenceCalibrationExecutionError(str(error)) from error
        result = self._codex_runner.run(
            CodexJob(
                job_id=_stable_job_id(
                    calibration_id, stage, contract_version=self.contract_version
                ),
                source_id=calibration_id,
                model=model,
                prompt=(
                    "Read AGENTS.md and input/task.json. Inspect every declared input needed "
                    "for this calibration stage. Return exactly the schema-constrained result."
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
            raise FocusedCrossReferenceCalibrationExecutionError(
                f"focused calibration stage {stage} failed: {result.failure_class}; "
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
            raise FocusedCrossReferenceCalibrationError(
                f"retained {stage} receipt is unreadable"
            ) from error
        if receipt.get("kind") != "codex_job" or receipt.get("model") != model:
            raise FocusedCrossReferenceCalibrationError(
                f"retained {stage} receipt identity changed"
            )
        validation = receipt.get("validation")
        if not isinstance(validation, dict) or validation.get("valid") is not True:
            raise FocusedCrossReferenceCalibrationError(f"retained {stage} receipt is not valid")
        if receipt.get("workspace_manifest_sha256") != fingerprint(workspace_manifest).sha256:
            raise FocusedCrossReferenceCalibrationError(
                f"retained {stage} workspace lineage changed"
            )
        schema_receipt = receipt.get("output_schema")
        if (
            not isinstance(schema_receipt, dict)
            or schema_receipt.get("sha256") != fingerprint(schema_path).sha256
        ):
            raise FocusedCrossReferenceCalibrationError(f"retained {stage} schema lineage changed")
        observed_outputs = {
            (str(item.get("sha256")), int(item.get("size_bytes", -1)))
            for item in receipt.get("outputs", [])
            if isinstance(item, dict)
        }
        for path in (events_path, output_path):
            observed = fingerprint(path)
            if (observed.sha256, observed.size_bytes) not in observed_outputs:
                raise FocusedCrossReferenceCalibrationError(
                    f"retained {stage} output fingerprint changed"
                )
        payload = _read_json_object(output_path, label=f"retained {stage} output")
        _validate_json_schema(payload, schema_path=schema_path)
        return payload

    def _stage_usage(self, root: Path) -> dict[str, Any]:
        stages: dict[str, dict[str, Any]] = {}
        totals: dict[str, int | float] = {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "uncached_input_tokens": 0,
            "output_tokens": 0,
            "effective_tokens": 0,
            "duration_seconds": 0.0,
        }
        for stage in self.stage_definitions:
            receipt = read_receipt(root / "receipts" / f"{stage}.json")
            events = receipt.get("events")
            last_event = events.get("last_event", {}) if isinstance(events, dict) else {}
            usage = last_event.get("usage", {}) if isinstance(last_event, dict) else {}
            process = receipt.get("process", {})
            input_tokens = _nonnegative_int(usage.get("input_tokens"))
            cached_tokens = _nonnegative_int(usage.get("cached_input_tokens"))
            output_tokens = _nonnegative_int(usage.get("output_tokens"))
            duration = (
                float(process.get("duration_seconds", 0.0)) if isinstance(process, dict) else 0.0
            )
            item = {
                "input_tokens": input_tokens,
                "cached_input_tokens": cached_tokens,
                "uncached_input_tokens": max(0, input_tokens - cached_tokens),
                "output_tokens": output_tokens,
                "effective_tokens": max(0, input_tokens - cached_tokens) + output_tokens,
                "duration_seconds": _rounded(duration),
            }
            stages[stage] = item
            for field in (
                "input_tokens",
                "cached_input_tokens",
                "uncached_input_tokens",
                "output_tokens",
                "effective_tokens",
            ):
                totals[field] = int(totals[field]) + int(item[field])
            totals["duration_seconds"] = float(totals["duration_seconds"]) + duration
        totals["duration_seconds"] = _rounded(float(totals["duration_seconds"]))
        return {"semantic_call_count": 2, "stages": stages, "totals": totals}

    def _record_stage_validation(
        self,
        *,
        root: Path,
        stage: str,
        output_path: Path,
        validated_ids: Sequence[str],
        extra: Mapping[str, Any],
    ) -> None:
        _write_or_verify_portable_json(
            root / "validations" / f"{stage}.json",
            {
                "schema_version": CALIBRATION_SCHEMA_VERSION,
                "kind": self.stage_validation_kind,
                "contract_version": self.contract_version,
                "stage": stage,
                "output": fingerprint(output_path, relative_to=root).to_dict(),
                "semantic_validation": "passed",
                "validated_item_count": len(validated_ids),
                "validated_ids": list(validated_ids),
                **dict(extra),
                "map_mutation_authorized": False,
            },
        )

    def _validate_mapper_output(
        self,
        payload: Mapping[str, Any],
        *,
        batch_id: str,
        source_ids: Sequence[str],
        records: Mapping[str, Record],
        evidence_ids_by_source: Mapping[str, frozenset[str]],
    ) -> tuple[dict[str, Any], ...]:
        return validate_focused_mapper_output(
            payload,
            batch_id=batch_id,
            source_ids=source_ids,
            records=records,
            evidence_ids_by_source=evidence_ids_by_source,
        )

    def _mapper_validation_extra(
        self,
        mapper_payload: Mapping[str, Any],
        keyed_relationships: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        return {"pair_surface_count": len(mapper_payload["coverage_surfaces"])}

    def _compile_metrics(self, **kwargs: Any) -> dict[str, Any]:
        return compile_focused_calibration_metrics(**kwargs)

    def _render_report(self, metrics: Mapping[str, Any]) -> str:
        return render_focused_report(metrics)


def load_reference_candidates(
    root: Path,
    *,
    corpus: FrozenBenchmarkCorpus,
    expected_batch_id: str,
    expected_source_ids: Sequence[str],
) -> ReferenceCandidateSet:
    """Load and revalidate the fixed eligible candidate denominator."""

    resolved = root.resolve()
    _validate_reference_artifacts(resolved)
    manifest = _read_json_object(resolved / "manifest.json", label="reference manifest")
    benchmark_id = _nonempty_text(manifest.get("benchmark_id"), label="reference benchmark ID")
    if manifest.get("batch_id") != expected_batch_id:
        raise FocusedCrossReferenceCalibrationError("reference benchmark batch identity changed")
    if manifest.get("coverage_source_ids") != list(expected_source_ids):
        raise FocusedCrossReferenceCalibrationError("reference benchmark cohort changed")
    probe_path = resolved / "jobs" / "blind-probe" / "output" / "result.json"
    comparison_path = resolved / "jobs" / "probe-comparison" / "output" / "result.json"
    current_path = resolved / "jobs" / "probe-comparison" / "input" / "current-relationships.json"
    probe = _read_json_object(probe_path, label="reference probe output")
    comparison = _read_json_object(comparison_path, label="reference comparison output")
    current = _read_json_object(current_path, label="reference graph projection")
    _validate_json_schema(
        probe,
        schema_path=(
            Path(__file__).parents[2]
            / "schemas"
            / "research-map"
            / "v1"
            / "cross-reference-blind-probe-benchmark.schema.json"
        ),
    )
    _validate_json_schema(
        comparison,
        schema_path=(
            Path(__file__).parents[2]
            / "schemas"
            / "research-map"
            / "v1"
            / "cross-reference-probe-comparison-benchmark.schema.json"
        ),
    )
    try:
        candidates = validate_blind_probe_output(
            probe,
            benchmark_id=benchmark_id,
            coverage_source_ids=expected_source_ids,
            records=corpus.record_index,
            evidence_ids_by_source=corpus.evidence_ids_by_source,
        )
    except CrossReferenceBenchmarkError as error:
        raise FocusedCrossReferenceCalibrationError(
            f"reference probe semantic validation failed: {error}"
        ) from error
    raw_relationships = current.get("relationships")
    if not isinstance(raw_relationships, list):
        raise FocusedCrossReferenceCalibrationError(
            "reference graph relationship projection is invalid"
        )
    relationship_ids = [
        str(item.get("relationship_id"))
        for item in raw_relationships
        if isinstance(item, dict) and item.get("relationship_id") is not None
    ]
    try:
        matches = validate_probe_comparison_output(
            comparison,
            benchmark_id=benchmark_id,
            expected_candidate_keys=[str(item["candidate_key"]) for item in candidates],
            allowed_relationship_ids=relationship_ids,
        )
    except CrossReferenceBenchmarkError as error:
        raise FocusedCrossReferenceCalibrationError(
            f"reference comparison semantic validation failed: {error}"
        ) from error
    candidate_by_key = {str(item["candidate_key"]): item for item in candidates}
    eligible: list[dict[str, Any]] = []
    for match in matches:
        if int(match["candidate_validity"]) < 3 or int(match["candidate_utility"]) < 2:
            continue
        candidate = candidate_by_key[str(match["candidate_key"])]
        eligible.append(
            {
                **candidate,
                "reference_candidate_validity": int(match["candidate_validity"]),
                "reference_candidate_utility": int(match["candidate_utility"]),
                "existing_graph_match_quality": int(match["match_quality"]),
                "existing_graph_match_kind": str(match["match_kind"]),
            }
        )
    metrics = _read_json_object(resolved / "metrics.json", label="reference metrics")
    coverage = metrics.get("coverage_probe")
    if not isinstance(coverage, dict):
        raise FocusedCrossReferenceCalibrationError("reference coverage metrics are invalid")
    observed_coverage = float(coverage.get("weighted_probe_coverage", -1.0))
    importance_weight = sum(int(item["importance"]) for item in eligible)
    recomputed_coverage = (
        sum(
            int(item["importance"]) * int(item["existing_graph_match_quality"]) / 4
            for item in eligible
        )
        / importance_weight
        if importance_weight
        else -1.0
    )
    if (
        len(eligible) != ELIGIBLE_REFERENCE_COUNT
        or importance_weight != ELIGIBLE_REFERENCE_IMPORTANCE
        or abs(observed_coverage - EXISTING_GRAPH_BASELINE) > 1e-6
        or abs(recomputed_coverage - observed_coverage) > 1e-6
    ):
        raise FocusedCrossReferenceCalibrationError(
            "reference candidate denominator or baseline coverage changed"
        )
    return ReferenceCandidateSet(
        benchmark_id=benchmark_id,
        batch_id=expected_batch_id,
        source_ids=tuple(expected_source_ids),
        candidates=tuple(eligible),
        existing_graph_coverage=observed_coverage,
        importance_weight=importance_weight,
        input_fingerprints=_fingerprint_tree(resolved),
    )


def _validate_reference_artifacts(root: Path) -> None:
    benchmark_receipt = _read_json_object(
        root / "benchmark-receipt.json", label="reference benchmark receipt"
    )
    if (
        benchmark_receipt.get("schema_version") != CALIBRATION_SCHEMA_VERSION
        or benchmark_receipt.get("kind") != "cross_reference_quality_benchmark"
        or benchmark_receipt.get("frozen_inputs_unchanged") is not True
        or benchmark_receipt.get("map_mutation_authorized") is not False
        or benchmark_receipt.get("semantic_stage_count") != 3
    ):
        raise FocusedCrossReferenceCalibrationError(
            "reference benchmark receipt is not an accepted immutable benchmark"
        )
    for name, path in (
        ("manifest", root / "manifest.json"),
        ("metrics", root / "metrics.json"),
        ("report", root / "report.md"),
    ):
        _require_receipted_file(benchmark_receipt.get(name), path=path, label=f"reference {name}")

    stage_schemas = {
        "edge-audit": "cross-reference-edge-audit-benchmark.schema.json",
        "blind-probe": "cross-reference-blind-probe-benchmark.schema.json",
        "probe-comparison": "cross-reference-probe-comparison-benchmark.schema.json",
    }
    schema_root = Path(__file__).parents[2] / "schemas" / "research-map" / "v1"
    for stage, schema_name in stage_schemas.items():
        receipt_path = root / "receipts" / f"{stage}.json"
        try:
            receipt = read_receipt(receipt_path)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise FocusedCrossReferenceCalibrationError(
                f"reference {stage} receipt is unreadable"
            ) from error
        validation = receipt.get("validation")
        events = receipt.get("events")
        if (
            receipt.get("kind") != "codex_job"
            or not isinstance(validation, dict)
            or validation.get("valid") is not True
            or not isinstance(events, dict)
            or events.get("terminal_event_seen") is not True
        ):
            raise FocusedCrossReferenceCalibrationError(
                f"reference {stage} semantic receipt is not terminal and valid"
            )
        workspace_manifest = root / "jobs" / stage / "manifest.json"
        if receipt.get("workspace_manifest_sha256") != fingerprint(workspace_manifest).sha256:
            raise FocusedCrossReferenceCalibrationError(
                f"reference {stage} workspace lineage changed"
            )
        schema_receipt = receipt.get("output_schema")
        if (
            not isinstance(schema_receipt, dict)
            or schema_receipt.get("sha256") != fingerprint(schema_root / schema_name).sha256
        ):
            raise FocusedCrossReferenceCalibrationError(f"reference {stage} schema lineage changed")
        output_path = root / "jobs" / stage / "output" / "result.json"
        events_path = root / "events" / f"{stage}.jsonl"
        for path in (output_path, events_path):
            observed = fingerprint(path)
            receipted_outputs = receipt.get("outputs")
            if not isinstance(receipted_outputs, list) or not any(
                isinstance(item, dict)
                and item.get("sha256") == observed.sha256
                and item.get("size_bytes") == observed.size_bytes
                for item in receipted_outputs
            ):
                raise FocusedCrossReferenceCalibrationError(
                    f"reference {stage} semantic artifact fingerprint changed"
                )
        stage_validation = _read_json_object(
            root / "validations" / f"{stage}.json",
            label=f"reference {stage} validation",
        )
        if (
            stage_validation.get("stage") != stage
            or stage_validation.get("semantic_validation") != "passed"
            or stage_validation.get("map_mutation_authorized") is not False
        ):
            raise FocusedCrossReferenceCalibrationError(
                f"reference {stage} deterministic validation is invalid"
            )
        _require_receipted_file(
            stage_validation.get("output"),
            path=output_path,
            label=f"reference {stage} validated output",
        )


def _require_receipted_file(value: Any, *, path: Path, label: str) -> None:
    if not isinstance(value, dict):
        raise FocusedCrossReferenceCalibrationError(f"{label} fingerprint is missing")
    observed = fingerprint(path)
    if value.get("sha256") != observed.sha256 or value.get("size_bytes") != observed.size_bytes:
        raise FocusedCrossReferenceCalibrationError(f"{label} fingerprint changed")


def validate_focused_mapper_output(
    payload: Mapping[str, Any],
    *,
    batch_id: str,
    source_ids: Sequence[str],
    records: Mapping[str, Record],
    evidence_ids_by_source: Mapping[str, frozenset[str]],
) -> tuple[dict[str, Any], ...]:
    """Validate product-compatible mapper rows and exact pair attention."""

    if payload.get("schema_version") != 1 or payload.get("batch_id") != batch_id:
        raise FocusedCrossReferenceCalibrationError("focused mapper identity changed")
    if payload.get("source_ids") != list(source_ids):
        raise FocusedCrossReferenceCalibrationError("focused mapper source identities changed")
    if payload.get("existing_relationship_reviews") != []:
        raise FocusedCrossReferenceCalibrationError(
            "graph-blind mapper must not review existing relationships"
        )
    raw_surfaces = payload.get("coverage_surfaces")
    raw_relationships = payload.get("relationships")
    if not isinstance(raw_surfaces, list) or not isinstance(raw_relationships, list):
        raise FocusedCrossReferenceCalibrationError(
            "focused mapper requires surfaces and relationships"
        )
    surfaces = [_mapping(item, label="focused pair surface") for item in raw_surfaces]
    relationships = [
        _mapping(item, label="focused proposed relationship") for item in raw_relationships
    ]
    expected_pairs = set(itertools.combinations(source_ids, 2))
    surface_by_key: dict[str, dict[str, Any]] = {}
    surface_pairs: dict[str, tuple[str, str]] = {}
    for surface in surfaces:
        key = _nonempty_text(surface.get("surface_key"), label="pair surface key")
        if key in surface_by_key:
            raise FocusedCrossReferenceCalibrationError("pair surface keys must be unique")
        record_ids = surface.get("source_record_ids")
        if (
            not isinstance(record_ids, list)
            or len(record_ids) < 2
            or len(set(record_ids)) != len(record_ids)
        ):
            raise FocusedCrossReferenceCalibrationError(
                "pair surface record IDs must be unique and non-empty"
            )
        surface_sources: set[str] = set()
        for record_id in record_ids:
            record = records.get(str(record_id))
            if (
                record is None
                or record.record_type != "atom"
                or record.payload.get("connectable") is not True
            ):
                raise FocusedCrossReferenceCalibrationError(
                    "pair surface cites an unknown or non-connectable atom"
                )
            if record.source_id not in source_ids:
                raise FocusedCrossReferenceCalibrationError(
                    "pair surface record is outside the cohort"
                )
            surface_sources.add(record.source_id)
        if len(surface_sources) != 2:
            raise FocusedCrossReferenceCalibrationError(
                "each pair surface must contain records from exactly two sources"
            )
        pair = tuple(sorted(surface_sources))
        if pair not in expected_pairs:
            raise FocusedCrossReferenceCalibrationError("pair surface is outside ownership")
        if pair in surface_pairs.values():
            raise FocusedCrossReferenceCalibrationError(
                "focused mapper must return exactly one surface per source pair"
            )
        disposition = surface.get("disposition")
        if disposition not in {
            "covered_by_proposal",
            "no_useful_relationship",
            "manual_review_candidate",
        }:
            raise FocusedCrossReferenceCalibrationError(
                "graph-blind pair surface has an invalid disposition"
            )
        if surface.get("existing_relationship_ids") != []:
            raise FocusedCrossReferenceCalibrationError(
                "graph-blind pair surface cannot cite existing relationships"
            )
        _nonempty_text(surface.get("label"), label="pair surface label")
        _nonempty_text(surface.get("rationale"), label="pair surface rationale")
        surface_by_key[key] = surface
        surface_pairs[key] = pair
    if set(surface_pairs.values()) != expected_pairs or len(surfaces) != len(expected_pairs):
        raise FocusedCrossReferenceCalibrationError(
            "focused mapper did not account for every source pair exactly once"
        )

    semantic_keys: set[tuple[str, str]] = set()
    relationships_by_surface: Counter[str] = Counter()
    keyed: list[dict[str, Any]] = []
    for relationship in relationships:
        surface_key = str(relationship.get("surface_key", ""))
        relationship_surface = surface_by_key.get(surface_key)
        if relationship_surface is None:
            raise FocusedCrossReferenceCalibrationError(
                "focused relationship cites an unknown pair surface"
            )
        left_id = str(relationship.get("left_record_id", ""))
        right_id = str(relationship.get("right_record_id", ""))
        left = _connectable_atom(left_id, records=records, source_ids=source_ids)
        right = _connectable_atom(right_id, records=records, source_ids=source_ids)
        if left.source_id == right.source_id:
            raise FocusedCrossReferenceCalibrationError(
                "focused relationship endpoints must be cross-source"
            )
        if tuple(sorted((left.source_id, right.source_id))) != surface_pairs[surface_key]:
            raise FocusedCrossReferenceCalibrationError(
                "focused relationship endpoints do not match their pair surface"
            )
        if (
            left_id not in relationship_surface["source_record_ids"]
            or right_id not in relationship_surface["source_record_ids"]
        ):
            raise FocusedCrossReferenceCalibrationError(
                "focused relationship endpoints are absent from their pair surface"
            )
        relation_type = relationship.get("relation_type")
        direction = relationship.get("direction")
        if relation_type not in RELATIONSHIP_TYPES or direction not in RELATIONSHIP_DIRECTIONS:
            raise FocusedCrossReferenceCalibrationError("focused relationship posture is invalid")
        if relation_type in SYMMETRIC_RELATIONSHIP_TYPES and direction != "symmetric":
            raise FocusedCrossReferenceCalibrationError(
                "focused symmetric type has invalid direction"
            )
        if relation_type in DIRECTED_RELATIONSHIP_TYPES and direction == "symmetric":
            raise FocusedCrossReferenceCalibrationError(
                "focused directed type requires a direction"
            )
        _validate_evidence(
            relationship.get("left_evidence_ids"),
            source_id=left.source_id,
            known=evidence_ids_by_source,
        )
        _validate_evidence(
            relationship.get("right_evidence_ids"),
            source_id=right.source_id,
            known=evidence_ids_by_source,
        )
        for field in (
            "comparison_surface",
            "scope_alignment",
            "assumption_alignment",
            "rationale",
        ):
            _nonempty_text(relationship.get(field), label=f"focused relationship {field}")
        _reject_adjudicative_text(relationship)
        endpoint_key = (min(left_id, right_id), max(left_id, right_id))
        if endpoint_key in semantic_keys:
            raise FocusedCrossReferenceCalibrationError(
                "focused mapper repeated an unordered endpoint pair"
            )
        semantic_keys.add(endpoint_key)
        proposal_key = (
            "focus_rel_" + hashlib.sha256(canonical_json_bytes(relationship)).hexdigest()[:24]
        )
        if any(item["proposal_key"] == proposal_key for item in keyed):
            raise FocusedCrossReferenceCalibrationError("focused proposal key collision")
        keyed.append({"proposal_key": proposal_key, **relationship})
        relationships_by_surface[surface_key] += 1
    for key, surface in surface_by_key.items():
        has_relationships = relationships_by_surface[key] > 0
        if (surface["disposition"] == "covered_by_proposal") != has_relationships:
            raise FocusedCrossReferenceCalibrationError(
                "pair surface disposition does not match proposed relationships"
            )
    return tuple(keyed)


def validate_focused_evaluation_output(
    payload: Mapping[str, Any],
    *,
    calibration_id: str,
    keyed_relationships: Sequence[Mapping[str, Any]],
    reference_candidates: Sequence[Mapping[str, Any]],
) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    """Require complete independent accounting for proposals and reference candidates."""

    if (
        payload.get("schema_version") != CALIBRATION_SCHEMA_VERSION
        or payload.get("calibration_id") != calibration_id
    ):
        raise FocusedCrossReferenceCalibrationError("focused evaluation identity changed")
    raw_audits = payload.get("proposal_audits")
    raw_matches = payload.get("reference_matches")
    if not isinstance(raw_audits, list) or not isinstance(raw_matches, list):
        raise FocusedCrossReferenceCalibrationError(
            "focused evaluation requires proposal audits and reference matches"
        )
    audits = tuple(_mapping(item, label="focused proposal audit") for item in raw_audits)
    matches = tuple(_mapping(item, label="focused reference match") for item in raw_matches)
    proposal_by_key = {str(item["proposal_key"]): item for item in keyed_relationships}
    _require_exact_ids(
        [str(item.get("proposal_key", "")) for item in audits],
        list(proposal_by_key),
        label="focused proposal",
    )
    for audit in audits:
        proposal = proposal_by_key[str(audit["proposal_key"])]
        scores = _mapping(audit.get("scores"), label="focused audit scores")
        for field in AUDIT_SCORE_FIELDS:
            _score(scores.get(field), label=field)
        if audit.get("disposition") not in AUDIT_DISPOSITIONS:
            raise FocusedCrossReferenceCalibrationError("focused audit disposition is invalid")
        assessment = audit.get("relation_type_assessment")
        recommended = audit.get("recommended_relation_type")
        gap_label = audit.get("ontology_gap_label")
        if assessment not in TYPE_ASSESSMENTS or recommended not in RECOMMENDED_TYPES:
            raise FocusedCrossReferenceCalibrationError(
                "focused relation-type assessment is invalid"
            )
        if assessment == "fit" and recommended != "current_type_is_best":
            raise FocusedCrossReferenceCalibrationError(
                "fit assessment must retain the current relationship type"
            )
        if assessment == "adjacent" and (
            recommended not in RELATIONSHIP_TYPES or recommended == proposal.get("relation_type")
        ):
            raise FocusedCrossReferenceCalibrationError(
                "adjacent assessment must name a different existing type"
            )
        if assessment == "wrong" and (
            recommended not in RELATIONSHIP_TYPES | frozenset({"uncertain"})
            or recommended == proposal.get("relation_type")
        ):
            raise FocusedCrossReferenceCalibrationError(
                "wrong assessment must name a different existing type or remain uncertain"
            )
        if assessment == "ontology_gap":
            if recommended != "no_current_type_fits" or not str(gap_label).strip():
                raise FocusedCrossReferenceCalibrationError(
                    "ontology-gap assessment must name its inexpressible comparison"
                )
        elif gap_label != "":
            raise FocusedCrossReferenceCalibrationError(
                "non-gap assessment cannot carry an ontology-gap label"
            )
        if assessment == "uncertain" and recommended != "uncertain":
            raise FocusedCrossReferenceCalibrationError(
                "uncertain assessment must retain uncertain recommendation"
            )
        _nonempty_text(audit.get("rationale"), label="focused audit rationale")

    reference_keys = [str(item["candidate_key"]) for item in reference_candidates]
    _require_exact_ids(
        [str(item.get("candidate_key", "")) for item in matches],
        reference_keys,
        label="reference candidate",
    )
    allowed_proposals = set(proposal_by_key)
    for match in matches:
        match_quality = _score(match.get("match_quality"), label="reference match quality")
        if match.get("match_kind") not in MATCH_KINDS:
            raise FocusedCrossReferenceCalibrationError("reference match kind is invalid")
        raw_keys = match.get("matched_proposal_keys")
        if not isinstance(raw_keys, list) or not all(isinstance(item, str) for item in raw_keys):
            raise FocusedCrossReferenceCalibrationError("reference match proposal keys are invalid")
        if len(raw_keys) != len(set(raw_keys)) or set(raw_keys) - allowed_proposals:
            raise FocusedCrossReferenceCalibrationError(
                "reference match cites unknown or duplicate focused proposals"
            )
        if match["match_kind"] == "none" and raw_keys:
            raise FocusedCrossReferenceCalibrationError(
                "unmatched reference candidate cannot cite focused proposals"
            )
        if match["match_kind"] == "none" and match_quality != 0:
            raise FocusedCrossReferenceCalibrationError(
                "unmatched reference candidate must have zero match quality"
            )
        if match["match_kind"] != "none" and not raw_keys:
            raise FocusedCrossReferenceCalibrationError(
                "matched reference candidate must cite a focused proposal"
            )
        if match["match_kind"] != "none" and match_quality == 0:
            raise FocusedCrossReferenceCalibrationError(
                "matched reference candidate must have positive match quality"
            )
        _nonempty_text(match.get("rationale"), label="reference match rationale")
    return audits, matches


def compile_focused_calibration_metrics(
    *,
    calibration_id: str,
    batch_id: str,
    source_ids: Sequence[str],
    mapper_payload: Mapping[str, Any],
    keyed_relationships: Sequence[Mapping[str, Any]],
    proposal_audits: Sequence[Mapping[str, Any]],
    reference: ReferenceCandidateSet,
    reference_matches: Sequence[Mapping[str, Any]],
    stage_usage: Mapping[str, Any],
) -> dict[str, Any]:
    """Compile continuous quality, coverage, breadth, and cost metrics separately."""

    reference_by_key = {str(item["candidate_key"]): item for item in reference.candidates}
    weighted_numerator = sum(
        int(reference_by_key[str(item["candidate_key"])]["importance"])
        * int(item["match_quality"])
        / 4
        for item in reference_matches
    )
    focused_coverage = weighted_numerator / reference.importance_weight
    useful = [item for item in proposal_audits if _audit_item_is_useful(item)]
    score_means = {
        field: _rounded(
            sum(
                int(_mapping(item["scores"], label="audit scores")[field])
                for item in proposal_audits
            )
            / len(proposal_audits)
        )
        if proposal_audits
        else None
        for field in AUDIT_SCORE_FIELDS
    }
    type_assessments = Counter(str(item["relation_type_assessment"]) for item in proposal_audits)
    relation_types = Counter(str(item["relation_type"]) for item in keyed_relationships)
    proposed_pairs = {
        tuple(
            sorted(
                (
                    str(item["left_record_id"]).split(":", 1)[0],
                    str(item["right_record_id"]).split(":", 1)[0],
                )
            )
        )
        for item in keyed_relationships
    }
    stages = _mapping(stage_usage.get("stages"), label="stage usage")
    mapper_usage = _mapping(stages.get("focused-mapper"), label="mapper usage")
    mapper_effective = int(mapper_usage.get("effective_tokens", 0))
    useful_rate = len(useful) / len(proposal_audits) if proposal_audits else 0.0
    relation_type_mean = score_means["relation_type_fit"]
    return {
        "schema_version": CALIBRATION_SCHEMA_VERSION,
        "contract_version": CALIBRATION_CONTRACT_VERSION,
        "calibration_id": calibration_id,
        "batch_id": batch_id,
        "source_ids": list(source_ids),
        "mapper": {
            "pair_surface_count": len(mapper_payload["coverage_surfaces"]),
            "proposal_count": len(keyed_relationships),
            "proposed_source_pair_count": len(proposed_pairs),
            "possible_source_pair_count": 10,
            "proposed_source_pair_breadth": _rounded(len(proposed_pairs) / 10),
            "relationship_types": dict(sorted(relation_types.items())),
        },
        "proposal_quality": {
            "audited_proposal_count": len(proposal_audits),
            "useful_proposal_count": len(useful),
            "useful_proposal_rate": _rounded(useful_rate),
            "score_means": score_means,
            "relation_type_assessments": dict(sorted(type_assessments.items())),
            "ontology_gap_count": type_assessments["ontology_gap"],
        },
        "reference_coverage": {
            "interpretation": "model-assisted fixed-cohort calibration; not objective recall",
            "reference_benchmark_id": reference.benchmark_id,
            "eligible_candidate_count": len(reference.candidates),
            "importance_weight_denominator": reference.importance_weight,
            "focused_weighted_match_numerator": _rounded(weighted_numerator),
            "focused_weighted_coverage": _rounded(focused_coverage),
            "existing_graph_weighted_coverage": _rounded(reference.existing_graph_coverage),
            "coverage_delta": _rounded(focused_coverage - reference.existing_graph_coverage),
        },
        "diagnostic_signals": {
            "coverage_delta_positive": focused_coverage > reference.existing_graph_coverage,
            "mean_relation_type_fit_at_least_3": (
                relation_type_mean is not None and relation_type_mean >= 3
            ),
            "useful_proposal_rate_at_least_two_thirds": useful_rate >= 2 / 3,
            "automatic_adoption_authorized": False,
        },
        "cost": {
            "stage_usage": stage_usage,
            "focused_mapper_effective_tokens_per_useful_proposal": (
                _rounded(mapper_effective / len(useful)) if useful else None
            ),
            "focused_mapper_effective_tokens_per_captured_importance_unit": (
                _rounded(mapper_effective / weighted_numerator) if weighted_numerator else None
            ),
            "evaluation_overhead_reported_separately": True,
        },
        "map_mutation_authorized": False,
    }


def render_focused_report(metrics: Mapping[str, Any]) -> str:
    mapper = _mapping(metrics["mapper"], label="mapper metrics")
    quality = _mapping(metrics["proposal_quality"], label="quality metrics")
    coverage = _mapping(metrics["reference_coverage"], label="coverage metrics")
    signals = _mapping(metrics["diagnostic_signals"], label="diagnostic signals")
    cost = _mapping(metrics["cost"], label="cost metrics")
    usage = _mapping(cost["stage_usage"], label="stage usage")
    stages = _mapping(usage["stages"], label="usage stages")
    lines = [
        "# Focused cross-reference calibration",
        "",
        f"Calibration: `{metrics['calibration_id']}`",
        "",
        "This is model-assisted calibration evidence. It does not adjudicate scientific "
        "truth, establish objective recall, or modify the research map.",
        "",
        "## Outcome",
        "",
        f"- Focused mapper proposals: {mapper['proposal_count']} across "
        f"{mapper['proposed_source_pair_count']} of 10 source pairs.",
        f"- Useful proposals: {quality['useful_proposal_count']} of "
        f"{quality['audited_proposal_count']} ({quality['useful_proposal_rate']}).",
        f"- Mean relation-type fit: {quality['score_means']['relation_type_fit']}.",
        f"- Evaluator-side ontology gaps: {quality['ontology_gap_count']}.",
        f"- Focused weighted reference coverage: {coverage['focused_weighted_coverage']}.",
        f"- Existing graph weighted reference coverage: "
        f"{coverage['existing_graph_weighted_coverage']}.",
        f"- Coverage delta: {coverage['coverage_delta']}.",
        "",
        "## Predeclared diagnostic signals",
        "",
        f"- Positive coverage delta: {signals['coverage_delta_positive']}.",
        f"- Mean relation-type fit at least 3: {signals['mean_relation_type_fit_at_least_3']}.",
        f"- Useful-proposal rate at least two thirds: "
        f"{signals['useful_proposal_rate_at_least_two_thirds']}.",
        "- These signals do not automatically authorize adoption.",
        "",
        "## Cost",
        "",
        "| Stage | Input | Cached input | Uncached input | Output | Effective | Time |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for stage in STAGE_DEFINITIONS:
        item = _mapping(stages[stage], label=f"{stage} usage")
        lines.append(
            f"| {stage} | {item['input_tokens']} | {item['cached_input_tokens']} | "
            f"{item['uncached_input_tokens']} | {item['output_tokens']} | "
            f"{item['effective_tokens']} | {item['duration_seconds']} s |"
        )
    lines.extend(
        (
            "",
            f"Mapper effective tokens per useful proposal: "
            f"{_display_metric(cost['focused_mapper_effective_tokens_per_useful_proposal'])}.",
            "Mapper effective tokens per captured reference-importance unit: "
            f"{_display_metric(cost['focused_mapper_effective_tokens_per_captured_importance_unit'])}.",
            "Generation and evaluation overhead remain separate; no composite score is used.",
            "",
            "## Relation types",
            "",
            f"- Proposed types: `{json.dumps(mapper['relationship_types'], sort_keys=True)}`",
            f"- Evaluator assessments: "
            f"`{json.dumps(quality['relation_type_assessments'], sort_keys=True)}`",
            "",
            "## Boundary",
            "",
            "The mapper saw only the five dossiers, task, and protocol. It did not receive "
            "the graph or reference candidates. The evaluator compared the unchanged proposal "
            "with the fixed 25-candidate denominator. No relationship was admitted, repaired, "
            "relabeled, or written to the map.",
            "",
        )
    )
    return "\n".join(lines)


def _connectable_atom(
    record_id: str,
    *,
    records: Mapping[str, Record],
    source_ids: Sequence[str],
) -> Record:
    record = records.get(record_id)
    if (
        record is None
        or record.record_type != "atom"
        or record.payload.get("connectable") is not True
    ):
        raise FocusedCrossReferenceCalibrationError(
            "focused relationship endpoint does not resolve to a connectable atom"
        )
    if record.source_id not in source_ids:
        raise FocusedCrossReferenceCalibrationError(
            "focused relationship endpoint is outside the cohort"
        )
    return record


def _load_frozen_corpus(root: Path, *, batch_id: str) -> FrozenBenchmarkCorpus:
    try:
        return load_frozen_benchmark_corpus(root, batch_id=batch_id)
    except CrossReferenceBenchmarkError as error:
        raise FocusedCrossReferenceCalibrationError(
            f"frozen calibration corpus is invalid: {error}"
        ) from error


def _validate_evidence(
    value: Any,
    *,
    source_id: str,
    known: Mapping[str, frozenset[str]],
) -> None:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) for item in value)
        or len(set(value)) != len(value)
        or set(value) - known.get(source_id, frozenset())
    ):
        raise FocusedCrossReferenceCalibrationError(
            "focused relationship evidence does not resolve on its endpoint side"
        )


def _audit_item_is_useful(item: Mapping[str, Any]) -> bool:
    scores = _mapping(item.get("scores"), label="audit scores")
    return (
        int(scores["evidence_grounding"]) >= 3
        and int(scores["relation_type_fit"]) >= 3
        and int(scores["scope_assumption_fit"]) >= 3
        and int(scores["research_utility"]) >= 2
        and int(scores["distinctiveness"]) >= 2
        and item.get("disposition") in {"strong", "usable"}
    )


def _require_exact_ids(observed: Sequence[str], expected: Sequence[str], *, label: str) -> None:
    if len(observed) != len(set(observed)) or set(observed) != set(expected):
        raise FocusedCrossReferenceCalibrationError(f"{label} IDs do not match")


def _stable_job_id(
    calibration_id: str,
    stage: str,
    *,
    contract_version: str = CALIBRATION_CONTRACT_VERSION,
) -> str:
    digest = hashlib.sha256(f"{contract_version}:{calibration_id}:{stage}".encode()).hexdigest()
    return f"focused_xref_job_{digest[:24]}"


def _combined_input_fingerprint(
    corpus: FrozenBenchmarkCorpus, reference: ReferenceCandidateSet
) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "corpus": list(corpus.input_fingerprints),
                "reference": list(reference.input_fingerprints),
            }
        )
    ).hexdigest()


def _fingerprint_tree(root: Path) -> tuple[dict[str, Any], ...]:
    if not root.is_dir():
        raise FocusedCrossReferenceCalibrationError(f"reference benchmark root is missing: {root}")
    return tuple(
        fingerprint(path, relative_to=root).to_dict()
        for path in sorted(item for item in root.rglob("*") if item.is_file())
    )


def _fingerprint_inventory(items: Sequence[Mapping[str, Any]]) -> str:
    return hashlib.sha256(canonical_json_bytes(list(items))).hexdigest()


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FocusedCrossReferenceCalibrationError(f"{label} is unreadable: {path}") from error
    if not isinstance(value, dict):
        raise FocusedCrossReferenceCalibrationError(f"{label} must be an object")
    return value


def _validate_json_schema(payload: Mapping[str, Any], *, schema_path: Path) -> None:
    schema = _read_json_object(schema_path, label="focused calibration schema")
    errors = sorted(
        Draft202012Validator(schema).iter_errors(dict(payload)), key=lambda item: list(item.path)
    )
    if errors:
        location = ".".join(str(part) for part in errors[0].path) or "record"
        raise FocusedCrossReferenceCalibrationError(f"{location}: {errors[0].message}")


def _write_or_verify_json(path: Path, value: Mapping[str, Any]) -> None:
    _write_or_verify_bytes(path, canonical_json_bytes(value))


def _write_or_verify_portable_json(path: Path, value: Mapping[str, Any]) -> None:
    if not path.exists():
        _write_or_verify_json(path, value)
        return
    retained = _read_json_object(path, label="retained focused calibration artifact")
    normalized = _normalize_fingerprint_paths(retained, value)
    if canonical_json_bytes(normalized) != canonical_json_bytes(value):
        raise FocusedCrossReferenceCalibrationError(
            f"retained focused calibration artifact changed: {path}"
        )


def _normalize_fingerprint_paths(retained: Any, expected: Any) -> Any:
    if isinstance(retained, dict) and isinstance(expected, Mapping):
        normalized = {
            key: _normalize_fingerprint_paths(item, expected.get(key))
            for key, item in retained.items()
        }
        fields = {"path", "sha256", "size_bytes"}
        if fields.issubset(expected) and fields.issubset(normalized):
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
            _normalize_fingerprint_paths(item, expected[index] if index < len(expected) else None)
            for index, item in enumerate(retained)
        ]
    return retained


def _write_or_verify_bytes(path: Path, data: bytes) -> None:
    if path.exists():
        if path.read_bytes() != data:
            raise FocusedCrossReferenceCalibrationError(
                f"retained focused calibration artifact changed: {path}"
            )
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


def _mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FocusedCrossReferenceCalibrationError(f"{label} must be an object")
    return value


def _score(value: Any, *, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 4:
        raise FocusedCrossReferenceCalibrationError(f"{label} must be an integer from 0 to 4")
    return value


def _nonempty_text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FocusedCrossReferenceCalibrationError(f"{label} must be non-empty")
    return value.strip()


def _reject_adjudicative_text(value: Mapping[str, Any]) -> None:
    text = json.dumps(value, ensure_ascii=False).lower()
    matched = next((phrase for phrase in _ADJUDICATIVE_PHRASES if phrase in text), None)
    if matched is not None:
        raise FocusedCrossReferenceCalibrationError(
            f"focused proposal crosses the non-adjudication boundary: {matched.strip()}"
        )


def _require_safe_identifier(value: str, *, label: str) -> None:
    if not _SAFE_IDENTIFIER.fullmatch(value):
        raise FocusedCrossReferenceCalibrationError(f"{label} is not a safe path component")


def _nonnegative_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _rounded(value: float) -> float:
    return round(value, 6)


def _display_metric(value: Any) -> str:
    return "undefined" if value is None else str(value)
