"""Relationship-local admission plans for holistic cross-reference proposals."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from research_map.cross_reference import (
    CrossReferenceValidationError,
    DiscoveryCandidate,
    EndpointRef,
    Relationship,
    validate_candidate_endpoints,
    validate_inspection_outcome,
    validate_schema,
)
from research_map.ids import canonical_source_pair, cross_reference_relationship_id
from research_map.receipts import canonical_json_bytes
from research_map.records import Record

AdmissionDisposition = Literal["admitted", "quarantined", "duplicate", "existing_noop"]


@dataclass(frozen=True, slots=True)
class AdmissionDiagnostic:
    code: str
    message: str
    surface_key: str | None = None
    relationship_index: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "surface_key": self.surface_key,
            "relationship_index": self.relationship_index,
        }


@dataclass(frozen=True, slots=True)
class AdmissionItem:
    index: int
    raw_row_sha256: str
    raw_endpoint_ids: tuple[str, str]
    normalized_endpoint_ids: tuple[str, str] | None
    surface_key: str | None
    disposition: AdmissionDisposition
    reason_codes: tuple[str, ...]
    normalizations: tuple[str, ...] = ()
    candidate: DiscoveryCandidate | None = None
    outcome: dict[str, Any] | None = None
    relationship: Relationship | None = None

    def to_receipt_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "raw_row_sha256": self.raw_row_sha256,
            "raw_endpoint_ids": list(self.raw_endpoint_ids),
            "normalized_endpoint_ids": (
                None if self.normalized_endpoint_ids is None else list(self.normalized_endpoint_ids)
            ),
            "surface_key": self.surface_key,
            "disposition": self.disposition,
            "reason_codes": list(self.reason_codes),
            "normalizations": list(self.normalizations),
            "candidate_id": None if self.candidate is None else self.candidate.candidate_id,
            "relationship_id": (
                None if self.relationship is None else self.relationship.relationship_id
            ),
        }


@dataclass(frozen=True, slots=True)
class AdmissionPlan:
    batch_id: str
    job_id: str
    raw_proposal_sha256: str
    items: tuple[AdmissionItem, ...]
    diagnostics: tuple[AdmissionDiagnostic, ...]
    observed_surfaces: dict[str, tuple[int, ...]]

    @property
    def counts(self) -> dict[str, int]:
        counts = {
            "admitted": 0,
            "quarantined": 0,
            "duplicate": 0,
            "existing_noop": 0,
        }
        for item in self.items:
            counts[item.disposition] += 1
        return counts

    @property
    def admitted(self) -> tuple[AdmissionItem, ...]:
        return tuple(item for item in self.items if item.disposition == "admitted")

    def receipt_payload(self, *, raw_proposal_path: str) -> dict[str, Any]:
        return {
            "kind": "cross_reference_admission",
            "batch_id": self.batch_id,
            "job_id": self.job_id,
            "raw_proposal": {
                "path": raw_proposal_path,
                "sha256": self.raw_proposal_sha256,
            },
            "counts": self.counts,
            "items": [item.to_receipt_dict() for item in self.items],
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "observed_surfaces": [
                {"surface_key": key, "relationship_indexes": list(indexes)}
                for key, indexes in sorted(self.observed_surfaces.items())
            ],
        }


def compile_holistic_admission(
    proposal: Mapping[str, Any],
    *,
    batch_id: str,
    job_id: str,
    raw_proposal_sha256: str,
    records: Mapping[str, Record],
    evidence_ids_by_source: Mapping[str, frozenset[str] | set[str]],
    existing_relationships: tuple[Relationship, ...],
    owned_pairs: frozenset[tuple[str, str]] | None,
    model: str,
    input_sha256: str,
    protocol_sha256: str,
    relationship_schema_sha256: str,
    schema_directory: Path,
) -> AdmissionPlan:
    """Compile one immutable proposal into independent deterministic dispositions."""

    raw_relationships = proposal.get("relationships")
    if not isinstance(raw_relationships, list):
        raise CrossReferenceValidationError("holistic proposal relationships are unusable")
    diagnostics, observed_surfaces = _coverage_diagnostics(
        proposal,
        records=records,
        existing_relationships=existing_relationships,
    )
    items: list[AdmissionItem] = []
    for index, value in enumerate(raw_relationships):
        if not isinstance(value, dict):
            raise CrossReferenceValidationError(
                f"holistic relationship envelope is unusable at index {index}"
            )
        raw = dict(value)
        raw_endpoint_ids = (
            str(raw.get("left_record_id", "")),
            str(raw.get("right_record_id", "")),
        )
        raw_row_sha256 = hashlib.sha256(canonical_json_bytes(raw)).hexdigest()
        surface_key = _optional_string(raw.get("surface_key"))
        left_record = records.get(raw_endpoint_ids[0])
        right_record = records.get(raw_endpoint_ids[1])
        if left_record is None or right_record is None:
            items.append(
                AdmissionItem(
                    index=index,
                    raw_row_sha256=raw_row_sha256,
                    raw_endpoint_ids=raw_endpoint_ids,
                    surface_key=surface_key,
                    normalized_endpoint_ids=None,
                    disposition="quarantined",
                    reason_codes=("endpoint_outside_frozen_corpus",),
                )
            )
            continue
        try:
            left_record, right_record, normalized, normalizations = _normalize_relationship(
                raw,
                left_record=left_record,
                right_record=right_record,
            )
            pair = canonical_source_pair(left_record.source_id, right_record.source_id)
            if owned_pairs is not None and pair not in owned_pairs:
                items.append(
                    AdmissionItem(
                        index=index,
                        raw_row_sha256=raw_row_sha256,
                        raw_endpoint_ids=raw_endpoint_ids,
                        surface_key=surface_key,
                        normalized_endpoint_ids=(left_record.id, right_record.id),
                        disposition="quarantined",
                        reason_codes=("outside_owned_source_pair",),
                        normalizations=normalizations,
                    )
                )
                continue
            candidate = DiscoveryCandidate.create(
                batch_id=batch_id,
                left_endpoint=EndpointRef.from_record(left_record),
                right_endpoint=EndpointRef.from_record(right_record),
                comparison_surface=str(normalized["comparison_surface"]),
                discovery_job_id=job_id,
            )
            validate_candidate_endpoints(candidate, records=records)
            validate_schema(
                candidate.to_dict(),
                schema_directory=schema_directory,
                schema_name="discovery-candidate.schema.json",
            )
            relationship = _canonical_relationship(
                normalized,
                candidate=candidate,
                batch_id=batch_id,
                job_id=job_id,
                model=model,
                input_sha256=input_sha256,
                protocol_sha256=protocol_sha256,
                relationship_schema_sha256=relationship_schema_sha256,
                schema_directory=schema_directory,
            )
            outcome = {
                "schema_version": 1,
                "candidate_id": candidate.candidate_id,
                "outcome": "relationship",
                "disposition_reason": normalized["rationale"],
                "relationship": relationship.to_dict(),
            }
            validate_schema(
                outcome,
                schema_directory=schema_directory,
                schema_name="inspection-outcome.schema.json",
            )
            validate_inspection_outcome(
                outcome,
                candidate=candidate,
                records=records,
                evidence_ids_by_source=evidence_ids_by_source,
            )
            items.append(
                AdmissionItem(
                    index=index,
                    raw_row_sha256=raw_row_sha256,
                    raw_endpoint_ids=raw_endpoint_ids,
                    surface_key=surface_key,
                    normalized_endpoint_ids=(left_record.id, right_record.id),
                    disposition="admitted",
                    reason_codes=(),
                    normalizations=normalizations,
                    candidate=candidate,
                    outcome=outcome,
                    relationship=relationship,
                )
            )
        except (CrossReferenceValidationError, KeyError, TypeError, ValueError) as error:
            items.append(
                AdmissionItem(
                    index=index,
                    raw_row_sha256=raw_row_sha256,
                    raw_endpoint_ids=raw_endpoint_ids,
                    surface_key=surface_key,
                    normalized_endpoint_ids=(left_record.id, right_record.id),
                    disposition="quarantined",
                    reason_codes=(_validation_reason_code(str(error)),),
                    normalizations=(),
                )
            )

    _apply_collisions(items, existing_relationships=existing_relationships)
    return AdmissionPlan(
        batch_id=batch_id,
        job_id=job_id,
        raw_proposal_sha256=raw_proposal_sha256,
        items=tuple(items),
        diagnostics=tuple(diagnostics),
        observed_surfaces={key: tuple(indexes) for key, indexes in observed_surfaces.items()},
    )


def _canonical_relationship(
    raw: Mapping[str, Any],
    *,
    candidate: DiscoveryCandidate,
    batch_id: str,
    job_id: str,
    model: str,
    input_sha256: str,
    protocol_sha256: str,
    relationship_schema_sha256: str,
    schema_directory: Path,
) -> Relationship:
    relation_type = str(raw["relation_type"])
    direction = str(raw["direction"])
    payload = {
        "schema_version": 1,
        "relationship_id": cross_reference_relationship_id(
            (
                candidate.left_endpoint.record_id,
                candidate.left_endpoint.revision,
                candidate.left_endpoint.record_sha256,
            ),
            (
                candidate.right_endpoint.record_id,
                candidate.right_endpoint.revision,
                candidate.right_endpoint.record_sha256,
            ),
            relation_type=relation_type,
            direction=direction,
        ),
        "revision": 1,
        "candidate_id": candidate.candidate_id,
        "batch_id": batch_id,
        "relation_type": relation_type,
        "direction": direction,
        "left_endpoint": candidate.left_endpoint.to_dict(),
        "right_endpoint": candidate.right_endpoint.to_dict(),
        **{
            key: raw[key]
            for key in (
                "left_evidence_ids",
                "right_evidence_ids",
                "comparison_surface",
                "scope_alignment",
                "assumption_alignment",
                "rationale",
                "qualifications",
            )
        },
        "provenance": {
            "inspection_job_id": job_id,
            "model": model,
            "protocol_sha256": protocol_sha256,
            "schema_sha256": relationship_schema_sha256,
            "input_sha256": input_sha256,
        },
    }
    validate_schema(
        payload,
        schema_directory=schema_directory,
        schema_name="relationship.schema.json",
    )
    return Relationship.from_mapping(payload)


def _normalize_relationship(
    raw: Mapping[str, Any],
    *,
    left_record: Record,
    right_record: Record,
) -> tuple[Record, Record, dict[str, Any], tuple[str, ...]]:
    if left_record.source_id == right_record.source_id:
        raise CrossReferenceValidationError(
            "holistic relationship endpoints must come from different sources"
        )
    normalized = dict(raw)
    if left_record.source_id < right_record.source_id:
        return left_record, right_record, normalized, ()
    left_record, right_record = right_record, left_record
    normalized["left_record_id"] = left_record.id
    normalized["right_record_id"] = right_record.id
    normalized["left_evidence_ids"] = list(raw["right_evidence_ids"])
    normalized["right_evidence_ids"] = list(raw["left_evidence_ids"])
    normalized["direction"] = {
        "symmetric": "symmetric",
        "left_to_right": "right_to_left",
        "right_to_left": "left_to_right",
    }[str(raw["direction"])]
    return left_record, right_record, normalized, ("canonical_endpoint_order",)


def _apply_collisions(
    items: list[AdmissionItem], *, existing_relationships: tuple[Relationship, ...]
) -> None:
    existing_ids = {item.relationship_id for item in existing_relationships}
    existing_pairs = {
        frozenset(
            {
                str(item.payload["left_endpoint"]["record_id"]),
                str(item.payload["right_endpoint"]["record_id"]),
            }
        )
        for item in existing_relationships
    }
    groups: dict[frozenset[str], list[int]] = defaultdict(list)
    for position, item in enumerate(items):
        if item.disposition == "admitted" and item.relationship is not None:
            assert item.normalized_endpoint_ids is not None
            groups[frozenset(item.normalized_endpoint_ids)].append(position)

    for endpoint_pair, positions in groups.items():
        relationships = [items[position].relationship for position in positions]
        assert all(relationship is not None for relationship in relationships)
        typed = [relationship for relationship in relationships if relationship is not None]
        relationship_ids = {relationship.relationship_id for relationship in typed}
        if len(relationship_ids) > 1:
            for position in positions:
                items[position] = replace(
                    items[position],
                    disposition="quarantined",
                    reason_codes=("conflicting_endpoint_collision",),
                )
            continue
        signatures = {_semantic_signature(relationship) for relationship in typed}
        if len(signatures) > 1:
            for position in positions:
                items[position] = replace(
                    items[position],
                    disposition="quarantined",
                    reason_codes=("conflicting_duplicate_payload",),
                )
            continue
        first, *duplicates = positions
        relationship = items[first].relationship
        assert relationship is not None
        if relationship.relationship_id in existing_ids:
            items[first] = replace(
                items[first], disposition="existing_noop", reason_codes=("already_exists",)
            )
        elif endpoint_pair in existing_pairs:
            items[first] = replace(
                items[first],
                disposition="quarantined",
                reason_codes=("conflicts_with_existing_endpoint_pair",),
            )
        for position in duplicates:
            items[position] = replace(
                items[position], disposition="duplicate", reason_codes=("exact_duplicate",)
            )


def _semantic_signature(relationship: Relationship) -> bytes:
    payload = relationship.to_dict()
    return canonical_json_bytes(
        {
            key: payload[key]
            for key in (
                "relation_type",
                "direction",
                "left_endpoint",
                "right_endpoint",
                "left_evidence_ids",
                "right_evidence_ids",
                "comparison_surface",
                "scope_alignment",
                "assumption_alignment",
                "rationale",
                "qualifications",
            )
        }
    )


def _coverage_diagnostics(
    proposal: Mapping[str, Any],
    *,
    records: Mapping[str, Record],
    existing_relationships: tuple[Relationship, ...],
) -> tuple[list[AdmissionDiagnostic], dict[str, list[int]]]:
    diagnostics: list[AdmissionDiagnostic] = []
    existing_by_id = {item.relationship_id: item for item in existing_relationships}
    reviews = proposal.get("existing_relationship_reviews", [])
    review_ids = [
        str(item.get("relationship_id", "")) for item in reviews if isinstance(item, dict)
    ]
    if len(review_ids) != len(set(review_ids)):
        diagnostics.append(
            AdmissionDiagnostic(
                "existing_review_repeated",
                "Existing relationship reviews contain duplicate identifiers.",
            )
        )
    missing_reviews = sorted(set(existing_by_id) - set(review_ids))
    unknown_reviews = sorted(set(review_ids) - set(existing_by_id))
    if missing_reviews or unknown_reviews:
        diagnostics.append(
            AdmissionDiagnostic(
                "existing_review_set_mismatch",
                "Existing review set mismatch: "
                f"missing={missing_reviews}, unknown={unknown_reviews}",
            )
        )

    surfaces: dict[str, Mapping[str, Any]] = {}
    raw_surfaces = proposal.get("coverage_surfaces", [])
    for raw_surface in raw_surfaces:
        if not isinstance(raw_surface, dict):
            continue
        surface_key = str(raw_surface.get("surface_key", ""))
        if surface_key in surfaces:
            diagnostics.append(
                AdmissionDiagnostic(
                    "surface_key_repeated",
                    "Coverage surface key is repeated.",
                    surface_key=surface_key,
                )
            )
            continue
        surfaces[surface_key] = raw_surface
        record_ids = [str(item) for item in raw_surface.get("source_record_ids", [])]
        if len(record_ids) != len(set(record_ids)):
            diagnostics.append(
                AdmissionDiagnostic(
                    "surface_record_repeated",
                    "Coverage surface repeats a source record.",
                    surface_key=surface_key,
                )
            )
        resolved = [records[record_id] for record_id in record_ids if record_id in records]
        unknown_records = sorted(set(record_ids) - set(records))
        if unknown_records:
            diagnostics.append(
                AdmissionDiagnostic(
                    "surface_record_unknown",
                    f"Coverage surface cites unknown records: {unknown_records}",
                    surface_key=surface_key,
                )
            )
        if any(record.record_type != "atom" for record in resolved):
            diagnostics.append(
                AdmissionDiagnostic(
                    "surface_record_not_atom",
                    "Coverage surface includes a non-atom record.",
                    surface_key=surface_key,
                )
            )
        if len({record.source_id for record in resolved}) < 2:
            diagnostics.append(
                AdmissionDiagnostic(
                    "surface_single_source",
                    "Coverage surface does not resolve across two sources.",
                    surface_key=surface_key,
                )
            )
        relationship_ids = [str(item) for item in raw_surface.get("existing_relationship_ids", [])]
        if len(relationship_ids) != len(set(relationship_ids)):
            diagnostics.append(
                AdmissionDiagnostic(
                    "surface_existing_relationship_repeated",
                    "Coverage surface repeats an existing relationship.",
                    surface_key=surface_key,
                )
            )
        unknown_existing = sorted(set(relationship_ids) - set(existing_by_id))
        if unknown_existing:
            diagnostics.append(
                AdmissionDiagnostic(
                    "surface_existing_relationship_unknown",
                    f"Coverage surface cites unknown relationships: {unknown_existing}",
                    surface_key=surface_key,
                )
            )
        disposition = str(raw_surface.get("disposition", ""))
        if any(record.payload.get("connectable") is not True for record in resolved) and (
            disposition != "manual_review_candidate"
        ):
            diagnostics.append(
                AdmissionDiagnostic(
                    "surface_nonconnectable_not_manual",
                    "Non-connectable coverage records are not routed to manual review.",
                    surface_key=surface_key,
                )
            )
        if disposition == "covered_by_existing" and not relationship_ids:
            diagnostics.append(
                AdmissionDiagnostic(
                    "covered_existing_missing_relationship",
                    "Covered-by-existing surface lacks an existing relationship.",
                    surface_key=surface_key,
                )
            )
        elif disposition != "covered_by_existing" and relationship_ids:
            diagnostics.append(
                AdmissionDiagnostic(
                    "non_existing_surface_cites_existing",
                    "A non-existing disposition cites existing relationships.",
                    surface_key=surface_key,
                )
            )
        for relationship_id in relationship_ids:
            existing = existing_by_id.get(relationship_id)
            if existing is None:
                continue
            endpoints = {
                str(existing.payload["left_endpoint"]["record_id"]),
                str(existing.payload["right_endpoint"]["record_id"]),
            }
            if not endpoints.issubset(set(record_ids)):
                diagnostics.append(
                    AdmissionDiagnostic(
                        "surface_omits_existing_endpoints",
                        f"Coverage surface omits endpoints for {relationship_id}.",
                        surface_key=surface_key,
                    )
                )

    observed_surfaces: dict[str, list[int]] = defaultdict(list)
    for index, raw_relationship in enumerate(proposal.get("relationships", [])):
        if not isinstance(raw_relationship, dict):
            continue
        surface_key = str(raw_relationship.get("surface_key", ""))
        observed_surfaces[surface_key].append(index)
        surface = surfaces.get(surface_key)
        if surface is None:
            diagnostics.append(
                AdmissionDiagnostic(
                    "relationship_surface_unknown",
                    "Relationship cites an unknown coverage surface.",
                    surface_key=surface_key,
                    relationship_index=index,
                )
            )
            continue
        if surface.get("disposition") != "covered_by_proposal":
            diagnostics.append(
                AdmissionDiagnostic(
                    "relationship_surface_disposition_mismatch",
                    "Relationship surface is not marked covered by proposal.",
                    surface_key=surface_key,
                    relationship_index=index,
                )
            )
        endpoints = {
            str(raw_relationship.get("left_record_id", "")),
            str(raw_relationship.get("right_record_id", "")),
        }
        if not endpoints.issubset({str(item) for item in surface.get("source_record_ids", [])}):
            diagnostics.append(
                AdmissionDiagnostic(
                    "surface_omits_relationship_endpoints",
                    "Coverage surface omits one or both proposed relationship endpoints.",
                    surface_key=surface_key,
                    relationship_index=index,
                )
            )

    for surface_key, surface in surfaces.items():
        has_relationship = bool(observed_surfaces.get(surface_key))
        disposition = str(surface.get("disposition", ""))
        if disposition == "covered_by_proposal" and not has_relationship:
            diagnostics.append(
                AdmissionDiagnostic(
                    "covered_proposal_missing_relationship",
                    "Covered-by-proposal surface has no proposed relationship.",
                    surface_key=surface_key,
                )
            )
        elif disposition != "covered_by_proposal" and has_relationship:
            diagnostics.append(
                AdmissionDiagnostic(
                    "non_proposal_surface_has_relationship",
                    "A non-proposal surface has proposed relationships.",
                    surface_key=surface_key,
                )
            )
    return diagnostics, observed_surfaces


def _validation_reason_code(message: str) -> str:
    lowered = message.lower()
    if "same source" in lowered or "different sources" in lowered:
        return "same_source_endpoints"
    if "not connectable" in lowered:
        return "endpoint_not_connectable"
    if "evidence does not resolve" in lowered:
        return "evidence_not_resolved"
    if "adjudicative" in lowered:
        return "adjudicative_language"
    if "direction" in lowered or "symmetric relationship" in lowered:
        return "invalid_relationship_direction"
    if "endpoint" in lowered:
        return "invalid_endpoint"
    return "relationship_validation_failed"


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None
