"""Production-neutral validation for multi-surface cross-reference proposals."""

from __future__ import annotations

import itertools
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from research_map.cross_reference import (
    DIRECTED_RELATIONSHIP_TYPES,
    RELATIONSHIP_DIRECTIONS,
    RELATIONSHIP_TYPES,
    SYMMETRIC_RELATIONSHIP_TYPES,
    CrossReferenceValidationError,
)
from research_map.records import Record

PAIR_DISPOSITIONS = frozenset(
    {"surfaces_found", "no_useful_relationship", "manual_review_candidate"}
)
SURFACE_DISPOSITIONS = frozenset(
    {"relationship_proposed", "ontology_gap_candidate", "no_allowed_relationship"}
)
COMPARISON_DIMENSIONS = frozenset(
    {
        "definition",
        "assumption",
        "formalism",
        "method",
        "model_architecture",
        "result",
        "quantitative_finding",
        "limitation",
        "open_question",
        "objection_response",
        "other",
    }
)
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


@dataclass(frozen=True, slots=True)
class MultiSurfaceProposalDiagnostics:
    """Deterministic proposal accounting retained by operational receipts."""

    expected_pair_count: int
    comparison_surface_count: int
    relationship_count: int
    pair_dispositions: dict[str, int]
    surface_dispositions: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "expected_pair_count": self.expected_pair_count,
            "comparison_surface_count": self.comparison_surface_count,
            "relationship_count": self.relationship_count,
            "pair_dispositions": dict(self.pair_dispositions),
            "surface_dispositions": dict(self.surface_dispositions),
        }


ValidationScope = Literal["fatal", "surface", "relationship"]


@dataclass(frozen=True, slots=True)
class MultiSurfaceValidationFinding:
    """One stable, path-addressed deterministic proposal defect."""

    reason_code: str
    path: str
    scope: ValidationScope
    message: str
    pair_key: str | None = None
    surface_key: str | None = None
    relationship_index: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason_code": self.reason_code,
            "path": self.path,
            "scope": self.scope,
            "message": self.message,
            "pair_key": self.pair_key,
            "surface_key": self.surface_key,
            "relationship_index": self.relationship_index,
        }


@dataclass(frozen=True, slots=True)
class MultiSurfaceValidationReport:
    """Exhaustive fatal/local findings and deterministic safe-row indexes."""

    batch_id: str
    source_ids: tuple[str, ...]
    expected_pair_count: int
    findings: tuple[MultiSurfaceValidationFinding, ...]
    accepted_surface_indexes: tuple[int, ...]
    rejected_surface_indexes: tuple[int, ...]
    accepted_relationship_indexes: tuple[int, ...]
    rejected_relationship_indexes: tuple[int, ...]

    @property
    def has_fatal_findings(self) -> bool:
        return any(finding.scope == "fatal" for finding in self.findings)

    def to_dict(self) -> dict[str, Any]:
        counts = Counter(finding.scope for finding in self.findings)
        return {
            "schema_version": "1.0",
            "kind": "multi_surface_validation_report",
            "batch_id": self.batch_id,
            "source_ids": list(self.source_ids),
            "expected_pair_count": self.expected_pair_count,
            "has_fatal_findings": self.has_fatal_findings,
            "finding_counts": {
                "fatal": counts["fatal"],
                "surface": counts["surface"],
                "relationship": counts["relationship"],
            },
            "findings": [finding.to_dict() for finding in self.findings],
            "projection": {
                "accepted_surface_indexes": list(self.accepted_surface_indexes),
                "rejected_surface_indexes": list(self.rejected_surface_indexes),
                "accepted_relationship_indexes": list(self.accepted_relationship_indexes),
                "rejected_relationship_indexes": list(self.rejected_relationship_indexes),
            },
        }


