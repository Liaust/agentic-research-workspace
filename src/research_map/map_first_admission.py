"""Side-effect-free admission for one map-first source proposal."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from research_map.graph import compile_graph
from research_map.hierarchical import (
    HierarchicalFinding,
    HierarchicalValidationReport,
    adapted_hierarchical_records,
    administrative_type_gap_annotations,
    validate_hierarchical_proposal,
)
from research_map.markdown import parse_markdown
from research_map.proposals import ProposalValidationError, dossier_from_wip
from research_map.records import Record, RecordError
from research_map.render import render_canonical_dossier
from research_map.validation import ValidationReport, validate_dossier


@dataclass(frozen=True, slots=True)
class MapFirstAdmissionResult:
    """One complete deterministic decision over authoritative proposal bytes."""

    proposal: dict[str, Any] | None
    records: tuple[Record, ...]
    report: HierarchicalValidationReport
    dossier_validation: ValidationReport | None
    calibration_tolerant: bool
    manual_review_reasons: tuple[str, ...]
    type_gap_annotations: tuple[dict[str, str], ...]
    graph_record_count: int | None = None
    graph_edge_count: int | None = None

    @property
    def ok(self) -> bool:
        return (
            self.report.ok
            and self.dossier_validation is not None
            and self.dossier_validation.ok
            and self.graph_record_count is not None
        )

    @property
    def failed_findings(self) -> tuple[HierarchicalFinding, ...]:
        return tuple(finding for finding in self.report.findings if finding.disposition == "failed")

    def failure_message(self) -> str:
        return "; ".join(
            f"{finding.code} [{finding.subject_id}]: {finding.message}"
            for finding in self.failed_findings
        )

    def to_dict(self) -> dict[str, Any]:
        payload = self.report.to_dict()
        payload["admission"] = {
            "calibration_tolerant": self.calibration_tolerant,
            "manual_review_retained": bool(
                self.proposal is not None
                and self.proposal.get("disposition") == "manual_review"
                and self.calibration_tolerant
            ),
            "manual_review_reasons": list(self.manual_review_reasons),
            "administrative_type_gap_annotations": list(self.type_gap_annotations),
            "dossier_valid": bool(
                self.dossier_validation is not None and self.dossier_validation.ok
            ),
            "canonical_round_trip_valid": self.graph_record_count is not None,
            "graph_compilation_valid": self.graph_record_count is not None,
            "graph_record_count": self.graph_record_count,
            "graph_edge_count": self.graph_edge_count,
        }
        return payload


def validate_map_first_proposal_file(
    proposal_path: Path,
    *,
    proposal_schema_path: Path,
    schema_directory: Path,
    expected_source_id: str,
    source_title: str,
    expected_asset_sha256: str,
    page_count: int,
    page_text_by_page: Mapping[int, str | None],
    calibration_tolerant: bool,
) -> MapFirstAdmissionResult:
    """Validate all deterministic stages required for source graph admission."""

    try:
        raw: Any = json.loads(proposal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return _failed_result(
            calibration_tolerant=calibration_tolerant,
            code="proposal_unreadable",
            subject_id=expected_source_id,
            message=str(error),
        )
    if not isinstance(raw, dict):
        return _failed_result(
            calibration_tolerant=calibration_tolerant,
            code="proposal_not_object",
            subject_id=expected_source_id,
            message="map-first proposal must be a JSON object",
        )
    proposal = raw
    hierarchy = validate_hierarchical_proposal(
        proposal,
        schema_path=proposal_schema_path,
        expected_source_id=expected_source_id,
        expected_asset_sha256=expected_asset_sha256,
        page_count=page_count,
        page_text_by_page=page_text_by_page,
    )
    manual_review_reasons = tuple(str(item) for item in proposal.get("manual_review_reasons", []))
    if not hierarchy.ok:
        return MapFirstAdmissionResult(
            proposal=proposal,
            records=(),
            report=hierarchy,
            dossier_validation=None,
            calibration_tolerant=calibration_tolerant,
            manual_review_reasons=manual_review_reasons,
            type_gap_annotations=(),
        )

    findings = list(hierarchy.findings)
    if proposal.get("disposition") == "manual_review" and not calibration_tolerant:
        findings.append(
            HierarchicalFinding(
                code="manual_review_required",
                disposition="failed",
                subject_id=expected_source_id,
                message="; ".join(manual_review_reasons) or "worker requested manual review",
            )
        )
        return MapFirstAdmissionResult(
            proposal=proposal,
            records=(),
            report=HierarchicalValidationReport(tuple(findings)),
            dossier_validation=None,
            calibration_tolerant=calibration_tolerant,
            manual_review_reasons=manual_review_reasons,
            type_gap_annotations=(),
        )

    type_gap_annotations = (
        administrative_type_gap_annotations(proposal) if calibration_tolerant else ()
    )
    try:
        records = adapted_hierarchical_records(
            proposal,
            annotate_missing_type_gaps=calibration_tolerant,
        )
        dossier = dossier_from_wip(
            source_id=expected_source_id,
            title=source_title,
            source_summary=str(proposal["paper_summary"]),
            records=tuple(record.to_dict() for record in records),
        )
    except (KeyError, ProposalValidationError, RecordError, TypeError, ValueError) as error:
        findings.append(
            HierarchicalFinding(
                code="record_adaptation_failed",
                disposition="failed",
                subject_id=_subject_id(str(error), expected_source_id),
                message=str(error),
            )
        )
        return MapFirstAdmissionResult(
            proposal=proposal,
            records=(),
            report=HierarchicalValidationReport(tuple(findings)),
            dossier_validation=None,
            calibration_tolerant=calibration_tolerant,
            manual_review_reasons=manual_review_reasons,
            type_gap_annotations=type_gap_annotations,
        )

    dossier_validation = validate_dossier(
        dossier,
        schema_directory=schema_directory,
        expected_asset_sha256=expected_asset_sha256,
        page_count=page_count,
    )
    if not dossier_validation.ok:
        findings.extend(
            HierarchicalFinding(
                code="dossier_invariant_failed",
                disposition="failed",
                subject_id=_subject_id(error, expected_source_id),
                message=error,
            )
            for error in dossier_validation.errors
        )
        return MapFirstAdmissionResult(
            proposal=proposal,
            records=records,
            report=HierarchicalValidationReport(tuple(findings)),
            dossier_validation=dossier_validation,
            calibration_tolerant=calibration_tolerant,
            manual_review_reasons=manual_review_reasons,
            type_gap_annotations=type_gap_annotations,
        )

    findings.append(
        HierarchicalFinding(
            code="dossier_valid",
            disposition="passed",
            subject_id=expected_source_id,
            message="adapted source-local dossier satisfies deterministic invariants",
        )
    )
    try:
        canonical = render_canonical_dossier(dossier)
        reparsed = parse_markdown(canonical.decode("utf-8")).dossier
        compilation = compile_graph(
            reparsed,
            schema_directory=schema_directory,
            expected_asset_sha256=expected_asset_sha256,
            page_count=page_count,
        )
        rebuilt_dossier = parse_markdown(render_canonical_dossier(reparsed).decode("utf-8")).dossier
        rebuilt = compile_graph(
            rebuilt_dossier,
            schema_directory=schema_directory,
            expected_asset_sha256=expected_asset_sha256,
            page_count=page_count,
        )
        if (
            compilation.graph_bytes() != rebuilt.graph_bytes()
            or compilation.context_jsonl_bytes() != rebuilt.context_jsonl_bytes()
        ):
            raise ValueError("canonical graph rebuild is not byte-equivalent")
    except (KeyError, OSError, TypeError, ValueError) as error:
        findings.append(
            HierarchicalFinding(
                code="projection_compilation_failed",
                disposition="failed",
                subject_id=_subject_id(str(error), expected_source_id),
                message=str(error),
            )
        )
        return MapFirstAdmissionResult(
            proposal=proposal,
            records=records,
            report=HierarchicalValidationReport(tuple(findings)),
            dossier_validation=dossier_validation,
            calibration_tolerant=calibration_tolerant,
            manual_review_reasons=manual_review_reasons,
            type_gap_annotations=type_gap_annotations,
        )

    findings.append(
        HierarchicalFinding(
            code="projection_compilation_valid",
            disposition="passed",
            subject_id=expected_source_id,
            message="canonical Markdown round-trip and source graph compilation are valid",
        )
    )
    return MapFirstAdmissionResult(
        proposal=proposal,
        records=records,
        report=HierarchicalValidationReport(tuple(findings)),
        dossier_validation=dossier_validation,
        calibration_tolerant=calibration_tolerant,
        manual_review_reasons=manual_review_reasons,
        type_gap_annotations=type_gap_annotations,
        graph_record_count=len(compilation.graph["nodes"]),
        graph_edge_count=len(compilation.graph["edges"]),
    )


def _failed_result(
    *,
    calibration_tolerant: bool,
    code: str,
    subject_id: str,
    message: str,
) -> MapFirstAdmissionResult:
    return MapFirstAdmissionResult(
        proposal=None,
        records=(),
        report=HierarchicalValidationReport(
            (
                HierarchicalFinding(
                    code=code,
                    disposition="failed",
                    subject_id=subject_id,
                    message=message,
                ),
            )
        ),
        dossier_validation=None,
        calibration_tolerant=calibration_tolerant,
        manual_review_reasons=(),
        type_gap_annotations=(),
    )


def _subject_id(error: str, fallback: str) -> str:
    subject, separator, _ = error.partition(": ")
    return subject if separator and subject else fallback
