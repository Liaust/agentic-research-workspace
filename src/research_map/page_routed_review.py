"""Page-routed source coverage and staged candidate validation.

The experimental contract closes ordered visible blocks on every page before
an immutable proposal can be revealed.  Every semantic block routes to one or
more independently retrievable units, every unit has exact source evidence and
a source-order narrative route, and every unit comparison freezes before any
patch artifact may exist.  Patch application and complete candidate admission
reuse the already validated retrievability-review boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from research_map.hierarchical_review import COLLECTION_DEFINITIONS
from research_map.retrievability_review import (
    FIDELITY_FIELDS,
    FORMAL_POSITION_KINDS,
    RETRIEVABILITY_DISPOSITIONS,
    CandidateValidationReport,
    ComparisonFreeze,
    validate_candidate_files,
)
from research_map.source_first_review import (
    POSITION_KINDS,
    ContractValidation,
    ProposalReveal,
    validate_reveal_receipt,
)

PAGE_BLOCK_KINDS = (
    "heading",
    "paragraph",
    "displayed_equation",
    "figure",
    "figure_caption",
    "table",
    "list",
    "footnote",
    "terminal_material",
    "other",
)
ANCHOR_MODES = (
    "verbatim_text",
    "displayed_math",
    "figure_region",
    "table_region",
    "visual_region",
)
ELLIPSIS_PATTERN = re.compile(r"(?:\.\s*){3}|\N{HORIZONTAL ELLIPSIS}")


class PageRoutedReviewError(ValueError):
    """Raised when a page-routed artifact cannot cross a deterministic gate."""


@dataclass(frozen=True, slots=True)
class _BlockRoute:
    page: int
    block_index: int
    semantic: bool
    unit_ids: frozenset[str]


def build_page_ledger_schema(page_count: int) -> dict[str, Any]:
    """Return the source-general page, block, and retrieval-unit schema."""

    if page_count < 1:
        raise ValueError("page_count must be positive")
    anchor = {
        "type": "object",
        "additionalProperties": False,
        "required": ["mode", "locator", "source_content"],
        "properties": {
            "mode": {"type": "string", "enum": list(ANCHOR_MODES)},
            "locator": {"type": "string", "minLength": 1},
            "source_content": {"type": "string", "minLength": 1},
        },
    }
    block = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "id",
            "block_index",
            "kind",
            "label",
            "opening_anchor",
            "closing_anchor",
            "semantic",
            "unit_ids",
            "exclusion_reason",
        ],
        "properties": {
            "id": {"type": "string", "pattern": "^block:[a-z0-9][a-z0-9:-]*$"},
            "block_index": {"type": "integer", "minimum": 1},
            "kind": {"type": "string", "enum": list(PAGE_BLOCK_KINDS)},
            "label": {"type": "string", "minLength": 1},
            "opening_anchor": anchor,
            "closing_anchor": anchor,
            "semantic": {"type": "boolean"},
            "unit_ids": {
                "type": "array",
                "uniqueItems": True,
                "items": {"type": "string", "pattern": "^unit:[a-z0-9][a-z0-9-]*$"},
            },
            "exclusion_reason": {"type": ["string", "null"]},
        },
    }
    page = {
        "type": "object",
        "additionalProperties": False,
        "required": ["page", "status", "blocks", "closure_note"],
        "properties": {
            "page": {"type": "integer", "minimum": 1, "maximum": page_count},
            "status": {"type": "string", "const": "closed"},
            "blocks": {"type": "array", "minItems": 1, "items": block},
            "closure_note": {"type": "string", "minLength": 1},
        },
    }
    evidence = {
        "type": "object",
        "additionalProperties": False,
        "required": ["page", "mode", "locator", "source_content"],
        "properties": {
            "page": {"type": "integer", "minimum": 1, "maximum": page_count},
            "mode": {"type": "string", "enum": list(ANCHOR_MODES)},
            "locator": {"type": "string", "minLength": 1},
            "source_content": {"type": "string", "minLength": 1},
        },
    }
    unit = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "id",
            "sequence",
            "kind",
            "label",
            "source_summary",
            "attribution",
            "epistemic_posture",
            "block_ids",
            "evidence",
            "predecessor_ids",
            "thread_opening",
            "connection_value",
            "retrieval_query",
            "split_rationale",
            "separable_units_remaining",
        ],
        "properties": {
            "id": {"type": "string", "pattern": "^unit:[a-z0-9][a-z0-9-]*$"},
            "sequence": {"type": "integer", "minimum": 1},
            "kind": {"type": "string", "enum": list(POSITION_KINDS)},
            "label": {"type": "string", "minLength": 1},
            "source_summary": {"type": "string", "minLength": 1},
            "attribution": {"type": "string", "minLength": 1},
            "epistemic_posture": {"type": "string", "minLength": 1},
            "block_ids": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": {"type": "string", "pattern": "^block:[a-z0-9][a-z0-9:-]*$"},
            },
            "evidence": {"type": "array", "minItems": 1, "items": evidence},
            "predecessor_ids": {
                "type": "array",
                "uniqueItems": True,
                "items": {"type": "string", "pattern": "^unit:[a-z0-9][a-z0-9-]*$"},
            },
            "thread_opening": {"type": "boolean"},
            "connection_value": {"type": "string", "minLength": 1},
            "retrieval_query": {"type": "string", "minLength": 1},
            "split_rationale": {"type": "string", "minLength": 1},
            "separable_units_remaining": {"type": "boolean", "const": False},
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Experimental page-routed source coverage ledger",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "source_id",
            "asset_sha256",
            "page_count",
            "methodology_note",
            "pages",
            "units",
        ],
        "properties": {
            "schema_version": {"type": "string", "const": "1.0"},
            "source_id": {"type": "string", "pattern": "^LIB-[0-9]{3}$"},
            "asset_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            "page_count": {"type": "integer", "const": page_count},
            "methodology_note": {"type": "string", "minLength": 1},
            "pages": {
                "type": "array",
                "minItems": page_count,
                "maxItems": page_count,
                "items": page,
            },
            "units": {"type": "array", "minItems": 1, "items": unit},
        },
    }


def build_unit_comparison_schema() -> dict[str, Any]:
    """Return the per-unit comparison schema frozen before patch drafting."""

    string_ids = {
        "type": "array",
        "uniqueItems": True,
        "items": {"type": "string", "minLength": 1},
    }
    routes = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "evidence_record_ids",
            "atom_record_ids",
            "move_record_ids",
            "source_object_ids",
        ],
        "properties": {
            "evidence_record_ids": string_ids,
            "atom_record_ids": string_ids,
            "move_record_ids": string_ids,
            "source_object_ids": string_ids,
        },
    }
    operation = {
        "type": "object",
        "additionalProperties": False,
        "required": ["operation", "collection", "record_id"],
        "properties": {
            "operation": {"type": "string", "enum": ["add", "replace"]},
            "collection": {"type": "string", "enum": list(COLLECTION_DEFINITIONS)},
            "record_id": {"type": "string", "minLength": 1},
        },
    }
    comparison = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "unit_id",
            "block_ids",
            "disposition",
            "existing_routes",
            "fidelity_check",
            "planned_patch_operations",
            "unresolved_finding_ids",
            "explanation",
        ],
        "properties": {
            "unit_id": {"type": "string", "pattern": "^unit:[a-z0-9][a-z0-9-]*$"},
            "block_ids": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": {"type": "string", "pattern": "^block:[a-z0-9][a-z0-9:-]*$"},
            },
            "disposition": {"type": "string", "enum": list(RETRIEVABILITY_DISPOSITIONS)},
            "existing_routes": routes,
            "fidelity_check": {
                "type": "object",
                "additionalProperties": False,
                "required": list(FIDELITY_FIELDS),
                "properties": {field: {"type": "boolean"} for field in FIDELITY_FIELDS},
            },
            "planned_patch_operations": {"type": "array", "items": operation},
            "unresolved_finding_ids": {
                "type": "array",
                "uniqueItems": True,
                "items": {
                    "type": "string",
                    "pattern": "^review-finding:[a-z0-9][a-z0-9-]*$",
                },
            },
            "explanation": {"type": "string", "minLength": 1},
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Experimental page-routed unit-to-map comparison",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "source_id",
            "asset_sha256",
            "ledger_sha256",
            "reviewed_proposal_sha256",
            "comparisons",
            "comparison_summary",
        ],
        "properties": {
            "schema_version": {"type": "string", "const": "1.0"},
            "source_id": {"type": "string", "pattern": "^LIB-[0-9]{3}$"},
            "asset_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            "ledger_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            "reviewed_proposal_sha256": {
                "type": "string",
                "pattern": "^[a-f0-9]{64}$",
            },
            "comparisons": {"type": "array", "minItems": 1, "items": comparison},
            "comparison_summary": {"type": "string", "minLength": 1},
        },
    }


def validate_page_ledger(
    ledger: Mapping[str, Any],
    *,
    expected_source_id: str,
    expected_asset_sha256: str,
    page_count: int,
) -> ContractValidation:
    """Validate complete page closure, exact routes, and source order."""

    errors = _schema_errors(build_page_ledger_schema(page_count), ledger, "ledger")
    if errors:
        return ContractValidation(tuple(errors))
    if ledger["source_id"] != expected_source_id:
        errors.append("ledger source_id does not match the registered source")
    if ledger["asset_sha256"] != expected_asset_sha256:
        errors.append("ledger asset_sha256 does not match the immutable asset")

    pages = list(ledger["pages"])
    page_numbers = [int(page["page"]) for page in pages]
    if page_numbers != list(range(1, page_count + 1)):
        errors.append("page ledgers must be complete and written in page order")

    block_routes: dict[str, _BlockRoute] = {}
    for page in pages:
        page_number = int(page["page"])
        blocks = list(page["blocks"])
        indexes = [int(block["block_index"]) for block in blocks]
        if indexes != list(range(1, len(blocks) + 1)):
            errors.append(f"page {page_number} block indexes must be contiguous")
        for block in blocks:
            block_id = str(block["id"])
            if block_id in block_routes:
                errors.append(f"block id appears more than once: {block_id}")
            routed_unit_ids = frozenset(str(value) for value in block["unit_ids"])
            semantic = bool(block["semantic"])
            reason = block["exclusion_reason"]
            if semantic:
                if not routed_unit_ids:
                    errors.append(f"semantic block must route at least one unit: {block_id}")
                if reason is not None:
                    errors.append(f"semantic block cannot have an exclusion reason: {block_id}")
            else:
                if routed_unit_ids:
                    errors.append(f"excluded block cannot route semantic units: {block_id}")
                if not isinstance(reason, str) or not reason.strip():
                    errors.append(f"excluded block requires a reason: {block_id}")
            for anchor_name in ("opening_anchor", "closing_anchor"):
                anchor = block[anchor_name]
                if anchor["mode"] == "verbatim_text" and ELLIPSIS_PATTERN.search(
                    str(anchor["source_content"])
                ):
                    errors.append(f"{block_id} {anchor_name} contains an ellipsis")
            block_routes[block_id] = _BlockRoute(
                page=page_number,
                block_index=int(block["block_index"]),
                semantic=semantic,
                unit_ids=routed_unit_ids,
            )

    units = list(ledger["units"])
    unit_ids = [str(unit["id"]) for unit in units]
    for unit_id, count in Counter(unit_ids).items():
        if count > 1:
            errors.append(f"unit id appears more than once: {unit_id}")
    sequences = [int(unit["sequence"]) for unit in units]
    if sequences != list(range(1, len(units) + 1)):
        errors.append("unit sequences must be contiguous and written in source order")
    units_by_id = {str(unit["id"]): unit for unit in units}
    sequence_by_id = {str(unit["id"]): int(unit["sequence"]) for unit in units}
    previous_first_route = (0, 0)
    for unit in units:
        unit_id = str(unit["id"])
        route_ids = [str(value) for value in unit["block_ids"]]
        routes: list[_BlockRoute] = []
        for block_id in route_ids:
            route = block_routes.get(block_id)
            if route is None:
                errors.append(f"unit routes unknown block: {unit_id} -> {block_id}")
                continue
            routes.append(route)
            if not route.semantic:
                errors.append(f"unit routes an excluded block: {unit_id} -> {block_id}")
            if unit_id not in route.unit_ids:
                errors.append(f"unit/block route is not bidirectional: {unit_id} -> {block_id}")
        if routes:
            first_route = min((route.page, route.block_index) for route in routes)
            if first_route < previous_first_route:
                errors.append(f"unit moves backwards in source order: {unit_id}")
            previous_first_route = max(previous_first_route, first_route)
            routed_pages = {route.page for route in routes}
            for evidence in unit["evidence"]:
                if int(evidence["page"]) not in routed_pages:
                    errors.append(f"unit evidence page is outside its block routes: {unit_id}")
                if evidence["mode"] == "verbatim_text" and ELLIPSIS_PATTERN.search(
                    str(evidence["source_content"])
                ):
                    errors.append(f"verbatim unit evidence contains an ellipsis: {unit_id}")
        predecessors = [str(value) for value in unit["predecessor_ids"]]
        if unit["thread_opening"] and predecessors:
            errors.append(f"thread-opening unit cannot cite a predecessor: {unit_id}")
        if not unit["thread_opening"] and not predecessors:
            errors.append(f"non-opening unit requires a predecessor route: {unit_id}")
        for predecessor_id in predecessors:
            if predecessor_id not in units_by_id:
                errors.append(f"unit predecessor is unknown: {unit_id} -> {predecessor_id}")
            elif sequence_by_id[predecessor_id] >= int(unit["sequence"]):
                errors.append(f"unit predecessor is not earlier: {unit_id} -> {predecessor_id}")

    for block_id, route in block_routes.items():
        if not route.semantic:
            continue
        for unit_id in route.unit_ids:
            unit = units_by_id.get(unit_id)
            if unit is None:
                errors.append(f"block routes unknown unit: {block_id} -> {unit_id}")
            elif block_id not in unit["block_ids"]:
                errors.append(f"block/unit route is not bidirectional: {block_id} -> {unit_id}")
    return ContractValidation(tuple(dict.fromkeys(errors)))


def reveal_proposal_after_page_ledger(
    *,
    ledger_path: Path,
    sealed_proposal_path: Path,
    destination_path: Path,
    receipt_path: Path,
    expected_source_id: str,
    expected_asset_sha256: str,
    expected_proposal_sha256: str,
    page_count: int,
) -> ProposalReveal:
    """Reveal a byte-identical proposal only after every page closes."""

    if destination_path.exists():
        raise FileExistsError(f"proposal destination already exists: {destination_path}")
    if receipt_path.exists():
        raise FileExistsError(f"reveal receipt already exists: {receipt_path}")
    ledger = _read_object(ledger_path)
    validation = validate_page_ledger(
        ledger,
        expected_source_id=expected_source_id,
        expected_asset_sha256=expected_asset_sha256,
        page_count=page_count,
    )
    if not validation.ok:
        raise PageRoutedReviewError("invalid page ledger: " + "; ".join(validation.errors))
    ledger_sha256 = _sha256(ledger_path)
    proposal_sha256 = _sha256(sealed_proposal_path)
    if proposal_sha256 != expected_proposal_sha256:
        raise PageRoutedReviewError(
            "sealed proposal sha256 does not match the immutable comparator"
        )
    proposal = _read_object(sealed_proposal_path)
    if proposal.get("source_id") != expected_source_id:
        raise PageRoutedReviewError(
            "sealed proposal source_id does not match the registered source"
        )
    relative_proposal_path = destination_path.relative_to(receipt_path.parent.parent)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(sealed_proposal_path, destination_path)
    if _sha256(destination_path) != proposal_sha256:
        destination_path.unlink(missing_ok=True)
        raise OSError("revealed proposal is not byte-identical to the sealed proposal")
    receipt = {
        "schema_version": "1.0",
        "source_id": expected_source_id,
        "asset_sha256": expected_asset_sha256,
        "inventory_sha256": ledger_sha256,
        "proposal_sha256": proposal_sha256,
        "proposal_path": relative_proposal_path.as_posix(),
    }
    try:
        _write_json(receipt_path, receipt)
    except Exception:
        destination_path.unlink(missing_ok=True)
        raise
    return ProposalReveal(ledger_sha256, proposal_sha256, destination_path, receipt_path)


def validate_unit_comparison(
    comparison: Mapping[str, Any],
    *,
    ledger: Mapping[str, Any],
    original_proposal: Mapping[str, Any],
    expected_ledger_sha256: str,
    expected_proposal_sha256: str,
    expected_asset_sha256: str,
) -> ContractValidation:
    """Validate one inspectable proposal disposition for every frozen unit."""

    errors = _schema_errors(build_unit_comparison_schema(), comparison, "comparison")
    if errors:
        return ContractValidation(tuple(errors))
    if comparison["source_id"] != ledger["source_id"]:
        errors.append("comparison source_id does not match the ledger")
    if comparison["asset_sha256"] != expected_asset_sha256:
        errors.append("comparison asset_sha256 does not match the immutable asset")
    if comparison["ledger_sha256"] != expected_ledger_sha256:
        errors.append("comparison ledger_sha256 does not match the frozen ledger")
    if comparison["reviewed_proposal_sha256"] != expected_proposal_sha256:
        errors.append("comparison proposal sha256 does not match the first proposal")

    units = {str(item["id"]): item for item in ledger["units"]}
    compared_ids = [str(item["unit_id"]) for item in comparison["comparisons"]]
    if Counter(compared_ids) != Counter(units.keys()):
        errors.append("comparison must classify every retrieval unit exactly once")
    record_ids = {
        "evidence_record_ids": {str(item["id"]) for item in original_proposal["evidence_records"]},
        "atom_record_ids": {str(item["id"]) for item in original_proposal["atom_records"]},
        "move_record_ids": {str(item["id"]) for item in original_proposal["move_records"]},
        "source_object_ids": {str(item["id"]) for item in original_proposal["source_objects"]},
    }
    original_ids_by_collection = {
        collection: {str(item["id"]) for item in original_proposal[collection]}
        for collection in COLLECTION_DEFINITIONS
    }
    all_original_ids = {
        identifier
        for identifiers in original_ids_by_collection.values()
        for identifier in identifiers
    }
    for item in comparison["comparisons"]:
        unit_id = str(item["unit_id"])
        unit = units.get(unit_id)
        if unit is None:
            continue
        if Counter(str(value) for value in item["block_ids"]) != Counter(
            str(value) for value in unit["block_ids"]
        ):
            errors.append(f"{unit_id}: comparison block routes do not match the ledger")
        routes = item["existing_routes"]
        for field, allowed in record_ids.items():
            for identifier in routes[field]:
                if identifier not in allowed:
                    route_name = field.removesuffix("_ids")
                    errors.append(f"{unit_id}: unknown {route_name} route {identifier}")
        fidelity = item["fidelity_check"]
        all_fidelity = all(bool(fidelity[field]) for field in FIDELITY_FIELDS)
        operations = item["planned_patch_operations"]
        findings = item["unresolved_finding_ids"]
        disposition = str(item["disposition"])
        for operation in operations:
            collection = str(operation["collection"])
            identifier = str(operation["record_id"])
            if (
                operation["operation"] == "replace"
                and identifier not in original_ids_by_collection[collection]
            ):
                errors.append(f"{unit_id}: replacement target is unknown: {identifier}")
            if operation["operation"] == "add" and identifier in all_original_ids:
                errors.append(f"{unit_id}: addition collides with existing id: {identifier}")
        if disposition == "fully_retrievable":
            if operations or findings:
                errors.append(f"{unit_id}: fully retrievable cannot cite patch work")
            if not all_fidelity:
                errors.append(f"{unit_id}: fully retrievable requires every fidelity check")
            for field in ("evidence_record_ids", "atom_record_ids", "move_record_ids"):
                if not routes[field]:
                    errors.append(f"{unit_id}: fully retrievable requires {field}")
            if unit["kind"] in FORMAL_POSITION_KINDS and not routes["source_object_ids"]:
                errors.append(f"{unit_id}: formal unit requires a source-object route")
        elif disposition in {"partially_retrievable", "missing", "misrepresented"}:
            if not operations or findings:
                errors.append(f"{unit_id}: repairable disposition requires patch work only")
            if all_fidelity:
                errors.append(f"{unit_id}: repairable disposition cannot pass every fidelity check")
            if disposition == "missing" and not any(
                operation["operation"] == "add" for operation in operations
            ):
                errors.append(f"{unit_id}: missing unit requires an addition")
            if disposition == "misrepresented" and not any(
                operation["operation"] == "replace" for operation in operations
            ):
                errors.append(f"{unit_id}: misrepresented unit requires a replacement")
        elif disposition == "unresolved":
            if operations or not findings:
                errors.append(f"{unit_id}: unresolved must cite findings only")
    return ContractValidation(tuple(dict.fromkeys(errors)))


def freeze_unit_comparison_file(
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
    """Freeze every unit comparison while no patch artifact exists."""

    if receipt_path.exists():
        raise FileExistsError(f"comparison receipt already exists: {receipt_path}")
    if forbidden_patch_path.exists():
        raise PageRoutedReviewError("comparison must be frozen before the first patch write")
    ledger = _read_object(ledger_path)
    proposal = _read_object(proposal_path)
    reveal_receipt = _read_object(reveal_receipt_path)
    reveal_validation = validate_reveal_receipt(
        reveal_receipt,
        inventory_path=ledger_path,
        proposal_path=proposal_path,
        expected_source_id=expected_source_id,
        expected_asset_sha256=expected_asset_sha256,
        expected_proposal_sha256=expected_proposal_sha256,
    )
    if not reveal_validation.ok:
        raise PageRoutedReviewError("invalid reveal state: " + "; ".join(reveal_validation.errors))
    comparison = _read_object(comparison_path)
    comparison_validation = validate_unit_comparison(
        comparison,
        ledger=ledger,
        original_proposal=proposal,
        expected_ledger_sha256=_sha256(ledger_path),
        expected_proposal_sha256=expected_proposal_sha256,
        expected_asset_sha256=expected_asset_sha256,
    )
    if not comparison_validation.ok:
        raise PageRoutedReviewError(
            "invalid unit comparison: " + "; ".join(comparison_validation.errors)
        )
    comparison_sha256 = _sha256(comparison_path)
    receipt = {
        "schema_version": "1.0",
        "source_id": expected_source_id,
        "asset_sha256": expected_asset_sha256,
        "inventory_sha256": _sha256(ledger_path),
        "proposal_sha256": expected_proposal_sha256,
        "comparison_sha256": comparison_sha256,
    }
    _write_json(receipt_path, receipt)
    return ComparisonFreeze(
        inventory_sha256=receipt["inventory_sha256"],
        proposal_sha256=expected_proposal_sha256,
        comparison_sha256=comparison_sha256,
        receipt_path=receipt_path,
    )


def validate_page_routed_candidate_files(
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
    """Reuse immutable patching and complete candidate admission unchanged."""

    return validate_candidate_files(
        inventory_path=ledger_path,
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


def _schema_errors(schema: Mapping[str, Any], payload: Mapping[str, Any], label: str) -> list[str]:
    findings = sorted(
        Draft202012Validator(dict(schema)).iter_errors(dict(payload)),
        key=lambda item: [str(part) for part in item.absolute_path],
    )
    return [
        f"{'.'.join(str(part) for part in finding.absolute_path) or label}: {finding.message}"
        for finding in findings
    ]


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise PageRoutedReviewError(f"expected a JSON object: {path}")
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
