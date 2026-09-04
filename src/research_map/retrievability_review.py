"""Granular source-first review and same-turn candidate validation.

This experimental contract separates three semantic stages inside one Codex
turn: a source-only inventory, a frozen item-level comparison, and a patch over
an immutable first proposal.  Deterministic helpers enforce that order and
materialize one derived candidate without changing canonical research state.
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

from research_map.hierarchical_review import (
    COLLECTION_DEFINITIONS,
    apply_review_patch,
)
from research_map.map_first_admission import validate_map_first_proposal_file
from research_map.source_first_review import (
    POSITION_KINDS,
    ContractValidation,
    ProposalReveal,
    validate_reveal_receipt,
)

RETRIEVABILITY_DISPOSITIONS = (
    "fully_retrievable",
    "partially_retrievable",
    "missing",
    "misrepresented",
    "unresolved",
)
FIDELITY_FIELDS = (
    "material_meaning",
    "attribution",
    "qualifications",
    "epistemic_posture",
    "exact_evidence",
    "source_role",
    "logical_placement",
)
FORMAL_POSITION_KINDS = frozenset({"equation", "figure", "table"})
ELLIPSIS_PATTERN = re.compile(r"(?:\.\s*){3}|\N{HORIZONTAL ELLIPSIS}")


class RetrievabilityReviewError(ValueError):
    """Raised when a staged review artifact cannot cross a deterministic gate."""


@dataclass(frozen=True, slots=True)
class ComparisonFreeze:
    """Hashes bound when the complete comparison is frozen before patching."""

    inventory_sha256: str
    proposal_sha256: str
    comparison_sha256: str
    receipt_path: Path


@dataclass(frozen=True, slots=True)
class CandidateValidationReport:
    """Complete deterministic result for one patch-derived candidate."""

    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    patch_sha256: str
    candidate_sha256: str | None
    semantic_records: int | None
    graph_nodes: int | None
    graph_edges: int | None

    @property
    def ok(self) -> bool:
        return not self.errors and self.candidate_sha256 is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "valid": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "patch_sha256": self.patch_sha256,
            "candidate_sha256": self.candidate_sha256,
            "semantic_records": self.semantic_records,
            "graph_nodes": self.graph_nodes,
            "graph_edges": self.graph_edges,
        }


def build_granular_inventory_schema(page_count: int) -> dict[str, Any]:
    """Return the source-neutral inventory schema used before proposal reveal."""

    if page_count < 1:
        raise ValueError("page_count must be positive")
    evidence = {
        "type": "object",
        "additionalProperties": False,
        "required": ["page", "mode", "locator", "source_content"],
        "properties": {
            "page": {"type": "integer", "minimum": 1, "maximum": page_count},
            "mode": {
                "type": "string",
                "enum": ["verbatim_text", "displayed_math", "figure_region", "table_region"],
            },
            "locator": {"type": "string", "minLength": 1},
            "source_content": {"type": "string", "minLength": 1},
        },
    }
    granularity_check = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "retrieval_query",
            "separable_positions_remaining",
            "split_rationale",
        ],
        "properties": {
            "retrieval_query": {"type": "string", "minLength": 1},
            "separable_positions_remaining": {"type": "boolean", "const": False},
            "split_rationale": {"type": "string", "minLength": 1},
        },
    }
    position = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "id",
            "sequence",
            "page_start",
            "page_end",
            "kind",
            "label",
            "source_summary",
            "attribution",
            "epistemic_posture",
            "evidence",
            "predecessor_ids",
            "connection_value",
            "granularity_check",
        ],
        "properties": {
            "id": {"type": "string", "pattern": "^inventory:[a-z0-9][a-z0-9-]*$"},
            "sequence": {"type": "integer", "minimum": 1},
            "page_start": {"type": "integer", "minimum": 1, "maximum": page_count},
            "page_end": {"type": "integer", "minimum": 1, "maximum": page_count},
            "kind": {"type": "string", "enum": list(POSITION_KINDS)},
            "label": {"type": "string", "minLength": 1},
            "source_summary": {"type": "string", "minLength": 1},
            "attribution": {"type": "string", "minLength": 1},
            "epistemic_posture": {"type": "string", "minLength": 1},
            "evidence": {"type": "array", "minItems": 1, "items": evidence},
            "predecessor_ids": {
                "type": "array",
                "uniqueItems": True,
                "items": {"type": "string", "pattern": "^inventory:[a-z0-9][a-z0-9-]*$"},
            },
            "connection_value": {"type": "string", "minLength": 1},
            "granularity_check": granularity_check,
        },
    }
    page_review = {
        "type": "object",
        "additionalProperties": False,
        "required": ["page", "disposition", "position_ids", "review_note"],
        "properties": {
            "page": {"type": "integer", "minimum": 1, "maximum": page_count},
            "disposition": {
                "type": "string",
                "enum": ["scientific_content_reviewed", "terminal_material_reviewed"],
            },
            "position_ids": {
                "type": "array",
                "uniqueItems": True,
                "items": {"type": "string", "pattern": "^inventory:[a-z0-9][a-z0-9-]*$"},
            },
            "review_note": {"type": "string", "minLength": 1},
        },
    }
    terminal_exclusion = {
        "type": "object",
        "additionalProperties": False,
        "required": ["page_start", "page_end", "description", "reason"],
        "properties": {
            "page_start": {"type": "integer", "minimum": 1, "maximum": page_count},
            "page_end": {"type": "integer", "minimum": 1, "maximum": page_count},
            "description": {"type": "string", "minLength": 1},
            "reason": {"type": "string", "minLength": 1},
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Experimental granular source-first inventory",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "source_id",
            "asset_sha256",
            "page_count",
            "methodology_note",
            "page_reviews",
            "positions",
            "terminal_exclusions",
        ],
        "properties": {
            "schema_version": {"type": "string", "const": "1.0"},
            "source_id": {"type": "string", "pattern": "^LIB-[0-9]{3}$"},
            "asset_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            "page_count": {"type": "integer", "const": page_count},
            "methodology_note": {"type": "string", "minLength": 1},
            "page_reviews": {
                "type": "array",
                "minItems": page_count,
                "maxItems": page_count,
                "items": page_review,
            },
            "positions": {"type": "array", "minItems": 1, "items": position},
            "terminal_exclusions": {"type": "array", "items": terminal_exclusion},
        },
    }


def build_retrievability_comparison_schema() -> dict[str, Any]:
    """Return the item-level comparison schema frozen before patch drafting."""

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
    fidelity = {
        "type": "object",
        "additionalProperties": False,
        "required": list(FIDELITY_FIELDS),
        "properties": {field: {"type": "boolean"} for field in FIDELITY_FIELDS},
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
            "inventory_id",
            "disposition",
            "existing_routes",
            "fidelity_check",
            "planned_patch_operations",
            "unresolved_finding_ids",
            "explanation",
        ],
        "properties": {
            "inventory_id": {
                "type": "string",
                "pattern": "^inventory:[a-z0-9][a-z0-9-]*$",
            },
            "disposition": {"type": "string", "enum": list(RETRIEVABILITY_DISPOSITIONS)},
            "existing_routes": routes,
            "fidelity_check": fidelity,
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
        "title": "Experimental granular inventory-to-map comparison",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "source_id",
            "asset_sha256",
            "inventory_sha256",
            "reviewed_proposal_sha256",
            "comparisons",
            "comparison_summary",
        ],
        "properties": {
            "schema_version": {"type": "string", "const": "1.0"},
            "source_id": {"type": "string", "pattern": "^LIB-[0-9]{3}$"},
            "asset_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            "inventory_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            "reviewed_proposal_sha256": {
                "type": "string",
                "pattern": "^[a-f0-9]{64}$",
            },
            "comparisons": {"type": "array", "minItems": 1, "items": comparison},
            "comparison_summary": {"type": "string", "minLength": 1},
        },
    }


def validate_granular_inventory(
    inventory: Mapping[str, Any],
    *,
    expected_source_id: str,
    expected_asset_sha256: str,
    page_count: int,
) -> ContractValidation:
    """Validate identity, exact snippets, page coverage, and source order."""

    errors = _schema_errors(build_granular_inventory_schema(page_count), inventory, "inventory")
    if errors:
        return ContractValidation(tuple(errors))
    if inventory["source_id"] != expected_source_id:
        errors.append("inventory source_id does not match the registered source")
    if inventory["asset_sha256"] != expected_asset_sha256:
        errors.append("inventory asset_sha256 does not match the immutable asset")

    page_counts = Counter(int(item["page"]) for item in inventory["page_reviews"])
    for page in range(1, page_count + 1):
        if page_counts[page] != 1:
            errors.append(f"inventory must review page {page} exactly once")

    positions = list(inventory["positions"])
    identifiers = [str(position["id"]) for position in positions]
    sequences = [int(position["sequence"]) for position in positions]
    for identifier, count in Counter(identifiers).items():
        if count > 1:
            errors.append(f"inventory position id appears more than once: {identifier}")
    if sequences != list(range(1, len(positions) + 1)):
        errors.append("inventory sequences must be contiguous and written in source order")

    positions_by_id = {str(position["id"]): position for position in positions}
    sequence_by_id = {str(position["id"]): int(position["sequence"]) for position in positions}
    routed_ids: list[str] = []
    previous_page = 0
    for position in positions:
        identifier = str(position["id"])
        page_start = int(position["page_start"])
        page_end = int(position["page_end"])
        if page_end < page_start:
            errors.append(f"inventory position has reversed page range: {identifier}")
        if page_start < previous_page:
            errors.append(f"inventory position moves backwards in source order: {identifier}")
        previous_page = max(previous_page, page_start)
        for evidence in position["evidence"]:
            page = int(evidence["page"])
            if not page_start <= page <= page_end:
                errors.append(f"evidence page is outside position range: {identifier}")
            if evidence["mode"] == "verbatim_text" and ELLIPSIS_PATTERN.search(
                str(evidence["source_content"])
            ):
                errors.append(f"verbatim inventory evidence contains an ellipsis: {identifier}")
        for predecessor_id in position["predecessor_ids"]:
            predecessor = str(predecessor_id)
            if predecessor not in positions_by_id:
                errors.append(f"inventory predecessor is unknown: {identifier} -> {predecessor}")
            elif sequence_by_id[predecessor] >= int(position["sequence"]):
                errors.append(
                    f"inventory predecessor is not earlier: {identifier} -> {predecessor}"
                )

    for review in inventory["page_reviews"]:
        page = int(review["page"])
        position_ids = [str(value) for value in review["position_ids"]]
        if review["disposition"] == "scientific_content_reviewed" and not position_ids:
            errors.append(f"scientific page {page} must route at least one position")
        for identifier in position_ids:
            routed_ids.append(identifier)
            position = positions_by_id.get(identifier)
            if position is None:
                errors.append(f"page {page} routes unknown inventory position: {identifier}")
            elif not int(position["page_start"]) <= page <= int(position["page_end"]):
                errors.append(f"page {page} is outside the route for {identifier}")
    for identifier in identifiers:
        if identifier not in routed_ids:
            errors.append(f"inventory position is not routed from any page: {identifier}")
    for exclusion in inventory["terminal_exclusions"]:
        if int(exclusion["page_end"]) < int(exclusion["page_start"]):
            errors.append("terminal exclusion has reversed page range")
    return ContractValidation(tuple(dict.fromkeys(errors)))


def reveal_proposal_after_granular_inventory(
    *,
    inventory_path: Path,
    sealed_proposal_path: Path,
    destination_path: Path,
    receipt_path: Path,
    expected_source_id: str,
    expected_asset_sha256: str,
    expected_proposal_sha256: str,
    page_count: int,
) -> ProposalReveal:
    """Reveal a byte-identical proposal only after the granular inventory passes."""

    if destination_path.exists():
        raise FileExistsError(f"proposal destination already exists: {destination_path}")
    if receipt_path.exists():
        raise FileExistsError(f"reveal receipt already exists: {receipt_path}")
    inventory = _read_object(inventory_path)
    validation = validate_granular_inventory(
        inventory,
        expected_source_id=expected_source_id,
        expected_asset_sha256=expected_asset_sha256,
        page_count=page_count,
    )
    if not validation.ok:
        raise RetrievabilityReviewError(
            "invalid granular inventory: " + "; ".join(validation.errors)
        )
    inventory_sha256 = _sha256(inventory_path)
    proposal_sha256 = _sha256(sealed_proposal_path)
    if proposal_sha256 != expected_proposal_sha256:
        raise RetrievabilityReviewError(
            "sealed proposal sha256 does not match the immutable comparator"
        )
    proposal = _read_object(sealed_proposal_path)
    if proposal.get("source_id") != expected_source_id:
        raise RetrievabilityReviewError(
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
        "inventory_sha256": inventory_sha256,
        "proposal_sha256": proposal_sha256,
        "proposal_path": relative_proposal_path.as_posix(),
    }
    try:
        _write_json(receipt_path, receipt)
    except Exception:
        destination_path.unlink(missing_ok=True)
        raise
    return ProposalReveal(inventory_sha256, proposal_sha256, destination_path, receipt_path)


def validate_retrievability_comparison(
    comparison: Mapping[str, Any],
    *,
    inventory: Mapping[str, Any],
    original_proposal: Mapping[str, Any],
    expected_inventory_sha256: str,
    expected_proposal_sha256: str,
    expected_asset_sha256: str,
) -> ContractValidation:
    """Validate complete item-level routes before any patch is drafted."""

    errors = _schema_errors(build_retrievability_comparison_schema(), comparison, "comparison")
    if errors:
        return ContractValidation(tuple(errors))
    if comparison["source_id"] != inventory["source_id"]:
        errors.append("comparison source_id does not match the inventory")
    if comparison["asset_sha256"] != expected_asset_sha256:
        errors.append("comparison asset_sha256 does not match the immutable asset")
    if comparison["inventory_sha256"] != expected_inventory_sha256:
        errors.append("comparison inventory_sha256 does not match the frozen inventory")
    if comparison["reviewed_proposal_sha256"] != expected_proposal_sha256:
        errors.append("comparison proposal sha256 does not match the first proposal")

    positions = {str(item["id"]): item for item in inventory["positions"]}
    compared_ids = [str(item["inventory_id"]) for item in comparison["comparisons"]]
    if Counter(compared_ids) != Counter(positions.keys()):
        errors.append("comparison must classify every inventory position exactly once")

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
        inventory_id = str(item["inventory_id"])
        position = positions.get(inventory_id)
        if position is None:
            continue
        routes = item["existing_routes"]
        for field, allowed in record_ids.items():
            for identifier in routes[field]:
                if identifier not in allowed:
                    errors.append(f"{inventory_id}: unknown {field[:-4]} route {identifier}")
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
                errors.append(f"{inventory_id}: replacement target is unknown: {identifier}")
            if operation["operation"] == "add" and identifier in all_original_ids:
                errors.append(f"{inventory_id}: addition collides with existing id: {identifier}")
        if disposition == "fully_retrievable":
            if operations or findings:
                errors.append(f"{inventory_id}: fully retrievable cannot cite patch work")
            if not all_fidelity:
                errors.append(f"{inventory_id}: fully retrievable requires every fidelity check")
            for field in ("evidence_record_ids", "atom_record_ids", "move_record_ids"):
                if not routes[field]:
                    errors.append(f"{inventory_id}: fully retrievable requires {field}")
            if position["kind"] in FORMAL_POSITION_KINDS and not routes["source_object_ids"]:
                errors.append(f"{inventory_id}: formal position requires a source-object route")
        elif disposition in {"partially_retrievable", "missing", "misrepresented"}:
            if not operations or findings:
                errors.append(f"{inventory_id}: repairable disposition requires patch work only")
            if all_fidelity:
                errors.append(
                    f"{inventory_id}: repairable disposition cannot pass every fidelity check"
                )
            if disposition == "missing" and not any(
                operation["operation"] == "add" for operation in operations
            ):
                errors.append(f"{inventory_id}: missing position requires an addition")
            if disposition == "misrepresented" and not any(
                operation["operation"] == "replace" for operation in operations
            ):
                errors.append(f"{inventory_id}: misrepresented position requires a replacement")
        elif disposition == "unresolved":
            if operations or not findings:
                errors.append(f"{inventory_id}: unresolved must cite findings only")
    return ContractValidation(tuple(dict.fromkeys(errors)))


def freeze_comparison_file(
    *,
    comparison_path: Path,
    receipt_path: Path,
    inventory_path: Path,
    reveal_receipt_path: Path,
    proposal_path: Path,
    forbidden_patch_path: Path,
    expected_source_id: str,
    expected_asset_sha256: str,
    expected_proposal_sha256: str,
) -> ComparisonFreeze:
    """Freeze a complete comparison only while no patch artifact exists."""

    if receipt_path.exists():
        raise FileExistsError(f"comparison receipt already exists: {receipt_path}")
    if forbidden_patch_path.exists():
        raise RetrievabilityReviewError("comparison must be frozen before the first patch write")
    inventory = _read_object(inventory_path)
    proposal = _read_object(proposal_path)
    reveal_receipt = _read_object(reveal_receipt_path)
    reveal_validation = validate_reveal_receipt(
        reveal_receipt,
        inventory_path=inventory_path,
        proposal_path=proposal_path,
        expected_source_id=expected_source_id,
        expected_asset_sha256=expected_asset_sha256,
        expected_proposal_sha256=expected_proposal_sha256,
    )
    if not reveal_validation.ok:
        raise RetrievabilityReviewError(
            "invalid reveal state: " + "; ".join(reveal_validation.errors)
        )
    comparison = _read_object(comparison_path)
    comparison_validation = validate_retrievability_comparison(
        comparison,
        inventory=inventory,
        original_proposal=proposal,
        expected_inventory_sha256=_sha256(inventory_path),
        expected_proposal_sha256=expected_proposal_sha256,
        expected_asset_sha256=expected_asset_sha256,
    )
    if not comparison_validation.ok:
        raise RetrievabilityReviewError(
            "invalid retrievability comparison: " + "; ".join(comparison_validation.errors)
        )
    comparison_sha256 = _sha256(comparison_path)
    receipt = {
        "schema_version": "1.0",
        "source_id": expected_source_id,
        "asset_sha256": expected_asset_sha256,
        "inventory_sha256": _sha256(inventory_path),
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


def validate_comparison_freeze_receipt(
    receipt: Mapping[str, Any],
    *,
    inventory_path: Path,
    proposal_path: Path,
    comparison_path: Path,
    expected_source_id: str,
    expected_asset_sha256: str,
    expected_proposal_sha256: str,
) -> ContractValidation:
    """Prove that inventory, proposal, and comparison are unchanged after freeze."""

    required = {
        "schema_version",
        "source_id",
        "asset_sha256",
        "inventory_sha256",
        "proposal_sha256",
        "comparison_sha256",
    }
    errors: list[str] = []
    if set(receipt) != required:
        return ContractValidation(("comparison receipt fields do not match the contract",))
    expected = {
        "schema_version": "1.0",
        "source_id": expected_source_id,
        "asset_sha256": expected_asset_sha256,
        "proposal_sha256": expected_proposal_sha256,
    }
    for field, value in expected.items():
        if receipt[field] != value:
            errors.append(f"comparison receipt {field} does not match the contract")
    for field, path in (
        ("inventory_sha256", inventory_path),
        ("proposal_sha256", proposal_path),
        ("comparison_sha256", comparison_path),
    ):
        if not path.is_file():
            errors.append(f"frozen {field.removesuffix('_sha256')} is missing")
        elif receipt[field] != _sha256(path):
            errors.append(f"frozen {field.removesuffix('_sha256')} changed after comparison freeze")
    return ContractValidation(tuple(errors))


def validate_candidate_files(
    *,
    inventory_path: Path,
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
    """Apply one frozen-comparison patch and validate the complete derived graph."""

    candidate_path.unlink(missing_ok=True)
    proposal = _read_object(proposal_path)
    comparison = _read_object(comparison_path)
    receipt = _read_object(comparison_receipt_path)
    patch = _read_object(patch_path)
    patch_sha256 = _sha256(patch_path)
    errors: list[str] = []
    warnings: list[str] = []

    freeze_validation = validate_comparison_freeze_receipt(
        receipt,
        inventory_path=inventory_path,
        proposal_path=proposal_path,
        comparison_path=comparison_path,
        expected_source_id=expected_source_id,
        expected_asset_sha256=expected_asset_sha256,
        expected_proposal_sha256=expected_proposal_sha256,
    )
    errors.extend(f"freeze:{error}" for error in freeze_validation.errors)

    candidate_sha256: str | None = None
    semantic_records: int | None = None
    graph_nodes: int | None = None
    graph_edges: int | None = None
    application = None
    if not errors:
        proposal_schema = _read_object(proposal_schema_path)
        application = apply_review_patch(
            proposal,
            patch,
            proposal_schema=proposal_schema,
            expected_proposal_sha256=expected_proposal_sha256,
            expected_asset_sha256=expected_asset_sha256,
            page_count=page_count,
            page_text_by_page={page: None for page in range(1, page_count + 1)},
        )
        errors.extend(f"patch:{error}" for error in application.errors)
        errors.extend(_validate_patch_accounting(comparison, patch))
        if (
            application.proposal_validation is not None
            and application.proposal_validation.has_uncheckable
        ):
            warnings.append(
                "exact prose and visual fidelity remain subject to external source review"
            )
        if not errors and application.ok and application.combined_proposal is not None:
            _write_json(candidate_path, application.combined_proposal)
            admission = validate_map_first_proposal_file(
                candidate_path,
                proposal_schema_path=proposal_schema_path,
                schema_directory=schema_directory,
                expected_source_id=expected_source_id,
                source_title=source_title,
                expected_asset_sha256=expected_asset_sha256,
                page_count=page_count,
                page_text_by_page={page: None for page in range(1, page_count + 1)},
                calibration_tolerant=False,
            )
            if not admission.ok:
                errors.extend(
                    f"candidate:{finding.code}:{finding.subject_id}:{finding.message}"
                    for finding in admission.failed_findings
                )
                candidate_path.unlink(missing_ok=True)
            else:
                candidate_sha256 = _sha256(candidate_path)
            semantic_records = len(admission.records) if admission.records else None
            graph_nodes = admission.graph_record_count
            graph_edges = admission.graph_edge_count

    report = CandidateValidationReport(
        errors=tuple(dict.fromkeys(errors)),
        warnings=tuple(dict.fromkeys(warnings)),
        patch_sha256=patch_sha256,
        candidate_sha256=candidate_sha256,
        semantic_records=semantic_records,
        graph_nodes=graph_nodes,
        graph_edges=graph_edges,
    )
    _write_json(report_path, report.to_dict())
    return report


def _validate_patch_accounting(
    comparison: Mapping[str, Any], patch: Mapping[str, Any]
) -> tuple[str, ...]:
    try:
        expected_operations = {
            (
                str(operation["operation"]),
                str(operation["collection"]),
                str(operation["record_id"]),
            )
            for item in comparison["comparisons"]
            for operation in item["planned_patch_operations"]
        }
        actual_operations = {
            (operation, collection, str(record["id"]))
            for operation, key in (("add", "additions"), ("replace", "replacements"))
            for collection in COLLECTION_DEFINITIONS
            for record in patch[key][collection]
        }
        expected_findings = {
            str(identifier)
            for item in comparison["comparisons"]
            for identifier in item["unresolved_finding_ids"]
        }
        actual_findings = {str(item["id"]) for item in patch["unresolved_findings"]}
    except (KeyError, TypeError):
        return ("patch accounting cannot be evaluated until its structure is valid",)
    errors: list[str] = []
    if expected_operations != actual_operations:
        errors.append("patch operations do not match the frozen comparison exactly")
    if expected_findings != actual_findings:
        errors.append("patch findings do not match the frozen comparison exactly")
    return tuple(errors)


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
        raise RetrievabilityReviewError(f"expected a JSON object: {path}")
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
