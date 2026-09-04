"""Fresh source-wide coherence audit with a bounded reopen loop."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from research_map.codex import CodexJob, CodexRunner
from research_map.ids import job_id, run_id
from research_map.proposals import dossier_from_wip
from research_map.reading import ReadingCoordinator, WorkerExecutionError, validate_source_wip
from research_map.receipts import canonical_json_bytes, read_receipt
from research_map.source import RegisteredSource, resolve_source, verify_asset
from research_map.state import StateRepository
from research_map.workspace import Workspace, WorkspaceBuilder, WorkspaceInput

AUDIT_FINDING_KINDS = frozenset(
    {
        "missing_landmark",
        "missing_evidence",
        "incorrect_attribution",
        "missing_qualification",
        "disconnected_atom",
        "missing_intermediate_move",
        "broken_branch",
        "incorrect_move_io",
        "forced_type",
        "manual_review_required",
    }
)
MAX_AUDIT_ATTEMPTS = 3
AUDIT_COORDINATOR_VERSION = "1.0.1"
REVIEWED_VERIFICATION_ATTEMPT = MAX_AUDIT_ATTEMPTS + 1
REVIEWED_VERIFICATION_RETRY_KIND = "audit:reviewed-verification-retry"


class AuditValidationError(ValueError):
    """Raised when a fresh audit cannot reach a bounded pass."""


@dataclass(frozen=True, slots=True)
class AuditFinding:
    payload: dict[str, Any]

    @property
    def finding_id(self) -> str:
        return str(self.payload["finding_id"])

    @property
    def kind(self) -> str:
        return str(self.payload["kind"])

    @property
    def reopen_scope_ids(self) -> tuple[str, ...]:
        return tuple(self.payload["repair"]["reopen_scope_ids"])

    @property
    def revise_record_ids(self) -> tuple[str, ...]:
        return tuple(self.payload["repair"]["revise_record_ids"])

    @property
    def add_for_landmark_ids(self) -> tuple[str, ...]:
        return tuple(self.payload["repair"]["add_for_landmark_ids"])


@dataclass(frozen=True, slots=True)
class AuditResult:
    source_id: str
    attempt: int
    outcome: str
    summary: str
    checks: dict[str, bool]
    inspection_ledger: dict[str, dict[str, tuple[str, ...]]]
    findings: tuple[AuditFinding, ...]
    warnings: tuple[str, ...]
    reviewed_continuation: dict[str, Any] | None
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class FindingVerificationDecision:
    payload: dict[str, Any]

    @property
    def finding_id(self) -> str:
        return str(self.payload["finding_id"])

    @property
    def verdict(self) -> str:
        return str(self.payload["verdict"])


@dataclass(frozen=True, slots=True)
class FindingVerificationResult:
    source_id: str
    attempt: int
    decisions: tuple[FindingVerificationDecision, ...]
    warnings: tuple[str, ...]
    payload: dict[str, Any]

    def decisions_for(self, verdict: str) -> tuple[FindingVerificationDecision, ...]:
        return tuple(decision for decision in self.decisions if decision.verdict == verdict)


@dataclass(frozen=True, slots=True)
class AuditRunResult:
    source_id: str
    run_id: str
    state: str
    audit_job_ids: tuple[str, ...]
    attempts: int
    open_findings: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "audit_job_ids": list(self.audit_job_ids),
            "attempts": self.attempts,
            "open_findings": list(self.open_findings),
            "warnings": list(self.warnings),
        }


class AuditCoordinator:
    def __init__(
        self,
        *,
        repository_root: Path,
        state_path: Path,
        cache_root: Path | None = None,
        codex_executable: str = "codex",
    ) -> None:
        self.repository_root = repository_root.resolve()
        self.state_path = state_path
        self.state = StateRepository(state_path)
        self.cache_root = cache_root or self.repository_root / ".cache" / "research-map"
        self.schema_directory = self.repository_root / "schemas" / "research-map" / "v1"
        self.protocol_directory = self.repository_root / "protocols" / "research-map" / "v1"
        self.codex_executable = codex_executable

    def run_through_audit_passed(
        self,
        source_id: str,
        *,
        model: str,
        reviewed_finding_ids: tuple[str, ...] = (),
        review_rationale: str | None = None,
        command_context: dict[str, Any] | None = None,
    ) -> AuditRunResult:
        if not model.strip():
            raise ValueError("audit run requires an explicit model")
        self.state.initialize()
        source = resolve_source(source_id, repository_root=self.repository_root)
        self._recover_rejected_audit_correction(source_id)
        if reviewed_finding_ids or review_rationale is not None:
            reviewed_trail = self._existing_reviewed_resume(
                source,
                model=model,
                reviewed_finding_ids=reviewed_finding_ids,
                review_rationale=review_rationale,
                command_context=command_context,
            )
            if reviewed_trail is not None:
                return self._resume_reviewed_verification(source, reviewed_trail)
            return self._run_reviewed_resume(
                source,
                model=model,
                reviewed_finding_ids=reviewed_finding_ids,
                review_rationale=review_rationale,
                command_context=command_context,
            )
        if self.state.source_state(source_id) == "audit_passed":
            audit_jobs = self.state.jobs_for_source(source_id, job_kind="audit")
            run_identifier = (
                str(audit_jobs[-1]["run_id"])
                if audit_jobs
                else self._run_identifier(source_id, model)
            )
            return self._result(source_id, run_identifier, ())
        if self.state.source_state(source_id) not in {"source_complete", "audit_failed"}:
            raise AuditValidationError("source must be complete before audit")

        prior_audit_jobs = self.state.jobs_for_source(source_id, job_kind="audit")
        if self.state.source_state(source_id) == "audit_failed" and prior_audit_jobs:
            run_identifier = str(prior_audit_jobs[-1]["run_id"])
            run = self.state.run_record(run_identifier)
            if run is None or run["model"] != model:
                raise AuditValidationError("model does not match the retained audit run")
        else:
            run_identifier = self._run_identifier(source_id, model)
            self.state.register_run(
                run_identifier,
                source_id=source_id,
                requested_through="audit_passed",
                model=model,
            )
        existing_jobs = self.state.jobs_for_run(run_identifier)
        completed_attempts = max(
            (int(job["attempt"]) for job in existing_jobs if job["kind"] == "audit"),
            default=0,
        )
        if completed_attempts >= MAX_AUDIT_ATTEMPTS:
            raise AuditValidationError("third failed audit requires manual review")
        if self.state.source_state(source_id) == "audit_failed":
            self._rerun_reopened_reading(source_id, model)

        warnings: list[str] = []
        for attempt in range(completed_attempts + 1, MAX_AUDIT_ATTEMPTS + 1):
            coverage, _ = validate_source_wip(
                self.state,
                source,
                schema_directory=self.schema_directory,
            )
            workspace = self._audit_workspace(
                source,
                run_identifier=run_identifier,
                attempt=attempt,
                coverage=coverage.to_dict(),
            )
            job_identifier, output_path = self._execute_audit_job(
                source=source,
                run_identifier=run_identifier,
                attempt=attempt,
                model=model,
                workspace=workspace,
            )
            verification_job_id: str | None = None
            verification: FindingVerificationResult | None = None
            actionable_findings: tuple[AuditFinding, ...] = ()
            try:
                inspection_expectations = self._inspection_expectations(source_id)
                result = load_audit_result(
                    output_path,
                    schema_path=self.schema_directory / "audit-result.schema.json",
                    expected_source_id=source_id,
                    expected_attempt=attempt,
                    record_ids={
                        str(record["id"]) for record in self.state.latest_wip_records(source_id)
                    },
                    landmark_ids=inspection_expectations["coverage"],
                    scope_ids={str(row["scope_id"]) for row in self.state.coverage_for(source_id)},
                    reopen_scope_ids={
                        str(row["scope_id"])
                        for row in self.state.coverage_for(source_id)
                        if row["scope_kind"] == "reading_scope"
                    },
                    inspection_expectations=inspection_expectations,
                )
                actionable_findings = result.findings
                if result.findings:
                    verification, verification_job_id = self._verify_audit_findings(
                        source=source,
                        run_identifier=run_identifier,
                        attempt=attempt,
                        model=model,
                        findings=result.findings,
                    )
                    confirmed_ids = {
                        decision.finding_id for decision in verification.decisions_for("confirmed")
                    }
                    actionable_findings = tuple(
                        finding
                        for finding in result.findings
                        if finding.finding_id in confirmed_ids
                    )
                applied = self.state.record_findings(
                    source_id,
                    audit_job_identifier=job_identifier,
                    findings=[finding.payload for finding in actionable_findings],
                )
                self._mark_job_succeeded(
                    job_identifier,
                    applied,
                    inspection_ledger=result.inspection_ledger,
                    finding_verification_job_id=verification_job_id,
                    finding_verification=(
                        [decision.payload for decision in verification.decisions]
                        if verification is not None
                        else None
                    ),
                )
            except (AuditValidationError, KeyError, ValueError) as error:
                self._mark_job_validation_failed(job_identifier, str(error))
                raise
            warnings.extend(result.warnings)
            if verification is not None:
                warnings.extend(verification.warnings)

            if result.outcome == "pass":
                self.state.resolve_open_findings(source_id)
                self.state.advance_source(
                    source_id,
                    "audit_passed",
                    run_identifier=run_identifier,
                    job_identifier=job_identifier,
                    receipt={
                        "kind": "source_audit_passed",
                        "attempt": attempt,
                        "checks": result.checks,
                        "inspection_ledger": result.inspection_ledger,
                    },
                )
                verify_asset(source.main_asset)
                return self._result(source_id, run_identifier, tuple(warnings))

            if verification is not None and verification.decisions_for("manual_review"):
                self.state.advance_source(
                    source_id,
                    "audit_failed",
                    run_identifier=run_identifier,
                    job_identifier=verification_job_id,
                    receipt={
                        "kind": "finding_verification_requires_manual_review",
                        "attempt": attempt,
                        "finding_ids": [
                            decision.finding_id
                            for decision in verification.decisions_for("manual_review")
                        ],
                        "wip_mutated": False,
                    },
                )
                raise AuditValidationError("finding verification requires manual source review")

            if not actionable_findings:
                rejected_ids = (
                    [decision.finding_id for decision in verification.decisions_for("rejected")]
                    if verification is not None
                    else []
                )
                warnings.append(
                    f"audit attempt {attempt}: proposed findings rejected before WIP mutation: "
                    f"{rejected_ids}"
                )
                if attempt >= MAX_AUDIT_ATTEMPTS:
                    raise AuditValidationError(
                        "third audit produced no independently confirmed finding"
                    )
                continue

            self.state.advance_source(
                source_id,
                "audit_failed",
                run_identifier=run_identifier,
                job_identifier=job_identifier,
                receipt={
                    "kind": "source_audit_failed",
                    "attempt": attempt,
                    "finding_ids": [finding.finding_id for finding in actionable_findings],
                    "finding_verification_job_id": verification_job_id,
                },
            )
            if result.outcome == "manual_review" or any(
                finding.kind == "manual_review_required" for finding in actionable_findings
            ):
                raise AuditValidationError("audit requires manual source review")
            if attempt >= MAX_AUDIT_ATTEMPTS:
                raise AuditValidationError("third failed audit requires manual review")
            self._reopen_scopes(source_id, job_identifier, attempt, actionable_findings)
            self._rerun_reopened_reading(source_id, model)

        raise AuditValidationError("audit loop ended without a pass")

    def _run_reviewed_resume(
        self,
        source: RegisteredSource,
        *,
        model: str,
        reviewed_finding_ids: tuple[str, ...],
        review_rationale: str | None,
        command_context: dict[str, Any] | None,
    ) -> AuditRunResult:
        review = self._validate_reviewed_resume(
            source,
            model=model,
            reviewed_finding_ids=reviewed_finding_ids,
            review_rationale=review_rationale,
            command_context=command_context,
        )
        originating_job_id = str(review["originating_audit"]["job_id"])
        run_identifier = str(review["originating_audit"]["run_id"])
        originating_attempt = int(review["originating_audit"]["attempt"])
        finding_payloads = tuple(
            AuditFinding(dict(finding)) for finding in review.pop("finding_payloads")
        )
        transition = self.state.advance_source(
            source.source_id,
            "reading",
            run_identifier=run_identifier,
            job_identifier=originating_job_id,
            receipt=review,
        )
        review["transition_id"] = transition.transition_id
        self._reopen_scopes(
            source.source_id,
            originating_job_id,
            originating_attempt,
            finding_payloads,
        )

        reopen_scope_ids = tuple(str(item) for item in review["reopen_scope_ids"])
        correction_kind = f"reading:{reopen_scope_ids[0]}"
        before_corrections = len(
            self.state.jobs_for_source(source.source_id, job_kind=correction_kind)
        )
        ReadingCoordinator(
            repository_root=self.repository_root,
            state_path=self.state_path,
            cache_root=self.cache_root,
            codex_executable=self.codex_executable,
        ).run_through_source_complete(
            source.source_id,
            model=model,
            reviewed_finding_ids=tuple(str(item) for item in review["selected_finding_ids"]),
            reviewed_resume=review,
        )
        after_corrections = len(
            self.state.jobs_for_source(source.source_id, job_kind=correction_kind)
        )
        if after_corrections != before_corrections + 1:
            raise AuditValidationError("reviewed resume did not run exactly one correction job")

        verification = {
            "kind": "reviewed_verification",
            "reviewed_resume_transition_id": transition.transition_id,
            "originating_audit_attempt": originating_attempt,
            "originating_audit_job_id": originating_job_id,
            "selected_finding_ids": list(review["selected_finding_ids"]),
            "allowed_record_ids": list(review["allowed_record_ids"]),
            "allowed_new_landmark_ids": list(review["allowed_new_landmark_ids"]),
            "allowed_reopen_scope_ids": list(review["reopen_scope_ids"]),
        }
        coverage, _ = validate_source_wip(
            self.state,
            source,
            schema_directory=self.schema_directory,
        )
        workspace = self._audit_workspace(
            source,
            run_identifier=run_identifier,
            attempt=REVIEWED_VERIFICATION_ATTEMPT,
            coverage=coverage.to_dict(),
            reviewed_continuation=verification,
        )
        job_identifier = job_id(run_identifier, "audit", REVIEWED_VERIFICATION_ATTEMPT)
        try:
            job_identifier, output_path = self._execute_audit_job(
                source=source,
                run_identifier=run_identifier,
                attempt=REVIEWED_VERIFICATION_ATTEMPT,
                model=model,
                workspace=workspace,
                reviewed_continuation=verification,
            )
        except (AuditValidationError, WorkerExecutionError) as error:
            self._stop_reviewed_verification(
                source.source_id,
                run_identifier=run_identifier,
                job_identifier=job_identifier,
                verification=verification,
                error=str(error),
            )
            raise
        try:
            inspection_expectations = self._inspection_expectations(source.source_id)
            result = load_audit_result(
                output_path,
                schema_path=self.schema_directory / "audit-result.schema.json",
                expected_source_id=source.source_id,
                expected_attempt=REVIEWED_VERIFICATION_ATTEMPT,
                record_ids={
                    str(record["id"]) for record in self.state.latest_wip_records(source.source_id)
                },
                landmark_ids=inspection_expectations["coverage"],
                scope_ids={
                    str(row["scope_id"]) for row in self.state.coverage_for(source.source_id)
                },
                reopen_scope_ids={
                    str(row["scope_id"])
                    for row in self.state.coverage_for(source.source_id)
                    if row["scope_kind"] == "reading_scope"
                },
                inspection_expectations=inspection_expectations,
                expected_reviewed_continuation=verification,
            )
            applied = self.state.record_findings(
                source.source_id,
                audit_job_identifier=job_identifier,
                findings=[finding.payload for finding in result.findings],
            )
            self._mark_job_succeeded(
                job_identifier,
                applied,
                inspection_ledger=result.inspection_ledger,
                reviewed_continuation=verification,
            )
        except (AuditValidationError, KeyError, ValueError) as error:
            self._mark_job_validation_failed(
                job_identifier,
                str(error),
                reviewed_continuation=verification,
            )
            self._stop_reviewed_verification(
                source.source_id,
                run_identifier=run_identifier,
                job_identifier=job_identifier,
                verification=verification,
                error=str(error),
            )
            raise

        if result.outcome == "pass":
            self.state.resolve_open_findings(source.source_id)
            self.state.advance_source(
                source.source_id,
                "audit_passed",
                run_identifier=run_identifier,
                job_identifier=job_identifier,
                receipt={
                    "kind": "source_audit_passed",
                    "attempt": REVIEWED_VERIFICATION_ATTEMPT,
                    "checks": result.checks,
                    "inspection_ledger": result.inspection_ledger,
                    "reviewed_continuation": verification,
                },
            )
            verify_asset(source.main_asset)
            return self._result(source.source_id, run_identifier, result.warnings)

        self._stop_reviewed_verification(
            source.source_id,
            run_identifier=run_identifier,
            job_identifier=job_identifier,
            verification=verification,
            error="fresh reviewed verification did not pass",
            finding_ids=tuple(finding.finding_id for finding in result.findings),
        )
        raise AuditValidationError(
            "reviewed verification audit failed; another reviewed resume is not permitted"
        )

    def _existing_reviewed_resume(
        self,
        source: RegisteredSource,
        *,
        model: str,
        reviewed_finding_ids: tuple[str, ...],
        review_rationale: str | None,
        command_context: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        reviewed_transitions: list[tuple[Any, dict[str, Any]]] = []
        for row in self.state.transitions_for("source", source.source_id):
            receipt = json.loads(str(row["receipt_json"]))
            if receipt.get("kind") == "reviewed_resume":
                reviewed_transitions.append((row, receipt))
        if not reviewed_transitions:
            return None
        if len(reviewed_transitions) != 1:
            raise AuditValidationError("reviewed resume trail is not unique")

        transition, review = reviewed_transitions[0]
        selected_ids = tuple(sorted(set(reviewed_finding_ids)))
        if list(selected_ids) != review.get("selected_finding_ids"):
            raise AuditValidationError("reviewed finding IDs do not match the retained resume")
        if review_rationale != review.get("rationale"):
            raise AuditValidationError("review rationale does not match the retained resume")
        if model != review.get("model"):
            raise AuditValidationError("review model does not match the retained resume")
        submitted_context = command_context or {
            "command": "run",
            "source": source.source_id,
            "through": "audit_passed",
            "model": model,
            "reviewed_finding": list(reviewed_finding_ids),
            "review_rationale": review_rationale,
        }
        if submitted_context != review.get("command_context"):
            raise AuditValidationError("command context does not match the retained resume")

        asset = source.main_asset
        expected_asset = {
            "asset_id": asset.asset_id,
            "sha256": asset.sha256,
            "size_bytes": asset.size_bytes,
        }
        if review.get("asset_fingerprint") != expected_asset:
            raise AuditValidationError("retained reviewed resume asset fingerprint changed")
        originating_audit = review.get("originating_audit")
        if not isinstance(originating_audit, dict):
            raise AuditValidationError("retained reviewed resume lacks its originating audit")
        run_identifier = str(originating_audit.get("run_id", ""))
        originating_job_id = str(originating_audit.get("job_id", ""))
        if int(originating_audit.get("attempt", 0)) != MAX_AUDIT_ATTEMPTS:
            raise AuditValidationError("retained reviewed resume has the wrong origin attempt")
        run = self.state.run_record(run_identifier)
        if run is None or run["source_id"] != source.source_id or run["model"] != model:
            raise AuditValidationError("retained reviewed resume run identity changed")

        verification = {
            "kind": "reviewed_verification",
            "reviewed_resume_transition_id": int(transition["transition_id"]),
            "originating_audit_attempt": MAX_AUDIT_ATTEMPTS,
            "originating_audit_job_id": originating_job_id,
            "selected_finding_ids": list(review["selected_finding_ids"]),
            "allowed_record_ids": list(review["allowed_record_ids"]),
            "allowed_new_landmark_ids": list(review["allowed_new_landmark_ids"]),
            "allowed_reopen_scope_ids": list(review["reopen_scope_ids"]),
        }
        retry_jobs = tuple(
            job
            for job in self.state.jobs_for_run(run_identifier)
            if job["kind"] == REVIEWED_VERIFICATION_RETRY_KIND
        )
        if retry_jobs:
            if (
                len(retry_jobs) == 1
                and retry_jobs[0]["state"] == "succeeded"
                and self.state.source_state(source.source_id) == "audit_passed"
            ):
                return {
                    "completed": True,
                    "run_id": run_identifier,
                    "model": model,
                    "verification": verification,
                }
            raise AuditValidationError("reviewed verification contract retry is already consumed")

        failed_job_id = job_id(run_identifier, "audit", REVIEWED_VERIFICATION_ATTEMPT)
        failed_job = self.state.job_record(failed_job_id)
        if failed_job is None:
            raise AuditValidationError("retained reviewed resume has no verification job")
        if failed_job["state"] == "succeeded":
            if self.state.source_state(source.source_id) == "audit_passed":
                return {
                    "completed": True,
                    "run_id": run_identifier,
                    "model": model,
                    "verification": verification,
                }
            raise AuditValidationError("schema-valid reviewed verification is terminal")
        if failed_job["state"] != "failed":
            raise AuditValidationError("reviewed verification failure is not retryable")

        receipt_path = failed_job.get("receipt_path")
        if not isinstance(receipt_path, str) or not Path(receipt_path).is_file():
            raise AuditValidationError("failed verification receipt is missing")
        failed_receipt = read_receipt(Path(receipt_path))
        output_path = (
            self.cache_root
            / "runs"
            / run_identifier
            / "audit"
            / str(REVIEWED_VERIFICATION_ATTEMPT)
            / "output"
            / f"{failed_job_id}.json"
        )
        validation = failed_receipt.get("validation")
        process = failed_receipt.get("process")
        if (
            failed_receipt.get("proposal") is not None
            or output_path.exists()
            or not isinstance(validation, dict)
            or validation.get("valid") is not False
            or not isinstance(process, dict)
            or int(process.get("returncode", 0)) == 0
        ):
            raise AuditValidationError("failed verification has semantic output and is terminal")
        if any(
            finding["audit_job_id"] == failed_job_id
            for finding in self.state.findings_for(source.source_id)
        ):
            raise AuditValidationError("failed verification already recorded semantic findings")

        events_path = (
            self.cache_root / "runs" / run_identifier / "events" / f"{failed_job_id}.jsonl"
        )
        failure_code = _preexecution_failure_code(events_path)
        if failure_code != "invalid_json_schema":
            raise AuditValidationError(
                "reviewed verification did not fail on a recognized pre-execution contract"
            )
        failed_job_receipts = [
            json.loads(str(row["receipt_json"]))
            for row in self.state.transitions_for("job", failed_job_id)
        ]
        if not failed_job_receipts or failed_job_receipts[-1].get("kind") != "worker_failed":
            raise AuditValidationError("failed verification job trail is incomplete")
        stop_receipts = [
            json.loads(str(row["receipt_json"]))
            for row in self.state.transitions_for("source", source.source_id)
            if row["job_id"] == failed_job_id
        ]
        terminal = next(
            (
                item
                for item in reversed(stop_receipts)
                if item.get("kind") == "reviewed_verification_failed"
            ),
            None,
        )
        if (
            terminal is None
            or terminal.get("finding_ids") != []
            or terminal.get("further_resume_permitted") is not False
            or terminal.get("reviewed_continuation") != verification
        ):
            raise AuditValidationError("failed verification terminal receipt is incomplete")

        correction_jobs: list[dict[str, Any]] = []
        applied_record_ids: tuple[str, ...] = ()
        for job in self.state.jobs_for_source(source.source_id):
            if not str(job["kind"]).startswith("reading:"):
                continue
            matched_review = False
            for row in self.state.transitions_for("job", str(job["job_id"])):
                receipt = json.loads(str(row["receipt_json"]))
                reviewed = receipt.get("reviewed_resume")
                if not isinstance(reviewed, dict):
                    continue
                if reviewed.get("transition_id") != transition["transition_id"]:
                    continue
                matched_review = True
                if receipt.get("kind") == "proposal_applied":
                    applied_record_ids = tuple(
                        sorted(str(item).rsplit("@", 1)[0] for item in receipt["records"])
                    )
            if matched_review:
                correction_jobs.append(job)
        unique_correction_jobs = {str(job["job_id"]): job for job in correction_jobs}
        if len(unique_correction_jobs) != 1:
            raise AuditValidationError("retained reviewed correction job is not unique")
        correction_job = next(iter(unique_correction_jobs.values()))
        if correction_job["state"] != "succeeded" or applied_record_ids != tuple(
            review["allowed_record_ids"]
        ):
            raise AuditValidationError("retained reviewed correction is incomplete")

        return {
            "completed": False,
            "run_id": run_identifier,
            "model": model,
            "failed_job_id": failed_job_id,
            "failure_code": failure_code,
            "reviewed_resume_transition_id": int(transition["transition_id"]),
            "verification": verification,
        }

    def _resume_reviewed_verification(
        self,
        source: RegisteredSource,
        trail: dict[str, Any],
    ) -> AuditRunResult:
        run_identifier = str(trail["run_id"])
        if trail["completed"]:
            return self._result(source.source_id, run_identifier, ())
        if self.state.source_state(source.source_id) != "audit_failed":
            raise AuditValidationError("verification retry requires retained audit_failed state")

        verification = dict(trail["verification"])
        failed_job_id = str(trail["failed_job_id"])
        retry_lineage = {
            "schema_version": "1.0",
            "kind": "reviewed_verification_contract_retry",
            "semantic_attempt": REVIEWED_VERIFICATION_ATTEMPT,
            "retry_origin_job_id": failed_job_id,
            "retry_origin_state": "failed",
            "retry_origin_failure_code": str(trail["failure_code"]),
            "reviewed_resume_transition_id": int(trail["reviewed_resume_transition_id"]),
        }
        coverage, _ = validate_source_wip(
            self.state,
            source,
            schema_directory=self.schema_directory,
        )
        self.state.advance_source(
            source.source_id,
            "reading",
            run_identifier=run_identifier,
            job_identifier=failed_job_id,
            receipt={
                "kind": "reviewed_verification_retry_started",
                "retry_lineage": retry_lineage,
                "wip_mutation_permitted": False,
            },
        )
        self.state.advance_source(
            source.source_id,
            "source_complete",
            run_identifier=run_identifier,
            job_identifier=failed_job_id,
            receipt={
                "kind": "reviewed_verification_retry_ready",
                "retry_lineage": retry_lineage,
                "coverage_complete": coverage.complete,
                "wip_mutated": False,
            },
        )
        workspace_root = (
            self.cache_root
            / "runs"
            / run_identifier
            / "reviewed-verification-retries"
            / failed_job_id
        )
        workspace = self._audit_workspace(
            source,
            run_identifier=run_identifier,
            attempt=REVIEWED_VERIFICATION_ATTEMPT,
            coverage=coverage.to_dict(),
            reviewed_continuation=verification,
            retry_lineage=retry_lineage,
            workspace_root=workspace_root,
        )
        retry_job_id = job_id(
            run_identifier,
            REVIEWED_VERIFICATION_RETRY_KIND,
            REVIEWED_VERIFICATION_ATTEMPT,
        )
        try:
            retry_job_id, output_path = self._execute_audit_job(
                source=source,
                run_identifier=run_identifier,
                attempt=REVIEWED_VERIFICATION_ATTEMPT,
                model=str(trail["model"]),
                workspace=workspace,
                reviewed_continuation=verification,
                job_kind=REVIEWED_VERIFICATION_RETRY_KIND,
                retry_lineage=retry_lineage,
            )
        except (AuditValidationError, WorkerExecutionError) as error:
            self._stop_reviewed_verification(
                source.source_id,
                run_identifier=run_identifier,
                job_identifier=retry_job_id,
                verification=verification,
                error=str(error),
                retry_lineage=retry_lineage,
            )
            raise
        try:
            inspection_expectations = self._inspection_expectations(source.source_id)
            result = load_audit_result(
                output_path,
                schema_path=self.schema_directory / "audit-result.schema.json",
                expected_source_id=source.source_id,
                expected_attempt=REVIEWED_VERIFICATION_ATTEMPT,
                record_ids={
                    str(record["id"]) for record in self.state.latest_wip_records(source.source_id)
                },
                landmark_ids=inspection_expectations["coverage"],
                scope_ids={
                    str(row["scope_id"]) for row in self.state.coverage_for(source.source_id)
                },
                reopen_scope_ids={
                    str(row["scope_id"])
                    for row in self.state.coverage_for(source.source_id)
                    if row["scope_kind"] == "reading_scope"
                },
                inspection_expectations=inspection_expectations,
                expected_reviewed_continuation=verification,
            )
            applied = self.state.record_findings(
                source.source_id,
                audit_job_identifier=retry_job_id,
                findings=[finding.payload for finding in result.findings],
            )
            self._mark_job_succeeded(
                retry_job_id,
                applied,
                inspection_ledger=result.inspection_ledger,
                reviewed_continuation=verification,
                retry_lineage=retry_lineage,
            )
        except (AuditValidationError, KeyError, ValueError) as error:
            self._mark_job_validation_failed(
                retry_job_id,
                str(error),
                reviewed_continuation=verification,
                retry_lineage=retry_lineage,
            )
            self._stop_reviewed_verification(
                source.source_id,
                run_identifier=run_identifier,
                job_identifier=retry_job_id,
                verification=verification,
                error=str(error),
                retry_lineage=retry_lineage,
            )
            raise

        if result.outcome == "pass":
            self.state.resolve_open_findings(source.source_id)
            self.state.advance_source(
                source.source_id,
                "audit_passed",
                run_identifier=run_identifier,
                job_identifier=retry_job_id,
                receipt={
                    "kind": "source_audit_passed",
                    "attempt": REVIEWED_VERIFICATION_ATTEMPT,
                    "checks": result.checks,
                    "inspection_ledger": result.inspection_ledger,
                    "reviewed_continuation": verification,
                    "retry_lineage": retry_lineage,
                },
            )
            verify_asset(source.main_asset)
            return self._result(source.source_id, run_identifier, result.warnings)

        self._stop_reviewed_verification(
            source.source_id,
            run_identifier=run_identifier,
            job_identifier=retry_job_id,
            verification=verification,
            error="fresh reviewed verification did not pass",
            finding_ids=tuple(finding.finding_id for finding in result.findings),
            retry_lineage=retry_lineage,
        )
        raise AuditValidationError(
            "reviewed verification audit failed; another reviewed resume is not permitted"
        )

    def _validate_reviewed_resume(
        self,
        source: RegisteredSource,
        *,
        model: str,
        reviewed_finding_ids: tuple[str, ...],
        review_rationale: str | None,
        command_context: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if not reviewed_finding_ids or review_rationale is None or not review_rationale.strip():
            raise AuditValidationError(
                "reviewed findings and a non-empty review rationale must be supplied together"
            )
        selected_ids = tuple(sorted(set(reviewed_finding_ids)))
        if len(selected_ids) != len(reviewed_finding_ids):
            raise AuditValidationError("reviewed finding IDs must not be repeated")
        if self.state.source_state(source.source_id) != "audit_failed":
            raise AuditValidationError("reviewed resume requires the retained audit_failed state")

        failed_transitions = tuple(
            row
            for row in self.state.transitions_for("source", source.source_id)
            if row["to_state"] == "audit_failed" and bool(row["applied"])
        )
        if not failed_transitions or failed_transitions[-1]["job_id"] is None:
            raise AuditValidationError("reviewed resume has no originating failed audit")
        originating_job_id = str(failed_transitions[-1]["job_id"])
        originating_job = self.state.job_record(originating_job_id)
        if (
            originating_job is None
            or originating_job["kind"] != "audit"
            or originating_job["state"] != "succeeded"
        ):
            raise AuditValidationError("latest failed audit job is incomplete")
        if int(originating_job["attempt"]) != MAX_AUDIT_ATTEMPTS:
            raise AuditValidationError(
                "reviewed resume is valid only after the latest third automatic audit"
            )
        run_identifier = str(originating_job["run_id"])
        run = self.state.run_record(run_identifier)
        if run is None or run["model"] != model:
            raise AuditValidationError("reviewed resume model does not match the retained run")
        audit_jobs = self.state.jobs_for_run(run_identifier)
        if any(
            job["kind"] == "audit" and int(job["attempt"]) > MAX_AUDIT_ATTEMPTS
            for job in audit_jobs
        ):
            raise AuditValidationError("reviewed resume was already used for this run")

        all_findings = self.state.findings_for(source.source_id)
        open_by_id = {
            str(finding["finding_id"]): finding
            for finding in all_findings
            if finding["status"] == "open"
        }
        all_by_id = {str(finding["finding_id"]): finding for finding in all_findings}
        selected_findings: list[dict[str, Any]] = []
        for finding_id in selected_ids:
            finding = open_by_id.get(finding_id)
            if finding is None:
                if finding_id in all_by_id:
                    raise AuditValidationError(f"reviewed finding is not open: {finding_id}")
                raise AuditValidationError(f"reviewed finding is unknown or stale: {finding_id}")
            if finding["audit_job_id"] != originating_job_id:
                raise AuditValidationError(
                    f"reviewed finding is not from the latest third audit: {finding_id}"
                )
            payload = dict(finding["payload"])
            if finding["kind"] == "manual_review_required":
                raise AuditValidationError(
                    f"manual-review finding cannot be reviewed-resumed: {finding_id}"
                )
            selected_findings.append(payload)

        active_record_ids = {
            str(record["id"]) for record in self.state.latest_wip_records(source.source_id)
        }
        coverage_by_id = {
            str(row["scope_id"]): row for row in self.state.coverage_for(source.source_id)
        }
        allowed_record_ids = tuple(
            sorted(
                {
                    str(record_id)
                    for finding in selected_findings
                    for record_id in finding.get("repair", {}).get("revise_record_ids", [])
                }
            )
        )
        allowed_new_landmark_ids = tuple(
            sorted(
                {
                    str(landmark_id)
                    for finding in selected_findings
                    for landmark_id in finding.get("repair", {}).get("add_for_landmark_ids", [])
                }
            )
        )
        if not allowed_record_ids and not allowed_new_landmark_ids:
            raise AuditValidationError("reviewed findings have no repair targets")
        if unknown := set(allowed_record_ids) - active_record_ids:
            raise AuditValidationError(
                f"reviewed findings reference non-active source records: {sorted(unknown)}"
            )
        reopen_scope_ids = tuple(
            sorted(
                {
                    str(scope_id)
                    for finding in selected_findings
                    for scope_id in finding.get("repair", {}).get("reopen_scope_ids", [])
                }
            )
        )
        if len(reopen_scope_ids) != 1:
            raise AuditValidationError(
                "reviewed findings must map to exactly one existing reading scope"
            )
        reopen_scope = coverage_by_id.get(reopen_scope_ids[0])
        if reopen_scope is None or reopen_scope["scope_kind"] != "reading_scope":
            raise AuditValidationError("reviewed finding has no existing reading-scope target")
        referenced_scope_ids = {
            str(scope_id)
            for finding in selected_findings
            for scope_id in finding.get("scope_ids", [])
        }
        if unknown := referenced_scope_ids - set(coverage_by_id):
            raise AuditValidationError(
                f"reviewed findings reference unknown source scopes: {sorted(unknown)}"
            )

        asset = source.main_asset
        return {
            "schema_version": "1.0",
            "kind": "reviewed_resume",
            "selected_finding_ids": list(selected_ids),
            "allowed_record_ids": list(allowed_record_ids),
            "allowed_new_landmark_ids": list(allowed_new_landmark_ids),
            "reopen_scope_ids": list(reopen_scope_ids),
            "rationale": review_rationale,
            "originating_audit": {
                "attempt": MAX_AUDIT_ATTEMPTS,
                "job_id": originating_job_id,
                "run_id": run_identifier,
            },
            "asset_fingerprint": {
                "asset_id": asset.asset_id,
                "sha256": asset.sha256,
                "size_bytes": asset.size_bytes,
            },
            "model": model,
            "command_context": command_context
            or {
                "command": "run",
                "source": source.source_id,
                "through": "audit_passed",
                "model": model,
                "reviewed_finding": list(reviewed_finding_ids),
                "review_rationale": review_rationale,
            },
            "finding_payloads": selected_findings,
        }

    def _stop_reviewed_verification(
        self,
        source_id: str,
        *,
        run_identifier: str,
        job_identifier: str,
        verification: dict[str, Any],
        error: str,
        finding_ids: tuple[str, ...] = (),
        retry_lineage: dict[str, Any] | None = None,
    ) -> None:
        if self.state.source_state(source_id) != "audit_failed":
            self.state.advance_source(
                source_id,
                "audit_failed",
                run_identifier=run_identifier,
                job_identifier=job_identifier,
                receipt={
                    "kind": "reviewed_verification_failed",
                    "attempt": REVIEWED_VERIFICATION_ATTEMPT,
                    "error": error,
                    "finding_ids": list(finding_ids),
                    "reviewed_continuation": verification,
                    "further_resume_permitted": False,
                    "retry_lineage": retry_lineage,
                },
            )

    def _audit_workspace(
        self,
        source: RegisteredSource,
        *,
        run_identifier: str,
        attempt: int,
        coverage: dict[str, Any],
        reviewed_continuation: dict[str, Any] | None = None,
        retry_lineage: dict[str, Any] | None = None,
        workspace_root: Path | None = None,
    ) -> Workspace:
        records = self.state.latest_wip_records(source.source_id)
        inspection_expectations = self._inspection_expectations(source.source_id)
        dossier = dossier_from_wip(
            source_id=source.source_id,
            title=_source_title(source),
            source_summary=_source_summary(records),
            records=records,
        )
        task = {
            "schema_version": "1.0",
            "run_id": run_identifier,
            "source_id": source.source_id,
            "stage": "audit",
            "attempt": attempt,
            "reviewed_continuation": reviewed_continuation,
            "retry_lineage": retry_lineage,
            "inspection_expectations": {
                lane: sorted(identifiers) for lane, identifiers in inspection_expectations.items()
            },
            "network_access": False,
        }
        inputs = (
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
                "audit.md",
                "audit_protocol",
                content=(self.protocol_directory / "audit.md").read_bytes(),
            ),
            WorkspaceInput(
                source.source_id,
                "wip.json",
                "validated_wip",
                content=canonical_json_bytes(dossier.to_dict()),
            ),
            WorkspaceInput(
                source.source_id,
                "coverage.json",
                "coverage_snapshot",
                content=canonical_json_bytes(coverage),
            ),
            WorkspaceInput(
                source.source_id,
                "audit-result.schema.json",
                "output_schema",
                area="templates",
                source_path=self.schema_directory / "audit-result.schema.json",
            ),
            WorkspaceInput(
                source.source_id,
                "validate_audit.py",
                "proposal_validator",
                area="tools",
                content=_audit_validator_source(self.repository_root).encode("utf-8"),
            ),
        )
        return WorkspaceBuilder(
            workspace_root or self.cache_root / "runs" / run_identifier,
            run_id=run_identifier,
            source=source,
        ).audit(attempt, prior_state_inputs=inputs)

    def _execute_audit_job(
        self,
        *,
        source: RegisteredSource,
        run_identifier: str,
        attempt: int,
        model: str,
        workspace: Workspace,
        reviewed_continuation: dict[str, Any] | None = None,
        job_kind: str = "audit",
        retry_lineage: dict[str, Any] | None = None,
    ) -> tuple[str, Path]:
        job_identifier = job_id(run_identifier, job_kind, attempt)
        self.state.advance_job(
            job_identifier,
            "pending",
            run_identifier=run_identifier,
            source_id=source.source_id,
            job_kind=job_kind,
            attempt=attempt,
            receipt={
                "kind": "job_created",
                "job_kind": job_kind,
                "reviewed_continuation": reviewed_continuation,
                "retry_lineage": retry_lineage,
            },
        )
        self.state.advance_job(
            job_identifier,
            "running",
            run_identifier=run_identifier,
            source_id=source.source_id,
            job_kind=job_kind,
            attempt=attempt,
            receipt={
                "kind": "worker_started",
                "model": model,
                "reviewed_continuation": reviewed_continuation,
                "retry_lineage": retry_lineage,
            },
        )
        output_path = workspace.path / "output" / f"{job_identifier}.json"
        receipt_path = (
            self.cache_root / "runs" / run_identifier / "receipts" / f"{job_identifier}.json"
        )
        run_result = CodexRunner(self.codex_executable).run(
            CodexJob(
                job_id=job_identifier,
                source_id=source.source_id,
                model=model,
                prompt=self._audit_prompt(
                    source,
                    attempt,
                    reviewed_continuation=reviewed_continuation,
                ),
                workspace=workspace.path,
                output_schema=workspace.path / "templates" / "audit-result.schema.json",
                events_path=(
                    self.cache_root / "runs" / run_identifier / "events" / f"{job_identifier}.jsonl"
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
                run_identifier=run_identifier,
                source_id=source.source_id,
                job_kind=job_kind,
                attempt=attempt,
                receipt={
                    "kind": "worker_failed",
                    "receipt_path": str(receipt_path),
                    "reviewed_continuation": reviewed_continuation,
                    "retry_lineage": retry_lineage,
                },
            )
            raise WorkerExecutionError(
                f"Codex job {job_identifier} exited with status {run_result.returncode}"
            )
        if not run_result.output_valid:
            self._mark_job_validation_failed(
                job_identifier,
                "; ".join(run_result.validation_errors),
                reviewed_continuation=reviewed_continuation,
                retry_lineage=retry_lineage,
            )
            raise AuditValidationError(
                f"Codex audit output failed schema validation: {job_identifier}"
            )
        return job_identifier, output_path

    def _verify_audit_findings(
        self,
        *,
        source: RegisteredSource,
        run_identifier: str,
        attempt: int,
        model: str,
        findings: tuple[AuditFinding, ...],
    ) -> tuple[FindingVerificationResult, str]:
        workspace = self._finding_verification_workspace(
            source,
            run_identifier=run_identifier,
            attempt=attempt,
            findings=findings,
        )
        job_identifier, output_path = self._execute_finding_verification_job(
            source=source,
            run_identifier=run_identifier,
            attempt=attempt,
            model=model,
            workspace=workspace,
            findings=findings,
        )
        try:
            result = load_finding_verification_result(
                output_path,
                schema_path=self.schema_directory / "finding-verification.schema.json",
                expected_source_id=source.source_id,
                expected_attempt=attempt,
                expected_finding_ids={finding.finding_id for finding in findings},
                page_count=source.main_asset.pdf_metadata.page_count,
            )
            self.state.advance_job(
                job_identifier,
                "succeeded",
                run_identifier=run_identifier,
                source_id=source.source_id,
                job_kind="finding_verification",
                attempt=attempt,
                receipt={
                    "kind": "finding_verification_applied",
                    "decisions": [decision.payload for decision in result.decisions],
                    "wip_mutated": False,
                },
            )
        except (AuditValidationError, KeyError, ValueError) as error:
            job = self.state.job_record(job_identifier)
            if job is not None and job["state"] != "validation_failed":
                self.state.advance_job(
                    job_identifier,
                    "validation_failed",
                    run_identifier=run_identifier,
                    source_id=source.source_id,
                    job_kind="finding_verification",
                    attempt=attempt,
                    receipt={
                        "kind": "finding_verification_validation_failed",
                        "error": str(error),
                        "wip_mutated": False,
                    },
                )
            raise
        return result, job_identifier

    def _finding_verification_workspace(
        self,
        source: RegisteredSource,
        *,
        run_identifier: str,
        attempt: int,
        findings: tuple[AuditFinding, ...],
    ) -> Workspace:
        records = self.state.latest_wip_records(source.source_id)
        dossier = dossier_from_wip(
            source_id=source.source_id,
            title=_source_title(source),
            source_summary=_source_summary(records),
            records=records,
        )
        task = {
            "schema_version": "1.0",
            "run_id": run_identifier,
            "source_id": source.source_id,
            "stage": "finding_verification",
            "attempt": attempt,
            "proposed_findings": [finding.payload for finding in findings],
            "network_access": False,
        }
        inputs = (
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
                "finding-verification.md",
                "finding_verification_protocol",
                content=(self.protocol_directory / "finding-verification.md").read_bytes(),
            ),
            WorkspaceInput(
                source.source_id,
                "wip.json",
                "validated_wip",
                content=canonical_json_bytes(dossier.to_dict()),
            ),
            WorkspaceInput(
                source.source_id,
                "finding-verification.schema.json",
                "output_schema",
                area="templates",
                source_path=self.schema_directory / "finding-verification.schema.json",
            ),
        )
        return WorkspaceBuilder(
            self.cache_root / "runs" / run_identifier,
            run_id=run_identifier,
            source=source,
        ).finding_verification(attempt, prior_state_inputs=inputs)

    def _execute_finding_verification_job(
        self,
        *,
        source: RegisteredSource,
        run_identifier: str,
        attempt: int,
        model: str,
        workspace: Workspace,
        findings: tuple[AuditFinding, ...],
    ) -> tuple[str, Path]:
        job_kind = "finding_verification"
        job_identifier = job_id(run_identifier, job_kind, attempt)
        finding_ids = [finding.finding_id for finding in findings]
        self.state.advance_job(
            job_identifier,
            "pending",
            run_identifier=run_identifier,
            source_id=source.source_id,
            job_kind=job_kind,
            attempt=attempt,
            receipt={
                "kind": "job_created",
                "job_kind": job_kind,
                "finding_ids": finding_ids,
                "wip_mutation_permitted": False,
            },
        )
        self.state.advance_job(
            job_identifier,
            "running",
            run_identifier=run_identifier,
            source_id=source.source_id,
            job_kind=job_kind,
            attempt=attempt,
            receipt={
                "kind": "worker_started",
                "model": model,
                "finding_ids": finding_ids,
                "wip_mutation_permitted": False,
            },
        )
        output_path = workspace.path / "output" / f"{job_identifier}.json"
        receipt_path = (
            self.cache_root / "runs" / run_identifier / "receipts" / f"{job_identifier}.json"
        )
        run_result = CodexRunner(self.codex_executable).run(
            CodexJob(
                job_id=job_identifier,
                source_id=source.source_id,
                model=model,
                prompt=self._finding_verification_prompt(source, attempt),
                workspace=workspace.path,
                output_schema=(workspace.path / "templates" / "finding-verification.schema.json"),
                events_path=(
                    self.cache_root / "runs" / run_identifier / "events" / f"{job_identifier}.jsonl"
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
                run_identifier=run_identifier,
                source_id=source.source_id,
                job_kind=job_kind,
                attempt=attempt,
                receipt={
                    "kind": "worker_failed",
                    "receipt_path": str(receipt_path),
                    "finding_ids": finding_ids,
                    "wip_mutated": False,
                },
            )
            raise WorkerExecutionError(
                f"Codex job {job_identifier} exited with status {run_result.returncode}"
            )
        if not run_result.output_valid:
            self.state.advance_job(
                job_identifier,
                "validation_failed",
                run_identifier=run_identifier,
                source_id=source.source_id,
                job_kind=job_kind,
                attempt=attempt,
                receipt={
                    "kind": "finding_verification_validation_failed",
                    "error": "; ".join(run_result.validation_errors),
                    "finding_ids": finding_ids,
                    "wip_mutated": False,
                },
            )
            raise AuditValidationError(
                f"Codex finding-verification output failed schema validation: {job_identifier}"
            )
        return job_identifier, output_path

    def _reopen_scopes(
        self,
        source_id: str,
        job_identifier: str,
        attempt: int,
        findings: tuple[AuditFinding, ...],
    ) -> None:
        reopen_ids = sorted(
            {scope_id for finding in findings for scope_id in finding.reopen_scope_ids}
        )
        if not reopen_ids:
            raise AuditValidationError("audit finding has no mapped reopen target")
        coverage_by_id = {str(row["scope_id"]): row for row in self.state.coverage_for(source_id)}
        for scope_id in reopen_ids:
            row = coverage_by_id[scope_id]
            details = dict(row["details"])
            details["reopened_by_audit_attempt"] = attempt
            self.state.upsert_coverage(
                source_id,
                scope_id=scope_id,
                scope_kind="reading_scope",
                disposition="reopened",
                details=details,
                job_identifier=job_identifier,
            )
        add_landmark_ids = sorted(
            {landmark_id for finding in findings for landmark_id in finding.add_for_landmark_ids}
        )
        for landmark_id in add_landmark_ids:
            coverage_id = f"landmark:{landmark_id}"
            landmark_row = coverage_by_id.get(coverage_id)
            if landmark_row is not None and landmark_row["scope_kind"] != "landmark":
                raise AuditValidationError(f"audit landmark ID collides: {landmark_id}")
            grounding_findings = [
                finding for finding in findings if landmark_id in finding.add_for_landmark_ids
            ]
            grounding_scopes = sorted(
                {
                    scope_id
                    for finding in grounding_findings
                    for scope_id in finding.reopen_scope_ids
                }
            )
            if len(grounding_scopes) != 1:
                raise AuditValidationError(
                    f"audit landmark addition must map to one reading batch: {landmark_id}"
                )
            batch_row = coverage_by_id.get(grounding_scopes[0])
            if batch_row is None or batch_row["scope_kind"] != "reading_scope":
                raise AuditValidationError(
                    f"audit landmark addition has no reading batch: {landmark_id}"
                )
            if landmark_row is None:
                grounding = grounding_findings[0]
                batch_details = dict(batch_row["details"])
                details = {
                    "landmark_id": landmark_id,
                    "label": str(grounding.payload["description"]),
                    "kind": "audit-discovered",
                    "page_start": int(batch_details["page_start"]),
                    "page_end": int(batch_details["page_end"]),
                    "rationale": str(grounding.payload["evidence"]),
                    "thread_hints": [],
                    "batch_id": grounding_scopes[0],
                    "introduced_by_finding_id": grounding.finding_id,
                }
            else:
                details = dict(landmark_row["details"])
            details["reopened_by_audit_attempt"] = attempt
            self.state.upsert_coverage(
                source_id,
                scope_id=coverage_id,
                scope_kind="landmark",
                disposition="reopened",
                details=details,
                job_identifier=job_identifier,
            )

    def _rerun_reopened_reading(self, source_id: str, model: str) -> None:
        ReadingCoordinator(
            repository_root=self.repository_root,
            state_path=self.state_path,
            cache_root=self.cache_root,
            codex_executable=self.codex_executable,
        ).run_through_source_complete(source_id, model=model)

    def _recover_rejected_audit_correction(self, source_id: str) -> None:
        if self.state.source_state(source_id) != "reading":
            return
        if not self.state.findings_for(source_id, status="open"):
            return
        transitions = self.state.transitions_for("source", source_id)
        latest_reading = next(
            (
                row
                for row in reversed(transitions)
                if row["to_state"] == "reading" and bool(row["applied"])
            ),
            None,
        )
        if latest_reading is None or latest_reading["job_id"] is None:
            return
        job = self.state.job_record(str(latest_reading["job_id"]))
        failed_corrections = {"failed", "validation_failed"}
        if job is not None and (
            not str(job["kind"]).startswith("reading:") or job["state"] not in failed_corrections
        ):
            run_identifier = latest_reading["run_id"]
            candidates = (
                ()
                if run_identifier is None
                else tuple(
                    candidate
                    for candidate in self.state.jobs_for_run(str(run_identifier))
                    if str(candidate["kind"]).startswith("reading:")
                    and candidate["state"] in failed_corrections
                )
            )
            job = candidates[-1] if candidates else None
        if (
            job is None
            or not str(job["kind"]).startswith("reading:")
            or job["state"] not in failed_corrections
        ):
            return
        self.state.advance_source(
            source_id,
            "audit_failed",
            run_identifier=str(job["run_id"]),
            job_identifier=str(job["job_id"]),
            receipt={
                "kind": "recovered_rejected_audit_correction",
                "prior_job_state": str(job["state"]),
                "wip_mutated": False,
                "retryable": True,
            },
        )

    def _run_identifier(self, source_id: str, model: str) -> str:
        digest = hashlib.sha256()
        digest.update(AUDIT_COORDINATOR_VERSION.encode())
        for path in (
            self.protocol_directory / "AGENTS.md",
            self.protocol_directory / "audit.md",
            self.protocol_directory / "finding-verification.md",
            self.schema_directory / "audit-result.schema.json",
            self.schema_directory / "finding-verification.schema.json",
        ):
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
        return run_id(source_id, f"audit_passed:{model}:{digest.hexdigest()}")

    def _inspection_expectations(self, source_id: str) -> dict[str, set[str]]:
        return audit_inspection_expectations(
            self.state.latest_wip_records(source_id),
            self.state.coverage_for(source_id),
        )

    def _audit_prompt(
        self,
        source: RegisteredSource,
        attempt: int,
        *,
        reviewed_continuation: dict[str, Any] | None = None,
    ) -> str:
        reviewed_context = ""
        attempt_label = f"{attempt} of {MAX_AUDIT_ATTEMPTS}"
        if reviewed_continuation is not None:
            attempt_label = f"{attempt} (single reviewed verification)"
            reviewed_context = f"""

