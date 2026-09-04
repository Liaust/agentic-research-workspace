"""Deterministic checks for the experimental inventory-first reader."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from research_map.graph import compile_graph
from research_map.hierarchical import adapted_hierarchical_records, validate_hierarchical_proposal
from research_map.records import Dossier, Record, RecordError
from research_map.validation import (
    DossierValidationError,
    validate_dossier,
    validate_record_vocabulary,
)

INVENTORY_CATEGORIES = frozenset(
    {
        "definition",
        "assumption",
        "reported_position",
        "claim",
        "question",
        "objection",
        "response",
        "qualification",
        "limitation",
        "method",
        "transition",
        "equation",
        "figure",
        "check",
        "result",
        "other_scientific",
        "terminal_or_non_scientific",
    }
)
RESOLUTION_DISPOSITIONS = frozenset(
    {"represented", "excluded_terminal_or_non_scientific", "manual_review"}
)


class InventoryFirstValidationError(ValueError):
    """Raised when scratch artifacts cannot be safely frozen or accepted."""


@dataclass(frozen=True, slots=True)
class CandidateBundleReport:
    """One transport-friendly report over all deterministic candidate checks."""

    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    inventory_positions: int
    resolution_dispositions: dict[str, int]
    semantic_records: int | None
    graph_nodes: int | None
    graph_edges: int | None
    inventory_sha256: str
    proposal_sha256: str

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "valid": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "inventory_positions": self.inventory_positions,
            "resolution_dispositions": dict(self.resolution_dispositions),
            "semantic_records": self.semantic_records,
            "graph_nodes": self.graph_nodes,
            "graph_edges": self.graph_edges,
            "inventory_sha256": self.inventory_sha256,
            "proposal_sha256": self.proposal_sha256,
        }


def canonical_json_sha256(value: Mapping[str, Any]) -> str:
    """Hash parsed JSON so final-message whitespace cannot change identity."""

    encoded = json.dumps(
        dict(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def read_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise InventoryFirstValidationError(f"expected a JSON object at {path}")
    return value


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def build_source_position_inventory_schema(page_count: int) -> dict[str, Any]:
    """Return the lightweight source-neutral scratch inventory schema."""

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Inventory-first source positions",
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "source_id", "positions"],
        "properties": {
            "schema_version": {"const": 1},
            "source_id": {"type": "string", "pattern": "^LIB-[0-9]{3}$"},
            "positions": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "position_id",
                        "sequence",
                        "page_start",
                        "page_end",
                        "category",
                        "source_grounded_description",
                        "source_locator",
                        "dependency_position_ids",
                    ],
                    "properties": {
                        "position_id": {
                            "type": "string",
                            "pattern": "^LIB-[0-9]{3}:position:[a-z0-9][a-z0-9-]*$",
                        },
                        "sequence": {"type": "integer", "minimum": 1},
                        "page_start": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": page_count,
                        },
                        "page_end": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": page_count,
                        },
                        "category": {
                            "type": "string",
                            "enum": sorted(INVENTORY_CATEGORIES),
                        },
                        "source_grounded_description": {
                            "type": "string",
                            "minLength": 1,
                        },
                        "source_locator": {"type": "string", "minLength": 1},
                        "dependency_position_ids": {
                            "type": "array",
                            "uniqueItems": True,
                            "items": {"type": "string"},
                        },
                    },
                },
            },
        },
    }


def build_inventory_resolution_schema() -> dict[str, Any]:
    """Return the source-neutral inventory-to-proposal resolution schema."""

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Inventory-first position resolutions",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "source_id",
            "resolutions",
        ],
        "properties": {
            "schema_version": {"const": 1},
            "source_id": {"type": "string", "pattern": "^LIB-[0-9]{3}$"},
            "resolutions": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "position_id",
                        "disposition",
                        "atom_ids",
                        "move_ids",
                        "evidence_ids",
                        "reason",
                    ],
                    "properties": {
                        "position_id": {"type": "string", "minLength": 1},
                        "disposition": {
                            "type": "string",
                            "enum": sorted(RESOLUTION_DISPOSITIONS),
                        },
                        "atom_ids": {
                            "type": "array",
                            "uniqueItems": True,
                            "items": {"type": "string"},
                        },
                        "move_ids": {
                            "type": "array",
                            "uniqueItems": True,
                            "items": {"type": "string"},
                        },
                        "evidence_ids": {
                            "type": "array",
                            "uniqueItems": True,
                            "items": {"type": "string"},
                        },
                        "reason": {"type": ["string", "null"]},
                    },
                },
            },
        },
    }


def validate_source_position_inventory(
    inventory: Mapping[str, Any], *, expected_source_id: str, page_count: int
) -> tuple[str, ...]:
    errors: list[str] = []
    if inventory.get("schema_version") != 1:
        errors.append("inventory schema_version must be 1")
    if inventory.get("source_id") != expected_source_id:
        errors.append(f"inventory source_id must be {expected_source_id}")
    raw_positions = inventory.get("positions")
    if not isinstance(raw_positions, list) or not raw_positions:
        return tuple(errors + ["inventory positions must be a non-empty list"])
    positions = [item for item in raw_positions if isinstance(item, dict)]
    if len(positions) != len(raw_positions):
        errors.append("every inventory position must be an object")

    identifiers = [item.get("position_id") for item in positions]
    duplicate_ids = sorted(
        str(identifier) for identifier, count in Counter(identifiers).items() if count > 1
    )
    errors.extend(f"duplicate inventory position_id: {item}" for item in duplicate_ids)
    known_ids = {item for item in identifiers if isinstance(item, str) and item}
    expected_sequences = list(range(1, len(positions) + 1))
    actual_sequences = [item.get("sequence") for item in positions]
    if actual_sequences != expected_sequences:
        errors.append("inventory sequences must be consecutive and source ordered")

    previous_start = 0
    for index, position in enumerate(positions, start=1):
        subject = str(position.get("position_id") or f"position[{index}]")
        if not subject.startswith(f"{expected_source_id}:position:"):
            errors.append(f"{subject}: position_id does not match the source")
        start = position.get("page_start")
        end = position.get("page_end")
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or not 1 <= start <= end <= page_count
        ):
            errors.append(f"{subject}: invalid page range")
        elif start < previous_start:
            errors.append(f"{subject}: page order moves backwards")
        else:
            previous_start = start
        if position.get("category") not in INVENTORY_CATEGORIES:
            errors.append(f"{subject}: unsupported inventory category")
        for field in ("source_grounded_description", "source_locator"):
            if not isinstance(position.get(field), str) or not str(position[field]).strip():
                errors.append(f"{subject}: {field} must be non-empty")
        dependencies = position.get("dependency_position_ids")
        if not isinstance(dependencies, list) or any(
            not isinstance(item, str) or not item for item in dependencies
        ):
            errors.append(f"{subject}: dependency_position_ids must be strings")
            continue
        for dependency in dependencies:
            if dependency not in known_ids:
                errors.append(f"{subject}: unknown dependency {dependency}")
            if dependency == subject:
                errors.append(f"{subject}: cannot depend on itself")
    return tuple(errors)


def freeze_inventory_file(
    inventory_path: Path,
    receipt_path: Path,
    *,
    expected_source_id: str,
    page_count: int,
) -> dict[str, Any]:
    inventory = read_json_object(inventory_path)
    errors = validate_source_position_inventory(
        inventory, expected_source_id=expected_source_id, page_count=page_count
    )
    if errors:
        raise InventoryFirstValidationError("; ".join(errors))
    receipt = {
        "schema_version": 1,
        "source_id": expected_source_id,
        "inventory_sha256": canonical_json_sha256(inventory),
        "position_count": len(inventory["positions"]),
    }
    write_json(receipt_path, receipt)
    return receipt


def validate_inventory_resolution(
    inventory: Mapping[str, Any],
    resolution: Mapping[str, Any],
    proposal: Mapping[str, Any],
    *,
    expected_source_id: str,
) -> tuple[str, ...]:
    errors: list[str] = []
    if resolution.get("schema_version") != 1:
        errors.append("resolution schema_version must be 1")
    if resolution.get("source_id") != expected_source_id:
        errors.append(f"resolution source_id must be {expected_source_id}")
    positions = {
        str(item["position_id"]): item
        for item in inventory.get("positions", [])
        if isinstance(item, dict) and isinstance(item.get("position_id"), str)
    }
    raw_resolutions = resolution.get("resolutions")
    if not isinstance(raw_resolutions, list):
        return tuple(errors + ["resolutions must be a list"])
    items = [item for item in raw_resolutions if isinstance(item, dict)]
    if len(items) != len(raw_resolutions):
        errors.append("every resolution must be an object")
    identifiers = [item.get("position_id") for item in items]
    for identifier, count in Counter(identifiers).items():
        if count > 1:
            errors.append(f"duplicate resolution for {identifier}")
    resolved_ids = {item for item in identifiers if isinstance(item, str)}
    for missing in sorted(set(positions) - resolved_ids):
        errors.append(f"missing resolution for {missing}")
    for extra in sorted(resolved_ids - set(positions)):
        errors.append(f"resolution references unknown position {extra}")

    record_ids = {
        "atom_ids": {
            str(item["id"])
            for item in proposal.get("atom_records", [])
            if isinstance(item, dict) and "id" in item
        },
        "move_ids": {
            str(item["id"])
            for item in proposal.get("move_records", [])
            if isinstance(item, dict) and "id" in item
        },
        "evidence_ids": {
            str(item["id"])
            for item in proposal.get("evidence_records", [])
            if isinstance(item, dict) and "id" in item
        },
    }
    for item in items:
        identifier = item.get("position_id")
        if not isinstance(identifier, str) or identifier not in positions:
            continue
        disposition = item.get("disposition")
        if disposition not in RESOLUTION_DISPOSITIONS:
            errors.append(f"{identifier}: unsupported disposition")
            continue
        references: dict[str, list[str]] = {}
        for field in ("atom_ids", "move_ids", "evidence_ids"):
            raw = item.get(field)
            if not isinstance(raw, list) or any(
                not isinstance(value, str) or not value for value in raw
            ):
                errors.append(f"{identifier}: {field} must be a string list")
                references[field] = []
                continue
            references[field] = raw
            for value in raw:
                if value not in record_ids[field]:
                    errors.append(f"{identifier}: unknown {field[:-1]} {value}")
        reason = item.get("reason")
        if disposition == "represented":
            for field in ("atom_ids", "move_ids", "evidence_ids"):
                if not references.get(field):
                    errors.append(f"{identifier}: represented requires {field}")
        elif disposition == "excluded_terminal_or_non_scientific":
            if positions[identifier].get("category") != "terminal_or_non_scientific":
                errors.append(f"{identifier}: only terminal inventory may be excluded")
            if any(references.values()):
                errors.append(f"{identifier}: excluded positions cannot cite records")
            if not isinstance(reason, str) or not reason.strip():
                errors.append(f"{identifier}: exclusion requires a reason")
        else:
            if any(references.values()):
                errors.append(f"{identifier}: manual review cannot claim record coverage")
            if not isinstance(reason, str) or not reason.strip():
                errors.append(f"{identifier}: manual review requires a reason")
    return tuple(errors)


def adapted_records(proposal: Mapping[str, Any]) -> tuple[Record, ...]:
    """Adapt the experimental segment surface to the accepted v1 dossier."""

    return adapted_hierarchical_records(proposal)


def evaluate_candidate_bundle(
    *,
    inventory: Mapping[str, Any],
    freeze_receipt: Mapping[str, Any],
    resolution: Mapping[str, Any],
    proposal: Mapping[str, Any],
    proposal_schema_path: Path,
    v1_schema_directory: Path,
    expected_source_id: str,
    expected_asset_sha256: str,
    source_title: str,
    page_count: int,
) -> CandidateBundleReport:
    """Validate scratch identity, proposal structure, vocabulary, and graph."""

    errors = list(
        validate_source_position_inventory(
            inventory, expected_source_id=expected_source_id, page_count=page_count
        )
    )
    warnings: list[str] = []
    inventory_sha256 = canonical_json_sha256(inventory)
    proposal_sha256 = canonical_json_sha256(proposal)
    if freeze_receipt.get("inventory_sha256") != inventory_sha256:
        errors.append("frozen inventory hash no longer matches the inventory")
    errors.extend(
        validate_inventory_resolution(
            inventory,
            resolution,
            proposal,
            expected_source_id=expected_source_id,
        )
    )

    hierarchy = validate_hierarchical_proposal(
        proposal,
        schema_path=proposal_schema_path,
        expected_source_id=expected_source_id,
        expected_asset_sha256=expected_asset_sha256,
        page_count=page_count,
        page_text_by_page={page: None for page in range(1, page_count + 1)},
    )
    errors.extend(
        f"hierarchy:{finding.code}:{finding.subject_id}:{finding.message}"
        for finding in hierarchy.findings
        if finding.disposition == "failed"
    )
    if hierarchy.has_uncheckable:
        warnings.append("exact prose and visual fidelity require external source review")

    semantic_records: int | None = None
    graph_nodes: int | None = None
    graph_edges: int | None = None
    try:
        records = adapted_records(proposal)
        semantic_records = len(records)
        dossier = Dossier.create(
            source_id=expected_source_id,
            title=source_title,
            summary=str(proposal.get("paper_summary", "")),
            records=records,
        )
        dossier_report = validate_dossier(
            dossier,
            schema_directory=v1_schema_directory,
            expected_asset_sha256=expected_asset_sha256,
            page_count=page_count,
        )
        errors.extend(f"dossier:{error}" for error in dossier_report.errors)
        vocabulary = validate_record_vocabulary(dossier.records)
        errors.extend(f"vocabulary:{error}" for error in vocabulary.errors)
        if vocabulary.type_gaps:
            warnings.append(f"candidate declares {len(vocabulary.type_gaps)} type gap(s)")
        if dossier_report.ok and vocabulary.ok:
            compilation = compile_graph(
                dossier,
                schema_directory=v1_schema_directory,
                expected_asset_sha256=expected_asset_sha256,
                page_count=page_count,
            )
            graph_nodes = len(compilation.graph["nodes"])
            graph_edges = len(compilation.graph["edges"])
    except (DossierValidationError, RecordError, KeyError, TypeError, ValueError) as error:
        errors.append(f"graph:{error}")

    raw_resolutions = resolution.get("resolutions", [])
    dispositions = Counter(
        str(item.get("disposition")) for item in raw_resolutions if isinstance(item, dict)
    )
    return CandidateBundleReport(
        errors=tuple(dict.fromkeys(errors)),
        warnings=tuple(dict.fromkeys(warnings)),
        inventory_positions=len(inventory.get("positions", [])),
        resolution_dispositions=dict(sorted(dispositions.items())),
        semantic_records=semantic_records,
        graph_nodes=graph_nodes,
        graph_edges=graph_edges,
        inventory_sha256=inventory_sha256,
        proposal_sha256=proposal_sha256,
    )
