"""Persistent source-local orientation and reading coordination."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from research_map.codex import CodexJob, CodexRunner
from research_map.coverage import (
    BatchPlan,
    CoverageEntry,
    CoverageReport,
    LandmarkPlan,
    ScopePlan,
    initial_coverage,
    validate_coverage,
)
from research_map.ids import job_id, run_id
from research_map.proposals import (
    ManualReviewRequired,
    OrientationProposal,
    ProposalValidationError,
    dossier_from_wip,
    load_orientation_proposal,
    load_reading_proposal,
)
from research_map.reading_correction_admission import (
    validate_reading_correction_file,
)
from research_map.receipts import canonical_json_bytes
from research_map.records import Record
from research_map.source import RegisteredSource, resolve_source, verify_asset
from research_map.state import StateRepository
from research_map.validation import ValidationReport, require_valid_dossier
from research_map.workspace import Workspace, WorkspaceBuilder, WorkspaceInput


class WorkerExecutionError(RuntimeError):
    """Raised when a Codex subprocess fails before a valid proposal is available."""


class ReadingValidationError(ValueError):
    """Raised when source-local reading cannot reach source_complete."""


@dataclass(frozen=True, slots=True)
class ReadingRunResult:
    source_id: str
    run_id: str
    state: str
    job_ids: tuple[str, ...]
    wip_record_count: int
    coverage: CoverageReport
    validation: ValidationReport
    warnings: tuple[str, ...]
    reader_mode: str = "staged"

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "job_ids": list(self.job_ids),
            "wip_record_count": self.wip_record_count,
            "coverage": self.coverage.to_dict(),
            "validation": {
                "ok": self.validation.ok,
                "errors": list(self.validation.errors),
                "type_gaps": list(self.validation.type_gaps),
            },
            "warnings": list(self.warnings),
            "reader_mode": self.reader_mode,
        }


class ReadingCoordinator:
    def __init__(
        self,
        *,
        repository_root: Path,
        state_path: Path,
        cache_root: Path | None = None,
        codex_executable: str = "codex",
    ) -> None:
        self.repository_root = repository_root.resolve()
        self.state = StateRepository(state_path)
        self.cache_root = cache_root or self.repository_root / ".cache" / "research-map"
        self.schema_directory = self.repository_root / "schemas" / "research-map" / "v1"
        self.protocol_directory = self.repository_root / "protocols" / "research-map" / "v1"
        self.codex_executable = codex_executable

    def run_through_source_complete(
        self,
        source_id: str,
        *,
        model: str,
        reviewed_finding_ids: tuple[str, ...] = (),
        reviewed_resume: dict[str, Any] | None = None,
    ) -> ReadingRunResult:
        if not model.strip():
            raise ValueError("reading run requires an explicit model")
        self.state.initialize()
        source = resolve_source(source_id, repository_root=self.repository_root)
        _register_source(self.state, source)
        contract_digest = self._contract_digest()
        run_identifier = run_id(source_id, f"source_complete:{model}:{contract_digest}")
        self.state.register_run(
            run_identifier,
            source_id=source_id,
            requested_through="source_complete",
            model=model,
        )
        run_root = self.cache_root / "runs" / run_identifier
        workspace = WorkspaceBuilder(run_root, run_id=run_identifier, source=source).reading(
            extra_inputs=self._workspace_inputs(source, run_identifier)
        )

        if self.state.source_state(source_id) == "audit_failed":
            self._seed_retained_orientation_for_audit_repair(
                source,
                run_identifier=run_identifier,
                workspace=workspace,
            )

        orientation, orientation_job_id = self._orientation(
            source=source,
            run_id_value=run_identifier,
            workspace=workspace,
            model=model,
        )
        current_state = self.state.source_state(source_id)
        if current_state == "registered":
            self.state.advance_source(
                source_id,
                "oriented",
                run_identifier=run_identifier,
                job_identifier=orientation_job_id,
                receipt={"kind": "orientation_applied", "job_id": orientation_job_id},
            )
        if not self.state.coverage_for(source_id):
            for entry in initial_coverage(
                source.main_asset.pdf_metadata.page_count,
                orientation.scopes,
                orientation.landmarks,
                orientation.batches,
            ):
                self._write_coverage(entry, orientation_job_id)

        if self.state.source_state(source_id) == "source_complete":
            return self._validated_result(
                source,
                run_identifier,
                source_summary=orientation.source_summary,
                warnings=orientation.warnings,
            )
        if self.state.source_state(source_id) in {"oriented", "audit_failed"}:
            self.state.advance_source(
                source_id,
                "reading",
                run_identifier=run_identifier,
                job_identifier=orientation_job_id,
                receipt={"kind": "reading_started", "job_id": orientation_job_id},
            )

        warnings = list(orientation.warnings)
        queue = list(orientation.batches)
        attempts: dict[str, int] = {}
        retry_errors: dict[str, str] = {}
        while queue:
            scope = queue.pop(0)
            coverage_rows = self.state.coverage_for(source_id)
            current_coverage = _coverage_by_id(coverage_rows)
            existing = current_coverage.get(scope.scope_id)
            if existing is not None and existing.disposition in {"covered", "not_substantive"}:
                continue
            force_retry = existing is not None and existing.disposition == "reopened"
            reopened_by_job_id = next(
                (
                    str(row["updated_by_job_id"])
                    for row in coverage_rows
                    if row["scope_id"] == scope.scope_id and row["updated_by_job_id"] is not None
                ),
                None,
            )
            correction_findings: tuple[dict[str, Any], ...] = ()
            allowed_record_ids: tuple[str, ...] = ()
            allowed_new_landmark_ids: tuple[str, ...] = ()
            if force_retry:
                correction_findings = self._mapped_findings_for_scope(
                    source_id,
                    scope.scope_id,
                    audit_job_identifier=reopened_by_job_id,
                    selected_finding_ids=reviewed_finding_ids,
                )
                allowed_record_ids, allowed_new_landmark_ids = self._correction_boundary(
                    correction_findings
                )
            assigned_landmarks = self._landmarks_for_batch(
                source,
                scope,
                orientation_landmarks=orientation.landmarks,
            )
            correction_contract = (
                {
                    "source_id": source.source_id,
                    "scope_id": scope.scope_id,
                    "landmark_ids": [landmark.landmark_id for landmark in assigned_landmarks],
                    "asset_sha256": source.main_asset.sha256,
                    "page_count": source.main_asset.pdf_metadata.page_count,
                    "allowed_record_ids": list(allowed_record_ids),
                    "allowed_new_landmark_ids": list(allowed_new_landmark_ids),
                    "findings": list(correction_findings),
                }
                if force_retry
                else None
            )
            attempts[scope.scope_id] = attempts.get(scope.scope_id, 0) + 1
            if attempts[scope.scope_id] > 2:
                raise ReadingValidationError(
                    f"reading scope exceeded bounded reopen attempts: {scope.scope_id}"
                )
            job_identifier, output_path = self._execute_job(
                source=source,
                run_id_value=run_identifier,
                workspace=workspace,
                model=model,
                job_kind=f"reading:{scope.scope_id}",
                output_schema_name="reading-proposal.schema.json",
                prompt=self._reading_prompt(
                    source,
                    scope,
                    attempts[scope.scope_id],
                    correction_findings=correction_findings,
                    allowed_record_ids=allowed_record_ids,
                    allowed_new_landmark_ids=allowed_new_landmark_ids,
                    landmarks=assigned_landmarks,
                    prior_validation_error=retry_errors.get(scope.scope_id),
                ),
                force_retry=force_retry,
                correction_findings=correction_findings,
                allowed_record_ids=allowed_record_ids,
                allowed_new_landmark_ids=allowed_new_landmark_ids,
                correction_contract=correction_contract,
                reviewed_resume=reviewed_resume,
            )
            unexpected_record_ids: tuple[str, ...] = ()
            try:
                if force_retry:
                    if correction_contract is None:
                        raise ReadingValidationError("correction contract was not constructed")
                    admission = validate_reading_correction_file(
                        output_path,
                        current_wip=self._wip_snapshot_payload(
                            source,
                            orientation.source_summary,
                        ),
                        correction_contract=correction_contract,
                        schema_directory=self.schema_directory,
                    )
                    unexpected_record_ids = admission.unexpected_record_ids
                    if not admission.ok:
                        if admission.manual_review_required:
                            raise ManualReviewRequired(admission.failure_message())
                        raise ProposalValidationError(admission.failure_message())
                    if admission.proposal is None:
                        raise ReadingValidationError(
                            "correction admission passed without a proposal"
                        )
                    proposal = admission.proposal
                else:
                    proposal = load_reading_proposal(
                        output_path,
                        schema_directory=self.schema_directory,
                        expected_source_id=source_id,
                        expected_scope_id=scope.scope_id,
                        expected_landmark_ids=tuple(
                            landmark.landmark_id for landmark in assigned_landmarks
                        ),
                        expected_asset_sha256=source.main_asset.sha256,
                        page_count=source.main_asset.pdf_metadata.page_count,
                    )
                    self._validate_proposal_revisions(source_id, proposal.records)
                if not force_retry:
                    self._validate_landmark_record_links(
                        proposal.landmark_dispositions,
                        available_record_ids={
                            str(record["id"]) for record in self.state.latest_wip_records(source_id)
                        }
                        | {record.id for record in proposal.records},
                    )
                applied = self.state.apply_wip_records(
                    source_id,
                    job_identifier=job_identifier,
                    records=[record.to_dict() for record in proposal.records],
                )
                self._write_coverage(
                    CoverageEntry(
                        scope.scope_id,
                        "reading_scope",
                        proposal.disposition,
                        {
                            **scope.to_dict(),
                            "scope_summary": proposal.scope_summary,
                            "attempt": attempts[scope.scope_id],
                        },
                    ),
                    job_identifier,
                )
                landmark_by_id = {
                    landmark.landmark_id: landmark
                    for landmark in self._all_runtime_landmarks(
                        source,
                        orientation_landmarks=orientation.landmarks,
                    )
                }
                landmark_coverage_details = {
                    str(row["details"].get("landmark_id")): dict(row["details"])
                    for row in self.state.coverage_for(source_id)
                    if row["scope_kind"] == "landmark"
                }
                for disposition in proposal.landmark_dispositions:
                    landmark = landmark_by_id[str(disposition["landmark_id"])]
                    self._write_coverage(
                        CoverageEntry(
                            landmark.coverage_id,
                            "landmark",
                            (
                                "covered"
                                if disposition["disposition"] == "represented"
                                else "not_substantive"
                            ),
                            {
                                **landmark_coverage_details.get(landmark.landmark_id, {}),
                                **landmark.to_dict(),
                                "batch_id": scope.batch_id,
                                "record_ids": list(disposition["record_ids"]),
                                "disposition_rationale": str(disposition["rationale"]),
                            },
                        ),
                        job_identifier,
                    )
                for reopen_scope_id in proposal.reopen_scope_ids:
                    reopened = next(
                        (item for item in orientation.batches if item.scope_id == reopen_scope_id),
                        None,
                    )
                    if reopened is None:
                        raise ProposalValidationError(
                            f"proposal requested unknown reopen scope: {reopen_scope_id}"
                        )
                    self._write_coverage(
                        CoverageEntry(
                            reopened.scope_id,
                            "reading_scope",
                            "reopened",
                            {**reopened.to_dict(), "reopened_by": scope.scope_id},
                        ),
                        job_identifier,
                    )
                    for landmark_id in reopened.landmark_ids:
                        landmark = landmark_by_id[landmark_id]
                        self._write_coverage(
                            CoverageEntry(
                                landmark.coverage_id,
                                "landmark",
                                "reopened",
                                {**landmark.to_dict(), "batch_id": reopened.batch_id},
                            ),
                            job_identifier,
                        )
                    queue.append(reopened)
                self._refresh_page_coverage(
                    source,
                    orientation.scopes,
                    tuple(landmark_by_id.values()),
                    orientation.batches,
                    job_identifier=job_identifier,
                )
                self._write_wip_snapshot(workspace, source, orientation.source_summary)
                self._mark_job_succeeded(
                    job_identifier,
                    applied,
                    allowed_record_ids=allowed_record_ids,
                    allowed_new_landmark_ids=allowed_new_landmark_ids,
                    reviewed_resume=reviewed_resume,
                )
                warnings.extend(proposal.warnings)
            except ManualReviewRequired as error:
                self._mark_job_validation_failed(
                    job_identifier,
                    str(error),
                    allowed_record_ids=allowed_record_ids,
                    allowed_new_landmark_ids=allowed_new_landmark_ids,
                    unexpected_record_ids=unexpected_record_ids,
                    reviewed_resume=reviewed_resume,
                )
                if force_retry:
                    self._restore_audit_failed_after_correction_error(
                        source_id,
                        run_identifier=run_identifier,
                        job_identifier=job_identifier,
                        error=str(error),
                    )
                raise
            except (ProposalValidationError, KeyError, ValueError) as error:
                error_text = str(error)
                self._mark_job_validation_failed(
                    job_identifier,
                    error_text,
                    allowed_record_ids=allowed_record_ids,
                    allowed_new_landmark_ids=allowed_new_landmark_ids,
                    unexpected_record_ids=unexpected_record_ids,
                    reviewed_resume=reviewed_resume,
                )
                if force_retry:
                    self._restore_audit_failed_after_correction_error(
                        source_id,
                        run_identifier=run_identifier,
                        job_identifier=job_identifier,
                        error=error_text,
                    )
                if not force_retry and attempts[scope.scope_id] < 2:
                    retry_errors[scope.scope_id] = error_text
                    warnings.append(
                        f"{scope.scope_id}: proposal rejected and retried once: {error_text}"
                    )
                    queue.insert(0, scope)
                    continue
                raise

        result = self._validated_result(
            source,
            run_identifier,
            source_summary=orientation.source_summary,
            warnings=tuple(warnings),
        )
        last_job = result.job_ids[-1] if result.job_ids else orientation_job_id
        self.state.advance_source(
            source_id,
            "source_complete",
            run_identifier=run_identifier,
            job_identifier=last_job,
            receipt={
                "kind": "source_reading_complete",
                "record_count": result.wip_record_count,
                "coverage_complete": result.coverage.complete,
            },
        )
        verify_asset(source.main_asset)
        return ReadingRunResult(
            source_id=result.source_id,
            run_id=result.run_id,
            state="source_complete",
            job_ids=result.job_ids,
            wip_record_count=result.wip_record_count,
            coverage=result.coverage,
            validation=result.validation,
            warnings=result.warnings,
        )

    def _restore_audit_failed_after_correction_error(
        self,
        source_id: str,
        *,
        run_identifier: str,
        job_identifier: str,
        error: str,
    ) -> None:
        if self.state.source_state(source_id) != "reading":
            return
        self.state.advance_source(
            source_id,
            "audit_failed",
            run_identifier=run_identifier,
            job_identifier=job_identifier,
            receipt={
                "kind": "audit_correction_rejected",
                "error": error,
                "wip_mutated": False,
                "retryable": True,
            },
        )

    def _seed_retained_orientation_for_audit_repair(
        self,
        source: RegisteredSource,
        *,
        run_identifier: str,
        workspace: Workspace,
    ) -> None:
        """Reuse accepted routing when an audit reopens a source.

        A source completed by another reader topology may not have a staged
        orientation job. The audit has already bounded its correction against
        the existing coverage map, so another semantic orientation call would
        add cost without changing the authorized repair boundary. Materialize
        that routing as a deterministic orientation result instead.
        """

        orientation_jobs = [
            job for job in self.state.jobs_for_run(run_identifier) if job["kind"] == "orientation"
        ]
        if any(job["state"] == "succeeded" for job in orientation_jobs):
            return

        coverage_rows = self.state.coverage_for(source.source_id)
        scope_rows = [row for row in coverage_rows if row["scope_kind"] == "context_scope"]
        landmark_rows = [row for row in coverage_rows if row["scope_kind"] == "landmark"]
        batch_rows = [row for row in coverage_rows if row["scope_kind"] == "reading_scope"]
        if not scope_rows or not landmark_rows or not batch_rows:
            raise ReadingValidationError(
                "audit repair cannot reuse routing because coverage is incomplete"
            )

        scopes = [
            {
                key: row["details"][key]
                for key in (
                    "scope_id",
                    "label",
                    "page_start",
                    "page_end",
                    "rationale",
                    "thread_hints",
                )
            }
            for row in scope_rows
        ]
        landmarks = [
            {
                key: row["details"][key]
                for key in (
                    "landmark_id",
                    "label",
                    "kind",
                    "page_start",
                    "page_end",
                    "rationale",
                    "thread_hints",
                )
            }
            for row in landmark_rows
        ]
        batches = [
            {
                key: row["details"][key]
                for key in (
                    "batch_id",
                    "label",
                    "scope_ids",
                    "landmark_ids",
                    "rationale",
                )
            }
            for row in batch_rows
        ]
        scope_ids = [str(scope["scope_id"]) for scope in scopes]
        landmark_ids = [str(landmark["landmark_id"]) for landmark in landmarks]
        records = self.state.latest_wip_records(source.source_id)
        source_summary = _source_summary(records, source.source_id)
        payload = {
            "schema_version": "1.0",
            "source_id": source.source_id,
            "source_summary": source_summary,
            "scopes": scopes,
            "landmarks": landmarks,
            "batches": batches,
            "thread_plan": [
                {
                    "thread_id": "retained-source-progression",
                    "label": "Retained source progression",
                    "kind": "argument",
                    "scope_ids": scope_ids,
                    "landmark_ids": landmark_ids,
                    "rationale": (
                        "Preserve the validated source routing while applying only "
                        "audit-authorized corrections."
                    ),
                }
            ],
            "type_gaps": [],
            "manual_review": False,
            "warnings": [
                "Reused validated coverage routing for audit repair; no orientation "
                "semantic call was made."
            ],
        }

        for job in orientation_jobs:
            if job["state"] != "superseded":
                self.state.advance_job(
                    str(job["job_id"]),
                    "superseded",
                    run_identifier=run_identifier,
                    source_id=source.source_id,
                    job_kind="orientation",
                    attempt=int(job["attempt"]),
                    receipt={
                        "kind": "orientation_replaced_by_retained_routing",
                        "semantic_output_reused": False,
                    },
                )
        attempt = max((int(job["attempt"]) for job in orientation_jobs), default=0) + 1
        orientation_job_id = job_id(run_identifier, "orientation", attempt)
        self.state.advance_job(
            orientation_job_id,
            "pending",
            run_identifier=run_identifier,
            source_id=source.source_id,
            job_kind="orientation",
            attempt=attempt,
            receipt={"kind": "retained_orientation_created", "semantic_call": False},
        )
        self.state.advance_job(
            orientation_job_id,
            "running",
            run_identifier=run_identifier,
            source_id=source.source_id,
            job_kind="orientation",
            attempt=attempt,
            receipt={"kind": "retained_orientation_materialized", "semantic_call": False},
        )
        output_path = workspace.path / "output" / f"{orientation_job_id}.json"
        output_path.write_bytes(canonical_json_bytes(payload))
        self._write_wip_snapshot(workspace, source, source_summary)
        self.state.advance_job(
            orientation_job_id,
            "succeeded",
            run_identifier=run_identifier,
            source_id=source.source_id,
            job_kind="orientation",
            attempt=attempt,
            receipt={
                "kind": "retained_orientation_applied",
                "semantic_call": False,
                "coverage_scope_ids": [str(row["scope_id"]) for row in coverage_rows],
            },
        )

    def _orientation(
        self,
        *,
        source: RegisteredSource,
        run_id_value: str,
        workspace: Workspace,
        model: str,
    ) -> tuple[OrientationProposal, str]:
        job_identifier, output_path = self._execute_job(
            source=source,
            run_id_value=run_id_value,
            workspace=workspace,
            model=model,
            job_kind="orientation",
            output_schema_name="orientation-proposal.schema.json",
            prompt=self._orientation_prompt(source),
        )
        try:
            proposal = load_orientation_proposal(
                output_path,
                schema_directory=self.schema_directory,
                expected_source_id=source.source_id,
                page_count=source.main_asset.pdf_metadata.page_count,
            )
            self._mark_job_succeeded(job_identifier, ())
        except (ProposalValidationError, ManualReviewRequired, ValueError) as error:
            self._mark_job_validation_failed(job_identifier, str(error))
            raise
        return proposal, job_identifier

    def _execute_job(
        self,
        *,
        source: RegisteredSource,
        run_id_value: str,
        workspace: Workspace,
        model: str,
        job_kind: str,
        output_schema_name: str,
        prompt: str,
        force_retry: bool = False,
        correction_findings: tuple[dict[str, Any], ...] = (),
        allowed_record_ids: tuple[str, ...] = (),
        allowed_new_landmark_ids: tuple[str, ...] = (),
        correction_contract: dict[str, Any] | None = None,
        reviewed_resume: dict[str, Any] | None = None,
    ) -> tuple[str, Path]:
        jobs = [job for job in self.state.jobs_for_run(run_id_value) if job["kind"] == job_kind]
        succeeded = next((job for job in reversed(jobs) if job["state"] == "succeeded"), None)
        if succeeded is not None and not force_retry:
            output_path = workspace.path / "output" / f"{succeeded['job_id']}.json"
            if not output_path.is_file():
                raise ReadingValidationError(f"successful job output is missing: {output_path}")
            return str(succeeded["job_id"]), output_path
        if jobs and jobs[-1]["state"] in {"pending", "running"} and not force_retry:
            attempt = int(jobs[-1]["attempt"])
        else:
            attempt = len(jobs) + 1
        job_identifier = job_id(run_id_value, job_kind, attempt)
        if self.state.job_state(job_identifier) is None:
            self.state.advance_job(
                job_identifier,
                "pending",
                run_identifier=run_id_value,
                source_id=source.source_id,
                job_kind=job_kind,
                attempt=attempt,
                receipt={
                    "kind": "job_created",
                    "job_kind": job_kind,
                    "correction_findings": list(correction_findings),
                    "allowed_record_ids": list(allowed_record_ids),
                    "allowed_new_landmark_ids": list(allowed_new_landmark_ids),
                    "reviewed_resume": reviewed_resume,
                },
            )
        self.state.advance_job(
            job_identifier,
            "running",
            run_identifier=run_id_value,
            source_id=source.source_id,
            job_kind=job_kind,
            attempt=attempt,
            receipt={
                "kind": "worker_started",
                "model": model,
                "correction_findings": list(correction_findings),
                "allowed_record_ids": list(allowed_record_ids),
                "allowed_new_landmark_ids": list(allowed_new_landmark_ids),
                "reviewed_resume": reviewed_resume,
            },
        )
        output_path = workspace.path / "output" / f"{job_identifier}.json"
        receipt_path = (
            self.cache_root / "runs" / run_id_value / "receipts" / f"{job_identifier}.json"
        )
        if correction_contract is not None:
            (workspace.path / "scratch" / "correction-contract.json").write_bytes(
                canonical_json_bytes(correction_contract)
            )
        run_result = CodexRunner(self.codex_executable).run(
            CodexJob(
                job_id=job_identifier,
                source_id=source.source_id,
                model=model,
                prompt=prompt,
                workspace=workspace.path,
                output_schema=workspace.path / "templates" / output_schema_name,
                events_path=(
                    self.cache_root / "runs" / run_id_value / "events" / f"{job_identifier}.jsonl"
                ),
                last_message_path=output_path,
                receipt_path=receipt_path,
            )
        )
        self.state.set_job_receipt(job_identifier, str(receipt_path))
        if run_result.returncode != 0:
            self.state.advance_job(
                job_identifier,
                "failed",
                run_identifier=run_id_value,
                source_id=source.source_id,
                job_kind=job_kind,
                attempt=attempt,
                receipt={
                    "kind": "worker_failed",
                    "receipt_path": str(receipt_path),
                    "reviewed_resume": reviewed_resume,
                },
            )
            raise WorkerExecutionError(
                f"Codex job {job_identifier} exited with status {run_result.returncode}"
            )
        if not run_result.output_valid:
            self._mark_job_validation_failed(
                job_identifier,
                "; ".join(run_result.validation_errors),
                allowed_record_ids=allowed_record_ids,
                allowed_new_landmark_ids=allowed_new_landmark_ids,
                reviewed_resume=reviewed_resume,
            )
            raise ReadingValidationError(
                f"Codex job output failed schema validation: {job_identifier}"
            )
        return job_identifier, output_path

    def _mark_job_succeeded(
        self,
        job_identifier: str,
        applied: tuple[str, ...],
        *,
        allowed_record_ids: tuple[str, ...] = (),
        allowed_new_landmark_ids: tuple[str, ...] = (),
        reviewed_resume: dict[str, Any] | None = None,
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
                "allowed_record_ids": list(allowed_record_ids),
                "allowed_new_landmark_ids": list(allowed_new_landmark_ids),
                "reviewed_resume": reviewed_resume,
            },
        )

    def _mark_job_validation_failed(
        self,
        job_identifier: str,
        error: str,
        *,
        allowed_record_ids: tuple[str, ...] = (),
        allowed_new_landmark_ids: tuple[str, ...] = (),
        unexpected_record_ids: tuple[str, ...] = (),
        reviewed_resume: dict[str, Any] | None = None,
    ) -> None:
        job = self.state.job_record(job_identifier)
        if job is None or job["state"] == "validation_failed":
            return
        self.state.advance_job(
            job_identifier,
            "validation_failed",
            run_identifier=str(job["run_id"]),
            source_id=str(job["source_id"]),
            job_kind=str(job["kind"]),
            attempt=int(job["attempt"]),
            receipt={
                "kind": "proposal_validation_failed",
                "error": error,
                "allowed_record_ids": list(allowed_record_ids),
                "allowed_new_landmark_ids": list(allowed_new_landmark_ids),
                "unexpected_record_ids": list(unexpected_record_ids),
                "reviewed_resume": reviewed_resume,
            },
        )

    def _workspace_inputs(
        self, source: RegisteredSource, run_identifier: str
    ) -> tuple[WorkspaceInput, ...]:
        task = {
            "schema_version": "1.0",
            "run_id": run_identifier,
            "source_id": source.source_id,
            "stages": ["orientation", "reading"],
            "network_access": False,
        }
        return (
            WorkspaceInput(
                source.source_id,
                "AGENTS.md",
                "workspace_authority",
                area="root",
                content=(self.protocol_directory / "AGENTS.md").read_bytes(),
            ),
            WorkspaceInput(
                source.source_id,
                "task.json",
                "task_contract",
                area="root",
                content=canonical_json_bytes(task),
            ),
            WorkspaceInput(
                source.source_id,
                "orientation.md",
                "orientation_protocol",
                content=(self.protocol_directory / "orientation.md").read_bytes(),
            ),
            WorkspaceInput(
                source.source_id,
                "reading.md",
                "reading_protocol",
                content=(self.protocol_directory / "reading.md").read_bytes(),
            ),
            WorkspaceInput(
                source.source_id,
                "orientation-proposal.schema.json",
                "output_schema",
                area="templates",
                source_path=self.schema_directory / "orientation-proposal.schema.json",
            ),
            WorkspaceInput(
                source.source_id,
                "reading-proposal.schema.json",
                "output_schema",
                area="templates",
                source_path=self.schema_directory / "reading-proposal.schema.json",
            ),
            WorkspaceInput(
                source.source_id,
                "validate_reading.py",
                "proposal_validator",
                area="tools",
                content=_reading_correction_validator_source(self.repository_root).encode("utf-8"),
            ),
        )

    def _contract_digest(self) -> str:
        digest = hashlib.sha256()
        for path in (
            self.protocol_directory / "AGENTS.md",
            self.protocol_directory / "orientation.md",
            self.protocol_directory / "reading.md",
            self.schema_directory / "orientation-proposal.schema.json",
            self.schema_directory / "reading-proposal.schema.json",
        ):
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
        return digest.hexdigest()

    def _orientation_prompt(self, source: RegisteredSource) -> str:
        return f"""Stage: orientation
