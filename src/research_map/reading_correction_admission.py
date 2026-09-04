"""Side-effect-free admission for one audit-authorized reading correction."""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from research_map.graph import compile_graph
from research_map.markdown import parse_markdown
from research_map.proposals import (
    ManualReviewRequired,
    ProposalValidationError,
    ReadingProposal,
    dossier_from_wip,
    load_reading_proposal,
)
from research_map.records import Record, RecordError
from research_map.render import render_canonical_dossier
from research_map.validation import ValidationReport, validate_dossier


@dataclass(frozen=True, slots=True)
class CorrectionAdmissionFinding:
    """One stable, record-addressable correction admission outcome."""

    code: str
    disposition: str
    subject_id: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "disposition": self.disposition,
            "subject_id": self.subject_id,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class CorrectionContract:
    """Explicit source-local inputs that bound an audit correction."""

    source_id: str
    scope_id: str
    landmark_ids: tuple[str, ...]
    asset_sha256: str
    page_count: int
    allowed_record_ids: tuple[str, ...]
    allowed_new_landmark_ids: tuple[str, ...]
    findings: tuple[dict[str, Any], ...]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> CorrectionContract:
        try:
            page_count = value["page_count"]
            if not isinstance(page_count, int) or isinstance(page_count, bool) or page_count < 1:
                raise ValueError("page_count must be a positive integer")
            findings = value["findings"]
            if not isinstance(findings, list) or not all(
                isinstance(item, dict) for item in findings
            ):
                raise ValueError("findings must be a list of objects")
            return cls(
                source_id=_required_string(value, "source_id"),
                scope_id=_required_string(value, "scope_id"),
                landmark_ids=_string_tuple(value, "landmark_ids"),
                asset_sha256=_required_string(value, "asset_sha256"),
                page_count=page_count,
                allowed_record_ids=_string_tuple(value, "allowed_record_ids"),
                allowed_new_landmark_ids=_string_tuple(value, "allowed_new_landmark_ids"),
                findings=tuple(copy.deepcopy(findings)),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ProposalValidationError(f"invalid correction contract: {error}") from error


@dataclass(frozen=True, slots=True)
class CorrectionAdmissionResult:
    """One complete deterministic decision over current WIP plus correction bytes."""

    source_id: str
    scope_id: str
    proposal: ReadingProposal | None
    candidate_records: tuple[dict[str, Any], ...]
    applied_revisions: tuple[str, ...]
    findings: tuple[CorrectionAdmissionFinding, ...]
    dossier_validation: ValidationReport | None
    unexpected_record_ids: tuple[str, ...] = ()
    graph_record_count: int | None = None
    graph_edge_count: int | None = None

    @property
    def failed_findings(self) -> tuple[CorrectionAdmissionFinding, ...]:
        return tuple(finding for finding in self.findings if finding.disposition == "failed")

    @property
    def ok(self) -> bool:
        return (
            not self.failed_findings
            and self.proposal is not None
            and self.dossier_validation is not None
            and self.dossier_validation.ok
            and self.graph_record_count is not None
        )

    @property
    def manual_review_required(self) -> bool:
        return any(finding.code == "manual_review_required" for finding in self.failed_findings)

    def failure_message(self) -> str:
        return "; ".join(
            f"{finding.code} [{finding.subject_id}]: {finding.message}"
            for finding in self.failed_findings
        )

    def to_dict(self) -> dict[str, Any]:
        proposed_ids = (
            ()
            if self.proposal is None
            else tuple(sorted(record.id for record in self.proposal.records))
        )
        return {
            "ok": self.ok,
            "source_id": self.source_id,
            "scope_id": self.scope_id,
            "record_ids": list(proposed_ids),
            "applied_revisions": list(self.applied_revisions),
            "unexpected_record_ids": list(self.unexpected_record_ids),
            "failure_message": self.failure_message(),
            "findings": [finding.to_dict() for finding in self.findings],
            "admission": {
                "candidate_record_count": len(self.candidate_records),
                "dossier_valid": bool(
                    self.dossier_validation is not None and self.dossier_validation.ok
                ),
                "canonical_round_trip_valid": self.graph_record_count is not None,
                "graph_compilation_valid": self.graph_record_count is not None,
                "graph_record_count": self.graph_record_count,
                "graph_edge_count": self.graph_edge_count,
            },
        }


def validate_reading_correction_file(
    proposal_path: Path,
    *,
    current_wip: Mapping[str, Any],
    correction_contract: Mapping[str, Any],
    schema_directory: Path,
) -> CorrectionAdmissionResult:
    """Validate the exact merged source dossier a correction would persist."""

    try:
        contract = CorrectionContract.from_mapping(correction_contract)
    except ProposalValidationError as error:
        return _failed_result(
            source_id=_optional_string(correction_contract.get("source_id"), "unknown-source"),
            scope_id=_optional_string(correction_contract.get("scope_id"), "unknown-scope"),
            code="correction_contract_invalid",
            subject_id="correction-contract",
            message=str(error),
        )

    try:
        title, summary, current_records = _current_wip_inputs(current_wip, contract)
        proposal = load_reading_proposal(
            proposal_path,
            schema_directory=schema_directory,
            expected_source_id=contract.source_id,
            expected_scope_id=contract.scope_id,
            expected_landmark_ids=contract.landmark_ids,
            expected_asset_sha256=contract.asset_sha256,
            page_count=contract.page_count,
        )
    except ManualReviewRequired as error:
        return _failed_result(
            source_id=contract.source_id,
            scope_id=contract.scope_id,
            code="manual_review_required",
            subject_id=contract.source_id,
            message=str(error),
        )
    except (ProposalValidationError, RecordError, TypeError, ValueError) as error:
        return _failed_result(
            source_id=contract.source_id,
            scope_id=contract.scope_id,
            code="proposal_invalid",
            subject_id=_subject_id(str(error), contract.source_id),
            message=str(error),
        )

    try:
        candidate_records, applied_revisions = overlay_wip_records(
            current_records,
            proposal.records,
            expected_source_id=contract.source_id,
        )
        _validate_correction_boundary(
            proposal,
            current_records=current_records,
            candidate_records=candidate_records,
            applied_revisions=applied_revisions,
            contract=contract,
        )
    except _CorrectionBoundaryError as error:
        return _failed_result(
            source_id=contract.source_id,
            scope_id=contract.scope_id,
            code="correction_boundary_failed",
            subject_id=_subject_id(str(error), contract.source_id),
            message=str(error),
            proposal=proposal,
            unexpected_record_ids=error.unexpected_record_ids,
        )
    except (ProposalValidationError, RecordError, TypeError, ValueError) as error:
        return _failed_result(
            source_id=contract.source_id,
            scope_id=contract.scope_id,
            code="correction_boundary_failed",
            subject_id=_subject_id(str(error), contract.source_id),
            message=str(error),
            proposal=proposal,
        )

    findings = [
        CorrectionAdmissionFinding(
            code="candidate_overlay_valid",
            disposition="passed",
            subject_id=contract.source_id,
            message="correction overlay matches active-WIP revision and replacement semantics",
        )
    ]
    try:
        dossier = dossier_from_wip(
            source_id=contract.source_id,
            title=title,
            source_summary=summary,
            records=candidate_records,
        )
    except (ProposalValidationError, RecordError, TypeError, ValueError) as error:
        findings.append(
            CorrectionAdmissionFinding(
                code="record_adaptation_failed",
                disposition="failed",
                subject_id=_subject_id(str(error), contract.source_id),
                message=str(error),
            )
        )
        return _result(
            contract,
            proposal,
            candidate_records,
            applied_revisions,
            findings,
        )

    dossier_validation = validate_dossier(
        dossier,
        schema_directory=schema_directory,
        expected_asset_sha256=contract.asset_sha256,
        page_count=contract.page_count,
    )
    if not dossier_validation.ok:
        findings.extend(
            CorrectionAdmissionFinding(
                code="dossier_invariant_failed",
                disposition="failed",
                subject_id=_subject_id(error, contract.source_id),
                message=error,
            )
            for error in dossier_validation.errors
        )
        return _result(
            contract,
            proposal,
            candidate_records,
            applied_revisions,
            findings,
            dossier_validation=dossier_validation,
        )

    findings.append(
        CorrectionAdmissionFinding(
            code="dossier_valid",
            disposition="passed",
            subject_id=contract.source_id,
            message="merged source-local dossier satisfies deterministic invariants",
        )
    )
    try:
        canonical = render_canonical_dossier(dossier)
        reparsed = parse_markdown(canonical.decode("utf-8")).dossier
        compilation = compile_graph(
            reparsed,
            schema_directory=schema_directory,
            expected_asset_sha256=contract.asset_sha256,
            page_count=contract.page_count,
        )
        rebuilt_dossier = parse_markdown(render_canonical_dossier(reparsed).decode("utf-8")).dossier
        rebuilt = compile_graph(
            rebuilt_dossier,
            schema_directory=schema_directory,
            expected_asset_sha256=contract.asset_sha256,
            page_count=contract.page_count,
        )
        if (
            compilation.graph_bytes() != rebuilt.graph_bytes()
            or compilation.context_jsonl_bytes() != rebuilt.context_jsonl_bytes()
        ):
            raise ValueError("canonical graph rebuild is not byte-equivalent")
    except (KeyError, OSError, TypeError, ValueError) as error:
        findings.append(
            CorrectionAdmissionFinding(
                code="projection_compilation_failed",
                disposition="failed",
                subject_id=_subject_id(str(error), contract.source_id),
                message=str(error),
            )
        )
        return _result(
            contract,
            proposal,
            candidate_records,
            applied_revisions,
            findings,
            dossier_validation=dossier_validation,
        )

    findings.append(
        CorrectionAdmissionFinding(
            code="projection_compilation_valid",
            disposition="passed",
            subject_id=contract.source_id,
            message="canonical Markdown round-trip and source graph compilation are valid",
        )
    )
    return _result(
        contract,
        proposal,
        candidate_records,
        applied_revisions,
        findings,
        dossier_validation=dossier_validation,
        graph_record_count=len(compilation.graph["nodes"]),
        graph_edge_count=len(compilation.graph["edges"]),
    )


def overlay_wip_records(
    current_records: Sequence[Mapping[str, Any]],
    proposal_records: Sequence[Record],
    *,
    expected_source_id: str,
) -> tuple[tuple[dict[str, Any], ...], tuple[str, ...]]:
    """Return active WIP after applying records with persistent-state semantics."""

    active: dict[str, Record] = {}
    for value in current_records:
        record = Record.from_mapping(value)
        if record.source_id != expected_source_id:
            raise ProposalValidationError(
                f"current WIP record does not belong to source: {record.id}"
            )
        if record.id in active:
            raise ProposalValidationError(f"current WIP repeats active record ID: {record.id}")
        active[record.id] = record

    applied: list[str] = []
    for record in proposal_records:
        if record.source_id != expected_source_id:
            raise ProposalValidationError(
                f"correction record does not belong to source: {record.id}"
            )
        existing = active.get(record.id)
        if existing is not None and record.revision == existing.revision:
            if record.to_dict() != existing.to_dict():
                raise ProposalValidationError(
                    f"WIP revision identity does not match: {record.id}@{record.revision}"
                )
            continue
        expected_revision = 1 if existing is None else existing.revision + 1
        if record.revision != expected_revision:
            raise ProposalValidationError(
                "proposal revision is not sequential: "
                f"{record.id}@{record.revision}; expected={expected_revision}"
            )
        active[record.id] = Record.from_mapping(record.to_dict())
        applied.append(f"{record.id}@{record.revision}")

    return (
        tuple(active[record_id].to_dict() for record_id in sorted(active)),
        tuple(applied),
    )


def _current_wip_inputs(
    current_wip: Mapping[str, Any], contract: CorrectionContract
) -> tuple[str, str, tuple[dict[str, Any], ...]]:
    if current_wip.get("schema_version") != 1:
        raise ProposalValidationError("current WIP schema_version must be 1")
    if current_wip.get("source_id") != contract.source_id:
        raise ProposalValidationError("current WIP source identity does not match contract")
    title = _required_string(current_wip, "title")
    summary = _required_string(current_wip, "summary")
    raw_records = current_wip.get("records")
    if not isinstance(raw_records, list) or not all(isinstance(item, dict) for item in raw_records):
        raise ProposalValidationError("current WIP records must be a list of objects")
    return title, summary, tuple(copy.deepcopy(raw_records))


def _validate_correction_boundary(
    proposal: ReadingProposal,
    *,
    current_records: Sequence[Mapping[str, Any]],
    candidate_records: Sequence[Mapping[str, Any]],
    applied_revisions: tuple[str, ...],
    contract: CorrectionContract,
) -> None:
    if proposal.reopen_scope_ids:
        raise ProposalValidationError(
            "audit-authorized correction cannot reopen another reading scope"
        )
    if not proposal.records:
        raise ProposalValidationError("reopened scope proposal contains no repairs")

    existing_ids = {str(record["id"]) for record in current_records}
    proposed_ids = {record.id for record in proposal.records}
    revised_ids = proposed_ids & existing_ids
    new_ids = proposed_ids - existing_ids
    unexpected_revisions = revised_ids - set(contract.allowed_record_ids)
    if unexpected_revisions:
        raise _CorrectionBoundaryError(
            "reopened scope proposal revises records outside its repair boundary: "
            f"{sorted(unexpected_revisions)}",
            unexpected_record_ids=tuple(sorted(unexpected_revisions)),
        )

    disposition_by_id = {
        str(disposition["landmark_id"]): disposition
        for disposition in proposal.landmark_dispositions
    }
    authorized_new_ids = {
        str(record_id)
        for landmark_id in contract.allowed_new_landmark_ids
        for record_id in disposition_by_id.get(landmark_id, {}).get("record_ids", [])
    }
    unexpected_new_ids = new_ids - authorized_new_ids
    if unexpected_new_ids:
        raise _CorrectionBoundaryError(
            "reopened scope proposal adds records outside its landmark repair boundary: "
            f"{sorted(unexpected_new_ids)}",
            unexpected_record_ids=tuple(sorted(unexpected_new_ids)),
        )

    available_ids = {str(record["id"]) for record in candidate_records}
    for landmark_id, disposition in disposition_by_id.items():
        unknown = {str(item) for item in disposition["record_ids"]} - available_ids
        if unknown:
            raise ProposalValidationError(
                f"landmark disposition {landmark_id} references unavailable records: "
                f"{sorted(unknown)}"
            )

    for finding in contract.findings:
        repair = finding.get("repair")
        if not isinstance(repair, dict):
            raise ProposalValidationError(
                f"mapped finding has no repair contract: {finding.get('finding_id')}"
            )
        revise_targets = {str(item) for item in repair.get("revise_record_ids", [])}
        add_targets = {str(item) for item in repair.get("add_for_landmark_ids", [])}
        revisions_addressed = not revise_targets or not proposed_ids.isdisjoint(revise_targets)
        additions_addressed = not add_targets or all(
            new_ids
            & {str(item) for item in disposition_by_id.get(landmark_id, {}).get("record_ids", [])}
            for landmark_id in add_targets
        )
        if not revisions_addressed or not additions_addressed:
            raise ProposalValidationError(
                "reopened scope proposal does not address mapped finding "
                f"{finding.get('finding_id')}"
            )

    if not applied_revisions:
        raise ProposalValidationError(
            f"reopened scope applied no new revision: {contract.scope_id}"
        )


def _result(
    contract: CorrectionContract,
    proposal: ReadingProposal,
    candidate_records: tuple[dict[str, Any], ...],
    applied_revisions: tuple[str, ...],
    findings: Sequence[CorrectionAdmissionFinding],
    *,
    dossier_validation: ValidationReport | None = None,
    graph_record_count: int | None = None,
    graph_edge_count: int | None = None,
) -> CorrectionAdmissionResult:
    return CorrectionAdmissionResult(
        source_id=contract.source_id,
        scope_id=contract.scope_id,
        proposal=proposal,
        candidate_records=tuple(copy.deepcopy(candidate_records)),
        applied_revisions=applied_revisions,
        findings=tuple(findings),
        dossier_validation=dossier_validation,
        graph_record_count=graph_record_count,
        graph_edge_count=graph_edge_count,
    )


def _failed_result(
    *,
    source_id: str,
    scope_id: str,
    code: str,
    subject_id: str,
    message: str,
    proposal: ReadingProposal | None = None,
    unexpected_record_ids: tuple[str, ...] = (),
) -> CorrectionAdmissionResult:
    return CorrectionAdmissionResult(
        source_id=source_id,
        scope_id=scope_id,
        proposal=proposal,
        candidate_records=(),
        applied_revisions=(),
        findings=(
            CorrectionAdmissionFinding(
                code=code,
                disposition="failed",
                subject_id=subject_id,
                message=message,
            ),
        ),
        dossier_validation=None,
        unexpected_record_ids=unexpected_record_ids,
    )


class _CorrectionBoundaryError(ProposalValidationError):
    def __init__(self, message: str, *, unexpected_record_ids: tuple[str, ...]) -> None:
        super().__init__(message)
        self.unexpected_record_ids = unexpected_record_ids


def _required_string(value: Mapping[str, Any], key: str) -> str:
    item = value[key]
    if not isinstance(item, str) or not item:
        raise ValueError(f"{key} must be a non-empty string")
    return item


def _optional_string(value: object, fallback: str) -> str:
    return value if isinstance(value, str) and value else fallback


def _string_tuple(value: Mapping[str, Any], key: str) -> tuple[str, ...]:
    items = value[key]
    if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
        raise ValueError(f"{key} must be a list of strings")
    return tuple(items)


def _subject_id(error: str, fallback: str) -> str:
    subject, separator, _ = error.partition(": ")
    return subject if separator and subject else fallback