def diagnose_multi_surface_proposal(
    payload: Mapping[str, Any],
    *,
    batch_id: str,
    source_ids: Sequence[str],
    records: Mapping[str, Record],
    evidence_ids_by_source: Mapping[str, frozenset[str] | set[str]],
    expected_pairs: Sequence[tuple[str, str]] | None = None,
) -> MultiSurfaceValidationReport:
    """Diagnose one proposal without turning row-local defects into shard failure."""

    sources = tuple(source_ids)
    findings: list[MultiSurfaceValidationFinding] = []

    def add(
        reason_code: str,
        path: str,
        scope: ValidationScope,
        message: str,
        *,
        pair_key: str | None = None,
        surface_key: str | None = None,
        relationship_index: int | None = None,
    ) -> None:
        finding = MultiSurfaceValidationFinding(
            reason_code=reason_code,
            path=path,
            scope=scope,
            message=message,
            pair_key=pair_key,
            surface_key=surface_key,
            relationship_index=relationship_index,
        )
        if finding not in findings:
            findings.append(finding)

    if len(sources) < 2 or len(set(sources)) != len(sources):
        add(
            "invalid_source_scope",
            "$.source_ids",
            "fatal",
            "multi-surface validation requires at least two unique sources",
        )
    if payload.get("schema_version") != 1:
        add(
            "proposal_schema_version_changed",
            "$.schema_version",
            "fatal",
            "multi-surface proposal schema version changed",
        )
    if payload.get("batch_id") != batch_id:
        add(
            "proposal_batch_identity_changed",
            "$.batch_id",
            "fatal",
            "multi-surface proposal batch identity changed",
        )
    if payload.get("source_ids") != list(sources):
        add(
            "proposal_source_identities_changed",
            "$.source_ids",
            "fatal",
            "multi-surface proposal source identities changed",
        )

    raw_ledgers = payload.get("pair_coverage")
    raw_surfaces = payload.get("comparison_surfaces")
    raw_relationships = payload.get("relationships")
    for value, path, code, label in (
        (raw_ledgers, "$.pair_coverage", "pair_coverage_not_array", "pair ledgers"),
        (
            raw_surfaces,
            "$.comparison_surfaces",
            "comparison_surfaces_not_array",
            "comparison surfaces",
        ),
        (
            raw_relationships,
            "$.relationships",
            "relationships_not_array",
            "relationships",
        ),
    ):
        if not isinstance(value, list):
            add(code, path, "fatal", f"multi-surface {label} must be an array")

    owned_pairs: tuple[tuple[str, str], ...]
    if expected_pairs is None:
        owned_pairs = tuple(itertools.combinations(sources, 2))
    else:
        owned_pairs = tuple(expected_pairs)
        if len(owned_pairs) != len(set(owned_pairs)) or any(
            len(pair) != 2 or pair[0] >= pair[1] or not set(pair).issubset(sources)
            for pair in owned_pairs
        ):
            add(
                "invalid_owned_pair_scope",
                "$.pair_coverage",
                "fatal",
                "declared owned pairs must be unique canonical pairs inside the source scope",
            )
            owned_pairs = ()
        else:
            owned_pairs = tuple(sorted(owned_pairs))
    expected_by_key = {_pair_key(*pair): pair for pair in owned_pairs}

    ledger_by_key: dict[str, Mapping[str, Any]] = {}
    listed_surface_owner: dict[str, str] = {}
    if isinstance(raw_ledgers, list):
        for index, value in enumerate(raw_ledgers):
            path = f"$.pair_coverage[{index}]"
            if not isinstance(value, dict):
                add(
                    "pair_ledger_not_object",
                    path,
                    "fatal",
                    "multi-surface pair ledger must be an object",
                )
                continue
            ledger = value
            pair_key_value = ledger.get("pair_key")
            pair_key = pair_key_value if isinstance(pair_key_value, str) else None
            if pair_key is None or not pair_key.strip():
                add(
                    "pair_ledger_key_invalid",
                    f"{path}.pair_key",
                    "fatal",
                    "pair ledger key must be non-empty",
                )
                continue
            if pair_key not in expected_by_key:
                add(
                    "pair_ledger_outside_owned_scope",
                    f"{path}.pair_key",
                    "fatal",
                    "pair ledger key is outside the owned pair scope",
                    pair_key=pair_key,
                )
                continue
            if pair_key in ledger_by_key:
                add(
                    "pair_ledger_duplicate",
                    f"{path}.pair_key",
                    "fatal",
                    "pair ledger keys must be unique",
                    pair_key=pair_key,
                )
                continue
            ledger_by_key[pair_key] = ledger
            if ledger.get("source_ids") != list(expected_by_key[pair_key]):
                add(
                    "pair_ledger_source_identities_changed",
                    f"{path}.source_ids",
                    "fatal",
                    "pair ledger source identities do not match its canonical pair",
                    pair_key=pair_key,
                )
            surface_keys_value = ledger.get("comparison_surface_keys")
            surface_keys: list[str] = []
            if (
                not isinstance(surface_keys_value, list)
                or not all(isinstance(item, str) and item.strip() for item in surface_keys_value)
                or len(surface_keys_value) != len(set(surface_keys_value))
            ):
                add(
                    "pair_ledger_surface_keys_invalid",
                    f"{path}.comparison_surface_keys",
                    "fatal",
                    "pair ledger surface keys must contain unique non-empty strings",
                    pair_key=pair_key,
                )
            else:
                surface_keys = surface_keys_value
            disposition = ledger.get("disposition")
            if disposition not in PAIR_DISPOSITIONS:
                add(
                    "pair_ledger_disposition_invalid",
                    f"{path}.disposition",
                    "fatal",
                    "pair ledger disposition is invalid",
                    pair_key=pair_key,
                )
            elif disposition == "surfaces_found" and not surface_keys:
                add(
                    "pair_ledger_surfaces_missing",
                    f"{path}.comparison_surface_keys",
                    "fatal",
                    "surfaces_found pair ledger must cite at least one surface",
                    pair_key=pair_key,
                )
            elif disposition != "surfaces_found" and surface_keys:
                add(
                    "pair_ledger_surfaces_disallowed",
                    f"{path}.comparison_surface_keys",
                    "fatal",
                    "only surfaces_found pair ledgers may cite comparison surfaces",
                    pair_key=pair_key,
                )
            if (
                not isinstance(ledger.get("rationale"), str)
                or not str(ledger.get("rationale", "")).strip()
            ):
                add(
                    "pair_ledger_rationale_empty",
                    f"{path}.rationale",
                    "fatal",
                    "pair ledger rationale must be non-empty",
                    pair_key=pair_key,
                )
            for surface_key in surface_keys:
                previous_owner = listed_surface_owner.get(surface_key)
                if previous_owner is not None:
                    add(
                        "pair_ledger_surface_owner_duplicate",
                        f"{path}.comparison_surface_keys",
                        "fatal",
                        "comparison surface must appear in exactly one pair ledger",
                        pair_key=pair_key,
                        surface_key=surface_key,
                    )
                else:
                    listed_surface_owner[surface_key] = pair_key
        missing_pairs = sorted(set(expected_by_key) - set(ledger_by_key))
        if missing_pairs or len(raw_ledgers) != len(owned_pairs):
            add(
                "owned_pair_accounting_incomplete",
                "$.pair_coverage",
                "fatal",
                "multi-surface proposal did not account for every source pair exactly once",
            )

    if any(finding.scope == "fatal" for finding in findings):
        return MultiSurfaceValidationReport(
            batch_id=batch_id,
            source_ids=sources,
            expected_pair_count=len(owned_pairs),
            findings=tuple(findings),
            accepted_surface_indexes=(),
            rejected_surface_indexes=tuple(
                range(len(raw_surfaces)) if isinstance(raw_surfaces, list) else ()
            ),
            accepted_relationship_indexes=(),
            rejected_relationship_indexes=tuple(
                range(len(raw_relationships)) if isinstance(raw_relationships, list) else ()
            ),
        )

    assert isinstance(raw_surfaces, list)
    assert isinstance(raw_relationships, list)
    surface_by_key: dict[str, tuple[int, Mapping[str, Any]]] = {}
    invalid_surface_indexes: set[int] = set()
    for index, value in enumerate(raw_surfaces):
        path = f"$.comparison_surfaces[{index}]"
        if not isinstance(value, dict):
            add(
                "comparison_surface_not_object",
                path,
                "surface",
                "multi-surface comparison surface must be an object",
            )
            invalid_surface_indexes.add(index)
            continue
        surface = value
        surface_key_value = surface.get("surface_key")
        current_surface_key = surface_key_value if isinstance(surface_key_value, str) else None
        if current_surface_key is None or not current_surface_key.strip():
            add(
                "comparison_surface_key_invalid",
                f"{path}.surface_key",
                "surface",
                "surface key must be non-empty",
            )
            invalid_surface_indexes.add(index)
            continue
        surface_key = current_surface_key
        if surface_key in surface_by_key:
            add(
                "comparison_surface_key_duplicate",
                f"{path}.surface_key",
                "surface",
                "comparison surface keys must be unique",
                surface_key=surface_key,
            )
            invalid_surface_indexes.update((index, surface_by_key[surface_key][0]))
        else:
            surface_by_key[surface_key] = (index, surface)
        owner_key = listed_surface_owner.get(surface_key)
        if owner_key is None:
            add(
                "comparison_surface_absent_from_pair_ledger",
                f"{path}.surface_key",
                "surface",
                "comparison surface is absent from the pair ledger",
                surface_key=surface_key,
            )
            invalid_surface_indexes.add(index)
            continue
        pair = expected_by_key[owner_key]
        if surface.get("source_ids") != list(pair):
            add(
                "comparison_surface_source_identities_changed",
                f"{path}.source_ids",
                "surface",
                "comparison surface source identities do not match its pair ledger",
                pair_key=owner_key,
                surface_key=surface_key,
            )
            invalid_surface_indexes.add(index)
        left = _diagnose_endpoint(
            surface.get("left_record_id"),
            path=f"{path}.left_record_id",
            records=records,
            source_ids=sources,
            add=add,
            pair_key=owner_key,
            surface_key=surface_key,
        )
        right = _diagnose_endpoint(
            surface.get("right_record_id"),
            path=f"{path}.right_record_id",
            records=records,
            source_ids=sources,
            add=add,
            pair_key=owner_key,
            surface_key=surface_key,
        )
        if left is None or right is None:
            invalid_surface_indexes.add(index)
        else:
            if (left.source_id, right.source_id) != pair:
                add(
                    "comparison_surface_endpoint_order_invalid",
                    path,
                    "surface",
                    "comparison surface endpoints do not match canonical pair order",
                    pair_key=owner_key,
                    surface_key=surface_key,
                )
                invalid_surface_indexes.add(index)
            for side, record, value_name in (
                ("left", left, "left_evidence_ids"),
                ("right", right, "right_evidence_ids"),
            ):
                if not _evidence_resolves(
                    surface.get(value_name),
                    source_id=record.source_id,
                    known=evidence_ids_by_source,
                ):
                    add(
                        "comparison_surface_evidence_invalid",
                        f"{path}.{value_name}",
                        "surface",
                        f"{side} evidence does not resolve on its endpoint side",
                        pair_key=owner_key,
                        surface_key=surface_key,
                    )
                    invalid_surface_indexes.add(index)
        if surface.get("comparison_dimension") not in COMPARISON_DIMENSIONS:
            add(
                "comparison_surface_dimension_invalid",
                f"{path}.comparison_dimension",
                "surface",
                "comparison surface dimension is invalid",
                pair_key=owner_key,
                surface_key=surface_key,
            )
            invalid_surface_indexes.add(index)
        if surface.get("disposition") not in SURFACE_DISPOSITIONS:
            add(
                "comparison_surface_disposition_invalid",
                f"{path}.disposition",
                "surface",
                "comparison surface disposition is invalid",
                pair_key=owner_key,
                surface_key=surface_key,
            )
            invalid_surface_indexes.add(index)
        for field in ("precise_connection", "scope_alignment", "assumption_alignment", "rationale"):
            if not isinstance(surface.get(field), str) or not str(surface.get(field, "")).strip():
                add(
                    "comparison_surface_text_empty",
                    f"{path}.{field}",
                    "surface",
                    f"comparison surface {field} must be non-empty",
                    pair_key=owner_key,
                    surface_key=surface_key,
                )
                invalid_surface_indexes.add(index)
        if not _is_unique_string_list(surface.get("qualifications")):
            add(
                "comparison_surface_qualifications_invalid",
                f"{path}.qualifications",
                "surface",
                "surface qualifications must contain unique non-empty strings",
                pair_key=owner_key,
                surface_key=surface_key,
            )
            invalid_surface_indexes.add(index)
        matched = _adjudicative_phrase(surface)
        if matched is not None:
            add(
                "comparison_surface_adjudicative_text",
                path,
                "surface",
                f"comparison surface crosses the non-adjudication boundary: {matched}",
                pair_key=owner_key,
                surface_key=surface_key,
            )
            invalid_surface_indexes.add(index)

    invalid_relationship_indexes: set[int] = set()
    relationship_indexes_by_surface: dict[str, list[int]] = {}
    endpoint_owner: dict[tuple[str, str], int] = {}
    for index, value in enumerate(raw_relationships):
        path = f"$.relationships[{index}]"
        if not isinstance(value, dict):
            add(
                "relationship_not_object",
                path,
                "relationship",
                "multi-surface relationship must be an object",
                relationship_index=index,
            )
            invalid_relationship_indexes.add(index)
            continue
        relationship = value
        surface_key_value = relationship.get("surface_key")
        surface_key = surface_key_value if isinstance(surface_key_value, str) else ""
        entry = surface_by_key.get(surface_key)
        if entry is None:
            add(
                "relationship_surface_unknown",
                f"{path}.surface_key",
                "relationship",
                "relationship cites an unknown comparison surface",
                surface_key=surface_key or None,
                relationship_index=index,
            )
            invalid_relationship_indexes.add(index)
        else:
            surface_index, linked_surface = entry
            relationship_indexes_by_surface.setdefault(surface_key, []).append(index)
            if surface_index in invalid_surface_indexes:
                add(
                    "relationship_surface_invalid",
                    f"{path}.surface_key",
                    "relationship",
                    "relationship cites an invalid comparison surface",
                    surface_key=surface_key,
                    relationship_index=index,
                )
                invalid_relationship_indexes.add(index)
            for field in (
                "left_record_id",
                "right_record_id",
                "left_evidence_ids",
                "right_evidence_ids",
            ):
                if relationship.get(field) != linked_surface.get(field):
                    add(
                        "relationship_surface_binding_changed",
                        f"{path}.{field}",
                        "relationship",
                        "relationship endpoint or evidence differs from its comparison surface",
                        surface_key=surface_key,
                        relationship_index=index,
                    )
                    invalid_relationship_indexes.add(index)
        left_id = relationship.get("left_record_id")
        right_id = relationship.get("right_record_id")
        if not isinstance(left_id, str) or not isinstance(right_id, str) or left_id == right_id:
            add(
                "relationship_endpoints_invalid",
                path,
                "relationship",
                "relationship endpoints must be two distinct record identifiers",
                surface_key=surface_key or None,
                relationship_index=index,
            )
            invalid_relationship_indexes.add(index)
        else:
            endpoint_pair = (min(left_id, right_id), max(left_id, right_id))
            previous_index = endpoint_owner.get(endpoint_pair)
            if previous_index is not None:
                add(
                    "relationship_endpoint_pair_duplicate",
                    path,
                    "relationship",
                    "multi-surface proposal repeated an unordered endpoint pair",
                    surface_key=surface_key or None,
                    relationship_index=index,
                )
                invalid_relationship_indexes.add(index)
            else:
                endpoint_owner[endpoint_pair] = index
        relation_type = relationship.get("relation_type")
        direction = relationship.get("direction")
        if relation_type not in RELATIONSHIP_TYPES:
            add(
                "relationship_type_invalid",
                f"{path}.relation_type",
                "relationship",
                "multi-surface relationship type is invalid",
                surface_key=surface_key or None,
                relationship_index=index,
            )
            invalid_relationship_indexes.add(index)
        if direction not in RELATIONSHIP_DIRECTIONS:
            add(
                "relationship_direction_invalid",
                f"{path}.direction",
                "relationship",
                "multi-surface relationship direction is invalid",
                surface_key=surface_key or None,
                relationship_index=index,
            )
            invalid_relationship_indexes.add(index)
        elif relation_type in SYMMETRIC_RELATIONSHIP_TYPES and direction != "symmetric":
            add(
                "relationship_symmetric_direction_invalid",
                f"{path}.direction",
                "relationship",
                "multi-surface symmetric type has invalid direction",
                surface_key=surface_key or None,
                relationship_index=index,
            )
            invalid_relationship_indexes.add(index)
        elif relation_type in DIRECTED_RELATIONSHIP_TYPES and direction == "symmetric":
            add(
                "relationship_directed_direction_invalid",
                f"{path}.direction",
                "relationship",
                "multi-surface directed type requires a direction",
                surface_key=surface_key or None,
                relationship_index=index,
            )
            invalid_relationship_indexes.add(index)
        for field in ("comparison_surface", "scope_alignment", "assumption_alignment", "rationale"):
            if (
                not isinstance(relationship.get(field), str)
                or not str(relationship.get(field, "")).strip()
            ):
                add(
                    "relationship_text_empty",
                    f"{path}.{field}",
                    "relationship",
                    f"relationship {field} must be non-empty",
                    surface_key=surface_key or None,
                    relationship_index=index,
                )
                invalid_relationship_indexes.add(index)
        if not _is_unique_string_list(relationship.get("qualifications")):
            add(
                "relationship_qualifications_invalid",
                f"{path}.qualifications",
                "relationship",
                "relationship qualifications must contain unique non-empty strings",
                surface_key=surface_key or None,
                relationship_index=index,
            )
            invalid_relationship_indexes.add(index)
        matched = _adjudicative_phrase(relationship)
        if matched is not None:
            add(
                "relationship_adjudicative_text",
                path,
                "relationship",
                f"relationship crosses the non-adjudication boundary: {matched}",
                surface_key=surface_key or None,
                relationship_index=index,
            )
            invalid_relationship_indexes.add(index)

    for surface_key, (surface_index, final_surface) in surface_by_key.items():
        indexes = relationship_indexes_by_surface.get(surface_key, [])
        if len(indexes) > 1:
            for relationship_index in indexes:
                add(
                    "relationship_surface_cardinality_exceeded",
                    f"$.relationships[{relationship_index}].surface_key",
                    "relationship",
                    "comparison surface may support at most one relationship",
                    surface_key=surface_key,
                    relationship_index=relationship_index,
                )
                invalid_relationship_indexes.add(relationship_index)
        proposed = final_surface.get("disposition") == "relationship_proposed"
        safe_relationship_count = sum(
            index not in invalid_relationship_indexes for index in indexes
        )
        if proposed != (safe_relationship_count == 1):
            add(
                "comparison_surface_relationship_count_mismatch",
                f"$.comparison_surfaces[{surface_index}].disposition",
                "surface",
                "comparison surface disposition does not match proposed relationships",
                surface_key=surface_key,
            )
            invalid_surface_indexes.add(surface_index)
            invalid_relationship_indexes.update(indexes)

    accepted_surface_indexes = tuple(
        index for index in range(len(raw_surfaces)) if index not in invalid_surface_indexes
    )
    accepted_relationship_indexes = tuple(
        index
        for index in range(len(raw_relationships))
        if index not in invalid_relationship_indexes
    )
    return MultiSurfaceValidationReport(
        batch_id=batch_id,
        source_ids=sources,
        expected_pair_count=len(owned_pairs),
        findings=tuple(findings),
        accepted_surface_indexes=accepted_surface_indexes,
        rejected_surface_indexes=tuple(sorted(invalid_surface_indexes)),
        accepted_relationship_indexes=accepted_relationship_indexes,
        rejected_relationship_indexes=tuple(sorted(invalid_relationship_indexes)),
    )