Source: {source.source_id}

Read AGENTS.md, task.json, input/orientation.md, input/source.json, and the complete
input/paper.pdf. Return only JSON matching templates/orientation-proposal.schema.json.
Inspect all {source.main_asset.pdf_metadata.page_count} PDF pages before proposing scopes.
Inventory source-native landmarks and group them into bounded reading batches while
preserving coherent scopes and threads. Do not perform scientific adjudication.
"""

    def _all_runtime_landmarks(
        self,
        source: RegisteredSource,
        *,
        orientation_landmarks: tuple[LandmarkPlan, ...],
    ) -> tuple[LandmarkPlan, ...]:
        by_id = {landmark.landmark_id: landmark for landmark in orientation_landmarks}
        for row in self.state.coverage_for(source.source_id):
            if row["scope_kind"] != "landmark":
                continue
            details = dict(row["details"])
            landmark_id = str(details.get("landmark_id", ""))
            if not landmark_id or landmark_id in by_id:
                continue
            by_id[landmark_id] = LandmarkPlan.from_mapping(
                details,
                page_count=source.main_asset.pdf_metadata.page_count,
            )
        return tuple(by_id[landmark_id] for landmark_id in sorted(by_id))

    def _landmarks_for_batch(
        self,
        source: RegisteredSource,
        batch: BatchPlan,
        *,
        orientation_landmarks: tuple[LandmarkPlan, ...],
    ) -> tuple[LandmarkPlan, ...]:
        landmarks = self._all_runtime_landmarks(
            source,
            orientation_landmarks=orientation_landmarks,
        )
        coverage_batch_by_landmark = {
            str(row["details"].get("landmark_id")): str(row["details"].get("batch_id"))
            for row in self.state.coverage_for(source.source_id)
            if row["scope_kind"] == "landmark"
        }
        return tuple(
            landmark
            for landmark in landmarks
            if landmark.landmark_id in batch.landmark_ids
            or coverage_batch_by_landmark.get(landmark.landmark_id) == batch.batch_id
        )

    def _reading_prompt(
        self,
        source: RegisteredSource,
        scope: BatchPlan,
        attempt: int,
        *,
        correction_findings: tuple[dict[str, Any], ...] = (),
        allowed_record_ids: tuple[str, ...] = (),
        allowed_new_landmark_ids: tuple[str, ...] = (),
        landmarks: tuple[LandmarkPlan, ...] = (),
        prior_validation_error: str | None = None,
    ) -> str:
        landmark_context = canonical_json_bytes(
            {
                "batch_id": scope.batch_id,
                "landmarks": [landmark.to_dict() for landmark in landmarks],
            }
        ).decode("utf-8")
        correction_context = ""
        if correction_findings:
            canonical_findings = canonical_json_bytes(
                {"source_id": source.source_id, "findings": list(correction_findings)}
            ).decode("utf-8")
            canonical_allowed_ids = canonical_json_bytes(list(allowed_record_ids)).decode("utf-8")
            canonical_new_landmarks = canonical_json_bytes(list(allowed_new_landmark_ids)).decode(
                "utf-8"
            )
            correction_context = f"""