This is the one permitted fresh verification after an explicit reviewed continuation.
Independently inspect the complete revised WIP against the immutable paper. Do not accept
the earlier finding or correction blindly. Return reviewed_continuation exactly as the
canonical metadata below. Any finding must use a new finding ID unique to this audit.
No finding from this audit will be automatically reopened.

Reviewed continuation metadata (canonical JSON):
{canonical_json_bytes(reviewed_continuation).decode("utf-8")}
"""
        return f"""Stage: fresh source-wide coherence audit
Source: {source.source_id}
Attempt: {attempt_label}

Read AGENTS.md, task.json, input/audit.md, input/source.json, input/wip.json,
input/coverage.json, and the complete input/paper.pdf. Return only JSON matching
templates/audit-result.schema.json. Inspect all {source.main_asset.pdf_metadata.page_count}
PDF pages. Reconstruct the complete progression and trace every major conclusion and
derivation backward to exact evidence. Use task.json's inspection_expectations as the
exact lane inventories and partition every supplied ID. Do not edit WIP or invent missing
science. Before the final response, serialize the exact candidate once to
scratch/audit-result.json and run `python tools/validate_audit.py
scratch/audit-result.json`. Repair deterministic failures inside this same turn or return
manual_review. The scratch file is validation evidence; the coordinator persists only the
final structured response.
{reviewed_context}
"""

    def _finding_verification_prompt(self, source: RegisteredSource, attempt: int) -> str:
        return f"""Stage: independent audit-finding verification