def validate_multi_surface_proposal(
    payload: Mapping[str, Any],
    *,
    batch_id: str,
    source_ids: Sequence[str],
    records: Mapping[str, Record],
    evidence_ids_by_source: Mapping[str, frozenset[str] | set[str]],
    expected_pairs: Sequence[tuple[str, str]] | None = None,
) -> MultiSurfaceProposalDiagnostics:
    """Validate pair accounting, comparison surfaces, and potential relationships."""

    sources = tuple(source_ids)
    if len(sources) < 2 or len(set(sources)) != len(sources):
        raise CrossReferenceValidationError(
            "multi-surface validation requires at least two unique sources"
        )
    if payload.get("schema_version") != 1 or payload.get("batch_id") != batch_id:
        raise CrossReferenceValidationError("multi-surface proposal identity changed")
    if payload.get("source_ids") != list(sources):
        raise CrossReferenceValidationError("multi-surface proposal source identities changed")

    raw_ledgers = _list(payload.get("pair_coverage"), label="multi-surface pair ledgers")
    raw_surfaces = _list(
        payload.get("comparison_surfaces"), label="multi-surface comparison surfaces"
    )
    raw_relationships = _list(payload.get("relationships"), label="multi-surface relationships")
    if expected_pairs is None:
        owned_pairs = tuple(itertools.combinations(sources, 2))
    else:
        owned_pairs = tuple(expected_pairs)
        if len(owned_pairs) != len(set(owned_pairs)) or any(
            len(pair) != 2 or pair[0] >= pair[1] or not set(pair).issubset(sources)
            for pair in owned_pairs
        ):
            raise CrossReferenceValidationError(
                "declared owned pairs must be unique canonical pairs inside the source scope"
            )
        owned_pairs = tuple(sorted(owned_pairs))
    expected_by_key = {_pair_key(*pair): pair for pair in owned_pairs}

    ledger_by_key: dict[str, Mapping[str, Any]] = {}
    listed_surface_owner: dict[str, str] = {}
    pair_dispositions: Counter[str] = Counter()
    for raw_ledger in raw_ledgers:
        ledger = _mapping(raw_ledger, label="multi-surface pair ledger")
        pair_key = _nonempty_text(ledger.get("pair_key"), label="pair ledger key")
        if pair_key in ledger_by_key or pair_key not in expected_by_key:
            raise CrossReferenceValidationError(
                "pair ledger keys must match every owned source pair exactly once"
            )
        if ledger.get("source_ids") != list(expected_by_key[pair_key]):
            raise CrossReferenceValidationError(
                "pair ledger source identities do not match its canonical pair"
            )
        surface_keys = _unique_strings(
            ledger.get("comparison_surface_keys"), label="pair ledger surface keys"
        )
        disposition = ledger.get("disposition")
        if disposition not in PAIR_DISPOSITIONS:
            raise CrossReferenceValidationError("pair ledger disposition is invalid")
        assert isinstance(disposition, str)
        if disposition == "surfaces_found" and not surface_keys:
            raise CrossReferenceValidationError(
                "surfaces_found pair ledger must cite at least one surface"
            )
        if disposition != "surfaces_found" and surface_keys:
            raise CrossReferenceValidationError(
                "only surfaces_found pair ledgers may cite comparison surfaces"
            )
        _nonempty_text(ledger.get("rationale"), label="pair ledger rationale")
        for surface_key in surface_keys:
            if surface_key in listed_surface_owner:
                raise CrossReferenceValidationError(
                    "comparison surface must appear in exactly one pair ledger"
                )
            listed_surface_owner[surface_key] = pair_key
        ledger_by_key[pair_key] = ledger
        pair_dispositions[disposition] += 1
    if set(ledger_by_key) != set(expected_by_key) or len(raw_ledgers) != len(owned_pairs):
        raise CrossReferenceValidationError(
            "multi-surface proposal did not account for every source pair exactly once"
        )

    surface_by_key: dict[str, Mapping[str, Any]] = {}
    surface_dispositions: Counter[str] = Counter()
    for raw_surface in raw_surfaces:
        surface = _mapping(raw_surface, label="multi-surface comparison surface")
        surface_key = _nonempty_text(surface.get("surface_key"), label="surface key")
        if surface_key in surface_by_key:
            raise CrossReferenceValidationError("comparison surface keys must be unique")
        owner_key = listed_surface_owner.get(surface_key)
        if owner_key is None:
            raise CrossReferenceValidationError("comparison surface is absent from the pair ledger")
        pair = expected_by_key[owner_key]
        if surface.get("source_ids") != list(pair):
            raise CrossReferenceValidationError(
                "comparison surface source identities do not match its pair ledger"
            )
        left = _connectable_atom(surface.get("left_record_id"), records=records, source_ids=sources)
        right = _connectable_atom(
            surface.get("right_record_id"), records=records, source_ids=sources
        )
        if (left.source_id, right.source_id) != pair:
            raise CrossReferenceValidationError(
                "comparison surface endpoints do not match canonical pair order"
            )
        _validate_evidence(
            surface.get("left_evidence_ids"),
            source_id=left.source_id,
            known=evidence_ids_by_source,
        )
        _validate_evidence(
            surface.get("right_evidence_ids"),
            source_id=right.source_id,
            known=evidence_ids_by_source,
        )
        if surface.get("comparison_dimension") not in COMPARISON_DIMENSIONS:
            raise CrossReferenceValidationError("comparison surface dimension is invalid")
        disposition = surface.get("disposition")
        if disposition not in SURFACE_DISPOSITIONS:
            raise CrossReferenceValidationError("comparison surface disposition is invalid")
        assert isinstance(disposition, str)
        for field in (
            "precise_connection",
            "scope_alignment",
            "assumption_alignment",
            "rationale",
        ):
            _nonempty_text(surface.get(field), label=f"comparison surface {field}")
        _unique_strings(surface.get("qualifications"), label="surface qualifications")
        _reject_adjudicative_text(surface)
        surface_by_key[surface_key] = surface
        surface_dispositions[disposition] += 1
    if set(surface_by_key) != set(listed_surface_owner):
        raise CrossReferenceValidationError("pair ledgers cite an unknown comparison surface")

    relationships_by_surface: Counter[str] = Counter()
    endpoint_pairs: set[tuple[str, str]] = set()
    for raw_relationship in raw_relationships:
        relationship = _mapping(raw_relationship, label="multi-surface relationship")
        surface_key = str(relationship.get("surface_key", ""))
        relationship_surface = surface_by_key.get(surface_key)
        if relationship_surface is None:
            raise CrossReferenceValidationError("relationship cites an unknown comparison surface")
        for field in (
            "left_record_id",
            "right_record_id",
            "left_evidence_ids",
            "right_evidence_ids",
        ):
            if relationship.get(field) != relationship_surface.get(field):
                raise CrossReferenceValidationError(
                    "relationship endpoint or evidence differs from its comparison surface"
                )
        left_id = str(relationship["left_record_id"])
        right_id = str(relationship["right_record_id"])
        endpoint_pair = (min(left_id, right_id), max(left_id, right_id))
        if endpoint_pair in endpoint_pairs:
            raise CrossReferenceValidationError(
                "multi-surface proposal repeated an unordered endpoint pair"
            )
        endpoint_pairs.add(endpoint_pair)
        relation_type = relationship.get("relation_type")
        direction = relationship.get("direction")
        if relation_type not in RELATIONSHIP_TYPES or direction not in RELATIONSHIP_DIRECTIONS:
            raise CrossReferenceValidationError("multi-surface relationship posture is invalid")
        if relation_type in SYMMETRIC_RELATIONSHIP_TYPES and direction != "symmetric":
            raise CrossReferenceValidationError(
                "multi-surface symmetric type has invalid direction"
            )
        if relation_type in DIRECTED_RELATIONSHIP_TYPES and direction == "symmetric":
            raise CrossReferenceValidationError("multi-surface directed type requires a direction")
        for field in (
            "comparison_surface",
            "scope_alignment",
            "assumption_alignment",
            "rationale",
        ):
            _nonempty_text(relationship.get(field), label=f"relationship {field}")
        _unique_strings(relationship.get("qualifications"), label="relationship qualifications")
        _reject_adjudicative_text(relationship)
        relationships_by_surface[surface_key] += 1

    for surface_key, surface in surface_by_key.items():
        relationship_count = relationships_by_surface[surface_key]
        if relationship_count > 1:
            raise CrossReferenceValidationError(
                "comparison surface may support at most one relationship"
            )
        proposed = surface.get("disposition") == "relationship_proposed"
        if proposed != (relationship_count == 1):
            raise CrossReferenceValidationError(
                "comparison surface disposition does not match proposed relationships"
            )

    return MultiSurfaceProposalDiagnostics(
        expected_pair_count=len(owned_pairs),
        comparison_surface_count=len(raw_surfaces),
        relationship_count=len(raw_relationships),
        pair_dispositions=dict(sorted(pair_dispositions.items())),
        surface_dispositions=dict(sorted(surface_dispositions.items())),
    )