This batch was reopened by the fresh auditor. Inspect every mapped finding below against
the immutable paper. These are audit targets, not facts to accept blindly, and they do
not authorize invented science. The repair boundary has two mutation classes. IDs in the
revision list may receive whole-record next revisions. New IDs may be proposed only when
their landmark disposition ties them to a landmark in the addition list. Do not revise or
add anything else. Preserve unaffected source-faithful content. If the paper cannot support
a correction, request manual review instead of returning an empty or no-op proposal.

Allowed revision record IDs (canonical JSON):
{canonical_allowed_ids}

Allowed new-record landmark IDs (canonical JSON):
{canonical_new_landmarks}

Mapped source-local audit findings (canonical JSON):
{canonical_findings}

Before returning the correction, serialize the exact candidate to
scratch/reading-proposal.json and run:

python tools/validate_reading.py scratch/reading-proposal.json

The validator overlays the candidate on the current WIP with persistent revision
semantics, checks the audit mutation boundary, and validates the complete merged source
map through dossier, evidence, argument-structure, canonical round-trip, and graph
invariants. Failures identify the relevant record when one is known. Repair the candidate
and rerun it within this same Codex turn. Return only the exact JSON that passed. If no
source-faithful candidate can pass, return a schema-valid manual-review proposal instead
of bypassing the validator.
"""
        retry_context = ""
        if prior_validation_error:
            retry_context = f"""

