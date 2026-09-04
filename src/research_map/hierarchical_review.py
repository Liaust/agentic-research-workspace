"""Patch-only review support for hierarchical whole-paper proposals.

The first proposal remains immutable.  A separate semantic reviewer may emit
only additions and same-ID replacements; this module validates that patch and
builds a derived combined proposal without editing the original object.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, cast

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from research_map.hierarchical import (
    HierarchicalValidationReport,
    validate_hierarchical_proposal,
)

COLLECTION_DEFINITIONS: dict[str, str] = {
    "argument_blocks": "argument_block",
    "evidence_records": "evidence",
    "atom_records": "atom",
    "move_records": "move",
    "thread_records": "thread",
    "source_objects": "source_object",
}


@dataclass(frozen=True, slots=True)
class ReviewPatchApplication:
    """Result of validating and applying one immutable review patch."""

    errors: tuple[str, ...]
    combined_proposal: dict[str, Any] | None
    proposal_validation: HierarchicalValidationReport | None
    additions: Mapping[str, int]
    replacements: Mapping[str, int]

    @property
    def ok(self) -> bool:
        return not self.errors and self.combined_proposal is not None


def build_review_patch_schema(
    proposal_schema: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a self-contained structured-output schema from the proposal schema."""

    definitions = deepcopy(dict(proposal_schema["$defs"]))
    collection_properties = {
        collection: {
            "type": "array",
            "items": {"$ref": f"#/$defs/{definition}"},
        }
        for collection, definition in COLLECTION_DEFINITIONS.items()
    }
    definitions["patch_collections"] = {
        "type": "object",
        "additionalProperties": False,
        "required": list(COLLECTION_DEFINITIONS),
        "properties": collection_properties,
    }
    definitions["operation_rationale"] = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "operation",
            "collection",
            "record_id",
            "source_pages",
            "evidence_ids",
            "description",
        ],
        "properties": {
            "operation": {"type": "string", "enum": ["add", "replace"]},
            "collection": {
                "type": "string",
                "enum": list(COLLECTION_DEFINITIONS),
            },
            "record_id": {"type": "string", "minLength": 1},
            "source_pages": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "integer", "minimum": 1},
            },
            "evidence_ids": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
            },
            "description": {"type": "string", "minLength": 1},
        },
    }
    definitions["unresolved_finding"] = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "id",
            "page_start",
            "page_end",
            "description",
            "reason",
        ],
        "properties": {
            "id": {
                "type": "string",
                "pattern": "^review-finding:[a-z0-9][a-z0-9-]*$",
            },
            "page_start": {"type": "integer", "minimum": 1},
            "page_end": {"type": "integer", "minimum": 1},
            "description": {"type": "string", "minLength": 1},
            "reason": {"type": "string", "minLength": 1},
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Experimental independent hierarchical review patch",
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema_version",
            "source_id",
            "asset_sha256",
            "reviewed_proposal_sha256",
            "review_disposition",
            "review_summary",
            "additions",
            "replacements",
            "operation_rationales",
            "unresolved_findings",
            "warnings",
        ],
        "properties": {
            "schema_version": {"type": "string", "const": "1.0"},
            "source_id": {"type": "string", "pattern": "^LIB-[0-9]{3}$"},
            "asset_sha256": {
                "type": "string",
                "pattern": "^[a-f0-9]{64}$",
            },
            "reviewed_proposal_sha256": {
                "type": "string",
                "pattern": "^[a-f0-9]{64}$",
            },
            "review_disposition": {
                "type": "string",
                "enum": ["no_changes", "patch_proposed", "manual_review"],
            },
            "review_summary": {"type": "string", "minLength": 1},
            "additions": {"$ref": "#/$defs/patch_collections"},
            "replacements": {"$ref": "#/$defs/patch_collections"},
            "operation_rationales": {
                "type": "array",
                "items": {"$ref": "#/$defs/operation_rationale"},
            },
            "unresolved_findings": {
                "type": "array",
                "items": {"$ref": "#/$defs/unresolved_finding"},
            },
            "warnings": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
            },
        },
        "$defs": definitions,
    }