def multi_surface_admission_projection(
    payload: Mapping[str, Any], *, existing_relationship_ids: Sequence[str] = ()
) -> dict[str, Any]:
    """Project validated production surfaces into the unchanged admission interface."""

    surfaces = _list(payload.get("comparison_surfaces"), label="multi-surface comparison surfaces")
    return {
        "relationships": list(
            _list(payload.get("relationships"), label="multi-surface relationships")
        ),
        "existing_relationship_reviews": [
            {"relationship_id": relationship_id}
            for relationship_id in sorted(existing_relationship_ids)
        ],
        "coverage_surfaces": [
            {
                "surface_key": str(surface["surface_key"]),
                "label": str(surface["precise_connection"]),
                "source_record_ids": [
                    str(surface["left_record_id"]),
                    str(surface["right_record_id"]),
                ],
                "disposition": (
                    "covered_by_proposal"
                    if surface["disposition"] == "relationship_proposed"
                    else (
                        "manual_review_candidate"
                        if surface["disposition"] == "ontology_gap_candidate"
                        else "no_useful_relationship"
                    )
                ),
                "existing_relationship_ids": [],
                "rationale": str(surface["rationale"]),
            }
            for surface in (
                _mapping(item, label="multi-surface comparison surface") for item in surfaces
            )
        ],
    }