The preceding proposal for this same batch was rejected before entering WIP. Inspect its
JSON in output/ and correct the deterministic validation error below. Do not weaken the
source mapping merely to satisfy validation. A rejected proposal is not revision history.
Determine revisions only from output/wip.json: an ID absent from WIP remains revision 1
even when that ID appeared in rejected JSON.

Prior validation error:
{prior_validation_error}
"""
        return f"""Stage: reading
Source: {source.source_id}
Assigned unit: {scope.scope_id} ({scope.label}), PDF pages {scope.page_start}-{scope.page_end}
Attempt: {attempt}

Read AGENTS.md, task.json, input/reading.md, input/source.json, the complete
input/paper.pdf, and output/wip.json when it exists. Return only JSON matching
templates/reading-proposal.schema.json. Map the assigned unit with exact evidence and
source-attributed records while preserving its scientific progression. Records are
proposals only. Use stable source-scoped IDs and sequential revisions; do not repeat an
existing record unless supplying its next correction revision. Account for every assigned
landmark and apply the atomization, evidence-segment, presentation-order, and
scientific-use rules in input/reading.md. The returned scope_id MUST be exactly
"{scope.scope_id}"; treat it as an opaque coordinator identity and do not expand or rename
it. Recommended atoms are claim, definition, assumption, concept, equation, method,
result, question, objection, response, limitation, and figure. Recommended move roles are
introduces, defines, assumes, derives, supports, justifies, applies, qualifies, contrasts,
interprets, objects, responds, and concludes. Recommended thread kinds are argument, derivation,
model-construction, question-method-result, definition-development, objection-response,
and comparison. Use these when faithful. Every other source-native slug MUST carry a
non-null type_gap whose slug exactly matches and whose rationale explains why the
recommended vocabulary is insufficient. Atom kinds and move roles are separate layers:
do not use a move-role word as an atom kind merely because a move concludes, qualifies,
or otherwise uses that atom. Classify the atom's addressable information independently.
Assigned landmark batch (canonical JSON):
{landmark_context}
If output contains a prior rejected proposal, inspect and correct it rather than repeating
its identity or type-gap errors. Never infer or endorse missing science.
{correction_context}
{retry_context}
"""

    def _mapped_findings_for_scope(
        self,
        source_id: str,
        scope_id: str,
        *,
        audit_job_identifier: str | None,
        selected_finding_ids: tuple[str, ...] = (),
    ) -> tuple[dict[str, Any], ...]:
        if audit_job_identifier is None:
            return ()
        mapped: list[dict[str, Any]] = []
        for finding in self.state.findings_for(source_id, status="open"):
            payload = dict(finding["payload"])
            repair = payload.get("repair", {})
            if scope_id not in repair.get("reopen_scope_ids", []):
                continue
            if finding["audit_job_id"] != audit_job_identifier:
                continue
            if selected_finding_ids and str(finding["finding_id"]) not in selected_finding_ids:
                continue
            mapped.append(
                {
                    "finding_id": str(payload["finding_id"]),
                    "kind": str(payload["kind"]),
                    "description": str(payload["description"]),
                    "record_ids": list(payload["record_ids"]),
                    "landmark_ids": list(payload["landmark_ids"]),
                    "evidence": str(payload["evidence"]),
                    "scope_ids": list(payload["scope_ids"]),
                    "repair": dict(repair),
                }
            )
        return tuple(mapped)

    def _correction_boundary(
        self, correction_findings: tuple[dict[str, Any], ...]
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        if not correction_findings:
            raise ManualReviewRequired("reopened scope has no active mapped audit findings")
        missing_targets = sorted(
            str(finding["finding_id"])
            for finding in correction_findings
            if not finding["repair"]["revise_record_ids"]
            and not finding["repair"]["add_for_landmark_ids"]
        )
        if missing_targets:
            raise ManualReviewRequired(
                "mapped audit findings have no repair targets: " + ", ".join(missing_targets)
            )
        return (
            tuple(
                sorted(
                    {
                        str(record_id)
                        for finding in correction_findings
                        for record_id in finding["repair"]["revise_record_ids"]
                    }
                )
            ),
            tuple(
                sorted(
                    {
                        str(landmark_id)
                        for finding in correction_findings
                        for landmark_id in finding["repair"]["add_for_landmark_ids"]
                    }
                )
            ),
        )

    def _validate_landmark_record_links(
        self,
        landmark_dispositions: tuple[dict[str, Any], ...],
        *,
        available_record_ids: set[str],
    ) -> None:
        for disposition in landmark_dispositions:
            referenced = {str(item) for item in disposition["record_ids"]}
            if unknown := referenced - available_record_ids:
                raise ProposalValidationError(
                    "landmark disposition references unavailable records; "
                    f"landmark_id={disposition['landmark_id']}; unknown={sorted(unknown)}"
                )

    def _validate_proposal_revisions(self, source_id: str, records: tuple[Record, ...]) -> None:
        active = {str(record["id"]): record for record in self.state.latest_wip_records(source_id)}
        for record in records:
            existing = active.get(record.id)
            if existing is None:
                if record.revision != 1:
                    raise ProposalValidationError(
                        "proposal revision is not sequential; rejected proposals are not "
                        f"revision history: {record.id}@{record.revision}; expected=1"
                    )
                continue
            current_revision = int(existing["revision"])
            if record.revision == current_revision and record.to_dict() == existing:
                continue
            expected = current_revision + 1
            if record.revision != expected:
                raise ProposalValidationError(
                    "proposal revision is not sequential: "
                    f"{record.id}@{record.revision}; expected={expected}"
                )

    def _write_coverage(self, entry: CoverageEntry, job_identifier: str) -> None:
        self.state.upsert_coverage(
            self.state.job_record(job_identifier)["source_id"],  # type: ignore[index]
            scope_id=entry.scope_id,
            scope_kind=entry.scope_kind,
            disposition=entry.disposition,
            details=entry.details,
            job_identifier=job_identifier,
        )

    def _refresh_page_coverage(
        self,
        source: RegisteredSource,
        scopes: tuple[ScopePlan, ...],
        landmarks: tuple[LandmarkPlan, ...],
        batches: tuple[BatchPlan, ...],
        *,
        job_identifier: str,
    ) -> None:
        by_id = _coverage_by_id(self.state.coverage_for(source.source_id))
        terminal_batches = {
            batch.batch_id
            for batch in batches
            if batch.batch_id in by_id
            and by_id[batch.batch_id].disposition in {"covered", "not_substantive"}
        }
        for scope in scopes:
            related_batches = {
                batch.batch_id for batch in batches if scope.scope_id in batch.scope_ids
            }
            terminal = bool(related_batches) and related_batches <= terminal_batches
            self._write_coverage(
                CoverageEntry(
                    f"context:{scope.scope_id}",
                    "context_scope",
                    "covered" if terminal else "pending",
                    scope.to_dict(),
                ),
                job_identifier,
            )
        for page in range(1, source.main_asset.pdf_metadata.page_count + 1):
            covering_scopes = [
                scope for scope in scopes if scope.page_start <= page <= scope.page_end
            ]
            covering_landmarks = [
                landmark
                for landmark in landmarks
                if landmark.page_start <= page <= landmark.page_end
            ]
            landmark_terminal = all(
                landmark.coverage_id in by_id
                and by_id[landmark.coverage_id].disposition in {"covered", "not_substantive"}
                for landmark in covering_landmarks
            )
            scope_terminal = all(
                all(
                    batch.batch_id in terminal_batches
                    for batch in batches
                    if scope.scope_id in batch.scope_ids
                )
                for scope in covering_scopes
            )
            terminal = landmark_terminal and scope_terminal
            self._write_coverage(
                CoverageEntry(
                    f"page:{page:04d}",
                    "page",
                    "covered" if terminal else "pending",
                    {
                        "page": page,
                        "context_scope_ids": [scope.scope_id for scope in covering_scopes],
                        "landmark_ids": [landmark.landmark_id for landmark in covering_landmarks],
                    },
                ),
                job_identifier,
            )

    def _write_wip_snapshot(
        self, workspace: Workspace, source: RegisteredSource, source_summary: str
    ) -> None:
        dossier = self._wip_snapshot_payload(source, source_summary)
        (workspace.path / "output" / "wip.json").write_bytes(canonical_json_bytes(dossier))

    def _wip_snapshot_payload(
        self, source: RegisteredSource, source_summary: str
    ) -> dict[str, Any]:
        records = self.state.latest_wip_records(source.source_id)
        return {
            "schema_version": 1,
            "source_id": source.source_id,
            "title": _source_title(source),
            "summary": source_summary,
            "records": list(records),
        }

    def _validated_result(
        self,
        source: RegisteredSource,
        run_identifier: str,
        *,
        source_summary: str,
        warnings: tuple[str, ...],
    ) -> ReadingRunResult:
        coverage = coverage_report_for(
            self.state,
            source.source_id,
            page_count=source.main_asset.pdf_metadata.page_count,
        )
        if not coverage.complete:
            raise ReadingValidationError("; ".join(coverage.errors))
        records = self.state.latest_wip_records(source.source_id)
        dossier = dossier_from_wip(
            source_id=source.source_id,
            title=_source_title(source),
            source_summary=source_summary,
            records=records,
        )
        validation = require_valid_dossier(
            dossier,
            schema_directory=self.schema_directory,
            expected_asset_sha256=source.main_asset.sha256,
            page_count=source.main_asset.pdf_metadata.page_count,
        )
        verify_asset(source.main_asset)
        return ReadingRunResult(
            source_id=source.source_id,
            run_id=run_identifier,
            state=self.state.source_state(source.source_id) or "registered",
            job_ids=tuple(str(job["job_id"]) for job in self.state.jobs_for_run(run_identifier)),
            wip_record_count=len(records),
            coverage=coverage,
            validation=validation,
            warnings=warnings,
        )


def coverage_report_for(
    repository: StateRepository, source_id: str, *, page_count: int
) -> CoverageReport:
    entries = tuple(
        CoverageEntry(
            scope_id=str(row["scope_id"]),
            scope_kind=str(row["scope_kind"]),
            disposition=str(row["disposition"]),
            details=dict(row["details"]),
        )
        for row in repository.coverage_for(source_id)
    )
    return validate_coverage(entries, page_count=page_count)


def validate_source_wip(
    repository: StateRepository,
    source: RegisteredSource,
    *,
    schema_directory: Path,
) -> tuple[CoverageReport, ValidationReport]:
    coverage = coverage_report_for(
        repository,
        source.source_id,
        page_count=source.main_asset.pdf_metadata.page_count,
    )
    if not coverage.complete:
        raise ReadingValidationError("; ".join(coverage.errors))
    records = repository.latest_wip_records(source.source_id)
    summary = " ".join(
        str(record.get("summary", ""))
        for record in records
        if record.get("record_type") == "thread"
    ).strip()
    dossier = dossier_from_wip(
        source_id=source.source_id,
        title=_source_title(source),
        source_summary=summary or f"Validated source-local dossier for {source.source_id}.",
        records=records,
    )
    validation = require_valid_dossier(
        dossier,
        schema_directory=schema_directory,
        expected_asset_sha256=source.main_asset.sha256,
        page_count=source.main_asset.pdf_metadata.page_count,
    )
    verify_asset(source.main_asset)
    return coverage, validation


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


def _source_summary(records: tuple[dict[str, Any], ...], source_id: str) -> str:
    summary = " ".join(
        str(record.get("summary", ""))
        for record in records
        if record.get("record_type") == "thread"
    ).strip()
    return summary or f"Validated source-local dossier for {source_id}."


def _reading_correction_validator_source(repository_root: Path) -> str:
    root = str(repository_root.resolve())
    return f"""#!/usr/bin/env python3