def apply_review_patch(
    original: Mapping[str, Any],
    patch: Mapping[str, Any],
    *,
    proposal_schema: Mapping[str, Any],
    expected_proposal_sha256: str,
    expected_asset_sha256: str,
    page_count: int,
    page_text_by_page: Mapping[int, str | None],
) -> ReviewPatchApplication:
    """Validate a patch and return a separately derived combined proposal."""

    errors: list[str] = []
    patch_schema = build_review_patch_schema(proposal_schema)
    schema_errors = sorted(
        Draft202012Validator(patch_schema).iter_errors(dict(patch)),
        key=lambda item: [str(part) for part in item.absolute_path],
    )
    if schema_errors:
        errors.extend(
            f"{'.'.join(str(part) for part in error.absolute_path) or 'patch'}: {error.message}"
            for error in schema_errors
        )
        return _failed(errors)

    source_id = str(original["source_id"])
    if patch["source_id"] != source_id:
        errors.append("patch source_id does not match the original proposal")
    if patch["asset_sha256"] != expected_asset_sha256:
        errors.append("patch asset_sha256 does not match the immutable source")
    if patch["reviewed_proposal_sha256"] != expected_proposal_sha256:
        errors.append("patch reviewed_proposal_sha256 does not match the first proposal")

    additions = {
        collection: len(patch["additions"][collection]) for collection in COLLECTION_DEFINITIONS
    }
    replacements = {
        collection: len(patch["replacements"][collection]) for collection in COLLECTION_DEFINITIONS
    }
    operation_count = sum(additions.values()) + sum(replacements.values())
    disposition = str(patch["review_disposition"])
    if disposition == "no_changes" and operation_count:
        errors.append("no_changes disposition cannot contain patch operations")
    if disposition == "patch_proposed" and not operation_count:
        errors.append("patch_proposed disposition requires at least one operation")
    if disposition == "manual_review" and not patch["unresolved_findings"]:
        errors.append("manual_review disposition requires an unresolved finding")
    if patch["unresolved_findings"] and disposition != "manual_review":
        errors.append("unresolved findings require manual_review disposition")

    original_ids_by_collection = {
        collection: {str(record["id"]) for record in original[collection]}
        for collection in COLLECTION_DEFINITIONS
    }
    all_original_ids = {
        record_id
        for identifiers in original_ids_by_collection.values()
        for record_id in identifiers
    }
    all_addition_ids: list[str] = []
    replacement_ids_by_collection: dict[str, list[str]] = {
        collection: [] for collection in COLLECTION_DEFINITIONS
    }
    expected_rationales: list[tuple[str, str, str]] = []
    for collection in COLLECTION_DEFINITIONS:
        for record in patch["additions"][collection]:
            record_id = str(record["id"])
            all_addition_ids.append(record_id)
            expected_rationales.append(("add", collection, record_id))
            if record_id in all_original_ids:
                errors.append(f"addition collides with existing id: {record_id}")
            if str(record["source_id"]) != source_id:
                errors.append(f"addition source_id mismatch: {record_id}")
        for record in patch["replacements"][collection]:
            record_id = str(record["id"])
            replacement_ids_by_collection[collection].append(record_id)
            expected_rationales.append(("replace", collection, record_id))
            if record_id not in original_ids_by_collection[collection]:
                errors.append(f"replacement target is unknown in {collection}: {record_id}")
            if str(record["source_id"]) != source_id:
                errors.append(f"replacement source_id mismatch: {record_id}")

    for record_id, count in Counter(all_addition_ids).items():
        if count > 1:
            errors.append(f"addition id appears more than once: {record_id}")
    for collection, record_ids in replacement_ids_by_collection.items():
        for record_id, count in Counter(record_ids).items():
            if count > 1:
                errors.append(f"replacement id appears more than once in {collection}: {record_id}")

    actual_rationales = [
        (
            str(item["operation"]),
            str(item["collection"]),
            str(item["record_id"]),
        )
        for item in patch["operation_rationales"]
    ]
    if Counter(actual_rationales) != Counter(expected_rationales):
        errors.append("operation rationales do not match patch operations exactly")
    for item in patch["operation_rationales"]:
        if any(int(page) > page_count for page in item["source_pages"]):
            errors.append(f"rationale page exceeds source page count: {item['record_id']}")
    for item in patch["unresolved_findings"]:
        if not 1 <= int(item["page_start"]) <= int(item["page_end"]) <= page_count:
            errors.append(f"unresolved finding has invalid page route: {item['id']}")

    if errors:
        return ReviewPatchApplication(tuple(errors), None, None, additions, replacements)

    combined = deepcopy(dict(original))
    for collection in COLLECTION_DEFINITIONS:
        replacement_by_id = {
            str(record["id"]): deepcopy(record) for record in patch["replacements"][collection]
        }
        combined[collection] = [
            replacement_by_id.get(str(record["id"]), deepcopy(record))
            for record in original[collection]
        ]
        combined[collection].extend(deepcopy(record) for record in patch["additions"][collection])

    final_evidence_ids = {str(record["id"]) for record in combined["evidence_records"]}
    for item in patch["operation_rationales"]:
        for evidence_id in item["evidence_ids"]:
            if evidence_id not in final_evidence_ids:
                errors.append(f"rationale evidence reference is unknown: {evidence_id}")

    validation = validate_hierarchical_proposal(
        combined,
        schema_path=cast(Any, _SchemaPathAdapter(proposal_schema)),
        expected_source_id=source_id,
        expected_asset_sha256=expected_asset_sha256,
        page_count=page_count,
        page_text_by_page=page_text_by_page,
    )
    if not validation.ok:
        errors.extend(
            f"combined proposal {finding.code}: {finding.subject_id}: {finding.message}"
            for finding in validation.findings
            if finding.disposition == "failed"
        )
    return ReviewPatchApplication(
        tuple(errors),
        combined if not errors else None,
        validation,
        additions,
        replacements,
    )


class _SchemaPathAdapter:
    """Path-like adapter for the validator's read-only schema dependency."""

    def __init__(self, schema: Mapping[str, Any]) -> None:
        self._payload = schema

    def read_text(self, *, encoding: str) -> str:
        return json.dumps(self._payload)


def _failed(errors: list[str]) -> ReviewPatchApplication:
    empty = {collection: 0 for collection in COLLECTION_DEFINITIONS}
    return ReviewPatchApplication(tuple(errors), None, None, empty, empty)