def multi_surface_granular_admission_projection(
    payload: Mapping[str, Any],
    *,
    validation_report: MultiSurfaceValidationReport,
    existing_relationship_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Project only mechanically safe local rows without changing their content."""

    if validation_report.has_fatal_findings:
        raise CrossReferenceValidationError(
            "fatal multi-surface findings prevent granular admission projection"
        )
    surfaces = _list(payload.get("comparison_surfaces"), label="multi-surface comparison surfaces")
    relationships = _list(payload.get("relationships"), label="multi-surface relationships")
    projected = {
        "relationships": [
            relationships[index] for index in validation_report.accepted_relationship_indexes
        ],
        "existing_relationship_reviews": [
            {"relationship_id": relationship_id}
            for relationship_id in sorted(existing_relationship_ids)
        ],
        "coverage_surfaces": [
            _surface_admission_projection(
                _mapping(surfaces[index], label="multi-surface comparison surface")
            )
            for index in validation_report.accepted_surface_indexes
        ],
    }
    return projected


def _surface_admission_projection(surface: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "surface_key": str(surface["surface_key"]),
        "label": str(surface["precise_connection"]),
        "source_record_ids": [
            str(surface["left_record_id"]),
            str(surface["right_record_id"]),
        ],
        "disposition": (
            "covered_by_proposal"
            if surface["disposition"] == "relationship_proposed"
            else (
                "manual_review_candidate"
                if surface["disposition"] == "ontology_gap_candidate"
                else "no_useful_relationship"
            )
        ),
        "existing_relationship_ids": [],
        "rationale": str(surface["rationale"]),
    }


def _diagnose_endpoint(
    value: Any,
    *,
    path: str,
    records: Mapping[str, Record],
    source_ids: Sequence[str],
    add: Any,
    pair_key: str,
    surface_key: str,
) -> Record | None:
    record_id = value if isinstance(value, str) else ""
    record = records.get(record_id)
    if (
        record is None
        or record.record_type != "atom"
        or record.payload.get("connectable") is not True
    ):
        add(
            "comparison_surface_endpoint_not_connectable",
            path,
            "surface",
            "multi-surface endpoint does not resolve to a connectable atom",
            pair_key=pair_key,
            surface_key=surface_key,
        )
        return None
    if record.source_id not in source_ids:
        add(
            "comparison_surface_endpoint_outside_scope",
            path,
            "surface",
            "multi-surface endpoint is outside the cohort",
            pair_key=pair_key,
            surface_key=surface_key,
        )
        return None
    return record


def _evidence_resolves(
    value: Any,
    *,
    source_id: str,
    known: Mapping[str, frozenset[str] | set[str]],
) -> bool:
    return bool(
        isinstance(value, list)
        and value
        and all(isinstance(item, str) for item in value)
        and len(value) == len(set(value))
        and not set(value) - set(known.get(source_id, frozenset()))
    )


def _is_unique_string_list(value: Any) -> bool:
    return bool(
        isinstance(value, list)
        and all(isinstance(item, str) and item.strip() for item in value)
        and len(value) == len(set(value))
    )


def _adjudicative_phrase(value: Mapping[str, Any]) -> str | None:
    text = json.dumps(value, ensure_ascii=False).lower()
    matched = next((phrase for phrase in _ADJUDICATIVE_PHRASES if phrase in text), None)
    return None if matched is None else matched.strip()


def _pair_key(left: str, right: str) -> str:
    return f"pair-{left.lower()}-{right.lower()}"


def _mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise CrossReferenceValidationError(f"{label} must be an object")
    return value


def _list(value: Any, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise CrossReferenceValidationError(f"{label} must be an array")
    return value


def _unique_strings(value: Any, *, label: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not all(isinstance(item, str) and item.strip() for item in value)
        or len(value) != len(set(value))
    ):
        raise CrossReferenceValidationError(f"{label} must contain unique non-empty strings")
    return value


def _nonempty_text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CrossReferenceValidationError(f"{label} must be non-empty")
    return value.strip()


def _connectable_atom(
    value: Any,
    *,
    records: Mapping[str, Record],
    source_ids: Sequence[str],
) -> Record:
    record_id = str(value or "")
    record = records.get(record_id)
    if (
        record is None
        or record.record_type != "atom"
        or record.payload.get("connectable") is not True
    ):
        raise CrossReferenceValidationError(
            "multi-surface endpoint does not resolve to a connectable atom"
        )
    if record.source_id not in source_ids:
        raise CrossReferenceValidationError("multi-surface endpoint is outside the cohort")
    return record


def _validate_evidence(
    value: Any,
    *,
    source_id: str,
    known: Mapping[str, frozenset[str] | set[str]],
) -> None:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) for item in value)
        or len(value) != len(set(value))
        or bool(set(value) - set(known.get(source_id, frozenset())))
    ):
        raise CrossReferenceValidationError(
            "multi-surface evidence does not resolve on its endpoint side"
        )


def _reject_adjudicative_text(value: Mapping[str, Any]) -> None:
    text = json.dumps(value, ensure_ascii=False).lower()
    matched = next((phrase for phrase in _ADJUDICATIVE_PHRASES if phrase in text), None)
    if matched is not None:
        raise CrossReferenceValidationError(
            f"multi-surface proposal crosses the non-adjudication boundary: {matched.strip()}"
        )
