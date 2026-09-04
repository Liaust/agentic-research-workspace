"""Mandatory-delta constraints over the page-routed review contract.

The page-routed implementation remains responsible for page closure, exact
source routes, immutable proposal reveal, frozen comparison accounting, patch
application, and complete candidate admission.  This module adds only the
experimental requirement that the reviewer produce a source-grounded,
semantically non-empty delta from at least one frozen retrieval unit.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from research_map.hierarchical_review import (
    COLLECTION_DEFINITIONS,
    build_review_patch_schema,
)
from research_map.page_routed_review import (
    freeze_unit_comparison_file,
    validate_page_routed_candidate_files,
    validate_unit_comparison,
)
from research_map.retrievability_review import CandidateValidationReport, ComparisonFreeze
from research_map.source_first_review import ContractValidation

REPAIRABLE_DISPOSITIONS = frozenset({"partially_retrievable", "missing", "misrepresented"})


class MandatoryDeltaReviewError(ValueError):
    """Raised when a mandatory-delta artifact cannot cross its next gate."""


def build_mandatory_delta_patch_schema(
    proposal_schema: Mapping[str, Any],
) -> dict[str, Any]:
    """Constrain structured output to a patch-proposed result without findings."""

    schema = deepcopy(build_review_patch_schema(proposal_schema))
    schema["title"] = "Experimental mandatory-delta review patch"
    properties = schema["properties"]
    properties["review_disposition"] = {"type": "string", "const": "patch_proposed"}
    properties["unresolved_findings"]["maxItems"] = 0
    return schema


def validate_mandatory_delta_comparison(
    comparison: Mapping[str, Any],
    *,
    ledger: Mapping[str, Any],
    original_proposal: Mapping[str, Any],
    expected_ledger_sha256: str,
    expected_proposal_sha256: str,
    expected_asset_sha256: str,
) -> ContractValidation:
    """Require a frozen, source-routed repair plan in addition to base validity."""

    base = validate_unit_comparison(
        comparison,
        ledger=ledger,
        original_proposal=original_proposal,
        expected_ledger_sha256=expected_ledger_sha256,
        expected_proposal_sha256=expected_proposal_sha256,
        expected_asset_sha256=expected_asset_sha256,
    )
    if not base.ok:
        return base

    comparisons = list(comparison["comparisons"])
    dispositions = [str(item["disposition"]) for item in comparisons]
    errors: list[str] = []
    if all(disposition == "fully_retrievable" for disposition in dispositions):
        errors.append("mandatory delta comparison cannot classify every unit fully_retrievable")

    repairable = [
        item for item in comparisons if str(item["disposition"]) in REPAIRABLE_DISPOSITIONS
    ]
    if not repairable:
        errors.append(
            "mandatory delta comparison requires a repairable non-fully-retrievable "
            "unit; unresolved-only output is invalid"
        )
    elif not any(item["planned_patch_operations"] for item in repairable):
        errors.append("mandatory delta comparison requires at least one planned operation")

    return ContractValidation(tuple(dict.fromkeys(errors)))


def freeze_mandatory_delta_comparison_file(
    *,
    comparison_path: Path,
    receipt_path: Path,
    ledger_path: Path,
    reveal_receipt_path: Path,
    proposal_path: Path,
    forbidden_patch_path: Path,
    expected_source_id: str,
    expected_asset_sha256: str,
    expected_proposal_sha256: str,
) -> ComparisonFreeze:
    """Freeze only a base-valid comparison that contains mandatory patch work."""

    ledger = _read_object(ledger_path)
    proposal = _read_object(proposal_path)
    comparison = _read_object(comparison_path)
    validation = validate_mandatory_delta_comparison(
        comparison,
        ledger=ledger,
        original_proposal=proposal,
        expected_ledger_sha256=_sha256(ledger_path),
        expected_proposal_sha256=expected_proposal_sha256,
        expected_asset_sha256=expected_asset_sha256,
    )
    if not validation.ok:
        raise MandatoryDeltaReviewError(
            "invalid mandatory delta comparison: " + "; ".join(validation.errors)
        )
    return freeze_unit_comparison_file(
        comparison_path=comparison_path,
        receipt_path=receipt_path,
        ledger_path=ledger_path,
        reveal_receipt_path=reveal_receipt_path,
        proposal_path=proposal_path,
        forbidden_patch_path=forbidden_patch_path,
        expected_source_id=expected_source_id,
        expected_asset_sha256=expected_asset_sha256,
        expected_proposal_sha256=expected_proposal_sha256,
    )


def validate_mandatory_delta_candidate_files(
    *,
    ledger_path: Path,
    proposal_path: Path,
    comparison_path: Path,
    comparison_receipt_path: Path,
    patch_path: Path,
    candidate_path: Path,
    report_path: Path,
    proposal_schema_path: Path,
    schema_directory: Path,
    expected_source_id: str,
    source_title: str,
    expected_asset_sha256: str,
    expected_proposal_sha256: str,
    page_count: int,
) -> CandidateValidationReport:
    """Enforce a routed non-empty delta, then reuse complete admission unchanged."""

    candidate_path.unlink(missing_ok=True)
    ledger = _read_object(ledger_path)
    proposal = _read_object(proposal_path)
    comparison = _read_object(comparison_path)
    patch = _read_object(patch_path)
    patch_sha256 = _sha256(patch_path)

    errors = list(
        validate_mandatory_delta_comparison(
            comparison,
            ledger=ledger,
            original_proposal=proposal,
            expected_ledger_sha256=_sha256(ledger_path),
            expected_proposal_sha256=expected_proposal_sha256,
            expected_asset_sha256=expected_asset_sha256,
        ).errors
    )
    errors.extend(
        _validate_mandatory_patch_routes(
            ledger=ledger,
            comparison=comparison,
            patch=patch,
        )
    )
    if errors:
        report = CandidateValidationReport(
            errors=tuple(dict.fromkeys(errors)),
            warnings=(),
            patch_sha256=patch_sha256,
            candidate_sha256=None,
            semantic_records=None,
            graph_nodes=None,
            graph_edges=None,
        )
        _write_json(report_path, report.to_dict())
        return report

    report = validate_page_routed_candidate_files(
        ledger_path=ledger_path,
        proposal_path=proposal_path,
        comparison_path=comparison_path,
        comparison_receipt_path=comparison_receipt_path,
        patch_path=patch_path,
        candidate_path=candidate_path,
        report_path=report_path,
        proposal_schema_path=proposal_schema_path,
        schema_directory=schema_directory,
        expected_source_id=expected_source_id,
        source_title=source_title,
        expected_asset_sha256=expected_asset_sha256,
        expected_proposal_sha256=expected_proposal_sha256,
        page_count=page_count,
    )
    if not report.ok:
        return report

    candidate = _read_object(candidate_path)
    if candidate != proposal:
        return report

    candidate_path.unlink(missing_ok=True)
    rejected = CandidateValidationReport(
        errors=tuple(
            dict.fromkeys(
                (*report.errors, "candidate is semantically identical to the sealed proposal")
            )
        ),
        warnings=report.warnings,
        patch_sha256=report.patch_sha256,
        candidate_sha256=None,
        semantic_records=report.semantic_records,
        graph_nodes=report.graph_nodes,
        graph_edges=report.graph_edges,
    )
    _write_json(report_path, rejected.to_dict())
    return rejected


def _validate_mandatory_patch_routes(
    *,
    ledger: Mapping[str, Any],
    comparison: Mapping[str, Any],
    patch: Mapping[str, Any],
) -> tuple[str, ...]:
    errors: list[str] = []
    try:
        disposition = str(patch["review_disposition"])
        unresolved_findings = list(patch["unresolved_findings"])
        actual_operations = {
            (operation, collection, str(record["id"]))
            for operation, key in (("add", "additions"), ("replace", "replacements"))
            for collection in COLLECTION_DEFINITIONS
            for record in patch[key][collection]
        }
        rationales = {
            (
                str(item["operation"]),
                str(item["collection"]),
                str(item["record_id"]),
            ): item
            for item in patch["operation_rationales"]
        }
        units = {str(unit["id"]): unit for unit in ledger["units"]}
        routed_operations: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
        for item in comparison["comparisons"]:
            if str(item["disposition"]) not in REPAIRABLE_DISPOSITIONS:
                continue
            unit = units[str(item["unit_id"])]
            for operation in item["planned_patch_operations"]:
                key = (
                    str(operation["operation"]),
                    str(operation["collection"]),
                    str(operation["record_id"]),
                )
                routed_operations.setdefault(key, []).append(unit)
    except (KeyError, TypeError):
        return ("mandatory delta patch routes cannot be evaluated until structure is valid",)

    if disposition == "no_changes":
        errors.append("mandatory delta rejects no_changes")
    elif disposition == "manual_review":
        errors.append("mandatory delta rejects manual_review")
    elif disposition != "patch_proposed":
        errors.append("mandatory delta requires patch_proposed disposition")
    if unresolved_findings and not actual_operations:
        errors.append("mandatory delta rejects unresolved-only output")
    if unresolved_findings:
        errors.append("mandatory delta candidate cannot terminate with unresolved findings")
    if not actual_operations:
        errors.append("mandatory delta requires at least one addition or replacement")

    for operation in sorted(actual_operations):
        routed_units = routed_operations.get(operation, [])
        if not routed_units:
            errors.append(
                "patch operation is not routed from a repairable frozen unit: "
                + "/".join(operation)
            )
            continue
        rationale = rationales.get(operation)
        if rationale is None:
            errors.append("patch operation lacks an exact rationale route: " + "/".join(operation))
            continue
        rationale_pages = {int(page) for page in rationale["source_pages"]}
        evidence_ids = [str(identifier) for identifier in rationale["evidence_ids"]]
        if not evidence_ids:
            errors.append("patch operation lacks an exact evidence record: " + "/".join(operation))
        required_pages = {
            int(evidence["page"]) for unit in routed_units for evidence in unit["evidence"]
        }
        if not required_pages.issubset(rationale_pages):
            errors.append(
                "patch operation rationale omits frozen-unit evidence pages: " + "/".join(operation)
            )
        for unit in routed_units:
            if not unit["block_ids"] or (
                not unit["thread_opening"] and not unit["predecessor_ids"]
            ):
                errors.append(
                    "patch operation lacks a frozen narrative placement route: "
                    + "/".join(operation)
                )

    return tuple(dict.fromkeys(errors))


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise MandatoryDeltaReviewError(f"expected a JSON object: {path}")
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
