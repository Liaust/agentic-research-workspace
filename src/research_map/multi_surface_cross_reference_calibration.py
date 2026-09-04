"""Read-only multi-surface cross-reference calibration over a frozen cohort."""

from __future__ import annotations

import hashlib
import itertools
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from research_map.cross_reference import (
    DIRECTED_RELATIONSHIP_TYPES,
    RELATIONSHIP_DIRECTIONS,
    RELATIONSHIP_TYPES,
    SYMMETRIC_RELATIONSHIP_TYPES,
)
from research_map.cross_reference_benchmark import AUDIT_SCORE_FIELDS, FrozenBenchmarkCorpus
from research_map.focused_cross_reference_calibration import (
    CALIBRATION_SCHEMA_VERSION,
    FocusedCalibrationResult,
    FocusedCrossReferenceCalibrationCoordinator,
    FocusedCrossReferenceCalibrationError,
    FocusedCrossReferenceCalibrationExecutionError,
    ReferenceCandidateSet,
    _audit_item_is_useful,
    _connectable_atom,
    _display_metric,
    _mapping,
    _nonempty_text,
    _reject_adjudicative_text,
    _rounded,
    _validate_evidence,
)
from research_map.receipts import canonical_json_bytes
from research_map.records import Record
from research_map.workspace import WorkspaceInput

MULTI_SURFACE_CONTRACT_VERSION = "multi-surface-cross-reference-v1"
FOCUSED_WEIGHTED_COVERAGE = 0.358824
FOCUSED_MAPPER_EFFECTIVE_TOKENS = 74292
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
MULTI_SURFACE_STAGE_DEFINITIONS = {
    "focused-mapper": (
        "cross_reference_holistic",
        "multi-surface-cross-reference-proposal.schema.json",
        "multi-surface-cross-reference-calibration.md",
    ),
    "independent-evaluation": (
        "cross_reference_benchmark_comparison",
        "focused-cross-reference-evaluation.schema.json",
        "focused-cross-reference-evaluation.md",
    ),
}

MultiSurfaceCrossReferenceCalibrationError = FocusedCrossReferenceCalibrationError
MultiSurfaceCrossReferenceCalibrationExecutionError = FocusedCrossReferenceCalibrationExecutionError
MultiSurfaceCalibrationResult = FocusedCalibrationResult