import json
import sys
from pathlib import Path

sys.path.insert(0, {root!r} + "/" + "src")

from research_map.reading_correction_admission import validate_reading_correction_file

if len(sys.argv) != 2:
    print(json.dumps({{"ok": False, "error": "usage: validate_reading.py PROPOSAL.json"}}))
    raise SystemExit(1)

workspace = Path(__file__).resolve().parents[1]
contract = json.loads(
    (workspace / "scratch" / "correction-contract.json").read_text(encoding="utf-8")
)
wip = json.loads((workspace / "output" / "wip.json").read_text(encoding="utf-8"))
admission = validate_reading_correction_file(
    Path(sys.argv[1]),
    current_wip=wip,
    correction_contract=contract,
    schema_directory=Path({root!r}) / "schemas" / "research-map" / "v1",
)
print(json.dumps(admission.to_dict(), sort_keys=True))
raise SystemExit(0 if admission.ok else 1)
"""


def _coverage_by_id(rows: tuple[dict[str, Any], ...]) -> dict[str, CoverageEntry]:
    return {
        str(row["scope_id"]): CoverageEntry(
            scope_id=str(row["scope_id"]),
            scope_kind=str(row["scope_kind"]),
            disposition=str(row["disposition"]),
            details=dict(row["details"]),
        )
        for row in rows
    }
