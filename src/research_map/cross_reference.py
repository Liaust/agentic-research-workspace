"""Typed contracts and fail-closed validation for cross-source mapping."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from research_map.ids import (
    canonical_source_pair,
    cross_reference_candidate_id,
    cross_reference_relationship_id,
)
from research_map.records import Record

SYMMETRIC_RELATIONSHIP_TYPES = frozenset(
    {
        "potential_same_referent",
        "potential_same_definition",
        "potential_equivalence",
        "potential_tension",
        "potential_contradiction",
    }
)
DIRECTED_RELATIONSHIP_TYPES = frozenset(
    {"potential_support", "potential_qualification", "potential_dependency"}
)
RELATIONSHIP_TYPES = SYMMETRIC_RELATIONSHIP_TYPES | DIRECTED_RELATIONSHIP_TYPES
RELATIONSHIP_DIRECTIONS = frozenset({"symmetric", "left_to_right", "right_to_left"})
INSPECTION_OUTCOMES = frozenset(
    {
        "relationship",
        "not_usefully_connected",
        "insufficient_extraction",
        "manual_review_candidate",
    }
)

_SOURCE_ID = re.compile(r"^LIB-[0-9]{3}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ADJUDICATIVE_PHRASES = (
    " is correct",
    " is incorrect",
    " is wrong",
    "proves that",
    "disproves",
    "must be rejected",
    "the correct theory",
    "the true account",
    "fills the missing premise",
)


class CrossReferenceValidationError(ValueError):
    """Raised when cross-reference data cannot enter durable state."""


@dataclass(frozen=True, slots=True)
class EndpointRef:
    source_id: str
    record_id: str
    revision: int
    record_sha256: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> EndpointRef:
        source_id = value.get("source_id")
        record_id = value.get("record_id")
        revision = value.get("revision")
        fingerprint = value.get("record_sha256")
        if not isinstance(source_id, str) or not _SOURCE_ID.fullmatch(source_id):
            raise CrossReferenceValidationError("endpoint source_id is invalid")
        if not isinstance(record_id, str) or not record_id.startswith(f"{source_id}:atom:"):
            raise CrossReferenceValidationError("endpoint must identify a source-local atom")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise CrossReferenceValidationError("endpoint revision must be positive")
        if not isinstance(fingerprint, str) or not _SHA256.fullmatch(fingerprint):
            raise CrossReferenceValidationError("endpoint record fingerprint is invalid")
        return cls(source_id, record_id, revision, fingerprint)

    @classmethod
    def from_record(cls, record: Record) -> EndpointRef:
        if record.record_type != "atom":
            raise CrossReferenceValidationError("cross-reference endpoints must be atoms")
        return cls(
            source_id=record.source_id,
            record_id=record.id,
            revision=record.revision,
            record_sha256=record_fingerprint(record.payload),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "record_id": self.record_id,
            "revision": self.revision,
            "record_sha256": self.record_sha256,
        }


@dataclass(frozen=True, slots=True)
class DiscoveryCandidate:
    candidate_id: str
    batch_id: str
    source_pair: tuple[str, str]
    left_endpoint: EndpointRef
    right_endpoint: EndpointRef
    comparison_surface: str
    discovery_job_id: str
    schema_version: int = 1

    @classmethod
    def create(
        cls,
        *,
        batch_id: str,
        left_endpoint: EndpointRef,
        right_endpoint: EndpointRef,
        comparison_surface: str,
        discovery_job_id: str,
    ) -> DiscoveryCandidate:
        if not batch_id.strip() or not discovery_job_id.strip():
            raise CrossReferenceValidationError("candidate batch and job IDs are required")
        if not comparison_surface.strip():
            raise CrossReferenceValidationError("candidate comparison surface is required")
        _reject_adjudicative_text({"comparison_surface": comparison_surface})
        pair = canonical_source_pair(left_endpoint.source_id, right_endpoint.source_id)
        if left_endpoint.source_id != pair[0]:
            left_endpoint, right_endpoint = right_endpoint, left_endpoint
        identifier = cross_reference_candidate_id(
            batch_id,
            (
                left_endpoint.record_id,
                left_endpoint.revision,
                left_endpoint.record_sha256,
            ),
            (
                right_endpoint.record_id,
                right_endpoint.revision,
                right_endpoint.record_sha256,
            ),
        )
        return cls(
            candidate_id=identifier,
            batch_id=batch_id,
            source_pair=pair,
            left_endpoint=left_endpoint,
            right_endpoint=right_endpoint,
            comparison_surface=comparison_surface.strip(),
            discovery_job_id=discovery_job_id,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> DiscoveryCandidate:
        if value.get("schema_version") != 1:
            raise CrossReferenceValidationError("candidate schema_version must be 1")
        pair = value.get("source_pair")
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or not all(isinstance(source, str) for source in pair)
        ):
            raise CrossReferenceValidationError("candidate source_pair is invalid")
        left_raw = value.get("left_endpoint")
        right_raw = value.get("right_endpoint")
        if not isinstance(left_raw, dict) or not isinstance(right_raw, dict):
            raise CrossReferenceValidationError("candidate endpoints are required")
        candidate = cls.create(
            batch_id=str(value.get("batch_id", "")),
            left_endpoint=EndpointRef.from_mapping(left_raw),
            right_endpoint=EndpointRef.from_mapping(right_raw),
            comparison_surface=str(value.get("comparison_surface", "")),
            discovery_job_id=str(value.get("discovery_job_id", "")),
        )
        if list(candidate.source_pair) != pair:
            raise CrossReferenceValidationError("candidate source_pair is not canonical")
        if value.get("candidate_id") != candidate.candidate_id:
            raise CrossReferenceValidationError("candidate ID is not deterministic")
        return candidate

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "candidate_id": self.candidate_id,
            "batch_id": self.batch_id,
            "source_pair": list(self.source_pair),
            "left_endpoint": self.left_endpoint.to_dict(),
            "right_endpoint": self.right_endpoint.to_dict(),
            "comparison_surface": self.comparison_surface,
            "discovery_job_id": self.discovery_job_id,
        }


@dataclass(frozen=True, slots=True)
class Relationship:
    payload: dict[str, Any]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> Relationship:
        payload = copy.deepcopy(dict(value))
        if payload.get("schema_version") != 1:
            raise CrossReferenceValidationError("relationship schema_version must be 1")
        relation_type = payload.get("relation_type")
        direction = payload.get("direction")
        if relation_type not in RELATIONSHIP_TYPES:
            raise CrossReferenceValidationError("relationship type is not allowed")
        if direction not in RELATIONSHIP_DIRECTIONS:
            raise CrossReferenceValidationError("relationship direction is not allowed")
        if relation_type in SYMMETRIC_RELATIONSHIP_TYPES and direction != "symmetric":
            raise CrossReferenceValidationError("symmetric relationship has invalid direction")
        if relation_type in DIRECTED_RELATIONSHIP_TYPES and direction == "symmetric":
            raise CrossReferenceValidationError("directed relationship requires a direction")
        left_raw = payload.get("left_endpoint")
        right_raw = payload.get("right_endpoint")
        if not isinstance(left_raw, dict) or not isinstance(right_raw, dict):
            raise CrossReferenceValidationError("relationship endpoints are required")
        left = EndpointRef.from_mapping(left_raw)
        right = EndpointRef.from_mapping(right_raw)
        canonical_source_pair(left.source_id, right.source_id)
        expected_id = cross_reference_relationship_id(
            (left.record_id, left.revision, left.record_sha256),
            (right.record_id, right.revision, right.record_sha256),
            relation_type=str(relation_type),
            direction=str(direction),
        )
        if payload.get("relationship_id") != expected_id:
            raise CrossReferenceValidationError("relationship ID is not deterministic")
        revision = payload.get("revision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise CrossReferenceValidationError("relationship revision must be positive")
        return cls(payload)

    @property
    def relationship_id(self) -> str:
        return str(self.payload["relationship_id"])

    @property
    def candidate_id(self) -> str:
        return str(self.payload["candidate_id"])

    @property
    def batch_id(self) -> str:
        return str(self.payload["batch_id"])

    @property
    def relation_type(self) -> str:
        return str(self.payload["relation_type"])

    @property
    def revision(self) -> int:
        return int(self.payload["revision"])

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self.payload)


def record_fingerprint(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def validate_schema(
    payload: Mapping[str, Any], *, schema_directory: Path, schema_name: str
) -> None:
    try:
        schema: Any = json.loads((schema_directory / schema_name).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CrossReferenceValidationError(f"cannot load {schema_name}: {error}") from error
    errors = sorted(
        Draft202012Validator(schema).iter_errors(dict(payload)),
        key=lambda item: list(item.path),
    )
    if errors:
        location = ".".join(str(part) for part in errors[0].path) or "record"
        raise CrossReferenceValidationError(f"{location}: {errors[0].message}")


def validate_candidate_endpoints(
    candidate: DiscoveryCandidate,
    *,
    records: Mapping[str, Record | Mapping[str, Any]],
) -> None:
    for endpoint in (candidate.left_endpoint, candidate.right_endpoint):
        value = records.get(endpoint.record_id)
        if value is None:
            raise CrossReferenceValidationError(
                f"candidate endpoint does not resolve: {endpoint.record_id}"
            )
        record = value if isinstance(value, Record) else Record.from_mapping(value)
        if record.source_id != endpoint.source_id or record.revision != endpoint.revision:
            raise CrossReferenceValidationError(
                f"candidate endpoint revision is stale: {endpoint.record_id}"
            )
        if record_fingerprint(record.payload) != endpoint.record_sha256:
            raise CrossReferenceValidationError(
                f"candidate endpoint fingerprint is stale: {endpoint.record_id}"
            )
        if record.record_type != "atom" or record.payload.get("connectable") is not True:
            raise CrossReferenceValidationError(
                f"candidate endpoint is not connectable: {endpoint.record_id}"
            )


def validate_inspection_outcome(
    payload: Mapping[str, Any],
    *,
    candidate: DiscoveryCandidate,
    records: Mapping[str, Record | Mapping[str, Any]],
    evidence_ids_by_source: Mapping[str, frozenset[str] | set[str]],
    existing_relationship_ids: frozenset[str] | set[str] = frozenset(),
) -> Relationship | None:
    outcome = payload.get("outcome")
    if outcome not in INSPECTION_OUTCOMES:
        raise CrossReferenceValidationError("inspection outcome is not allowed")
    if payload.get("candidate_id") != candidate.candidate_id:
        raise CrossReferenceValidationError("inspection candidate identity does not match")
    validate_candidate_endpoints(candidate, records=records)
    relationship_raw = payload.get("relationship")
    if outcome != "relationship":
        if relationship_raw is not None:
            raise CrossReferenceValidationError(
                "non-relationship outcome cannot carry a relationship"
            )
        if (
            outcome == "insufficient_extraction"
            and not str(payload.get("disposition_reason", "")).strip()
        ):
            raise CrossReferenceValidationError(
                "insufficient extraction requires an exact deficiency"
            )
        _reject_adjudicative_text({"rationale": payload.get("disposition_reason", "")})
        return None
    if not isinstance(relationship_raw, dict):
        raise CrossReferenceValidationError("relationship outcome requires a relationship")
    relationship = Relationship.from_mapping(relationship_raw)
    if relationship.candidate_id != candidate.candidate_id:
        raise CrossReferenceValidationError("relationship candidate identity does not match")
    if relationship.batch_id != candidate.batch_id:
        raise CrossReferenceValidationError("relationship batch identity does not match")
    if relationship.relationship_id in existing_relationship_ids:
        raise CrossReferenceValidationError("relationship already exists")
    left = EndpointRef.from_mapping(relationship.payload["left_endpoint"])
    right = EndpointRef.from_mapping(relationship.payload["right_endpoint"])
    if left != candidate.left_endpoint or right != candidate.right_endpoint:
        raise CrossReferenceValidationError("relationship endpoints differ from candidate")
    for field, endpoint in (
        ("left_evidence_ids", left),
        ("right_evidence_ids", right),
    ):
        evidence_ids = relationship.payload.get(field)
        if not isinstance(evidence_ids, list) or not evidence_ids:
            raise CrossReferenceValidationError(f"relationship {field} must not be empty")
        known = evidence_ids_by_source.get(endpoint.source_id, frozenset())
        unknown = sorted(set(str(value) for value in evidence_ids) - set(known))
        if unknown:
            raise CrossReferenceValidationError(
                f"relationship evidence does not resolve for {endpoint.source_id}: {unknown}"
            )
    _reject_adjudicative_text(relationship.payload)
    return relationship


def validate_relationship_against_records(
    relationship: Relationship,
    *,
    records: Mapping[str, Record | Mapping[str, Any]],
    evidence_ids_by_source: Mapping[str, frozenset[str] | set[str]],
) -> None:
    """Revalidate one canonical relationship without trusting operational state."""

    left = EndpointRef.from_mapping(relationship.payload["left_endpoint"])
    right = EndpointRef.from_mapping(relationship.payload["right_endpoint"])
    canonical_source_pair(left.source_id, right.source_id)
    for endpoint in (left, right):
        raw_record = records.get(endpoint.record_id)
        if raw_record is None:
            raise CrossReferenceValidationError(
                f"relationship endpoint does not resolve: {endpoint.record_id}"
            )
        record = raw_record if isinstance(raw_record, Record) else Record.from_mapping(raw_record)
        if record.revision != endpoint.revision:
            raise CrossReferenceValidationError(
                f"relationship endpoint revision is stale: {endpoint.record_id}"
            )
        if record_fingerprint(record.payload) != endpoint.record_sha256:
            raise CrossReferenceValidationError(
                f"relationship endpoint fingerprint is stale: {endpoint.record_id}"
            )
        if record.record_type != "atom" or record.payload.get("connectable") is not True:
            raise CrossReferenceValidationError(
                f"relationship endpoint is not connectable: {endpoint.record_id}"
            )
    for field, endpoint in (
        ("left_evidence_ids", left),
        ("right_evidence_ids", right),
    ):
        evidence_ids = relationship.payload.get(field)
        if not isinstance(evidence_ids, list) or not evidence_ids:
            raise CrossReferenceValidationError(f"relationship {field} must not be empty")
        known = evidence_ids_by_source.get(endpoint.source_id, frozenset())
        unknown = sorted(set(str(value) for value in evidence_ids) - set(known))
        if unknown:
            raise CrossReferenceValidationError(
                f"relationship evidence does not resolve for {endpoint.source_id}: {unknown}"
            )
    _reject_adjudicative_text(relationship.payload)


def _reject_adjudicative_text(payload: Mapping[str, Any]) -> None:
    fields = (
        "comparison_surface",
        "scope_alignment",
        "assumption_alignment",
        "rationale",
    )
    text = " ".join(str(payload.get(field, "")) for field in fields).lower()
    for phrase in _ADJUDICATIVE_PHRASES:
        if phrase in text:
            raise CrossReferenceValidationError(
                f"relationship contains adjudicative language: {phrase.strip()}"
            )


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"))