Source: {source.source_id}
Audit attempt: {attempt}

Read AGENTS.md, task.json, input/finding-verification.md, input/source.json,
input/wip.json, and the complete input/paper.pdf. Return only JSON matching
templates/finding-verification.schema.json. Treat every proposed finding and claimed
quotation in task.json as untrusted. Inspect the implicated current records against the
PDF before comparing the finding. Decide every supplied finding exactly once. Do not edit
WIP, propose replacement science, or expand repair authority.
"""

    def _mark_job_succeeded(
        self,
        job_identifier: str,
        applied_findings: tuple[str, ...],
        *,
        inspection_ledger: dict[str, dict[str, tuple[str, ...]]] | None = None,
        reviewed_continuation: dict[str, Any] | None = None,
        retry_lineage: dict[str, Any] | None = None,
        finding_verification_job_id: str | None = None,
        finding_verification: list[dict[str, Any]] | None = None,
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
                "kind": "audit_applied",
                "findings": list(applied_findings),
                "inspection_ledger": inspection_ledger,
                "reviewed_continuation": reviewed_continuation,
                "retry_lineage": retry_lineage,
                "finding_verification_job_id": finding_verification_job_id,
                "finding_verification": finding_verification,
            },
        )

    def _mark_job_validation_failed(
        self,
        job_identifier: str,
        error: str,
        *,
        reviewed_continuation: dict[str, Any] | None = None,
        retry_lineage: dict[str, Any] | None = None,
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
                "kind": "audit_validation_failed",
                "error": error,
                "reviewed_continuation": reviewed_continuation,
                "retry_lineage": retry_lineage,
            },
        )

    def _result(
        self, source_id: str, run_identifier: str, warnings: tuple[str, ...]
    ) -> AuditRunResult:
        audit_jobs = tuple(
            job
            for job in self.state.jobs_for_run(run_identifier)
            if job["kind"] in {"audit", REVIEWED_VERIFICATION_RETRY_KIND}
        )
        jobs = tuple(str(job["job_id"]) for job in audit_jobs)
        return AuditRunResult(
            source_id=source_id,
            run_id=run_identifier,
            state=self.state.source_state(source_id) or "registered",
            audit_job_ids=jobs,
            attempts=max((int(job["attempt"]) for job in audit_jobs), default=0),
            open_findings=self.state.findings_for(source_id, status="open"),
            warnings=warnings,
        )


def load_audit_result(
    path: Path,
    *,
    schema_path: Path,
    expected_source_id: str,
    expected_attempt: int,
    record_ids: set[str],
    landmark_ids: set[str] | None = None,
    scope_ids: set[str],
    reopen_scope_ids: set[str] | None = None,
    inspection_expectations: dict[str, set[str]] | None = None,
    expected_reviewed_continuation: dict[str, Any] | None = None,
) -> AuditResult:
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
        schema: Any = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AuditValidationError(f"audit result is unreadable: {error}") from error
    errors = sorted(
        Draft202012Validator(schema).iter_errors(payload), key=lambda item: list(item.path)
    )
    if errors:
        location = ".".join(str(item) for item in errors[0].path) or "audit"
        raise AuditValidationError(f"{location}: {errors[0].message}")
    if not isinstance(payload, dict):
        raise AuditValidationError("audit result is not an object")
    if payload["source_id"] != expected_source_id or payload["attempt"] != expected_attempt:
        raise AuditValidationError("audit source or attempt identity does not match")
    reviewed_continuation = payload.get("reviewed_continuation")
    if expected_reviewed_continuation is None:
        if reviewed_continuation is not None:
            raise AuditValidationError("ordinary audit supplied reviewed-continuation metadata")
        if expected_attempt > MAX_AUDIT_ATTEMPTS:
            raise AuditValidationError("audit attempt exceeds the automatic budget")
    elif reviewed_continuation != expected_reviewed_continuation:
        raise AuditValidationError("reviewed-continuation metadata does not match")
    valid_reopen_scope_ids = scope_ids if reopen_scope_ids is None else reopen_scope_ids
    valid_landmark_ids = set() if landmark_ids is None else landmark_ids
    expected_inspections = inspection_expectations or {
        "coverage": set(),
        "fidelity": set(),
        "reasoning": set(record_ids),
    }
    inspection_ledger: dict[str, dict[str, tuple[str, ...]]] = {}
    failed_inspection_ids: set[str] = set()
    for lane_name in ("coverage", "fidelity", "reasoning"):
        lane = payload["inspection_ledger"][lane_name]
        expected = tuple(str(item) for item in lane["expected_ids"])
        passed = tuple(str(item) for item in lane["passed_ids"])
        failed = tuple(str(item) for item in lane["failed_ids"])
        for label, identifiers in (
            ("expected", expected),
            ("passed", passed),
            ("failed", failed),
        ):
            if len(set(identifiers)) != len(identifiers):
                raise AuditValidationError(f"audit {lane_name} lane repeats {label} IDs")
        expected_set = set(expected)
        passed_set = set(passed)
        failed_set = set(failed)
        coordinator_expected = expected_inspections.get(lane_name, set())
        if expected_set != coordinator_expected:
            raise AuditValidationError(
                f"audit {lane_name} expected IDs do not match coordinator inventory; "
                f"missing={sorted(coordinator_expected - expected_set)}; "
                f"unexpected={sorted(expected_set - coordinator_expected)}"
            )
        if overlap := passed_set & failed_set:
            raise AuditValidationError(
                f"audit {lane_name} lane both passes and fails IDs: {sorted(overlap)}"
            )
        if passed_set | failed_set != expected_set:
            raise AuditValidationError(
                f"audit {lane_name} lane does not partition every expected ID"
            )
        failed_inspection_ids.update(failed_set)
        inspection_ledger[lane_name] = {
            "expected_ids": expected,
            "passed_ids": passed,
            "failed_ids": failed,
        }
    findings = tuple(AuditFinding(dict(finding)) for finding in payload["findings"])
    for finding in findings:
        if finding.kind not in AUDIT_FINDING_KINDS:
            raise AuditValidationError(f"unsupported finding kind: {finding.kind}")
        referenced_records = set(finding.payload["record_ids"])
        referenced_landmarks = set(finding.payload["landmark_ids"])
        referenced_scopes = set(finding.payload["scope_ids"])
        if len(referenced_records) != len(finding.payload["record_ids"]):
            raise AuditValidationError(f"finding repeats record endpoints: {finding.finding_id}")
        if len(referenced_scopes) != len(finding.payload["scope_ids"]):
            raise AuditValidationError(f"finding repeats scope endpoints: {finding.finding_id}")
        if len(referenced_landmarks) != len(finding.payload["landmark_ids"]):
            raise AuditValidationError(f"finding repeats landmark endpoints: {finding.finding_id}")
        if len(set(finding.reopen_scope_ids)) != len(finding.reopen_scope_ids):
            raise AuditValidationError(f"finding repeats reopen scopes: {finding.finding_id}")
        if not referenced_records and not referenced_scopes and not referenced_landmarks:
            raise AuditValidationError(
                f"finding has no source-local endpoint: {finding.finding_id}"
            )
        if unknown := referenced_records - record_ids:
            raise AuditValidationError(f"finding references unknown records: {sorted(unknown)}")
        if unknown := referenced_scopes - scope_ids:
            raise AuditValidationError(f"finding references unknown scopes: {sorted(unknown)}")
        if unknown := set(finding.reopen_scope_ids) - valid_reopen_scope_ids:
            raise AuditValidationError(f"finding reopens unknown scopes: {sorted(unknown)}")
        revise_ids = set(finding.revise_record_ids)
        add_ids = set(finding.add_for_landmark_ids)
        if unknown := revise_ids - referenced_records:
            raise AuditValidationError(
                f"finding revises records outside its endpoints: {sorted(unknown)}"
            )
        if unknown := add_ids - referenced_landmarks:
            raise AuditValidationError(
                f"finding adds for landmarks outside its endpoints: {sorted(unknown)}"
            )
        if finding.kind != "missing_landmark" and (
            unknown := referenced_landmarks - valid_landmark_ids
        ):
            raise AuditValidationError(f"finding references unknown landmarks: {sorted(unknown)}")
        if finding.kind == "missing_landmark" and not add_ids:
            raise AuditValidationError("missing-landmark finding has no addition target")
        if finding.kind != "manual_review_required" and not (revise_ids or add_ids):
            raise AuditValidationError("automatic audit finding has no repair target")
        if finding.kind != "manual_review_required" and not finding.reopen_scope_ids:
            raise AuditValidationError("automatic audit finding has no reopen batch")
        if finding.kind == "manual_review_required" and (
            revise_ids or add_ids or finding.reopen_scope_ids
        ):
            raise AuditValidationError("manual-review finding grants automatic repair")
        if finding.kind == "manual_review_required" and not finding.payload["manual_review_reason"]:
            raise AuditValidationError("manual-review finding lacks a reason")
        if uninspected_revisions := revise_ids - failed_inspection_ids:
            raise AuditValidationError(
                "finding revision targets are not failed inspection IDs: "
                f"{sorted(uninspected_revisions)}"
            )
        known_add_ids = add_ids & valid_landmark_ids
        if uninspected_landmarks := known_add_ids - failed_inspection_ids:
            raise AuditValidationError(
                "finding landmark targets are not failed inspection IDs: "
                f"{sorted(uninspected_landmarks)}"
            )
    finding_endpoint_ids = {
        identifier
        for finding in findings
        for identifier in (
            *finding.payload["record_ids"],
            *finding.payload["landmark_ids"],
        )
    }
    if ungrounded_failures := failed_inspection_ids - finding_endpoint_ids:
        raise AuditValidationError(
            f"audit failed inspection IDs lack findings: {sorted(ungrounded_failures)}"
        )
    outcome = str(payload["outcome"])
    checks = {str(key): bool(value) for key, value in payload["checks"].items()}
    if outcome == "pass" and (findings or failed_inspection_ids or not all(checks.values())):
        raise AuditValidationError(
            "audit pass requires complete passing inspection ledgers, every check, and no findings"
        )
    if outcome == "reopen" and (
        not findings or not any(item.reopen_scope_ids for item in findings)
    ):
        raise AuditValidationError("audit reopen requires mapped findings")
    if outcome == "manual_review" and not findings:
        raise AuditValidationError("manual-review outcome requires a finding")
    return AuditResult(
        source_id=expected_source_id,
        attempt=expected_attempt,
        outcome=outcome,
        summary=str(payload["summary"]),
        checks=checks,
        inspection_ledger=inspection_ledger,
        findings=findings,
        warnings=tuple(payload["warnings"]),
        reviewed_continuation=(
            dict(reviewed_continuation) if isinstance(reviewed_continuation, dict) else None
        ),
        payload=payload,
    )


def load_finding_verification_result(
    path: Path,
    *,
    schema_path: Path,
    expected_source_id: str,
    expected_attempt: int,
    expected_finding_ids: set[str],
    page_count: int,
) -> FindingVerificationResult:
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
        schema: Any = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AuditValidationError(f"finding verification is unreadable: {error}") from error
    errors = sorted(
        Draft202012Validator(schema).iter_errors(payload), key=lambda item: list(item.path)
    )
    if errors:
        location = ".".join(str(item) for item in errors[0].path) or "verification"
        raise AuditValidationError(f"{location}: {errors[0].message}")
    if not isinstance(payload, dict):
        raise AuditValidationError("finding verification is not an object")
    if payload["source_id"] != expected_source_id or payload["attempt"] != expected_attempt:
        raise AuditValidationError("finding verification source or attempt does not match")
    decisions = tuple(
        FindingVerificationDecision(dict(decision)) for decision in payload["decisions"]
    )
    decision_ids = [decision.finding_id for decision in decisions]
    if len(set(decision_ids)) != len(decision_ids):
        raise AuditValidationError("finding verification repeats a finding decision")
    if set(decision_ids) != expected_finding_ids:
        raise AuditValidationError(
            "finding verification does not partition every proposed finding; "
            f"missing={sorted(expected_finding_ids - set(decision_ids))}; "
            f"unexpected={sorted(set(decision_ids) - expected_finding_ids)}"
        )
    for decision in decisions:
        manual_reason = decision.payload["manual_review_reason"]
        if decision.verdict == "manual_review":
            if not isinstance(manual_reason, str) or not manual_reason.strip():
                raise AuditValidationError(
                    f"manual finding verification lacks a reason: {decision.finding_id}"
                )
        elif manual_reason is not None:
            raise AuditValidationError(
                f"non-manual finding verification supplies a manual reason: {decision.finding_id}"
            )
        for segment in decision.payload["source_evidence"]:
            page_start = int(segment["page_start"])
            page_end = int(segment["page_end"])
            if page_start > page_end or page_end > page_count:
                raise AuditValidationError(
                    f"finding verification evidence route is invalid: {decision.finding_id}"
                )
    return FindingVerificationResult(
        source_id=expected_source_id,
        attempt=expected_attempt,
        decisions=decisions,
        warnings=tuple(str(item) for item in payload["warnings"]),
        payload=payload,
    )


def audit_inspection_expectations(
    records: tuple[dict[str, Any], ...],
    coverage_rows: tuple[dict[str, Any], ...],
) -> dict[str, set[str]]:
    coverage_ids = {
        str(row["details"]["landmark_id"])
        for row in coverage_rows
        if row["scope_kind"] == "landmark" and row["details"].get("landmark_id")
    }
    evidence_ids = {
        str(record["id"]) for record in records if record.get("record_type") == "evidence"
    }
    fidelity_atom_ids = {
        str(record["id"])
        for record in records
        if record.get("record_type") == "atom" and record.get("fidelity") is not None
    }
    reasoning_ids = {
        str(record["id"])
        for record in records
        if record.get("record_type") in {"atom", "move", "thread"}
    }
    return {
        "coverage": coverage_ids,
        "fidelity": evidence_ids | fidelity_atom_ids,
        "reasoning": reasoning_ids,
    }


def _source_title(source: RegisteredSource) -> str:
    identity = source.metadata.get("identity")
    if isinstance(identity, dict) and isinstance(identity.get("title"), str):
        return str(identity["title"])
    return source.source_id


def _audit_validator_source(repository_root: Path) -> str:
    root = repr(str(repository_root))
    return f"""#!/usr/bin/env python3
