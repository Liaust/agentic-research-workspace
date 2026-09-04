"""One-call map-first visual source-reading coordinator."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from research_map.codex import CodexCapabilityError, CodexJob, CodexRunner
from research_map.coverage import BatchPlan, LandmarkPlan, ScopePlan, initial_coverage
from research_map.ids import job_id, run_id
from research_map.map_first_admission import validate_map_first_proposal_file
from research_map.proposals import ManualReviewRequired, ProposalValidationError
from research_map.reading import (
    ReadingRunResult,
    ReadingValidationError,
    WorkerExecutionError,
    coverage_report_for,
)
from research_map.receipts import canonical_json_bytes, fingerprint, read_receipt
from research_map.source import RegisteredSource, resolve_source, verify_asset
from research_map.state import StateRepository
from research_map.visual import VisualTransportError, extract_page_text, prepare_page_images
from research_map.workspace import Workspace, WorkspaceBuilder, WorkspaceInput

MAP_FIRST_READER_MODE = "map-first-single-visual"
MAP_FIRST_JOB_KIND = "reading:map-first-single-visual"


@dataclass(frozen=True, slots=True)
class MapFirstPaths:
    protocol: Path
    proposal_schema: Path


class MapFirstReadingCoordinator:
    """Coordinate exactly one whole-paper semantic reading turn."""

    def __init__(
        self,
        *,
        repository_root: Path,
        state_path: Path,
        cache_root: Path | None = None,
        codex_executable: str = "codex",
        page_renderer: str = "pdftoppm",
        text_extractor: str = "pdftotext",
        job_timeout_seconds: float | None = None,
    ) -> None:
        self.repository_root = repository_root.resolve()
        self.state = StateRepository(state_path)
        self.cache_root = cache_root or self.repository_root / ".cache" / "research-map"
        self.codex_executable = codex_executable
        self.page_renderer = page_renderer
        self.text_extractor = text_extractor
        if job_timeout_seconds is not None and job_timeout_seconds <= 0:
            raise ValueError("map-first job timeout must be positive")
        self.job_timeout_seconds = job_timeout_seconds
        self.schema_directory = self.repository_root / "schemas" / "research-map" / "v1"
        self.paths = MapFirstPaths(
            protocol=(
                self.repository_root
                / "protocols"
                / "research-map"
                / "experiments"
                / "hierarchical-visual-reader.md"
            ),
            proposal_schema=(
                self.repository_root
                / "schemas"
                / "research-map"
                / "experiments"
                / "hierarchical-reading-proposal.schema.json"
            ),
        )

    def run_through_source_complete(
        self, source_id: str, *, model: str, calibration_tolerant: bool = False
    ) -> ReadingRunResult:
        if not model.strip():
            raise ValueError("map-first reading requires an explicit model")
        self.state.initialize()
        source = resolve_source(source_id, repository_root=self.repository_root)
        _register_source(self.state, source)
        asset = source.main_asset
        contract_digest = _contract_digest(self.paths)
        run_identifier = run_id(
            source_id,
            f"source_complete:{MAP_FIRST_READER_MODE}:{model}:{contract_digest}",
        )
        self.state.register_run(
            run_identifier,
            source_id=source_id,
            requested_through="source_complete",
            model=model,
        )
        run_root = self.cache_root / "runs" / run_identifier
        pages = prepare_page_images(
            asset.path,
            asset_sha256=asset.sha256,
            page_count=asset.pdf_metadata.page_count,
            destination=run_root / "rendered-pages",
            executable=self.page_renderer,
        )
        workspace = WorkspaceBuilder(run_root, run_id=run_identifier, source=source).reading(
            extra_inputs=self._workspace_inputs(
                source,
                run_identifier,
                pages,
                calibration_tolerant=calibration_tolerant,
            )
        )
        job_identifier = job_id(run_identifier, MAP_FIRST_JOB_KIND, 1)
        output_path = workspace.path / "output" / "proposal.json"
        receipt_path = run_root / "receipts" / f"{job_identifier}.json"
        job_state = self.state.job_state(job_identifier)
        if job_state == "succeeded":
            if not output_path.is_file():
                raise ReadingValidationError(
                    f"successful map-first proposal is missing: {output_path}"
                )
        elif job_state == "running" and _valid_retained_output(receipt_path, output_path):
            pass
        elif job_state == "validation_failed" and _valid_retained_output(receipt_path, output_path):
            self.state.advance_job(
                job_identifier,
                "running",
                run_identifier=run_identifier,
                source_id=source.source_id,
                job_kind=MAP_FIRST_JOB_KIND,
                attempt=1,
                receipt={
                    "kind": "post_generation_validation_resumed",
                    "semantic_output_reused": True,
                    "semantic_attempts": 1,
                },
            )
        elif job_state is not None:
            raise ReadingValidationError(
                "map-first semantic attempt already started and cannot be retried; "
                f"job={job_identifier}; state={job_state}"
            )
        else:
            self._run_one_job(
                source=source,
                run_identifier=run_identifier,
                job_identifier=job_identifier,
                model=model,
                workspace=workspace,
                output_path=output_path,
                receipt_path=receipt_path,
            )

        try:
            admission = validate_map_first_proposal_file(
                output_path,
                proposal_schema_path=self.paths.proposal_schema,
                schema_directory=self.schema_directory,
                expected_source_id=source_id,
                source_title=_source_title(source),
                expected_asset_sha256=asset.sha256,
                page_count=asset.pdf_metadata.page_count,
                page_text_by_page=extract_page_text(
                    asset.path,
                    page_count=asset.pdf_metadata.page_count,
                    executable=self.text_extractor,
                ),
                calibration_tolerant=calibration_tolerant,
            )
            if not admission.ok:
                if any(item.code == "manual_review_required" for item in admission.failed_findings):
                    raise ManualReviewRequired(admission.failure_message())
                raise ProposalValidationError(admission.failure_message())
            if admission.proposal is None or admission.dossier_validation is None:
                raise ProposalValidationError(
                    "map-first admission passed without a proposal or dossier validation"
                )
            proposal = admission.proposal
            report = admission.report
            records = admission.records
            validation = admission.dossier_validation
            manual_review_reasons = admission.manual_review_reasons
            type_gap_annotations = admission.type_gap_annotations
            applied = self.state.apply_wip_records(
                source_id,
                job_identifier=job_identifier,
                records=tuple(record.to_dict() for record in records),
            )
            self._write_complete_coverage(source, job_identifier)
            validation_payload = admission.to_dict()
            validation_payload["calibration"] = {
                "tolerant": calibration_tolerant,
                "manual_review_retained": bool(proposal.get("disposition") == "manual_review"),
                "manual_review_reasons": list(manual_review_reasons),
                "administrative_type_gap_annotations": list(type_gap_annotations),
            }
            self._mark_job_succeeded(job_identifier, applied, validation_payload)
        except (
            ManualReviewRequired,
            ProposalValidationError,
            VisualTransportError,
            KeyError,
            OSError,
            ValueError,
        ) as error:
            self._mark_job_validation_failed(job_identifier, str(error))
            raise

        self._advance_source_after_valid_proposal(source_id, run_identifier, job_identifier)
        coverage = coverage_report_for(
            self.state, source_id, page_count=asset.pdf_metadata.page_count
        )
        if not coverage.complete:
            raise ReadingValidationError("; ".join(coverage.errors))
        self._write_wip_snapshot(workspace, source, str(proposal["paper_summary"]))
        verify_asset(asset)
        warnings = tuple(str(item) for item in proposal.get("warnings", [])) + tuple(
            f"deterministic check uncheckable: {item.code} [{item.subject_id}]"
            for item in report.findings
            if item.disposition == "uncheckable"
        )
        if calibration_tolerant and proposal.get("disposition") == "manual_review":
            warnings += tuple(
                f"provisional calibration retained manual_review: {reason}"
                for reason in manual_review_reasons
            ) or ("provisional calibration retained manual_review",)
        warnings += tuple(
            "provisional calibration added administrative type_gap for "
            f"{item['record_id']} ({item['field']}={item['slug']})"
            for item in type_gap_annotations
        )
        return ReadingRunResult(
            source_id=source_id,
            run_id=run_identifier,
            state=self.state.source_state(source_id) or "registered",
            job_ids=(job_identifier,),
            wip_record_count=len(self.state.latest_wip_records(source_id)),
            coverage=coverage,
            validation=validation,
            warnings=warnings,
            reader_mode=MAP_FIRST_READER_MODE,
        )

    def _workspace_inputs(
        self,
        source: RegisteredSource,
        run_identifier: str,
        pages: tuple[Path, ...],
        *,
        calibration_tolerant: bool,
    ) -> tuple[WorkspaceInput, ...]:
        asset = source.main_asset
        page_paths = [f"input/pages/page-{page:03d}.png" for page in range(1, len(pages) + 1)]
        task = {
            "schema_version": 1,
            "run_id": run_identifier,
            "stage": MAP_FIRST_READER_MODE,
            "source_id": source.source_id,
            "scope_id": "whole-paper",
            "asset_path": "input/paper.pdf",
            "asset_sha256": asset.sha256,
            "page_count": len(pages),
            "page_image_paths": page_paths,
            "output_schema_path": "templates/hierarchical-reading-proposal.schema.json",
            "output_path": "output/proposal.json",
            "proposal_artifact_is_authoritative": True,
            "final_response_contains_proposal": False,
            "semantic_call_limit": 1,
            "external_validation_after_generation": True,
            "calibration_tolerant": calibration_tolerant,
            "mapper_inputs": [
                "AGENTS.md",
                "input/task.json",
                "input/source.json",
                "input/paper.pdf",
                *page_paths,
                "templates/hierarchical-reading-proposal.schema.json",
                "tools/validate_proposal.py",
            ],
            "forbidden_inputs": [
                "cross-source records",
                "accepted dossier",
                "prior proposal",
                "manual target inventory",
                "manual defect list",
                "external page-text extraction as semantic input",
                "network access",
            ],
        }
        inputs: list[WorkspaceInput] = [
            WorkspaceInput(
                source_id=source.source_id,
                destination="AGENTS.md",
                kind="reader_protocol",
                area="root",
                source_path=self.paths.protocol,
            ),
            WorkspaceInput(
                source_id=source.source_id,
                destination="task.json",
                kind="reader_task",
                content=canonical_json_bytes(task),
            ),
            WorkspaceInput(
                source_id=source.source_id,
                destination="hierarchical-reading-proposal.schema.json",
                kind="output_schema",
                area="templates",
                source_path=self.paths.proposal_schema,
            ),
            WorkspaceInput(
                source_id=source.source_id,
                destination="validate_proposal.py",
                kind="deterministic_validator",
                area="tools",
                content=_validator_source(self.repository_root).encode("utf-8"),
            ),
        ]
        inputs.extend(
            WorkspaceInput(
                source_id=source.source_id,
                destination=f"pages/page-{page:03d}.png",
                kind="page_image",
                source_path=path,
            )
            for page, path in enumerate(pages, start=1)
        )
        return tuple(inputs)

    def _run_one_job(
        self,
        *,
        source: RegisteredSource,
        run_identifier: str,
        job_identifier: str,
        model: str,
        workspace: Workspace,
        output_path: Path,
        receipt_path: Path,
    ) -> None:
        try:
            runner = CodexRunner(self.codex_executable)
        except CodexCapabilityError as error:
            raise WorkerExecutionError(str(error)) from error
        self.state.advance_job(
            job_identifier,
            "pending",
            run_identifier=run_identifier,
            source_id=source.source_id,
            job_kind=MAP_FIRST_JOB_KIND,
            attempt=1,
            receipt={"kind": "job_created", "semantic_call_limit": 1},
        )
        self.state.advance_job(
            job_identifier,
            "running",
            run_identifier=run_identifier,
            source_id=source.source_id,
            job_kind=MAP_FIRST_JOB_KIND,
            attempt=1,
            receipt={"kind": "worker_started", "model": model, "semantic_attempt": 1},
        )
        result = runner.run(
            CodexJob(
                job_id=job_identifier,
                source_id=source.source_id,
                model=model,
                prompt=(
                    "Read AGENTS.md and input/task.json. Read input/paper.pdf and every "
                    "ordered page image completely in source order. Build the map in scratch "
                    "if useful, perform the protocol's final source-to-map sweep, serialize the "
                    "one authoritative candidate to output/proposal.json, and run "
                    "tools/validate_proposal.py on that exact file. Repair deterministic failures "
                    "from hierarchical, adapted-record, dossier, canonical-round-trip, and "
                    "source-graph checks in place inside this same turn or "
                    "return manual_review. The validator's uncheckable math/figure findings are "
                    "delegations to your direct page-image inspection, not failures or automatic "
                    "manual-review reasons. Leave the validated proposal at output/proposal.json. "
                    "Do not reproduce the proposal in your terminal response; reply only with a "
                    "short completion statement after the file is final."
                ),
                workspace=workspace.path,
                output_schema=None,
                events_path=(
                    self.cache_root / "runs" / run_identifier / "events" / f"{job_identifier}.jsonl"
                ),
                last_message_path=None,
                receipt_path=receipt_path,
                authoritative_output_path=output_path,
                authoritative_output_schema=(
                    workspace.path / "templates" / "hierarchical-reading-proposal.schema.json"
                ),
                timeout_seconds=self.job_timeout_seconds,
            )
        )
        self.state.set_job_receipt(job_identifier, str(receipt_path))
        if result.returncode != 0:
            self.state.advance_job(
                job_identifier,
                "failed",
                run_identifier=run_identifier,
                source_id=source.source_id,
                job_kind=MAP_FIRST_JOB_KIND,
                attempt=1,
                receipt={"kind": "worker_failed", "receipt_path": str(receipt_path)},
            )
            raise WorkerExecutionError(
                f"Codex job {job_identifier} exited with status {result.returncode}"
            )
        if not result.output_valid:
            self._mark_job_validation_failed(job_identifier, "; ".join(result.validation_errors))
            raise ReadingValidationError(
                f"Codex job output failed schema validation: {job_identifier}"
            )

    def _write_complete_coverage(self, source: RegisteredSource, job_identifier: str) -> None:
        page_count = source.main_asset.pdf_metadata.page_count
        entries = initial_coverage(
            page_count,
            (
                ScopePlan(
                    scope_id="whole-paper",
                    label="Whole paper",
                    page_start=1,
                    page_end=page_count,
                    rationale="The selected reader consumes the complete source in one turn.",
                    thread_hints=("source progression",),
                ),
            ),
            (
                LandmarkPlan(
                    landmark_id="whole-paper-map",
                    label="Whole-paper map",
                    kind="argument",
                    page_start=1,
                    page_end=page_count,
                    rationale="Validated hierarchical records route the source progression.",
                    thread_hints=("source progression",),
                ),
            ),
            (
                BatchPlan(
                    batch_id="whole-paper",
                    label="Whole-paper single visual turn",
                    scope_ids=("whole-paper",),
                    landmark_ids=("whole-paper-map",),
                    rationale="The selected mode has exactly one semantic reading turn.",
                    page_start=1,
                    page_end=page_count,
                ),
            ),
        )
        for entry in entries:
            self.state.upsert_coverage(
                source.source_id,
                scope_id=entry.scope_id,
                scope_kind=entry.scope_kind,
                disposition="covered",
                details={
                    **entry.details,
                    "basis": (
                        "validated whole-paper proposal; not an independent completeness claim"
                    ),
                },
                job_identifier=job_identifier,
            )

    def _mark_job_succeeded(
        self,
        job_identifier: str,
        applied: tuple[str, ...],
        hierarchical_validation: dict[str, Any],
    ) -> None:
        job = self.state.job_record(job_identifier)
        if job is None or job["state"] == "succeeded":
            return
        self.state.advance_job(
            job_identifier,
            "succeeded",
            run_identifier=str(job["run_id"]),
            source_id=str(job["source_id"]),
            job_kind=str(job["kind"]),
            attempt=int(job["attempt"]),
            receipt={
                "kind": "proposal_applied",
                "records": list(applied),
                "hierarchical_validation": hierarchical_validation,
            },
        )

    def _mark_job_validation_failed(self, job_identifier: str, error: str) -> None:
        job = self.state.job_record(job_identifier)
        if job is None or job["state"] in {"validation_failed", "succeeded"}:
            return
        self.state.advance_job(
            job_identifier,
            "validation_failed",
            run_identifier=str(job["run_id"]),
            source_id=str(job["source_id"]),
            job_kind=str(job["kind"]),
            attempt=int(job["attempt"]),
            receipt={"kind": "proposal_rejected", "error": error, "retry_allowed": False},
        )

    def _advance_source_after_valid_proposal(
        self, source_id: str, run_identifier: str, job_identifier: str
    ) -> None:
        state = self.state.source_state(source_id)
        if state == "registered":
            self.state.advance_source(
                source_id,
                "oriented",
                run_identifier=run_identifier,
                job_identifier=job_identifier,
                receipt={
                    "kind": "whole_paper_scope_bound",
                    "reader_mode": MAP_FIRST_READER_MODE,
                },
            )
            state = "oriented"
        if state == "oriented":
            self.state.advance_source(
                source_id,
                "reading",
                run_identifier=run_identifier,
                job_identifier=job_identifier,
                receipt={"kind": "whole_paper_reading_applied", "semantic_turns": 1},
            )
            state = "reading"
        if state == "reading":
            self.state.advance_source(
                source_id,
                "source_complete",
                run_identifier=run_identifier,
                job_identifier=job_identifier,
                receipt={
                    "kind": "source_reading_complete",
                    "reader_mode": MAP_FIRST_READER_MODE,
                    "record_count": len(self.state.latest_wip_records(source_id)),
                },
            )

    def _write_wip_snapshot(
        self, workspace: Workspace, source: RegisteredSource, summary: str
    ) -> None:
        dossier = {
            "schema_version": 1,
            "source_id": source.source_id,
            "title": _source_title(source),
            "summary": summary,
            "records": list(self.state.latest_wip_records(source.source_id)),
        }
        (workspace.path / "output" / "wip.json").write_bytes(canonical_json_bytes(dossier))


def _register_source(repository: StateRepository, source: RegisteredSource) -> None:
    source_record = source.to_record()
    repository.register_source(
        source.source_id,
        metadata={
            "schema_version": source.schema_version,
            "source_yaml_path": source_record["source_yaml_path"],
            "source_yaml_sha256": source.source_yaml_sha256,
            "metadata": dict(source.metadata),
        },
        assets=[asset.to_record() for asset in source.assets],
        receipt={
            "kind": "source_registration",
            "schema_version": source.schema_version,
            "source_yaml_sha256": source.source_yaml_sha256,
            "asset_sha256": [asset.sha256 for asset in source.assets],
        },
    )


def _source_title(source: RegisteredSource) -> str:
    identity = source.metadata.get("identity")
    if isinstance(identity, dict) and isinstance(identity.get("title"), str):
        return str(identity["title"])
    return source.source_id


def _contract_digest(paths: MapFirstPaths) -> str:
    digest = hashlib.sha256()
    for path in (paths.protocol, paths.proposal_schema):
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _valid_retained_output(receipt_path: Path, output_path: Path) -> bool:
    if not receipt_path.is_file() or not output_path.is_file():
        return False
    receipt = read_receipt(receipt_path)
    validation = receipt.get("validation")
    outputs = receipt.get("outputs")
    if not isinstance(validation, dict) or validation.get("valid") is not True:
        return False
    if not isinstance(outputs, list):
        return False
    observed = fingerprint(output_path)
    return any(
        isinstance(item, dict)
        and item.get("path") == str(output_path)
        and item.get("sha256") == observed.sha256
        and item.get("size_bytes") == observed.size_bytes
        for item in outputs
    )


def _validator_source(repository_root: Path) -> str:
    root = repr(str(repository_root))
    return f"""#!/usr/bin/env python3
import json
import sys
from pathlib import Path

sys.path.insert(0, {root!s} + "/" + "src")
from research_map.map_first_admission import validate_map_first_proposal_file
from research_map.visual import extract_page_text

workspace = Path(__file__).resolve().parents[1]
task = json.loads((workspace / "input" / "task.json").read_text(encoding="utf-8"))
source = json.loads((workspace / "input" / "source.json").read_text(encoding="utf-8"))
proposal = Path(sys.argv[1])
identity = source.get("metadata", {{}}).get("identity", {{}})
source_title = identity.get("title", task["source_id"])
admission = validate_map_first_proposal_file(
    proposal,
    proposal_schema_path=workspace / task["output_schema_path"],
    schema_directory=Path({root!s}) / "schemas" / "research-map" / "v1",
    expected_source_id=task["source_id"],
    source_title=source_title,
    expected_asset_sha256=task["asset_sha256"],
    page_count=task["page_count"],
    page_text_by_page=extract_page_text(
        workspace / task["asset_path"], page_count=task["page_count"]
    ),
    calibration_tolerant=bool(task["calibration_tolerant"]),
)
print(json.dumps(admission.to_dict(), sort_keys=True))
raise SystemExit(0 if admission.ok else 1)
"""
