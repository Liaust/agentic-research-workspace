"""Deterministic staging for a source-first hierarchical proposal review.

The reviewer must freeze a page-complete inventory before it can see the
proposal.  After reveal, a second artifact accounts for every inventory
position and every proposed patch operation.  These files are experimental
audit surfaces, not canonical research records.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from research_map.hierarchical_review import COLLECTION_DEFINITIONS

POSITION_KINDS = (
    "definition",
    "claim",
    "question",
    "assumption",
    "argument_step",
    "objection",
    "response",
    "reported_position",
    "qualification",
    "limitation",
    "method",
    "construction",
    "intermediate_result",
    "conclusion",
    "transition",
    "equation",
    "figure",
    "table",
    "other",
)
COMPARISON_DISPOSITIONS = (
    "covered",
    "requires_addition",
    "requires_replacement",
    "unresolved",
)


@dataclass(frozen=True, slots=True)
class ContractValidation:
    """Deterministic validation result for one staging artifact."""

    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True, slots=True)
class ProposalReveal:
    """Hashes and paths bound by one successful proposal reveal."""

    inventory_sha256: str
    proposal_sha256: str
    proposal_path: Path
    receipt_path: Path


def build_source_inventory_schema(page_count: int) -> dict[str, Any]:
    """Return the strict schema for the frozen pre-proposal source inventory."""

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
        ],
        "properties": {
            "id": {
                "type": "string",
                "pattern": "^inventory:[a-z0-9][a-z0-9-]*$",
            },
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
        "title": "Experimental source-first review inventory",
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


def build_inventory_comparison_schema() -> dict[str, Any]:
    """Return the strict post-reveal inventory-to-proposal comparison schema."""

    patch_operation = {
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
            "proposal_record_ids",
            "patch_operations",
            "unresolved_finding_ids",
            "explanation",
        ],
        "properties": {
            "inventory_id": {
                "type": "string",
                "pattern": "^inventory:[a-z0-9][a-z0-9-]*$",
            },
            "disposition": {"type": "string", "enum": list(COMPARISON_DISPOSITIONS)},
            "proposal_record_ids": {
                "type": "array",
                "uniqueItems": True,
                "items": {"type": "string", "minLength": 1},
            },
            "patch_operations": {
                "type": "array",
                "items": patch_operation,
            },
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
        "title": "Experimental source-first inventory comparison",
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


def validate_source_inventory(
    inventory: Mapping[str, Any],
    *,
    expected_source_id: str,
    expected_asset_sha256: str,
    page_count: int,
) -> ContractValidation:
    """Validate schema, identity, page coverage, evidence, and source order."""

    errors = _schema_errors(build_source_inventory_schema(page_count), inventory, "inventory")
    if errors:
        return ContractValidation(tuple(errors))
    if inventory["source_id"] != expected_source_id:
        errors.append("inventory source_id does not match the registered source")
    if inventory["asset_sha256"] != expected_asset_sha256:
        errors.append("inventory asset_sha256 does not match the immutable asset")

    page_reviews = inventory["page_reviews"]
    page_counts = Counter(int(item["page"]) for item in page_reviews)
    for page in range(1, page_count + 1):
        if page_counts[page] != 1:
            errors.append(f"inventory must review page {page} exactly once")

    positions = inventory["positions"]
    ids = [str(position["id"]) for position in positions]
    sequences = [int(position["sequence"]) for position in positions]
    for position_id, count in Counter(ids).items():
        if count > 1:
            errors.append(f"inventory position id appears more than once: {position_id}")
    for sequence, count in Counter(sequences).items():
        if count > 1:
            errors.append(f"inventory sequence appears more than once: {sequence}")
    if sorted(sequences) != list(range(1, len(sequences) + 1)):
        errors.append("inventory sequences must be contiguous from 1 in source order")

    positions_by_id = {str(position["id"]): position for position in positions}
    sequence_by_id = {str(position["id"]): int(position["sequence"]) for position in positions}
    routed_ids: list[str] = []
    for review in page_reviews:
        page = int(review["page"])
        position_ids = [str(value) for value in review["position_ids"]]
        if review["disposition"] == "scientific_content_reviewed" and not position_ids:
            errors.append(f"scientific page {page} must route at least one position")
        for position_id in position_ids:
            routed_ids.append(position_id)
            position = positions_by_id.get(position_id)
            if position is None:
                errors.append(f"page {page} routes unknown inventory position: {position_id}")
            elif not int(position["page_start"]) <= page <= int(position["page_end"]):
                errors.append(f"page {page} is outside the route for {position_id}")

    for position in positions:
        position_id = str(position["id"])
        page_start = int(position["page_start"])
        page_end = int(position["page_end"])
        if page_end < page_start:
            errors.append(f"inventory position has reversed page range: {position_id}")
        if position_id not in routed_ids:
            errors.append(f"inventory position is not routed from any page: {position_id}")
        for evidence in position["evidence"]:
            page = int(evidence["page"])
            if not page_start <= page <= page_end:
                errors.append(f"evidence page is outside position range: {position_id}")
        for predecessor_id in position["predecessor_ids"]:
            predecessor = str(predecessor_id)
            if predecessor not in positions_by_id:
                errors.append(f"inventory predecessor is unknown: {position_id} -> {predecessor}")
            elif sequence_by_id[predecessor] >= int(position["sequence"]):
                errors.append(
                    f"inventory predecessor is not earlier: {position_id} -> {predecessor}"
                )

    for exclusion in inventory["terminal_exclusions"]:
        if int(exclusion["page_end"]) < int(exclusion["page_start"]):
            errors.append("terminal exclusion has reversed page range")
    return ContractValidation(tuple(errors))


def reveal_proposal(
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
    """Reveal a byte-identical proposal only after the inventory validates."""

    if destination_path.exists():
        raise FileExistsError(f"proposal destination already exists: {destination_path}")
    if receipt_path.exists():
        raise FileExistsError(f"reveal receipt already exists: {receipt_path}")
    inventory = _read_object(inventory_path)
    validation = validate_source_inventory(
        inventory,
        expected_source_id=expected_source_id,
        expected_asset_sha256=expected_asset_sha256,
        page_count=page_count,
    )
    if not validation.ok:
        raise ValueError("invalid source inventory: " + "; ".join(validation.errors))
    inventory_sha256 = _sha256(inventory_path)
    proposal_sha256 = _sha256(sealed_proposal_path)
    if proposal_sha256 != expected_proposal_sha256:
        raise ValueError("sealed proposal sha256 does not match the immutable comparator")
    proposal = _read_object(sealed_proposal_path)
    if proposal.get("source_id") != expected_source_id:
        raise ValueError("sealed proposal source_id does not match the registered source")

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
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    except Exception:
        destination_path.unlink(missing_ok=True)
        raise
    return ProposalReveal(inventory_sha256, proposal_sha256, destination_path, receipt_path)


def validate_reveal_receipt(
    receipt: Mapping[str, Any],
    *,
    inventory_path: Path,
    proposal_path: Path,
    expected_source_id: str,
    expected_asset_sha256: str,
    expected_proposal_sha256: str,
) -> ContractValidation:
    """Prove that the frozen inventory and revealed proposal remain unchanged."""

    required = {
        "schema_version",
        "source_id",
        "asset_sha256",
        "inventory_sha256",
        "proposal_sha256",
        "proposal_path",
    }
    errors: list[str] = []
    if set(receipt) != required:
        errors.append("reveal receipt fields do not match the contract")
        return ContractValidation(tuple(errors))
    if receipt["schema_version"] != "1.0":
        errors.append("reveal receipt schema_version must be 1.0")
    if receipt["source_id"] != expected_source_id:
        errors.append("reveal receipt source_id does not match the registered source")
    if receipt["asset_sha256"] != expected_asset_sha256:
        errors.append("reveal receipt asset_sha256 does not match the immutable asset")
    if not inventory_path.is_file():
        errors.append("frozen inventory is missing after reveal")
    elif receipt["inventory_sha256"] != _sha256(inventory_path):
        errors.append("frozen inventory changed after proposal reveal")
    if not proposal_path.is_file():
        errors.append("revealed proposal is missing")
    elif receipt["proposal_sha256"] != _sha256(proposal_path):
        errors.append("revealed proposal changed after reveal")
    if receipt["proposal_sha256"] != expected_proposal_sha256:
        errors.append("reveal receipt proposal sha256 does not match the immutable comparator")
    return ContractValidation(tuple(errors))


def validate_inventory_comparison(
    comparison: Mapping[str, Any],
    *,
    inventory: Mapping[str, Any],
    original_proposal: Mapping[str, Any],
    patch: Mapping[str, Any],
    expected_inventory_sha256: str,
    expected_proposal_sha256: str,
    expected_asset_sha256: str,
) -> ContractValidation:
    """Cross-check one-to-one inventory coverage and exact patch accounting."""

    errors = _schema_errors(build_inventory_comparison_schema(), comparison, "comparison")
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

    inventory_ids = [str(item["id"]) for item in inventory["positions"]]
    compared_ids = [str(item["inventory_id"]) for item in comparison["comparisons"]]
    if Counter(compared_ids) != Counter(inventory_ids):
        errors.append("comparison must classify every inventory position exactly once")

    proposal_ids = {
        str(record["id"])
        for collection in COLLECTION_DEFINITIONS
        for record in original_proposal[collection]
    }
    expected_operations = {
        (operation, collection, str(record["id"]))
        for operation, key in (("add", "additions"), ("replace", "replacements"))
        for collection in COLLECTION_DEFINITIONS
        for record in patch[key][collection]
    }
    actual_operations: set[tuple[str, str, str]] = set()
    expected_findings = {str(item["id"]) for item in patch["unresolved_findings"]}
    actual_findings: set[str] = set()
    for item in comparison["comparisons"]:
        disposition = str(item["disposition"])
        proposal_record_ids = [str(value) for value in item["proposal_record_ids"]]
        operations = [
            (str(value["operation"]), str(value["collection"]), str(value["record_id"]))
            for value in item["patch_operations"]
        ]
        finding_ids = [str(value) for value in item["unresolved_finding_ids"]]
        for proposal_id in proposal_record_ids:
            if proposal_id not in proposal_ids:
                errors.append(f"comparison references unknown proposal record: {proposal_id}")
        actual_operations.update(operations)
        actual_findings.update(finding_ids)
        if disposition == "covered":
            if not proposal_record_ids:
                errors.append(f"covered comparison lacks proposal records: {item['inventory_id']}")
            if operations or finding_ids:
                errors.append(f"covered comparison cannot cite patch work: {item['inventory_id']}")
        elif disposition == "requires_addition":
            if not any(operation == "add" for operation, _, _ in operations):
                errors.append(f"requires_addition must cite an addition: {item['inventory_id']}")
            if finding_ids:
                errors.append(
                    f"requires_addition cannot cite unresolved findings: {item['inventory_id']}"
                )
        elif disposition == "requires_replacement":
            if not any(operation == "replace" for operation, _, _ in operations):
                errors.append(
                    f"requires_replacement must cite a replacement: {item['inventory_id']}"
                )
            if finding_ids:
                errors.append(
                    f"requires_replacement cannot cite unresolved findings: {item['inventory_id']}"
                )
        elif disposition == "unresolved":
            if operations or not finding_ids:
                errors.append(
                    f"unresolved comparison must cite findings only: {item['inventory_id']}"
                )

    if actual_operations != expected_operations:
        errors.append("comparison patch operations do not account for the patch exactly")
    if actual_findings != expected_findings:
        errors.append("comparison unresolved findings do not account for the patch exactly")
    return ContractValidation(tuple(errors))


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
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