import json
import sys
from pathlib import Path

sys.path.insert(0, {root!s} + "/" + "src")
from research_map.audit import load_audit_result

workspace = Path(__file__).resolve().parents[1]
task = json.loads((workspace / "task.json").read_text(encoding="utf-8"))
wip = json.loads((workspace / "input" / "wip.json").read_text(encoding="utf-8"))
coverage = json.loads((workspace / "input" / "coverage.json").read_text(encoding="utf-8"))
proposal = Path(sys.argv[1])
entries = coverage["entries"]
expectations = {{
    lane: set(values) for lane, values in task["inspection_expectations"].items()
}}
try:
    result = load_audit_result(
        proposal,
        schema_path=workspace / "templates" / "audit-result.schema.json",
        expected_source_id=task["source_id"],
        expected_attempt=task["attempt"],
        record_ids={{str(item["id"]) for item in wip["records"]}},
        landmark_ids=expectations["coverage"],
        scope_ids={{str(item["scope_id"]) for item in entries}},
        reopen_scope_ids={{
            str(item["scope_id"])
            for item in entries
            if item["scope_kind"] == "reading_scope"
        }},
        inspection_expectations=expectations,
    )
except Exception as error:
    print(json.dumps({{"ok": False, "error": str(error)}}, sort_keys=True))
    raise SystemExit(1)
print(json.dumps({{
    "ok": True,
    "outcome": result.outcome,
    "finding_ids": [item.finding_id for item in result.findings],
}}, sort_keys=True))
"""


def _source_summary(records: tuple[dict[str, Any], ...]) -> str:
    summaries = [
        str(record["summary"])
        for record in records
        if record.get("record_type") == "thread" and record.get("summary")
    ]
    return " ".join(summaries) or "Complete source-local WIP dossier."


def _preexecution_failure_code(events_path: Path) -> str | None:
    try:
        lines = events_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    completed = False
    failure_codes: list[str] = []
    for line in lines:
        try:
            event: Any = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(event, dict):
            return None
        if event.get("type") == "turn.completed":
            completed = True
        if event.get("type") != "error" or not isinstance(event.get("message"), str):
            continue
        try:
            error_payload: Any = json.loads(str(event["message"]))
        except json.JSONDecodeError:
            continue
        if not isinstance(error_payload, dict):
            continue
        error = error_payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("code"), str):
            failure_codes.append(str(error["code"]))
    if completed or len(failure_codes) != 1:
        return None
    return failure_codes[0]