class MultiSurfaceCrossReferenceCalibrationCoordinator(FocusedCrossReferenceCalibrationCoordinator):
    """Run one graph-blind variable-surface mapper and the unchanged evaluator."""

    contract_version = MULTI_SURFACE_CONTRACT_VERSION
    stage_definitions = MULTI_SURFACE_STAGE_DEFINITIONS
    receipt_kind = "multi_surface_cross_reference_calibration"
    stage_validation_kind = "multi_surface_cross_reference_stage_validation"

    def _manifest(
        self,
        *,
        corpus: FrozenBenchmarkCorpus,
        reference: ReferenceCandidateSet,
        calibration_id: str,
        model: str,
        source_ids: tuple[str, ...],
    ) -> dict[str, Any]:
        manifest = super()._manifest(
            corpus=corpus,
            reference=reference,
            calibration_id=calibration_id,
            model=model,
            source_ids=source_ids,
        )
        manifest.update(
            {
                "mapper_pair_ledger_count": 10,
                "mapper_comparison_surface_cardinality": "zero_or_many_per_pair",
                "mapper_surface_quota": None,
                "mapper_relationship_quota": None,
                "focused_comparator_weighted_coverage": FOCUSED_WEIGHTED_COVERAGE,
                "focused_comparator_mapper_effective_tokens": (FOCUSED_MAPPER_EFFECTIVE_TOKENS),
            }
        )
        return manifest

    def _mapper_inputs(
        self,
        *,
        corpus: FrozenBenchmarkCorpus,
        calibration_id: str,
        batch_id: str,
        source_ids: tuple[str, ...],
    ) -> tuple[WorkspaceInput, ...]:
        owned_pairs = [
            {
                "pair_key": _pair_key(left, right),
                "source_ids": [left, right],
            }
            for left, right in itertools.combinations(source_ids, 2)
        ]
        task = {
            "schema_version": CALIBRATION_SCHEMA_VERSION,
            "stage": "multi-surface-five-source-cross-reference",
            "calibration_id": calibration_id,
            "batch_id": batch_id,
            "source_ids": list(source_ids),
            "owned_source_pairs": owned_pairs,
            "required_pair_ledger_count": 10,
            "comparison_surfaces_per_pair": "zero_or_many",
            "current_relationships_available": False,
            "reference_candidates_available": False,
            "comparison_surface_quota": None,
            "relationship_quota": None,
            "map_mutation_authorized": False,
        }
        return (
            self._protocol_input(calibration_id, "focused-mapper"),
            WorkspaceInput(
                source_id=calibration_id,
                destination="task.json",
                kind="multi_surface_calibration_task",
                content=canonical_json_bytes(task),
            ),
            *self._dossier_inputs(corpus, source_ids, calibration_id=calibration_id),
        )

    def _evaluation_inputs(
        self,
        *,
        corpus: FrozenBenchmarkCorpus,
        reference: ReferenceCandidateSet,
        calibration_id: str,
        batch_id: str,
        source_ids: tuple[str, ...],
        mapper_payload: Mapping[str, Any],
        keyed_relationships: Sequence[Mapping[str, Any]],
    ) -> tuple[WorkspaceInput, ...]:
        proposal_keys = [str(item["proposal_key"]) for item in keyed_relationships]
        reference_keys = [str(item["candidate_key"]) for item in reference.candidates]
        task = {
            "schema_version": CALIBRATION_SCHEMA_VERSION,
            "stage": "focused-cross-reference-independent-evaluation",
            "calibration_id": calibration_id,
            "batch_id": batch_id,
            "source_ids": list(source_ids),
            "proposal_keys": proposal_keys,
            "reference_candidate_keys": reference_keys,
            "rubric_score_range": [0, 4],
            "score_fields": list(AUDIT_SCORE_FIELDS),
            "reference_candidates_are_objective_truth": False,
            "map_mutation_authorized": False,
        }
        proposal_projection = {
            "schema_version": CALIBRATION_SCHEMA_VERSION,
            "calibration_id": calibration_id,
            "batch_id": batch_id,
            "source_ids": list(source_ids),
            "raw_proposal_sha256": hashlib.sha256(canonical_json_bytes(mapper_payload)).hexdigest(),
            "pair_coverage": mapper_payload["pair_coverage"],
            "comparison_surfaces": mapper_payload["comparison_surfaces"],
            "relationships": list(keyed_relationships),
        }
        reference_projection = {
            "schema_version": CALIBRATION_SCHEMA_VERSION,
            "benchmark_id": reference.benchmark_id,
            "batch_id": batch_id,
            "source_ids": list(source_ids),
            "eligibility_gate": {
                "candidate_validity_minimum": 3,
                "candidate_utility_minimum": 2,
            },
            "candidates": list(reference.candidates),
        }
        return (
            self._protocol_input(calibration_id, "independent-evaluation"),
            WorkspaceInput(
                source_id=calibration_id,
                destination="task.json",
                kind="focused_evaluation_task",
                content=canonical_json_bytes(task),
            ),
            WorkspaceInput(
                source_id=calibration_id,
                destination="focused-proposal.json",
                kind="multi_surface_mapper_output",
                content=canonical_json_bytes(proposal_projection),
            ),
            WorkspaceInput(
                source_id=calibration_id,
                destination="reference-candidates.json",
                kind="fixed_reference_candidates",
                content=canonical_json_bytes(reference_projection),
            ),
            *self._dossier_inputs(corpus, source_ids, calibration_id=calibration_id),
        )

    def _validate_mapper_output(
        self,
        payload: Mapping[str, Any],
        *,
        batch_id: str,
        source_ids: Sequence[str],
        records: Mapping[str, Record],
        evidence_ids_by_source: Mapping[str, frozenset[str]],
    ) -> tuple[dict[str, Any], ...]:
        return validate_multi_surface_mapper_output(
            payload,
            batch_id=batch_id,
            source_ids=source_ids,
            records=records,
            evidence_ids_by_source=evidence_ids_by_source,
        )

    def _mapper_validation_extra(
        self,
        mapper_payload: Mapping[str, Any],
        keyed_relationships: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        return {
            "pair_ledger_count": len(mapper_payload["pair_coverage"]),
            "comparison_surface_count": len(mapper_payload["comparison_surfaces"]),
            "proposal_count": len(keyed_relationships),
        }

    def _compile_metrics(self, **kwargs: Any) -> dict[str, Any]:
        return compile_multi_surface_calibration_metrics(**kwargs)

    def _render_report(self, metrics: Mapping[str, Any]) -> str:
        return render_multi_surface_report(metrics)


def validate_multi_surface_mapper_output(
    payload: Mapping[str, Any],
    *,
    batch_id: str,
    source_ids: Sequence[str],
    records: Mapping[str, Record],
    evidence_ids_by_source: Mapping[str, frozenset[str]],
) -> tuple[dict[str, Any], ...]:
    """Validate exact pair attention and variable source-grounded comparison surfaces."""

    if payload.get("schema_version") != 1 or payload.get("batch_id") != batch_id:
        raise FocusedCrossReferenceCalibrationError("multi-surface mapper identity changed")
    if payload.get("source_ids") != list(source_ids):
        raise FocusedCrossReferenceCalibrationError(
            "multi-surface mapper source identities changed"
        )
    raw_ledgers = payload.get("pair_coverage")
    raw_surfaces = payload.get("comparison_surfaces")
    raw_relationships = payload.get("relationships")
    if not all(isinstance(value, list) for value in (raw_ledgers, raw_surfaces, raw_relationships)):
        raise FocusedCrossReferenceCalibrationError(
            "multi-surface mapper requires pair ledgers, surfaces, and relationships"
        )
    assert isinstance(raw_ledgers, list)
    assert isinstance(raw_surfaces, list)
    assert isinstance(raw_relationships, list)
    expected_pairs = tuple(itertools.combinations(source_ids, 2))
    expected_by_key = {_pair_key(*pair): pair for pair in expected_pairs}
    ledger_by_key: dict[str, dict[str, Any]] = {}
    listed_surface_owner: dict[str, str] = {}
    for raw_ledger in raw_ledgers:
        ledger = _mapping(raw_ledger, label="multi-surface pair ledger")
        pair_key = _nonempty_text(ledger.get("pair_key"), label="pair ledger key")
        if pair_key in ledger_by_key or pair_key not in expected_by_key:
            raise FocusedCrossReferenceCalibrationError(
                "pair ledger keys must match every owned source pair exactly once"
            )
        if ledger.get("source_ids") != list(expected_by_key[pair_key]):
            raise FocusedCrossReferenceCalibrationError(
                "pair ledger source identities do not match its canonical pair"
            )
        surface_keys = ledger.get("comparison_surface_keys")
        if (
            not isinstance(surface_keys, list)
            or not all(isinstance(item, str) for item in surface_keys)
            or len(surface_keys) != len(set(surface_keys))
        ):
            raise FocusedCrossReferenceCalibrationError(
                "pair ledger surface keys must be unique strings"
            )
        disposition = ledger.get("disposition")
        if disposition not in PAIR_DISPOSITIONS:
            raise FocusedCrossReferenceCalibrationError("pair ledger disposition is invalid")
        if disposition == "surfaces_found" and not surface_keys:
            raise FocusedCrossReferenceCalibrationError(
                "surfaces_found pair ledger must cite at least one surface"
            )
        if disposition != "surfaces_found" and surface_keys:
            raise FocusedCrossReferenceCalibrationError(
                "only surfaces_found pair ledgers may cite comparison surfaces"
            )
        _nonempty_text(ledger.get("rationale"), label="pair ledger rationale")
        for surface_key in surface_keys:
            if surface_key in listed_surface_owner:
                raise FocusedCrossReferenceCalibrationError(
                    "comparison surface must appear in exactly one pair ledger"
                )
            listed_surface_owner[surface_key] = pair_key
        ledger_by_key[pair_key] = ledger
    if set(ledger_by_key) != set(expected_by_key) or len(raw_ledgers) != 10:
        raise FocusedCrossReferenceCalibrationError(
            "multi-surface mapper did not account for every source pair exactly once"
        )

    surface_by_key: dict[str, dict[str, Any]] = {}
    for raw_surface in raw_surfaces:
        surface = _mapping(raw_surface, label="multi-surface comparison surface")
        surface_key = _nonempty_text(surface.get("surface_key"), label="comparison surface key")
        if surface_key in surface_by_key:
            raise FocusedCrossReferenceCalibrationError("comparison surface keys must be unique")
        owner_key = listed_surface_owner.get(surface_key)
        if owner_key is None:
            raise FocusedCrossReferenceCalibrationError(
                "comparison surface is absent from the pair ledger"
            )
        pair = expected_by_key[owner_key]
        if surface.get("source_ids") != list(pair):
            raise FocusedCrossReferenceCalibrationError(
                "comparison surface source identities do not match its pair ledger"
            )
        left_id = str(surface.get("left_record_id", ""))
        right_id = str(surface.get("right_record_id", ""))
        left = _connectable_atom(left_id, records=records, source_ids=source_ids)
        right = _connectable_atom(right_id, records=records, source_ids=source_ids)
        if (left.source_id, right.source_id) != pair:
            raise FocusedCrossReferenceCalibrationError(
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
            raise FocusedCrossReferenceCalibrationError("comparison surface dimension is invalid")
        if surface.get("disposition") not in SURFACE_DISPOSITIONS:
            raise FocusedCrossReferenceCalibrationError("comparison surface disposition is invalid")
        for field in (
            "precise_connection",
            "scope_alignment",
            "assumption_alignment",
            "rationale",
        ):
            _nonempty_text(surface.get(field), label=f"comparison surface {field}")
        _reject_adjudicative_text(surface)
        surface_by_key[surface_key] = surface
    if set(surface_by_key) != set(listed_surface_owner):
        raise FocusedCrossReferenceCalibrationError(
            "pair ledgers cite an unknown comparison surface"
        )

    relationships_by_surface: Counter[str] = Counter()
    endpoint_pairs: set[tuple[str, str]] = set()
    keyed: list[dict[str, Any]] = []
    for raw_relationship in raw_relationships:
        relationship = _mapping(raw_relationship, label="multi-surface relationship")
        surface_key = str(relationship.get("surface_key", ""))
        relationship_surface = surface_by_key.get(surface_key)
        if relationship_surface is None:
            raise FocusedCrossReferenceCalibrationError(
                "relationship cites an unknown comparison surface"
            )
        for field in (
            "left_record_id",
            "right_record_id",
            "left_evidence_ids",
            "right_evidence_ids",
        ):
            if relationship.get(field) != relationship_surface.get(field):
                raise FocusedCrossReferenceCalibrationError(
                    "relationship endpoint or evidence differs from its comparison surface"
                )
        left_id = str(relationship["left_record_id"])
        right_id = str(relationship["right_record_id"])
        endpoint_pair = (min(left_id, right_id), max(left_id, right_id))
        if endpoint_pair in endpoint_pairs:
            raise FocusedCrossReferenceCalibrationError(
                "multi-surface mapper repeated an unordered endpoint pair"
            )
        endpoint_pairs.add(endpoint_pair)
        relation_type = relationship.get("relation_type")
        direction = relationship.get("direction")
        if relation_type not in RELATIONSHIP_TYPES or direction not in RELATIONSHIP_DIRECTIONS:
            raise FocusedCrossReferenceCalibrationError(
                "multi-surface relationship posture is invalid"
            )
        if relation_type in SYMMETRIC_RELATIONSHIP_TYPES and direction != "symmetric":
            raise FocusedCrossReferenceCalibrationError(
                "multi-surface symmetric type has invalid direction"
            )
        if relation_type in DIRECTED_RELATIONSHIP_TYPES and direction == "symmetric":
            raise FocusedCrossReferenceCalibrationError(
                "multi-surface directed type requires a direction"
            )
        for field in (
            "comparison_surface",
            "scope_alignment",
            "assumption_alignment",
            "rationale",
        ):
            _nonempty_text(relationship.get(field), label=f"multi-surface relationship {field}")
        _reject_adjudicative_text(relationship)
        proposal_key = (
            "focus_rel_" + hashlib.sha256(canonical_json_bytes(relationship)).hexdigest()[:24]
        )
        if any(item["proposal_key"] == proposal_key for item in keyed):
            raise FocusedCrossReferenceCalibrationError("multi-surface proposal key collision")
        keyed.append({"proposal_key": proposal_key, **relationship})
        relationships_by_surface[surface_key] += 1
    for surface_key, surface in surface_by_key.items():
        proposed = surface.get("disposition") == "relationship_proposed"
        if proposed != (relationships_by_surface[surface_key] == 1):
            raise FocusedCrossReferenceCalibrationError(
                "comparison surface disposition does not match proposed relationships"
            )
    return tuple(keyed)


def compile_multi_surface_calibration_metrics(
    *,
    calibration_id: str,
    batch_id: str,
    source_ids: Sequence[str],
    mapper_payload: Mapping[str, Any],
    keyed_relationships: Sequence[Mapping[str, Any]],
    proposal_audits: Sequence[Mapping[str, Any]],
    reference: ReferenceCandidateSet,
    reference_matches: Sequence[Mapping[str, Any]],
    stage_usage: Mapping[str, Any],
) -> dict[str, Any]:
    """Compile separate continuous coverage, quality, breadth, and cost measures."""

    reference_by_key = {str(item["candidate_key"]): item for item in reference.candidates}
    weighted_numerator = sum(
        int(reference_by_key[str(item["candidate_key"])]["importance"])
        * int(item["match_quality"])
        / 4
        for item in reference_matches
    )
    weighted_coverage = weighted_numerator / reference.importance_weight
    useful = [item for item in proposal_audits if _audit_item_is_useful(item)]
    score_means = {
        field: (
            _rounded(
                sum(
                    int(_mapping(item["scores"], label="audit scores")[field])
                    for item in proposal_audits
                )
                / len(proposal_audits)
            )
            if proposal_audits
            else None
        )
        for field in AUDIT_SCORE_FIELDS
    }
    type_assessments = Counter(str(item["relation_type_assessment"]) for item in proposal_audits)
    relation_types = Counter(str(item["relation_type"]) for item in keyed_relationships)
    surfaces = [
        _mapping(item, label="comparison surface") for item in mapper_payload["comparison_surfaces"]
    ]
    dimensions = Counter(str(item["comparison_dimension"]) for item in surfaces)
    surface_dispositions = Counter(str(item["disposition"]) for item in surfaces)
    surfaces_per_pair = [
        len(_mapping(item, label="pair ledger")["comparison_surface_keys"])
        for item in mapper_payload["pair_coverage"]
    ]
    proposed_pairs = {
        tuple(
            sorted(
                (
                    str(item["left_record_id"]).split(":", 1)[0],
                    str(item["right_record_id"]).split(":", 1)[0],
                )
            )
        )
        for item in keyed_relationships
    }
    stages = _mapping(stage_usage.get("stages"), label="stage usage")
    mapper_usage = _mapping(stages.get("focused-mapper"), label="mapper usage")
    mapper_effective = int(mapper_usage.get("effective_tokens", 0))
    useful_rate = len(useful) / len(proposal_audits) if proposal_audits else 0.0
    token_delta = mapper_effective - FOCUSED_MAPPER_EFFECTIVE_TOKENS
    coverage_delta_from_focused = weighted_coverage - FOCUSED_WEIGHTED_COVERAGE
    return {
        "schema_version": CALIBRATION_SCHEMA_VERSION,
        "contract_version": MULTI_SURFACE_CONTRACT_VERSION,
        "calibration_id": calibration_id,
        "batch_id": batch_id,
        "source_ids": list(source_ids),
        "mapper": {
            "pair_ledger_count": len(mapper_payload["pair_coverage"]),
            "comparison_surface_count": len(surfaces),
            "source_pairs_with_surfaces": sum(value > 0 for value in surfaces_per_pair),
            "surface_count_per_pair": {
                "minimum": min(surfaces_per_pair),
                "maximum": max(surfaces_per_pair),
                "mean": _rounded(sum(surfaces_per_pair) / len(surfaces_per_pair)),
                "counts": surfaces_per_pair,
            },
            "comparison_dimensions": dict(sorted(dimensions.items())),
            "surface_dispositions": dict(sorted(surface_dispositions.items())),
            "proposal_count": len(keyed_relationships),
            "proposed_source_pair_count": len(proposed_pairs),
            "possible_source_pair_count": 10,
            "proposed_source_pair_breadth": _rounded(len(proposed_pairs) / 10),
            "relationship_types": dict(sorted(relation_types.items())),
        },
        "proposal_quality": {
            "audited_proposal_count": len(proposal_audits),
            "useful_proposal_count": len(useful),
            "useful_proposal_rate": _rounded(useful_rate),
            "score_means": score_means,
            "relation_type_assessments": dict(sorted(type_assessments.items())),
            "ontology_gap_count": type_assessments["ontology_gap"],
        },
        "reference_coverage": {
            "interpretation": "model-assisted fixed-cohort calibration; not objective recall",
            "reference_benchmark_id": reference.benchmark_id,
            "eligible_candidate_count": len(reference.candidates),
            "importance_weight_denominator": reference.importance_weight,
            "multi_surface_weighted_match_numerator": _rounded(weighted_numerator),
            "multi_surface_weighted_coverage": _rounded(weighted_coverage),
            "focused_weighted_coverage": FOCUSED_WEIGHTED_COVERAGE,
            "existing_graph_weighted_coverage": _rounded(reference.existing_graph_coverage),
            "coverage_delta_from_focused": _rounded(coverage_delta_from_focused),
            "coverage_delta_from_existing_graph": _rounded(
                weighted_coverage - reference.existing_graph_coverage
            ),
        },
        "diagnostic_signals": {
            "coverage_improved_over_focused": weighted_coverage > FOCUSED_WEIGHTED_COVERAGE,
            "mean_relation_type_fit_at_least_3": (
                score_means["relation_type_fit"] is not None
                and score_means["relation_type_fit"] >= 3
            ),
            "useful_proposal_rate_at_least_two_thirds": useful_rate >= 2 / 3,
            "automatic_adoption_authorized": False,
        },
        "cost": {
            "stage_usage": stage_usage,
            "focused_comparator_mapper_effective_tokens": FOCUSED_MAPPER_EFFECTIVE_TOKENS,
            "additional_mapper_effective_tokens_vs_focused": token_delta,
            "mapper_effective_tokens_per_useful_proposal": (
                _rounded(mapper_effective / len(useful)) if useful else None
            ),
            "mapper_effective_tokens_per_captured_importance_unit": (
                _rounded(mapper_effective / weighted_numerator) if weighted_numerator else None
            ),
            "incremental_coverage_per_additional_mapper_effective_token": (
                _rounded(coverage_delta_from_focused / token_delta) if token_delta > 0 else None
            ),
            "evaluation_overhead_reported_separately": True,
        },
        "map_mutation_authorized": False,
    }


def render_multi_surface_report(metrics: Mapping[str, Any]) -> str:
    mapper = _mapping(metrics["mapper"], label="mapper metrics")
    quality = _mapping(metrics["proposal_quality"], label="quality metrics")
    coverage = _mapping(metrics["reference_coverage"], label="coverage metrics")
    signals = _mapping(metrics["diagnostic_signals"], label="diagnostic signals")
    cost = _mapping(metrics["cost"], label="cost metrics")
    usage = _mapping(cost["stage_usage"], label="stage usage")
    stages = _mapping(usage["stages"], label="usage stages")
    lines = [
        "# Multi-surface single-call cross-reference calibration",
        "",
        f"Calibration: `{metrics['calibration_id']}`",
        "",
        "This is model-assisted calibration evidence. It does not adjudicate scientific "
        "truth, establish objective recall, or modify the research map.",
        "",
        "## Outcome",
        "",
        f"- Comparison surfaces: {mapper['comparison_surface_count']} across "
        f"{mapper['source_pairs_with_surfaces']} of 10 source pairs.",
        f"- Proposals: {mapper['proposal_count']} across "
        f"{mapper['proposed_source_pair_count']} of 10 source pairs.",
        f"- Useful proposals: {quality['useful_proposal_count']} of "
        f"{quality['audited_proposal_count']} ({quality['useful_proposal_rate']}).",
        f"- Mean relation-type fit: {quality['score_means']['relation_type_fit']}.",
        f"- Mean argumentative importance: {quality['score_means']['argumentative_importance']}.",
        f"- Mean distinctiveness: {quality['score_means']['distinctiveness']}.",
        f"- Multi-surface weighted reference coverage: "
        f"{coverage['multi_surface_weighted_coverage']}.",
        f"- Focused weighted reference coverage: {coverage['focused_weighted_coverage']}.",
        f"- Existing graph weighted reference coverage: "
        f"{coverage['existing_graph_weighted_coverage']}.",
        f"- Coverage delta from focused: {coverage['coverage_delta_from_focused']}.",
        "",
        "## Breadth",
        "",
        f"- Surfaces per pair: `{json.dumps(mapper['surface_count_per_pair'], sort_keys=True)}`",
        f"- Comparison dimensions: `{json.dumps(mapper['comparison_dimensions'], sort_keys=True)}`",
        f"- Surface dispositions: `{json.dumps(mapper['surface_dispositions'], sort_keys=True)}`",
        "",
        "## Diagnostic signals",
        "",
        f"- Coverage improved over focused: {signals['coverage_improved_over_focused']}.",
        f"- Mean relation-type fit at least 3: {signals['mean_relation_type_fit_at_least_3']}.",
        f"- Useful-proposal rate at least two thirds: "
        f"{signals['useful_proposal_rate_at_least_two_thirds']}.",
        "- These signals do not automatically authorize adoption.",
        "",
        "## Cost",
        "",
        "| Stage | Input | Cached input | Uncached input | Output | Effective | Time |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for stage in MULTI_SURFACE_STAGE_DEFINITIONS:
        item = _mapping(stages[stage], label=f"{stage} usage")
        lines.append(
            f"| {stage} | {item['input_tokens']} | {item['cached_input_tokens']} | "
            f"{item['uncached_input_tokens']} | {item['output_tokens']} | "
            f"{item['effective_tokens']} | {item['duration_seconds']} s |"
        )
    lines.extend(
        (
            "",
            "Additional mapper effective tokens versus focused: "
            f"{cost['additional_mapper_effective_tokens_vs_focused']}.",
            "Incremental coverage per additional mapper effective token: "
            f"{_display_metric(cost['incremental_coverage_per_additional_mapper_effective_token'])}.",
            "Mapper effective tokens per useful proposal: "
            f"{_display_metric(cost['mapper_effective_tokens_per_useful_proposal'])}.",
            "Generation and evaluation overhead remain separate; no composite score is used.",
            "",
            "## Boundary",
            "",
            "The mapper saw only the five dossiers, task, and protocol. It did not receive "
            "the graph, fixed candidates, or focused result. The unchanged evaluator compared "
            "the unchanged proposal with the same 25-candidate denominator. No relationship "
            "was admitted, repaired, relabeled, or written to the map.",
            "",
        )
    )
    return "\n".join(lines)


def _pair_key(left: str, right: str) -> str:
    return f"pair-{left.lower()}-{right.lower()}"
