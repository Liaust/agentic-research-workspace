"""Deterministic holistic cross-reference coordination and compilation."""

from __future__ import annotations

import hashlib
import itertools
import json
import threading
from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from research_map.codex import CodexCapabilityError, CodexJob, CodexRunner
from research_map.corpus_graph import (
    CorpusGraphCompiler,
    CorpusGraphError,
    CorpusGraphMaterialization,
)
from research_map.cross_reference import (
    CrossReferenceValidationError,
    DiscoveryCandidate,
    EndpointRef,
    Relationship,
    record_fingerprint,
    validate_candidate_endpoints,
    validate_inspection_outcome,
    validate_schema,
)
from research_map.cross_reference_admission import (
    AdmissionPlan,
    compile_holistic_admission,
)
from research_map.ids import (
    artifact_id,
    canonical_source_pair,
    cross_reference_batch_id,
    cross_reference_job_id,
    cross_reference_relationship_id,
    holistic_cross_reference_job_id,
)
from research_map.markdown import load_markdown
from research_map.multi_surface_contract import (
    MultiSurfaceProposalDiagnostics,
    multi_surface_admission_projection,
    validate_multi_surface_proposal,
)
from research_map.receipts import (
    canonical_json_bytes,
    fingerprint,
    read_receipt,
    write_receipt,
)
from research_map.records import Dossier, Record
from research_map.relationship_markdown import (
    RelationshipMarkdownError,
    load_relationship_markdown,
    materialize_relationship_markdown,
)
from research_map.source import resolve_source, verify_asset
from research_map.state import StateRepository
from research_map.workspace import (
    CrossReferenceWorkspaceBuilder,
    Workspace,
    WorkspaceInput,
)

DISCOVERY_JOB_KIND = "discovery"
INSPECTION_JOB_KIND = "inspection"
HOLISTIC_JOB_KIND = "holistic"
HOLISTIC_STRATEGY = "holistic-multi-surface-v1"
LEGACY_HOLISTIC_STRATEGY = "holistic-expansive-v2"
SOURCE_SHARDED_STRATEGY = "holistic-expansive-source-sharded-v1"
HOLISTIC_PROMPT = (
    "Read AGENTS.md, input/task.json, and every record in "
    "input/corpus-records.json. Inspect every declared unordered source pair "
    "inside one graph-blind turn, retain zero or more materially distinct "
    "comparison surfaces per pair, and return only source-grounded potential "
    "relationships supported by those surfaces."
)
LEGACY_HOLISTIC_PROMPT = (
    "Read AGENTS.md, input/task.json, and every record in "
    "input/corpus-records.json. Explore the complete corpus iteratively, "
    "revisiting earlier sources as new comparison surfaces emerge, and return "
    "complete source-grounded potential relationships only."
)
SOURCE_SHARDED_PROMPT = (
    "Read AGENTS.md, input/task.json, and every record in "
    "input/corpus-records.json. Explore the authorized focus-to-focus and "
    "focus-to-context source pairs iteratively, revisiting earlier sources as "
    "new comparison surfaces emerge. Return complete source-grounded potential "
    "relationships only for the owned source pairs in input/task.json."
)
TERMINAL_CANDIDATE_STATES = frozenset(
    {
        "relationship",
        "not_usefully_connected",
        "insufficient_extraction",
        "manual_review_candidate",
    }
)


class CrossReferenceExecutionError(RuntimeError):
    """Raised when a bounded cross-reference semantic job cannot complete."""


@dataclass(frozen=True, slots=True)
class SourceProjection:
    source_id: str
    input_fingerprint: str
    dossier: Dossier
    records: dict[str, Record]
    contexts: dict[str, dict[str, Any]]
    catalogue: dict[str, Any]
    graph_path: Path


@dataclass(frozen=True, slots=True)
class SourceShard:
    focus_sources: tuple[str, ...]
    context_sources: tuple[str, ...]
    source_ids: tuple[str, ...]
    owned_pairs: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class OwnedPairShard:
    """One arbitrary source scope with exact unordered pair ownership."""

    shard_id: str
    source_ids: tuple[str, ...]
    owned_pairs: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class CrossReferenceRunResult:
    batch_id: str
    strategy: str
    source_ids: tuple[str, ...]
    source_groups: tuple[tuple[str, ...], ...]
    state: str
    job_ids: tuple[str, ...]
    candidate_count: int
    outcome_counts: dict[str, int]
    relationship_ids: tuple[str, ...]
    admission_counts: dict[str, int]
    admission_receipts: tuple[str, ...]
    proposal_diagnostics: tuple[dict[str, Any], ...]
    owned_pair_count: int = 0
    uncovered_pairs: tuple[tuple[str, str], ...] = ()
    shard_receipts: tuple[dict[str, Any], ...] = ()
    semantic_jobs_started: int = 0
    duration_seconds: float = 0.0
    worker_events_emitted: int = 0
    token_usage: dict[str, int] | None = None
    peak_mapper_concurrency: int = 0
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "source_ids": list(self.source_ids),
            "source_groups": [list(group) for group in self.source_groups],
            "job_ids": list(self.job_ids),
            "candidate_count": self.candidate_count,
            "outcome_counts": dict(self.outcome_counts),
            "relationship_ids": list(self.relationship_ids),
            "admission_counts": dict(self.admission_counts),
            "admission_receipts": list(self.admission_receipts),
            "proposal_diagnostics": [dict(item) for item in self.proposal_diagnostics],
            "owned_pair_count": self.owned_pair_count,
            "uncovered_pairs": [list(pair) for pair in self.uncovered_pairs],
            "shard_receipts": [dict(item) for item in self.shard_receipts],
            "semantic_jobs_started": self.semantic_jobs_started,
            "duration_seconds": self.duration_seconds,
            "worker_events_emitted": self.worker_events_emitted,
            "token_usage": self.token_usage or _empty_token_usage(),
            "peak_mapper_concurrency": self.peak_mapper_concurrency,
            "warnings": list(self.warnings),
        }


class CrossReferenceCoordinator:
    def __init__(
        self,
        *,
        repository_root: Path,
        state_path: Path,
        cache_root: Path | None = None,
        vault_root: Path | None = None,
        build_root: Path | None = None,
        relationship_root: Path | None = None,
        codex_executable: str = "codex",
        max_inspection_packet_bytes: int = 131_072,
        eligible_source_states: frozenset[str] | None = None,
        job_timeout_seconds: float | None = None,
        allow_failed_proposal_revalidation: bool = False,
    ) -> None:
        if max_inspection_packet_bytes < 4096:
            raise ValueError("inspection packet bound must be at least 4096 bytes")
        self.repository_root = repository_root.resolve()
        self.state = StateRepository(state_path)
        self.cache_root = cache_root or self.repository_root / ".cache" / "research-map"
        self.vault_root = vault_root or self.repository_root / "vault" / "sources"
        self.build_root = build_root or self.repository_root / "build" / "research-map"
        self.relationship_root = relationship_root or self.vault_root.parent / "relationships"
        self.schema_directory = self.repository_root / "schemas" / "research-map" / "v1"
        self.protocol_directory = self.repository_root / "protocols" / "research-map" / "v1"
        self.codex_executable = codex_executable
        self.max_inspection_packet_bytes = max_inspection_packet_bytes
        self.eligible_source_states = eligible_source_states or frozenset({"graph_inserted"})
        if job_timeout_seconds is not None and job_timeout_seconds <= 0:
            raise ValueError("cross-reference job timeout must be positive")
        self.job_timeout_seconds = job_timeout_seconds
        self.allow_failed_proposal_revalidation = allow_failed_proposal_revalidation

    def run(
        self,
        source_ids: tuple[str, ...],
        *,
        through: str,
        model: str,
        source_groups: tuple[tuple[str, ...], ...] | None = None,
        revalidate_retained: bool = False,
    ) -> CrossReferenceRunResult:
        sources = tuple(sorted(source_id.strip() for source_id in source_ids))
        if len(sources) < 2 or len(set(sources)) != len(sources):
            raise CrossReferenceValidationError(
                "cross-reference requires at least two unique sources"
            )
        if through not in {"discovered", "inspected", "compiled"}:
            raise CrossReferenceValidationError(
                "cross-reference stage must be discovered, inspected, or compiled"
            )
        if not model.strip():
            raise CrossReferenceValidationError("cross-reference model is required")
        groups = _canonical_source_groups(sources, source_groups)
        strategy = _source_sharded_strategy(groups) if groups else HOLISTIC_STRATEGY
        self.state.initialize()
        projections = {source_id: self._projection(source_id) for source_id in sources}
        fingerprints = {
            source_id: projection.input_fingerprint for source_id, projection in projections.items()
        }
        batch_identifier = cross_reference_batch_id(
            fingerprints.items(),
            requested_through=through,
            model=model,
            strategy=strategy,
        )
        self.state.register_cross_reference_batch(
            batch_identifier,
            source_ids=sources,
            input_fingerprints=fingerprints,
            requested_through=through,
            model=model,
            strategy=strategy,
            eligible_source_states=self.eligible_source_states,
        )
        warnings: list[str] = []
        state = str(self.state.cross_reference_batch_record(batch_identifier)["state"])  # type: ignore[index]
        if revalidate_retained:
            self._preflight_retained_revalidation(batch_identifier)
            warnings.extend(
                self._cross_reference_holistically(
                    batch_identifier,
                    sources=sources,
                    projections=projections,
                    model=model,
                    source_groups=groups,
                    strategy=strategy,
                )
            )
            if through == "compiled":
                self._compile_batch(
                    batch_identifier,
                    sources=sources,
                    projections=projections,
                    replace_existing=True,
                )
            return self._result(
                batch_identifier,
                warnings=tuple(warnings),
                source_groups=groups,
            )
        if state == "created":
            self.state.advance_cross_reference_batch(
                batch_identifier,
                "discovering",
                receipt={"kind": "candidate_discovery_started"},
            )
            state = "discovering"
        if state == "discovering":
            warnings.extend(
                self._cross_reference_holistically(
                    batch_identifier,
                    sources=sources,
                    projections=projections,
                    model=model,
                    source_groups=groups,
                    strategy=strategy,
                )
            )
            self.state.advance_cross_reference_batch(
                batch_identifier,
                "discovered",
                receipt={
                    "kind": "candidate_discovery_complete",
                    "candidate_count": len(
                        self.state.cross_reference_candidates_for_batch(batch_identifier)
                    ),
                },
            )
            state = "discovered"
        if through in {"inspected", "compiled"} and state == "discovered":
            self.state.advance_cross_reference_batch(
                batch_identifier,
                "inspecting",
                receipt={"kind": "candidate_inspection_started"},
            )
            state = "inspecting"
        if through in {"inspected", "compiled"} and state == "inspecting":
            self.state.advance_cross_reference_batch(
                batch_identifier,
                "inspected",
                receipt={
                    "kind": "holistic_proposal_validation_complete",
                    "candidate_count": len(
                        self.state.cross_reference_candidates_for_batch(batch_identifier)
                    ),
                },
            )
            state = "inspected"
        if through == "compiled" and state in {"inspected", "compiled"}:
            materialization = self._compile_batch(
                batch_identifier,
                sources=sources,
                projections=projections,
            )
            if state == "inspected":
                self.state.advance_cross_reference_batch(
                    batch_identifier,
                    "compiled",
                    receipt={
                        "kind": "corpus_graph_compiled",
                        "relationship_count": len(
                            self.state.cross_reference_relationships_for_batch(batch_identifier)
                        ),
                        "graph": materialization.artifacts[0],
                        "manifest": materialization.artifacts[1],
                    },
                )
        return self._result(
            batch_identifier,
            warnings=tuple(warnings),
            source_groups=groups,
        )

    def run_owned_pair_shards(
        self,
        source_ids: tuple[str, ...],
        *,
        shards: tuple[OwnedPairShard, ...],
        model: str,
        strategy: str,
        pre_uncovered_pairs: tuple[tuple[str, str], ...] = (),
    ) -> CrossReferenceRunResult:
        """Run exact-pair multi-surface shards with two workers and no retries."""

        sources = tuple(sorted(source_id.strip() for source_id in source_ids))
        _validate_owned_pair_shards(
            sources,
            shards,
            pre_uncovered_pairs=pre_uncovered_pairs,
        )
        if not model.strip() or not strategy.strip():
            raise CrossReferenceValidationError(
                "owned-pair cross-reference requires model and strategy identities"
            )
        contract_sha256 = hashlib.sha256(
            canonical_json_bytes(
                {
                    "protocol": fingerprint(
                        self.protocol_directory / "holistic-multi-surface.md"
                    ).sha256,
                    "proposal_schema": fingerprint(
                        self.schema_directory / "holistic-multi-surface-proposal.schema.json"
                    ).sha256,
                    "relationship_schema": fingerprint(
                        self.schema_directory / "relationship.schema.json"
                    ).sha256,
                }
            )
        ).hexdigest()
        strategy = f"{strategy}:contract-{contract_sha256}"
        self.state.initialize()
        projections = {source_id: self._projection(source_id) for source_id in sources}
        fingerprints = {
            source_id: projection.input_fingerprint for source_id, projection in projections.items()
        }
        batch_identifier = cross_reference_batch_id(
            fingerprints.items(),
            requested_through="compiled",
            model=model,
            strategy=strategy,
        )
        self.state.register_cross_reference_batch(
            batch_identifier,
            source_ids=sources,
            input_fingerprints=fingerprints,
            requested_through="compiled",
            model=model,
            strategy=strategy,
            eligible_source_states=self.eligible_source_states,
        )
        coverage_path = (
            self.cache_root / "cross-reference" / batch_identifier / "pair-coverage.json"
        )
        batch = self.state.cross_reference_batch_record(batch_identifier)
        if batch is None:
            raise CrossReferenceExecutionError("owned-pair batch disappeared")
        state = str(batch["state"])
        semantic_jobs_before = {
            str(job["job_id"])
            for job in self.state.cross_reference_jobs_for_batch(batch_identifier)
        }
        peak = 0
        if state == "created":
            self.state.advance_cross_reference_batch(
                batch_identifier,
                "discovering",
                receipt={"kind": "owned_pair_discovery_started"},
            )
            state = "discovering"
        if state == "discovering":
            active = 0
            lock = threading.Lock()

            def execute(shard: OwnedPairShard) -> tuple[str, ...]:
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                try:
                    return self._cross_reference_holistic_job(
                        batch_identifier,
                        sources=shard.source_ids,
                        projections=projections,
                        model=model,
                        strategy=strategy,
                        protocol_name="holistic-multi-surface.md",
                        schema_name="holistic-multi-surface-proposal.schema.json",
                        prompt=HOLISTIC_PROMPT,
                        multi_surface=True,
                        owned_pairs=frozenset(shard.owned_pairs),
                        job_anchor_pair=shard.owned_pairs[0],
                        shard_id=shard.shard_id,
                    )
                finally:
                    with lock:
                        active -= 1

            future_shards: dict[Future[tuple[str, ...]], OwnedPairShard] = {}
            with ThreadPoolExecutor(max_workers=2) as executor:
                for shard in shards:
                    future_shards[executor.submit(execute, shard)] = shard
                outcomes: dict[str, dict[str, Any]] = {}
                for future in as_completed(future_shards):
                    shard = future_shards[future]
                    job_id = holistic_cross_reference_job_id(batch_identifier, shard.source_ids, 1)
                    outcome: dict[str, Any]
                    try:
                        shard_warnings = future.result()
                    except (CrossReferenceExecutionError, KeyError, TypeError, ValueError) as error:
                        outcome = {
                            "shard_id": shard.shard_id,
                            "job_id": job_id,
                            "source_ids": list(shard.source_ids),
                            "owned_pairs": [list(pair) for pair in shard.owned_pairs],
                            "status": "failed",
                            "failure_class": type(error).__name__,
                            "failure_reason": str(error),
                            "warnings": [],
                        }
                    else:
                        outcome = {
                            "shard_id": shard.shard_id,
                            "job_id": job_id,
                            "source_ids": list(shard.source_ids),
                            "owned_pairs": [list(pair) for pair in shard.owned_pairs],
                            "status": "succeeded",
                            "failure_class": None,
                            "failure_reason": None,
                            "warnings": list(shard_warnings),
                        }
                    telemetry = _cross_reference_job_telemetry(
                        state=self.state,
                        cache_root=self.cache_root,
                        batch_id=batch_identifier,
                        job_id=job_id,
                    )
                    outcomes[shard.shard_id] = {**outcome, **telemetry}
            ordered_outcomes = [outcomes[shard.shard_id] for shard in shards]
            failed_pairs = {
                tuple(pair)
                for outcome in ordered_outcomes
                if outcome["status"] == "failed"
                for pair in outcome["owned_pairs"]
            }
            uncovered = tuple(sorted({*pre_uncovered_pairs, *failed_pairs}))
            successful_pairs = {
                tuple(pair)
                for outcome in ordered_outcomes
                if outcome["status"] == "succeeded"
                for pair in outcome["owned_pairs"]
            }
            _validate_pair_outcomes(
                sources,
                successful_pairs=successful_pairs,
                uncovered_pairs=set(uncovered),
            )
            semantic_jobs = len(self.state.cross_reference_jobs_for_batch(batch_identifier))
            coverage_telemetry = _aggregate_shard_telemetry(ordered_outcomes)
            coverage = {
                "kind": "owned_pair_cross_reference_coverage",
                "batch_id": batch_identifier,
                "strategy": strategy,
                "source_ids": list(sources),
                "pair_count": len(tuple(itertools.combinations(sources, 2))),
                "successful_pairs": [list(pair) for pair in sorted(successful_pairs)],
                "uncovered_pairs": [list(pair) for pair in uncovered],
                "shards": ordered_outcomes,
                "semantic_jobs_started": semantic_jobs,
                "duration_seconds": coverage_telemetry["duration_seconds"],
                "worker_events_emitted": coverage_telemetry["worker_events_emitted"],
                "token_usage": coverage_telemetry["token_usage"],
                "peak_mapper_concurrency": peak,
            }
            _write_or_verify_receipt(coverage_path, coverage)
            self.state.advance_cross_reference_batch(
                batch_identifier,
                "discovered",
                receipt={
                    "kind": "owned_pair_discovery_complete",
                    "pair_count": coverage["pair_count"],
                    "uncovered_pair_count": len(uncovered),
                    "coverage_sha256": fingerprint(coverage_path).sha256,
                },
            )
            state = "discovered"
        if not coverage_path.is_file():
            raise CrossReferenceExecutionError("owned-pair coverage receipt is missing")
        coverage = read_receipt(coverage_path)
        raw_source_ids = coverage.get("source_ids")
        raw_successful_pairs = coverage.get("successful_pairs")
        raw_uncovered_pairs = coverage.get("uncovered_pairs")
        raw_shards = coverage.get("shards")
        raw_peak = coverage.get("peak_mapper_concurrency")
        raw_semantic_jobs = coverage.get("semantic_jobs_started")
        if (
            coverage.get("batch_id") != batch_identifier
            or coverage.get("strategy") != strategy
            or not isinstance(raw_source_ids, list)
            or tuple(str(item) for item in raw_source_ids) != sources
            or not isinstance(raw_successful_pairs, list)
            or not isinstance(raw_uncovered_pairs, list)
            or not isinstance(raw_shards, list)
            or not all(isinstance(item, dict) for item in raw_shards)
            or not isinstance(raw_peak, int)
            or not isinstance(raw_semantic_jobs, int)
            or isinstance(raw_semantic_jobs, bool)
            or raw_semantic_jobs < 0
        ):
            raise CrossReferenceValidationError("owned-pair coverage identity changed")
        successful_pairs = _receipt_pairs(raw_successful_pairs, label="successful owned pairs")
        uncovered_pairs = _receipt_pairs(raw_uncovered_pairs, label="uncovered owned pairs")
        shard_receipts = tuple(dict(item) for item in raw_shards)
        if len(shard_receipts) != len(shards):
            raise CrossReferenceValidationError("owned-pair shard receipt count changed")
        for observed, expected in zip(shard_receipts, shards, strict=True):
            expected_job_id = holistic_cross_reference_job_id(
                batch_identifier, expected.source_ids, 1
            )
            raw_observed_pairs = observed.get("owned_pairs")
            if not isinstance(raw_observed_pairs, list):
                raise CrossReferenceValidationError("owned-pair shard pairs changed")
            expected_telemetry = _cross_reference_job_telemetry(
                state=self.state,
                cache_root=self.cache_root,
                batch_id=batch_identifier,
                job_id=expected_job_id,
            )
            if (
                observed.get("shard_id") != expected.shard_id
                or observed.get("job_id") != expected_job_id
                or observed.get("source_ids") != list(expected.source_ids)
                or _receipt_pairs(raw_observed_pairs, label="shard owned pairs")
                != set(expected.owned_pairs)
                or any(observed.get(key) != value for key, value in expected_telemetry.items())
            ):
                raise CrossReferenceValidationError(
                    "owned-pair shard identity or telemetry changed"
                )
        aggregate_telemetry = _aggregate_shard_telemetry(list(shard_receipts))
        if (
            raw_semantic_jobs
            != sum(bool(shard["semantic_job_started"]) for shard in shard_receipts)
            or coverage.get("duration_seconds") != aggregate_telemetry["duration_seconds"]
            or coverage.get("worker_events_emitted") != aggregate_telemetry["worker_events_emitted"]
            or coverage.get("token_usage") != aggregate_telemetry["token_usage"]
        ):
            raise CrossReferenceValidationError("owned-pair coverage telemetry changed")
        _validate_pair_outcomes(
            sources,
            successful_pairs=successful_pairs,
            uncovered_pairs=uncovered_pairs,
        )
        if state == "discovered":
            self.state.advance_cross_reference_batch(
                batch_identifier,
                "inspecting",
                receipt={"kind": "owned_pair_validation_started"},
            )
            state = "inspecting"
        if state == "inspecting":
            self.state.advance_cross_reference_batch(
                batch_identifier,
                "inspected",
                receipt={"kind": "owned_pair_validation_complete"},
            )
            state = "inspected"
        if state in {"inspected", "compiled"}:
            materialization = self._compile_batch(
                batch_identifier,
                sources=sources,
                projections=projections,
            )
            if state == "inspected":
                self.state.advance_cross_reference_batch(
                    batch_identifier,
                    "compiled",
                    receipt={
                        "kind": "owned_pair_corpus_graph_compiled",
                        "uncovered_pair_count": len(uncovered_pairs),
                        "graph": materialization.artifacts[0],
                        "manifest": materialization.artifacts[1],
                    },
                )
        result = self._result(
            batch_identifier,
            warnings=tuple(
                warning for shard in shard_receipts for warning in _receipt_warnings(shard)
            ),
        )
        current_job_ids = {
            str(job["job_id"])
            for job in self.state.cross_reference_jobs_for_batch(batch_identifier)
        }
        new_job_ids = current_job_ids.difference(semantic_jobs_before)
        invocation_shards = [
            shard for shard in shard_receipts if str(shard.get("job_id")) in new_job_ids
        ]
        invocation_telemetry = _aggregate_shard_telemetry(invocation_shards)
        return CrossReferenceRunResult(
            batch_id=result.batch_id,
            strategy=result.strategy,
            source_ids=result.source_ids,
            source_groups=result.source_groups,
            state=result.state,
            job_ids=result.job_ids,
            candidate_count=result.candidate_count,
            outcome_counts=result.outcome_counts,
            relationship_ids=result.relationship_ids,
            admission_counts=result.admission_counts,
            admission_receipts=result.admission_receipts,
            proposal_diagnostics=result.proposal_diagnostics,
            owned_pair_count=len(tuple(itertools.combinations(sources, 2))),
            uncovered_pairs=tuple(sorted(uncovered_pairs)),
            shard_receipts=shard_receipts,
            semantic_jobs_started=len(new_job_ids),
            duration_seconds=invocation_telemetry["duration_seconds"],
            worker_events_emitted=invocation_telemetry["worker_events_emitted"],
            token_usage=invocation_telemetry["token_usage"],
            peak_mapper_concurrency=raw_peak,
            warnings=result.warnings,
        )

    def _preflight_retained_revalidation(self, batch_id: str) -> None:
        if not self.allow_failed_proposal_revalidation:
            raise CrossReferenceValidationError(
                "retained proposal revalidation is not enabled for this coordinator"
            )
        batch = self.state.cross_reference_batch_record(batch_id)
        if batch is None or batch["state"] not in {"discovered", "inspected", "compiled"}:
            raise CrossReferenceValidationError(
                "retained proposal revalidation requires a terminal discovered batch"
            )
        jobs = self.state.cross_reference_jobs_for_batch(batch_id)
        if not jobs:
            raise CrossReferenceValidationError(
                "retained proposal revalidation requires existing jobs"
            )
        for job in jobs:
            if job["kind"] != HOLISTIC_JOB_KIND or job["state"] not in {
                "validation_failed",
                "succeeded",
            }:
                raise CrossReferenceValidationError(
                    "retained proposal revalidation accepts only terminal holistic jobs: "
                    f"{job['job_id']}; state={job['state']}"
                )
            workspace = self.cache_root / "cross-reference" / batch_id / "jobs" / str(job["job_id"])
            output_path = workspace / "output" / "proposal.json"
            receipt_path = (
                self.cache_root
                / "cross-reference"
                / batch_id
                / "receipts"
                / f"{job['job_id']}.json"
            )
            if not _valid_retained_output(receipt_path, output_path):
                raise CrossReferenceValidationError(
                    f"retained proposal or its original worker receipt is invalid: {job['job_id']}"
                )

    def _projection(self, source_id: str) -> SourceProjection:
        observed_state = self.state.source_state(source_id)
        if observed_state not in self.eligible_source_states:
            raise CrossReferenceValidationError(
                "cross-reference source is not in an eligible state: "
                f"{source_id}; state={observed_state}; "
                f"eligible={sorted(self.eligible_source_states)}"
            )
        canonical_path = self.vault_root / source_id / "paper.md"
        graph_path = self.build_root / source_id / "graph.json"
        contexts_path = self.build_root / source_id / "context-envelopes.jsonl"
        manifest_path = self.build_root / source_id / "manifest.json"
        for path in (canonical_path, graph_path, contexts_path, manifest_path):
            if not path.is_file():
                raise CrossReferenceValidationError(
                    f"cross-reference source projection is missing: {path}"
                )
        source = resolve_source(source_id, repository_root=self.repository_root)
        for asset in source.assets:
            verify_asset(asset)
        document = load_markdown(canonical_path)
        if document.dossier.source_id != source_id:
            raise CrossReferenceValidationError("canonical dossier source identity changed")
        graph = _load_object(graph_path)
        manifest = _load_object(manifest_path)
        if graph.get("source_id") != source_id or manifest.get("source_id") != source_id:
            raise CrossReferenceValidationError("compiled source projection identity changed")
        _verify_projection_manifest(
            manifest,
            source_asset={
                "asset_id": source.main_asset.asset_id,
                "sha256": source.main_asset.sha256,
                "size_bytes": source.main_asset.size_bytes,
            },
            artifacts={
                "canonical_markdown": canonical_path,
                "source_graph": graph_path,
                "context_envelopes": contexts_path,
            },
        )
        contexts: dict[str, dict[str, Any]] = {}
        for line in contexts_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            context = json.loads(line)
            if not isinstance(context, dict) or context.get("source_id") != source_id:
                raise CrossReferenceValidationError("context envelope identity changed")
            contexts[str(context["atom_id"])] = context
        records = document.dossier.index()
        atoms = [
            record
            for record in document.dossier.records_of_type("atom")
            if record.payload.get("connectable") is True
        ]
        catalogue = {
            "schema_version": 1,
            "source_id": source_id,
            "atoms": [
                {
                    "record_id": atom.id,
                    "revision": atom.revision,
                    "record_sha256": record_fingerprint(atom.payload),
                    "kind": atom.payload["kind"],
                    "label": atom.payload["label"],
                    "statement": atom.payload["statement"],
                    "scope": atom.payload["scope"],
                    "attribution": atom.payload["attribution"],
                    "epistemic_posture": atom.payload["epistemic_posture"],
                    "qualifications": atom.payload["qualifications"],
                    "standalone_context": atom.payload["standalone_context"],
                }
                for atom in sorted(atoms, key=lambda item: item.id)
            ],
        }
        identity = {
            "source_id": source_id,
            "canonical": fingerprint(canonical_path).sha256,
            "graph": fingerprint(graph_path).sha256,
            "contexts": fingerprint(contexts_path).sha256,
            "manifest": fingerprint(manifest_path).sha256,
            "asset": source.main_asset.sha256,
        }
        return SourceProjection(
            source_id=source_id,
            input_fingerprint=hashlib.sha256(canonical_json_bytes(identity)).hexdigest(),
            dossier=document.dossier,
            records=records,
            contexts=contexts,
            catalogue=catalogue,
            graph_path=graph_path,
        )

    def _compile_batch(
        self,
        batch_id: str,
        *,
        sources: tuple[str, ...],
        projections: dict[str, SourceProjection],
        replace_existing: bool = False,
    ) -> CorpusGraphMaterialization:
        raw_relationships = self.state.cross_reference_relationships_for_batch(batch_id)
        relationships = tuple(
            Relationship.from_mapping({key: value for key, value in raw.items() if key != "state"})
            for raw in raw_relationships
        )
        grouped: dict[tuple[str, str], list[Relationship]] = {
            pair: [] for pair in itertools.combinations(sources, 2)
        }
        for relationship in relationships:
            left = EndpointRef.from_mapping(relationship.payload["left_endpoint"])
            right = EndpointRef.from_mapping(relationship.payload["right_endpoint"])
            grouped[canonical_source_pair(left.source_id, right.source_id)].append(relationship)
        relationship_paths: list[Path] = []
        try:
            for pair, pair_relationships in sorted(grouped.items()):
                path = self.relationship_root / f"{pair[0]}--{pair[1]}" / "relationships.md"
                document = materialize_relationship_markdown(
                    path,
                    source_pair=pair,
                    relationships=tuple(pair_relationships),
                )
                expected = {item.relationship_id: item.to_dict() for item in pair_relationships}
                observed = {
                    item.relationship_id: item.to_dict()
                    for item in document.relationships
                    if item.batch_id == batch_id
                }
                if observed != expected:
                    raise CrossReferenceValidationError(
                        "canonical relationship Markdown differs from durable batch state"
                    )
                relationship_paths.append(path)
            materialization = CorpusGraphCompiler(
                repository_root=self.repository_root,
                build_root=self.build_root,
                schema_directory=self.schema_directory,
            ).materialize(
                batch_id=batch_id,
                source_graph_paths={
                    source_id: projections[source_id].graph_path for source_id in sources
                },
                relationship_paths=tuple(relationship_paths),
                replace_existing=replace_existing,
            )
        except (CorpusGraphError, RelationshipMarkdownError) as error:
            raise CrossReferenceValidationError(str(error)) from error

        for pair, path in zip(sorted(grouped), relationship_paths, strict=True):
            item = fingerprint(path)
            kind = f"canonical_relationship_markdown:{pair[0]}--{pair[1]}"
            self.state.register_cross_reference_artifact(
                artifact_id(batch_id, kind, item.sha256),
                batch_identifier=batch_id,
                job_identifier=None,
                kind="canonical_relationship_markdown",
                path=item.path,
                sha256=item.sha256,
                size_bytes=item.size_bytes,
                metadata={"source_pair": list(pair)},
            )
        for kind, path in (
            ("corpus_graph", materialization.graph_path),
            ("corpus_manifest", materialization.manifest_path),
        ):
            item = fingerprint(path)
            self.state.register_cross_reference_artifact(
                artifact_id(batch_id, kind, item.sha256),
                batch_identifier=batch_id,
                job_identifier=None,
                kind=kind,
                path=item.path,
                sha256=item.sha256,
                size_bytes=item.size_bytes,
                metadata={"source_ids": list(sources)},
            )
        return materialization

    def _cross_reference_holistically(
        self,
        batch_id: str,
        *,
        sources: tuple[str, ...],
        projections: dict[str, SourceProjection],
        model: str,
        source_groups: tuple[tuple[str, ...], ...],
        strategy: str,
    ) -> tuple[str, ...]:
        if not source_groups:
            return self._cross_reference_holistic_job(
                batch_id,
                sources=sources,
                projections=projections,
                model=model,
                strategy=strategy,
                protocol_name="holistic-multi-surface.md",
                schema_name="holistic-multi-surface-proposal.schema.json",
                prompt=HOLISTIC_PROMPT,
                multi_surface=True,
                owned_pairs=frozenset(itertools.combinations(sources, 2)),
            )

        warnings: list[str] = []
        for shard in _source_shards(source_groups):
            try:
                warnings.extend(
                    self._cross_reference_holistic_job(
                        batch_id,
                        sources=shard.source_ids,
                        projections=projections,
                        model=model,
                        strategy=strategy,
                        protocol_name="source-sharded-cross-reference.md",
                        schema_name="holistic-cross-reference-proposal.schema.json",
                        prompt=SOURCE_SHARDED_PROMPT,
                        focus_sources=shard.focus_sources,
                        context_sources=shard.context_sources,
                        owned_pairs=frozenset(shard.owned_pairs),
                        job_anchor_pair=canonical_source_pair(
                            shard.focus_sources[0], shard.context_sources[0]
                        ),
                    )
                )
            except (CrossReferenceExecutionError, KeyError, TypeError, ValueError) as error:
                warnings.append(
                    "source-sharded cross-reference job failed without retry: "
                    f"focus={list(shard.focus_sources)}, "
                    f"context={list(shard.context_sources)}, error={error}"
                )
        return tuple(warnings)

    def _cross_reference_holistic_job(
        self,
        batch_id: str,
        *,
        sources: tuple[str, ...],
        projections: dict[str, SourceProjection],
        model: str,
        strategy: str,
        protocol_name: str,
        schema_name: str,
        prompt: str,
        multi_surface: bool = False,
        focus_sources: tuple[str, ...] = (),
        context_sources: tuple[str, ...] = (),
        owned_pairs: frozenset[tuple[str, str]] | None = None,
        job_anchor_pair: tuple[str, str] | None = None,
        shard_id: str | None = None,
    ) -> tuple[str, ...]:
        job_identifier = holistic_cross_reference_job_id(batch_id, sources, 1)
        existing_relationships = self._existing_relationships(
            source_ids=frozenset(sources), exclude_batch_id=batch_id
        )
        if owned_pairs is not None:
            existing_relationships = tuple(
                relationship
                for relationship in existing_relationships
                if _relationship_source_pair(relationship) in owned_pairs
            )
        corpus_bundle = {
            "schema_version": 1,
            "batch_id": batch_id,
            "strategy": strategy,
            "sources": [
                {
                    "source_id": source_id,
                    "input_fingerprint": projections[source_id].input_fingerprint,
                    "dossier": projections[source_id].dossier.to_dict(),
                }
                for source_id in sources
            ],
        }
        if not multi_surface:
            corpus_bundle["existing_relationships"] = [
                relationship.to_dict() for relationship in existing_relationships
            ]
        input_sha256 = hashlib.sha256(canonical_json_bytes(corpus_bundle)).hexdigest()
        task = {
            "schema_version": 1,
            "batch_id": batch_id,
            "job_id": job_identifier,
            "stage": HOLISTIC_JOB_KIND,
            "strategy": strategy,
            "source_ids": list(sources),
            "source_record_counts": {
                source_id: len(projections[source_id].records) for source_id in sources
            },
            "input_sha256": input_sha256,
            "authorized_inputs": ["input/corpus-records.json"],
            "forbidden_inputs": ["PDFs", "source-reader scratch", "new scientific nodes"],
        }
        if multi_surface:
            assert owned_pairs is not None
            declared_pairs = sorted(owned_pairs)
            task.update(
                {
                    "topology": "holistic_multi_surface_single_call",
                    "owned_source_pairs": [
                        {
                            "pair_key": _multi_surface_pair_key(*pair),
                            "source_ids": list(pair),
                        }
                        for pair in declared_pairs
                    ],
                    "expected_pair_count": len(declared_pairs),
                    "comparison_surfaces_per_pair": "zero_or_many",
                    "comparison_surface_quota": None,
                    "relationship_quota": None,
                    "current_relationships_available": False,
                }
            )
            if shard_id is not None:
                task["shard_id"] = shard_id
        elif owned_pairs is not None:
            task.update(
                {
                    "topology": "cyclic_source_shards",
                    "focus_source_ids": list(focus_sources),
                    "context_source_ids": list(context_sources),
                    "owned_source_pairs": [list(pair) for pair in sorted(owned_pairs)],
                }
            )
        retained_job = self.state.cross_reference_job_record(job_identifier)
        if retained_job is not None and retained_job["state"] in {
            "validation_failed",
            "succeeded",
        }:
            workspace_path = (
                self.cache_root / "cross-reference" / batch_id / "jobs" / job_identifier
            )
            retained_task = _load_object(workspace_path / "input" / "task.json")
            retained_corpus = _load_object(workspace_path / "input" / "corpus-records.json")
            if retained_task != task or retained_corpus != corpus_bundle:
                raise CrossReferenceValidationError(
                    "retained holistic workspace inputs differ from frozen job identity"
                )
            manifest_path = workspace_path / "manifest.json"
            workspace = Workspace(
                path=workspace_path,
                manifest_path=manifest_path,
                manifest=_load_object(manifest_path),
            )
            protocol_path = workspace_path / "AGENTS.md"
        else:
            workspace = self._workspace(
                batch_id=batch_id,
                job_id=job_identifier,
                source_ids=sources,
                kind="cross_reference_holistic",
                protocol_name=protocol_name,
                schema_name=schema_name,
                task=task,
                inputs=(("corpus-records.json", "canonical_corpus_records", corpus_bundle),),
            )
            protocol_path = self.protocol_directory / protocol_name
        output_path = self._execute_job(
            batch_id=batch_id,
            job_id=job_identifier,
            job_kind=HOLISTIC_JOB_KIND,
            pair=job_anchor_pair or (sources[0], sources[-1]),
            source_ids=sources,
            attempt=1,
            model=model,
            workspace=workspace,
            schema_name=schema_name,
            prompt=prompt,
        )
        try:
            proposal = _load_object(output_path)
            validate_schema(
                proposal,
                schema_directory=self.schema_directory,
                schema_name=schema_name,
            )
            if proposal["batch_id"] != batch_id:
                raise CrossReferenceValidationError("holistic job batch identity changed")
            if tuple(proposal["source_ids"]) != sources:
                raise CrossReferenceValidationError("holistic job source scope changed")
            all_records = {
                record_id: record
                for source_id in sources
                for record_id, record in projections[source_id].records.items()
            }
            evidence_by_source = {
                source_id: frozenset(
                    record.id
                    for record in projections[source_id].dossier.records_of_type("evidence")
                )
                for source_id in sources
            }
            diagnostics: MultiSurfaceProposalDiagnostics | None = None
            admission_proposal: dict[str, Any] = proposal
            if multi_surface:
                diagnostics = _validate_multi_surface_contract(
                    proposal,
                    batch_id=batch_id,
                    source_ids=sources,
                    records=all_records,
                    evidence_ids_by_source=evidence_by_source,
                    expected_pairs=tuple(sorted(owned_pairs or ())),
                )
                admission_proposal = multi_surface_admission_projection(
                    proposal,
                    existing_relationship_ids=tuple(
                        relationship.relationship_id for relationship in existing_relationships
                    ),
                )
            plan = compile_holistic_admission(
                admission_proposal,
                batch_id=batch_id,
                job_id=job_identifier,
                raw_proposal_sha256=fingerprint(output_path).sha256,
                records=all_records,
                evidence_ids_by_source=evidence_by_source,
                existing_relationships=existing_relationships,
                owned_pairs=owned_pairs,
                model=model,
                input_sha256=input_sha256,
                protocol_sha256=fingerprint(protocol_path).sha256,
                relationship_schema_sha256=fingerprint(
                    self.schema_directory / "relationship.schema.json"
                ).sha256,
                schema_directory=self.schema_directory,
            )

            for item in plan.admitted:
                if item.candidate is None or item.outcome is None or item.relationship is None:
                    raise CrossReferenceValidationError(
                        "admitted relationship lacks prepared durable records"
                    )
                candidate = item.candidate
                self.state.record_cross_reference_candidate(candidate)
                candidate_row = self.state.cross_reference_candidate_record(candidate.candidate_id)
                if candidate_row is None:
                    raise CrossReferenceValidationError("holistic candidate disappeared")
                if candidate_row["state"] in TERMINAL_CANDIDATE_STATES:
                    if candidate_row["outcome"] != item.outcome:
                        raise CrossReferenceValidationError(
                            "terminal holistic outcome changed during replay"
                        )
                    continue
                if candidate_row["state"] == "discovered":
                    self.state.advance_cross_reference_candidate(
                        candidate.candidate_id,
                        "inspecting",
                        inspection_job_identifier=job_identifier,
                        receipt={"kind": "holistic_relationship_validation_started"},
                    )
                self.state.advance_cross_reference_candidate(
                    candidate.candidate_id,
                    "relationship",
                    inspection_job_identifier=job_identifier,
                    outcome=item.outcome,
                    receipt={"kind": "holistic_relationship_validated"},
                )
                self.state.record_cross_reference_relationship(
                    item.relationship, inspection_job_identifier=job_identifier
                )
            admission_receipt = self._record_admission_receipt(
                batch_id,
                job_identifier,
                output_path=output_path,
                plan=plan,
            )
            self._succeed_job(
                job_identifier,
                record_count=len(plan.items),
                admission_counts=plan.counts,
                admission_receipt=str(admission_receipt),
                proposal_diagnostics=diagnostics,
            )
            self._record_output_artifact(batch_id, job_identifier, output_path)
            warnings = [str(item) for item in proposal["warnings"]]
            if plan.counts["quarantined"]:
                warnings.append(
                    "relationship-local admission quarantined "
                    f"{plan.counts['quarantined']} of {len(plan.items)} proposed rows; "
                    f"receipt={admission_receipt}"
                )
            return tuple(warnings)
        except (KeyError, TypeError, ValueError) as error:
            self._fail_job(job_identifier, str(error))
            raise

    def _existing_relationships(
        self, *, source_ids: frozenset[str], exclude_batch_id: str
    ) -> tuple[Relationship, ...]:
        relationships: dict[str, Relationship] = {}
        if not self.relationship_root.is_dir():
            return ()
        try:
            for path in sorted(self.relationship_root.glob("*/relationships.md")):
                document = load_relationship_markdown(path)
                for relationship in document.relationships:
                    left = EndpointRef.from_mapping(relationship.payload["left_endpoint"])
                    right = EndpointRef.from_mapping(relationship.payload["right_endpoint"])
                    if relationship.batch_id != exclude_batch_id and {
                        left.source_id,
                        right.source_id,
                    }.issubset(source_ids):
                        relationships[relationship.relationship_id] = relationship
        except RelationshipMarkdownError as error:
            raise CrossReferenceValidationError(str(error)) from error
        return tuple(relationships[key] for key in sorted(relationships))

    def _discover_pair(
        self,
        batch_id: str,
        left: SourceProjection,
        right: SourceProjection,
        *,
        model: str,
    ) -> tuple[str, ...]:
        pair = canonical_source_pair(left.source_id, right.source_id)
        job_identifier = cross_reference_job_id(batch_id, DISCOVERY_JOB_KIND, *pair, 1)
        task = {
            "schema_version": 1,
            "batch_id": batch_id,
            "job_id": job_identifier,
            "stage": DISCOVERY_JOB_KIND,
            "source_pair": list(pair),
            "authorized_inputs": [
                "input/catalog-left.json",
                "input/catalog-right.json",
            ],
            "forbidden_inputs": ["PDFs", "evidence records", "source-reader scratch"],
        }
        workspace = self._workspace(
            batch_id=batch_id,
            job_id=job_identifier,
            source_ids=pair,
            kind="cross_reference_discovery",
            protocol_name="cross-reference-discovery.md",
            schema_name="discovery-proposal.schema.json",
            task=task,
            inputs=(
                ("catalog-left.json", "atom_catalogue", left.catalogue),
                ("catalog-right.json", "atom_catalogue", right.catalogue),
            ),
        )
        output_path = self._execute_job(
            batch_id=batch_id,
            job_id=job_identifier,
            job_kind=DISCOVERY_JOB_KIND,
            pair=pair,
            attempt=1,
            model=model,
            workspace=workspace,
            schema_name="discovery-proposal.schema.json",
            prompt=(
                "Read AGENTS.md, input/task.json, and both atom catalogues. Return only "
                "existing endpoint pairs worth evidence-bounded inspection."
            ),
        )
        try:
            proposal = _load_object(output_path)
            validate_schema(
                proposal,
                schema_directory=self.schema_directory,
                schema_name="discovery-proposal.schema.json",
            )
            if tuple(proposal["source_pair"]) != pair:
                raise CrossReferenceValidationError("discovery source pair changed")
            seen: set[tuple[str, str]] = set()
            all_records = {**left.records, **right.records}
            for raw in proposal["candidates"]:
                left_id = str(raw["left_record_id"])
                right_id = str(raw["right_record_id"])
                endpoint_pair = (left_id, right_id)
                if endpoint_pair in seen:
                    raise CrossReferenceValidationError("discovery repeated an endpoint pair")
                seen.add(endpoint_pair)
                left_record = left.records.get(left_id)
                right_record = right.records.get(right_id)
                if left_record is None or right_record is None:
                    raise CrossReferenceValidationError(
                        "discovery returned an endpoint outside its source catalogue"
                    )
                candidate = DiscoveryCandidate.create(
                    batch_id=batch_id,
                    left_endpoint=EndpointRef.from_record(left_record),
                    right_endpoint=EndpointRef.from_record(right_record),
                    comparison_surface=str(raw["comparison_surface"]),
                    discovery_job_id=job_identifier,
                )
                validate_candidate_endpoints(candidate, records=all_records)
                validate_schema(
                    candidate.to_dict(),
                    schema_directory=self.schema_directory,
                    schema_name="discovery-candidate.schema.json",
                )
                self.state.record_cross_reference_candidate(candidate)
            self._succeed_job(job_identifier, record_count=len(seen))
            self._record_output_artifact(batch_id, job_identifier, output_path)
            return tuple(str(item) for item in proposal["warnings"])
        except (KeyError, TypeError, ValueError) as error:
            self._fail_job(job_identifier, str(error))
            raise

    def _inspect_candidates(
        self,
        batch_id: str,
        *,
        sources: tuple[str, ...],
        projections: dict[str, SourceProjection],
        model: str,
    ) -> tuple[str, ...]:
        warnings: list[str] = []
        rows = self.state.cross_reference_candidates_for_batch(batch_id)
        for pair in itertools.combinations(sources, 2):
            pair_rows = [row for row in rows if tuple(row["source_pair"]) == pair]
            packets = _partition_packets(
                [self._inspection_item(row, projections) for row in pair_rows],
                max_bytes=self.max_inspection_packet_bytes,
            )
            for packet_number, packet in enumerate(packets, start=1):
                warnings.extend(
                    self._inspect_packet(
                        batch_id,
                        pair=pair,
                        packet_number=packet_number,
                        packet=packet,
                        projections=projections,
                        model=model,
                    )
                )
        return tuple(warnings)

    def _inspection_item(
        self, row: dict[str, Any], projections: dict[str, SourceProjection]
    ) -> dict[str, Any]:
        candidate = DiscoveryCandidate.from_mapping(
            {
                "schema_version": 1,
                **{
                    key: value
                    for key, value in row.items()
                    if key
                    in {
                        "candidate_id",
                        "batch_id",
                        "source_pair",
                        "left_endpoint",
                        "right_endpoint",
                        "comparison_surface",
                        "discovery_job_id",
                    }
                },
                "source_pair": list(row["source_pair"]),
            }
        )
        return {
            "candidate": candidate.to_dict(),
            "left_context": _endpoint_context(
                candidate.left_endpoint, projections[candidate.left_endpoint.source_id]
            ),
            "right_context": _endpoint_context(
                candidate.right_endpoint, projections[candidate.right_endpoint.source_id]
            ),
            "existing_relationships": [],
        }

    def _inspect_packet(
        self,
        batch_id: str,
        *,
        pair: tuple[str, str],
        packet_number: int,
        packet: list[dict[str, Any]],
        projections: dict[str, SourceProjection],
        model: str,
    ) -> tuple[str, ...]:
        job_identifier = cross_reference_job_id(batch_id, INSPECTION_JOB_KIND, *pair, packet_number)
        packet_payload = {"schema_version": 1, "candidates": packet}
        packet_bytes = canonical_json_bytes(packet_payload)
        task = {
            "schema_version": 1,
            "batch_id": batch_id,
            "job_id": job_identifier,
            "stage": INSPECTION_JOB_KIND,
            "source_pair": list(pair),
            "candidate_ids": [item["candidate"]["candidate_id"] for item in packet],
            "input_sha256": hashlib.sha256(packet_bytes).hexdigest(),
            "forbidden_inputs": ["PDFs", "unselected records", "source-reader scratch"],
        }
        workspace = self._workspace(
            batch_id=batch_id,
            job_id=job_identifier,
            source_ids=pair,
            kind="cross_reference_inspection",
            protocol_name="cross-reference-inspection.md",
            schema_name="inspection-proposal.schema.json",
            task=task,
            inputs=(("inspection-packet.json", "inspection_packet", packet_payload),),
        )
        output_path = self._execute_job(
            batch_id=batch_id,
            job_id=job_identifier,
            job_kind=INSPECTION_JOB_KIND,
            pair=pair,
            attempt=packet_number,
            model=model,
            workspace=workspace,
            schema_name="inspection-proposal.schema.json",
            prompt=(
                "Read AGENTS.md, input/task.json, and the complete bounded inspection "
                "packet. Return one terminal evidence-grounded outcome per candidate."
            ),
        )
        for item in packet:
            candidate_id = str(item["candidate"]["candidate_id"])
            row = self.state.cross_reference_candidate_record(candidate_id)
            if row is not None and row["state"] == "discovered":
                self.state.advance_cross_reference_candidate(
                    candidate_id,
                    "inspecting",
                    inspection_job_identifier=job_identifier,
                    receipt={"kind": "candidate_inspection_started"},
                )
        try:
            proposal = _load_object(output_path)
            validate_schema(
                proposal,
                schema_directory=self.schema_directory,
                schema_name="inspection-proposal.schema.json",
            )
            expected_ids = {str(item["candidate"]["candidate_id"]) for item in packet}
            observed_ids = [str(item["candidate_id"]) for item in proposal["outcomes"]]
            if set(observed_ids) != expected_ids or len(observed_ids) != len(expected_ids):
                raise CrossReferenceValidationError(
                    "inspection must return every candidate exactly once"
                )
            all_records = {
                record_id: record
                for source in pair
                for record_id, record in projections[source].records.items()
            }
            evidence_by_source = {
                source: frozenset(
                    record.id for record in projections[source].dossier.records_of_type("evidence")
                )
                for source in pair
            }
            existing_ids = {
                str(item["relationship_id"])
                for item in self.state.cross_reference_relationships_for_batch(batch_id)
            }
            for raw in proposal["outcomes"]:
                candidate_id = str(raw["candidate_id"])
                candidate_row = self.state.cross_reference_candidate_record(candidate_id)
                if candidate_row is None:
                    raise CrossReferenceValidationError("inspection candidate disappeared")
                candidate = DiscoveryCandidate.from_mapping(
                    {
                        "schema_version": 1,
                        "candidate_id": candidate_row["candidate_id"],
                        "batch_id": candidate_row["batch_id"],
                        "source_pair": list(candidate_row["source_pair"]),
                        "left_endpoint": candidate_row["left_endpoint"],
                        "right_endpoint": candidate_row["right_endpoint"],
                        "comparison_surface": candidate_row["comparison_surface"],
                        "discovery_job_id": candidate_row["discovery_job_id"],
                    }
                )
                outcome = self._canonical_outcome(
                    raw,
                    candidate=candidate,
                    batch_id=batch_id,
                    job_id=job_identifier,
                    model=model,
                    input_sha256=str(task["input_sha256"]),
                )
                if candidate_row["state"] in TERMINAL_CANDIDATE_STATES:
                    if candidate_row["outcome"] != outcome:
                        raise CrossReferenceValidationError(
                            "terminal inspection outcome changed during replay"
                        )
                    continue
                validate_schema(
                    outcome,
                    schema_directory=self.schema_directory,
                    schema_name="inspection-outcome.schema.json",
                )
                relationship = validate_inspection_outcome(
                    outcome,
                    candidate=candidate,
                    records=all_records,
                    evidence_ids_by_source=evidence_by_source,
                    existing_relationship_ids=existing_ids,
                )
                self.state.advance_cross_reference_candidate(
                    candidate_id,
                    str(outcome["outcome"]),
                    inspection_job_identifier=job_identifier,
                    outcome=outcome,
                    receipt={"kind": "candidate_inspection_complete"},
                )
                if relationship is not None:
                    self.state.record_cross_reference_relationship(
                        relationship, inspection_job_identifier=job_identifier
                    )
                    existing_ids.add(relationship.relationship_id)
            self._succeed_job(job_identifier, record_count=len(observed_ids))
            self._record_output_artifact(batch_id, job_identifier, output_path)
            return tuple(str(item) for item in proposal["warnings"])
        except (KeyError, TypeError, ValueError) as error:
            self._fail_job(job_identifier, str(error))
            raise

    def _canonical_outcome(
        self,
        raw: dict[str, Any],
        *,
        candidate: DiscoveryCandidate,
        batch_id: str,
        job_id: str,
        model: str,
        input_sha256: str,
        protocol_name: str = "cross-reference-inspection.md",
    ) -> dict[str, Any]:
        raw_relationship = raw.get("relationship")
        relationship_payload: dict[str, Any] | None = None
        if isinstance(raw_relationship, dict):
            relation_type = str(raw_relationship["relation_type"])
            direction = str(raw_relationship["direction"])
            relationship_payload = {
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
                    key: raw_relationship[key]
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
                    "protocol_sha256": fingerprint(self.protocol_directory / protocol_name).sha256,
                    "schema_sha256": fingerprint(
                        self.schema_directory / "relationship.schema.json"
                    ).sha256,
                    "input_sha256": input_sha256,
                },
            }
            validate_schema(
                relationship_payload,
                schema_directory=self.schema_directory,
                schema_name="relationship.schema.json",
            )
            Relationship.from_mapping(relationship_payload)
        return {
            "schema_version": 1,
            "candidate_id": raw["candidate_id"],
            "outcome": raw["outcome"],
            "disposition_reason": raw["disposition_reason"],
            "relationship": relationship_payload,
        }

    def _workspace(
        self,
        *,
        batch_id: str,
        job_id: str,
        source_ids: tuple[str, ...],
        kind: str,
        protocol_name: str,
        schema_name: str,
        task: dict[str, Any],
        inputs: tuple[tuple[str, str, dict[str, Any]], ...],
    ) -> Workspace:
        workspace_inputs = [
            WorkspaceInput(
                source_id=batch_id,
                destination="AGENTS.md",
                kind="stage_protocol",
                area="root",
                source_path=self.protocol_directory / protocol_name,
            ),
            WorkspaceInput(
                source_id=batch_id,
                destination="task.json",
                kind="stage_task",
                content=canonical_json_bytes(task),
            ),
            WorkspaceInput(
                source_id=batch_id,
                destination=schema_name,
                kind="output_schema",
                area="templates",
                source_path=self.schema_directory / schema_name,
            ),
        ]
        workspace_inputs.extend(
            WorkspaceInput(
                source_id=batch_id,
                destination=destination,
                kind=input_kind,
                content=canonical_json_bytes(payload),
            )
            for destination, input_kind, payload in inputs
        )
        path = self.cache_root / "cross-reference" / batch_id / "jobs" / job_id
        return CrossReferenceWorkspaceBuilder(
            path,
            batch_id=batch_id,
            job_id=job_id,
            source_ids=source_ids,
        ).build(kind=kind, inputs=tuple(workspace_inputs))

    def _execute_job(
        self,
        *,
        batch_id: str,
        job_id: str,
        job_kind: str,
        pair: tuple[str, str],
        source_ids: tuple[str, ...] | None = None,
        attempt: int,
        model: str,
        workspace: Workspace,
        schema_name: str,
        prompt: str,
    ) -> Path:
        output_path = workspace.path / "output" / "proposal.json"
        receipt_path = (
            self.cache_root / "cross-reference" / batch_id / "receipts" / f"{job_id}.json"
        )
        current = self.state.cross_reference_job_record(job_id)
        if current is not None and current["state"] == "succeeded":
            if not output_path.is_file():
                raise CrossReferenceExecutionError("successful job output is missing")
            if _valid_retained_output(receipt_path, output_path) and current.get(
                "receipt_path"
            ) != str(receipt_path):
                self.state.set_cross_reference_job_receipt(job_id, str(receipt_path))
            return output_path
        if current is not None and current["state"] == "running":
            if _valid_retained_output(receipt_path, output_path):
                return output_path
            raise CrossReferenceExecutionError(
                f"cross-reference job already started and cannot be repeated: {job_id}"
            )
        if (
            current is not None
            and current["state"] == "validation_failed"
            and self.allow_failed_proposal_revalidation
            and _valid_retained_output(receipt_path, output_path)
        ):
            if current.get("receipt_path") != str(receipt_path):
                self.state.set_cross_reference_job_receipt(job_id, str(receipt_path))
            self._transition_job(
                job_id,
                "running",
                {
                    "kind": "retained_proposal_revalidation_started",
                    "semantic_call_repeated": False,
                },
            )
            return output_path
        if current is not None:
            raise CrossReferenceExecutionError(
                f"cross-reference job cannot be retried: {job_id}; state={current['state']}"
            )
        try:
            runner = CodexRunner(self.codex_executable)
        except CodexCapabilityError as error:
            raise CrossReferenceExecutionError(str(error)) from error
        for target in ("pending", "running"):
            self.state.advance_cross_reference_job(
                job_id,
                target,
                batch_identifier=batch_id,
                job_kind=job_kind,
                source_a=pair[0],
                source_b=pair[1],
                attempt=attempt,
                receipt={"kind": f"cross_reference_job_{target}", "model": model},
                source_ids=source_ids,
            )
        result = runner.run(
            CodexJob(
                job_id=job_id,
                source_id=batch_id,
                model=model,
                prompt=prompt,
                workspace=workspace.path,
                output_schema=workspace.path / "templates" / schema_name,
                events_path=self.cache_root
                / "cross-reference"
                / batch_id
                / "events"
                / f"{job_id}.jsonl",
                last_message_path=output_path,
                receipt_path=receipt_path,
                timeout_seconds=self.job_timeout_seconds,
            )
        )
        self.state.set_cross_reference_job_receipt(job_id, str(receipt_path))
        if result.returncode != 0:
            self._transition_job(job_id, "failed", {"kind": "worker_failed"})
            raise CrossReferenceExecutionError(
                f"cross-reference job exited with status {result.returncode}: {job_id}"
            )
        if not result.output_valid:
            self._fail_job(job_id, "; ".join(result.validation_errors))
            raise CrossReferenceExecutionError(
                f"cross-reference output failed schema validation: {job_id}"
            )
        return output_path

    def _succeed_job(
        self,
        job_id: str,
        *,
        record_count: int,
        admission_counts: dict[str, int] | None = None,
        admission_receipt: str | None = None,
        proposal_diagnostics: MultiSurfaceProposalDiagnostics | None = None,
    ) -> None:
        job = self.state.cross_reference_job_record(job_id)
        if job is not None and job["state"] == "succeeded":
            return
        receipt: dict[str, Any] = {
            "kind": "validated_proposal_applied",
            "record_count": record_count,
        }
        if admission_counts is not None:
            receipt["admission_counts"] = dict(admission_counts)
        if admission_receipt is not None:
            receipt["admission_receipt"] = admission_receipt
        if proposal_diagnostics is not None:
            receipt["proposal_diagnostics"] = proposal_diagnostics.to_dict()
        self._transition_job(
            job_id,
            "succeeded",
            receipt,
        )

    def _fail_job(self, job_id: str, error: str) -> None:
        job = self.state.cross_reference_job_record(job_id)
        if job is not None and job["state"] == "running":
            self._transition_job(
                job_id,
                "validation_failed",
                {"kind": "proposal_rejected", "error": error, "retry_allowed": False},
            )

    def _transition_job(self, job_id: str, target: str, receipt: dict[str, Any]) -> None:
        job = self.state.cross_reference_job_record(job_id)
        if job is None:
            raise CrossReferenceExecutionError(f"cross-reference job is missing: {job_id}")
        self.state.advance_cross_reference_job(
            job_id,
            target,
            batch_identifier=str(job["batch_id"]),
            job_kind=str(job["kind"]),
            source_a=str(job["source_pair"][0]),
            source_b=str(job["source_pair"][1]),
            attempt=int(job["attempt"]),
            receipt=receipt,
            source_ids=tuple(str(item) for item in job["source_ids"]),
        )

    def _record_output_artifact(self, batch_id: str, job_id: str, output_path: Path) -> None:
        item = fingerprint(output_path)
        self.state.register_cross_reference_artifact(
            artifact_id(batch_id, f"semantic_output:{job_id}", item.sha256),
            batch_identifier=batch_id,
            job_identifier=job_id,
            kind="semantic_output",
            path=item.path,
            sha256=item.sha256,
            size_bytes=item.size_bytes,
            metadata={"job_id": job_id},
        )

    def _record_admission_receipt(
        self,
        batch_id: str,
        job_id: str,
        *,
        output_path: Path,
        plan: AdmissionPlan,
    ) -> Path:
        receipt_path = (
            self.cache_root / "cross-reference" / batch_id / "receipts" / f"{job_id}.admission.json"
        )
        write_receipt(
            receipt_path,
            plan.receipt_payload(raw_proposal_path=str(output_path)),
        )
        receipt = read_receipt(receipt_path)
        validate_schema(
            receipt,
            schema_directory=self.schema_directory,
            schema_name="cross-reference-admission-receipt.schema.json",
        )
        if fingerprint(output_path).sha256 != receipt["raw_proposal"]["sha256"]:
            raise CrossReferenceValidationError(
                "admission receipt does not address the immutable raw proposal"
            )
        item = fingerprint(receipt_path)
        self.state.register_cross_reference_artifact(
            artifact_id(batch_id, f"admission_receipt:{job_id}", item.sha256),
            batch_identifier=batch_id,
            job_identifier=job_id,
            kind="cross_reference_admission_receipt",
            path=item.path,
            sha256=item.sha256,
            size_bytes=item.size_bytes,
            metadata={"job_id": job_id, "counts": plan.counts},
        )
        return receipt_path

    def _result(
        self,
        batch_id: str,
        *,
        warnings: tuple[str, ...],
        source_groups: tuple[tuple[str, ...], ...] = (),
    ) -> CrossReferenceRunResult:
        batch = self.state.cross_reference_batch_record(batch_id)
        if batch is None:
            raise CrossReferenceExecutionError("cross-reference batch disappeared")
        candidates = self.state.cross_reference_candidates_for_batch(batch_id)
        counts: dict[str, int] = {}
        for candidate in candidates:
            state = str(candidate["state"])
            if state in TERMINAL_CANDIDATE_STATES:
                counts[state] = counts.get(state, 0) + 1
        relationships = self.state.cross_reference_relationships_for_batch(batch_id)
        admission_counts = {
            "admitted": 0,
            "quarantined": 0,
            "duplicate": 0,
            "existing_noop": 0,
        }
        admission_receipts: list[str] = []
        proposal_diagnostics: list[dict[str, Any]] = []
        jobs = self.state.cross_reference_jobs_for_batch(batch_id)
        for job in jobs:
            transition_receipt = job.get("transition_receipt")
            if isinstance(transition_receipt, dict):
                raw_diagnostics = transition_receipt.get("proposal_diagnostics")
                if isinstance(raw_diagnostics, dict):
                    proposal_diagnostics.append({"job_id": str(job["job_id"]), **raw_diagnostics})
            path = (
                self.cache_root
                / "cross-reference"
                / batch_id
                / "receipts"
                / f"{job['job_id']}.admission.json"
            )
            if not path.is_file():
                continue
            receipt = read_receipt(path)
            validate_schema(
                receipt,
                schema_directory=self.schema_directory,
                schema_name="cross-reference-admission-receipt.schema.json",
            )
            raw_path = Path(str(receipt["raw_proposal"]["path"]))
            if (
                not raw_path.is_file()
                or fingerprint(raw_path).sha256 != receipt["raw_proposal"]["sha256"]
            ):
                raise CrossReferenceValidationError(
                    f"admission receipt raw proposal changed: {path}"
                )
            for disposition in admission_counts:
                admission_counts[disposition] += int(receipt["counts"][disposition])
            admission_receipts.append(str(path))
        return CrossReferenceRunResult(
            batch_id=batch_id,
            strategy=str(batch["strategy"]),
            source_ids=tuple(batch["source_ids"]),
            source_groups=source_groups,
            state=str(batch["state"]),
            job_ids=tuple(str(job["job_id"]) for job in jobs),
            candidate_count=len(candidates),
            outcome_counts=counts,
            relationship_ids=tuple(
                str(relationship["relationship_id"]) for relationship in relationships
            ),
            admission_counts=admission_counts,
            admission_receipts=tuple(admission_receipts),
            proposal_diagnostics=tuple(proposal_diagnostics),
            warnings=warnings,
        )


def _canonical_source_groups(
    sources: tuple[str, ...],
    source_groups: tuple[tuple[str, ...], ...] | None,
) -> tuple[tuple[str, ...], ...]:
    if source_groups is None:
        return ()
    if len(source_groups) != 3:
        raise CrossReferenceValidationError(
            "source-sharded cross-reference requires exactly three source groups"
        )
    groups = tuple(
        sorted(tuple(sorted(source_id.strip() for source_id in group)) for group in source_groups)
    )
    if any(not group for group in groups):
        raise CrossReferenceValidationError("source groups must not be empty")
    grouped_sources = tuple(source_id for group in groups for source_id in group)
    if len(set(grouped_sources)) != len(grouped_sources):
        raise CrossReferenceValidationError("source groups must be disjoint")
    if tuple(sorted(grouped_sources)) != sources:
        raise CrossReferenceValidationError(
            "source groups must contain every requested source exactly once"
        )
    return groups


def _validate_multi_surface_contract(
    proposal: dict[str, Any],
    *,
    batch_id: str,
    source_ids: tuple[str, ...],
    records: dict[str, Record],
    evidence_ids_by_source: dict[str, frozenset[str]],
    expected_pairs: tuple[tuple[str, str], ...] | None = None,
) -> MultiSurfaceProposalDiagnostics:
    return validate_multi_surface_proposal(
        proposal,
        batch_id=batch_id,
        source_ids=source_ids,
        records=records,
        evidence_ids_by_source=evidence_ids_by_source,
        expected_pairs=expected_pairs,
    )


def _multi_surface_pair_key(left: str, right: str) -> str:
    return f"pair-{left.lower()}-{right.lower()}"


def _source_sharded_strategy(groups: tuple[tuple[str, ...], ...]) -> str:
    topology_sha256 = hashlib.sha256(canonical_json_bytes(groups)).hexdigest()[:12]
    return f"{SOURCE_SHARDED_STRATEGY}:{topology_sha256}"


def _source_shards(groups: tuple[tuple[str, ...], ...]) -> tuple[SourceShard, ...]:
    if len(groups) != 3:
        raise CrossReferenceValidationError(
            "source-sharded cross-reference requires exactly three source groups"
        )
    shards: list[SourceShard] = []
    observed_pairs: list[tuple[str, str]] = []
    for index, focus in enumerate(groups):
        context = groups[(index + 1) % len(groups)]
        owned_pairs = tuple(
            sorted(
                (
                    *itertools.combinations(focus, 2),
                    *(canonical_source_pair(left, right) for left in focus for right in context),
                )
            )
        )
        observed_pairs.extend(owned_pairs)
        shards.append(
            SourceShard(
                focus_sources=focus,
                context_sources=context,
                source_ids=tuple(sorted((*focus, *context))),
                owned_pairs=owned_pairs,
            )
        )
    all_sources = tuple(sorted(source for group in groups for source in group))
    expected_pairs = tuple(itertools.combinations(all_sources, 2))
    if (
        len(observed_pairs) != len(set(observed_pairs))
        or tuple(sorted(observed_pairs)) != expected_pairs
    ):
        raise CrossReferenceValidationError(
            "source-sharded ownership must cover every source pair exactly once"
        )
    return tuple(shards)


def _relationship_source_pair(relationship: Relationship) -> tuple[str, str]:
    left = EndpointRef.from_mapping(relationship.payload["left_endpoint"])
    right = EndpointRef.from_mapping(relationship.payload["right_endpoint"])
    return canonical_source_pair(left.source_id, right.source_id)


def _validate_owned_pair_shards(
    sources: tuple[str, ...],
    shards: tuple[OwnedPairShard, ...],
    *,
    pre_uncovered_pairs: tuple[tuple[str, str], ...],
) -> None:
    if len(sources) < 2 or len(set(sources)) != len(sources):
        raise CrossReferenceValidationError(
            "owned-pair cross-reference requires at least two unique sources"
        )
    if len({shard.shard_id for shard in shards}) != len(shards):
        raise CrossReferenceValidationError("owned-pair shard identities must be unique")
    observed: list[tuple[str, str]] = []
    for shard in shards:
        if (
            not shard.shard_id.strip()
            or len(shard.source_ids) < 2
            or tuple(sorted(shard.source_ids)) != shard.source_ids
            or len(set(shard.source_ids)) != len(shard.source_ids)
            or not set(shard.source_ids).issubset(sources)
            or not shard.owned_pairs
        ):
            raise CrossReferenceValidationError("owned-pair shard scope is invalid")
        for pair in shard.owned_pairs:
            if pair != canonical_source_pair(*pair) or not set(pair).issubset(shard.source_ids):
                raise CrossReferenceValidationError(
                    "owned pair is not canonical or is outside its shard scope"
                )
            observed.append(pair)
    for pair in pre_uncovered_pairs:
        if pair != canonical_source_pair(*pair) or not set(pair).issubset(sources):
            raise CrossReferenceValidationError("pre-uncovered pair is invalid")
        observed.append(pair)
    expected = tuple(itertools.combinations(sources, 2))
    if len(observed) != len(set(observed)) or tuple(sorted(observed)) != expected:
        raise CrossReferenceValidationError(
            "owned-pair shards and planning failures must cover every source pair exactly once"
        )


def _validate_pair_outcomes(
    sources: tuple[str, ...],
    *,
    successful_pairs: set[tuple[str, str]],
    uncovered_pairs: set[tuple[str, str]],
) -> None:
    expected = set(itertools.combinations(sources, 2))
    if successful_pairs.intersection(uncovered_pairs):
        raise CrossReferenceValidationError("source pair cannot be both successful and uncovered")
    if successful_pairs.union(uncovered_pairs) != expected:
        raise CrossReferenceValidationError(
            "cross-reference outcomes must account for every source pair"
        )


def _receipt_pairs(value: list[Any], *, label: str) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for raw in value:
        if (
            not isinstance(raw, list)
            or len(raw) != 2
            or not all(isinstance(item, str) for item in raw)
        ):
            raise CrossReferenceValidationError(f"{label} receipt is invalid")
        pair = (raw[0], raw[1])
        if pair != canonical_source_pair(*pair) or pair in pairs:
            raise CrossReferenceValidationError(f"{label} receipt is invalid")
        pairs.add(pair)
    return pairs


def _receipt_warnings(shard: Mapping[str, Any]) -> tuple[str, ...]:
    value = shard.get("warnings")
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise CrossReferenceValidationError("owned-pair shard warnings are invalid")
    return tuple(value)


def _write_or_verify_receipt(path: Path, payload: Mapping[str, Any]) -> None:
    expected = {"schema_version": "1.0", **dict(payload)}
    if path.is_file():
        if read_receipt(path) != expected:
            raise CrossReferenceValidationError(f"retained cross-reference receipt differs: {path}")
        return
    write_receipt(path, payload)


def _validate_owned_source_pair(
    source_a: str,
    source_b: str,
    *,
    owned_pairs: frozenset[tuple[str, str]] | None,
) -> None:
    source_pair = canonical_source_pair(source_a, source_b)
    if owned_pairs is not None and source_pair not in owned_pairs:
        raise CrossReferenceValidationError(
            f"holistic relationship is outside the job's owned source pairs: {source_pair}"
        )


def _normalize_holistic_relationship(
    raw: dict[str, Any],
    *,
    left_record: Record,
    right_record: Record,
) -> tuple[Record, Record, dict[str, Any]]:
    if left_record.source_id == right_record.source_id:
        raise CrossReferenceValidationError(
            "holistic relationship endpoints must come from different sources"
        )
    normalized = dict(raw)
    if left_record.source_id < right_record.source_id:
        return left_record, right_record, normalized
    left_record, right_record = right_record, left_record
    normalized["left_record_id"] = left_record.id
    normalized["right_record_id"] = right_record.id
    normalized["left_evidence_ids"], normalized["right_evidence_ids"] = (
        list(raw["right_evidence_ids"]),
        list(raw["left_evidence_ids"]),
    )
    normalized["direction"] = {
        "symmetric": "symmetric",
        "left_to_right": "right_to_left",
        "right_to_left": "left_to_right",
    }[str(raw["direction"])]
    return left_record, right_record, normalized


def _validate_holistic_coverage(
    proposal: dict[str, Any],
    *,
    records: dict[str, Record],
    existing_relationships: tuple[Relationship, ...],
) -> None:
    existing_by_id = {
        relationship.relationship_id: relationship for relationship in existing_relationships
    }
    review_ids = [
        str(review["relationship_id"]) for review in proposal["existing_relationship_reviews"]
    ]
    if len(review_ids) != len(set(review_ids)):
        raise CrossReferenceValidationError(
            "holistic coverage repeated an existing relationship review"
        )
    if set(review_ids) != set(existing_by_id):
        missing = sorted(set(existing_by_id) - set(review_ids))
        unknown = sorted(set(review_ids) - set(existing_by_id))
        raise CrossReferenceValidationError(
            "holistic coverage reviews do not match existing relationships: "
            f"missing={missing}, unknown={unknown}"
        )

    surfaces: dict[str, dict[str, Any]] = {}
    for surface in proposal["coverage_surfaces"]:
        surface_key = str(surface["surface_key"])
        if surface_key in surfaces:
            raise CrossReferenceValidationError(
                f"holistic coverage repeated surface key: {surface_key}"
            )
        record_ids = [str(item) for item in surface["source_record_ids"]]
        if len(record_ids) != len(set(record_ids)):
            raise CrossReferenceValidationError(
                f"holistic coverage surface repeated a record: {surface_key}"
            )
        resolved: list[Record] = []
        for record_id in record_ids:
            record = records.get(record_id)
            if record is None:
                raise CrossReferenceValidationError(
                    f"holistic coverage record is outside the frozen corpus: {record_id}"
                )
            if record.record_type != "atom":
                raise CrossReferenceValidationError(
                    f"holistic coverage record is not an atom: {record_id}"
                )
            resolved.append(record)
        if len({record.source_id for record in resolved}) < 2:
            raise CrossReferenceValidationError(
                f"holistic coverage surface does not span sources: {surface_key}"
            )

        relationship_ids = [str(item) for item in surface["existing_relationship_ids"]]
        if len(relationship_ids) != len(set(relationship_ids)):
            raise CrossReferenceValidationError(
                f"holistic coverage surface repeated an existing relationship: {surface_key}"
            )
        unknown_relationships = sorted(set(relationship_ids) - set(existing_by_id))
        if unknown_relationships:
            raise CrossReferenceValidationError(
                "holistic coverage surface cites unknown existing relationships: "
                f"{unknown_relationships}"
            )
        disposition = str(surface["disposition"])
        if any(record.payload.get("connectable") is not True for record in resolved) and (
            disposition != "manual_review_candidate"
        ):
            raise CrossReferenceValidationError(
                "holistic coverage may include a non-connectable atom only as a "
                f"manual-review surface: {surface_key}"
            )
        if disposition == "covered_by_existing":
            if not relationship_ids:
                raise CrossReferenceValidationError(
                    f"covered-by-existing surface lacks a relationship: {surface_key}"
                )
            surface_record_ids = set(record_ids)
            for relationship_id in relationship_ids:
                relationship = existing_by_id[relationship_id]
                endpoints = {
                    str(relationship.payload["left_endpoint"]["record_id"]),
                    str(relationship.payload["right_endpoint"]["record_id"]),
                }
                if not endpoints.issubset(surface_record_ids):
                    raise CrossReferenceValidationError(
                        "covered-by-existing surface omits cited relationship endpoints: "
                        f"{surface_key}"
                    )
        elif relationship_ids:
            raise CrossReferenceValidationError(
                f"non-existing coverage surface cites an existing relationship: {surface_key}"
            )
        surfaces[surface_key] = surface

    proposal_counts: dict[str, int] = {}
    existing_endpoint_pairs = {
        frozenset(
            {
                str(relationship.payload["left_endpoint"]["record_id"]),
                str(relationship.payload["right_endpoint"]["record_id"]),
            }
        )
        for relationship in existing_relationships
    }
    for relationship in proposal["relationships"]:
        surface_key = str(relationship["surface_key"])
        surface = surfaces.get(surface_key)
        if surface is None:
            raise CrossReferenceValidationError(
                f"holistic relationship cites an unknown coverage surface: {surface_key}"
            )
        if surface["disposition"] != "covered_by_proposal":
            raise CrossReferenceValidationError(
                f"holistic relationship surface is not covered by proposal: {surface_key}"
            )
        endpoint_ids = {
            str(relationship["left_record_id"]),
            str(relationship["right_record_id"]),
        }
        if frozenset(endpoint_ids) in existing_endpoint_pairs:
            raise CrossReferenceValidationError(
                "holistic proposal repeats an existing relationship endpoint pair"
            )
        if not endpoint_ids.issubset(set(surface["source_record_ids"])):
            raise CrossReferenceValidationError(
                f"holistic coverage surface omits proposal endpoints: {surface_key}"
            )
        proposal_counts[surface_key] = proposal_counts.get(surface_key, 0) + 1

    for surface_key, surface in surfaces.items():
        has_proposal = proposal_counts.get(surface_key, 0) > 0
        if surface["disposition"] == "covered_by_proposal" and not has_proposal:
            raise CrossReferenceValidationError(
                f"covered-by-proposal surface lacks a relationship: {surface_key}"
            )
        if surface["disposition"] != "covered_by_proposal" and has_proposal:
            raise CrossReferenceValidationError(
                f"non-proposal coverage surface received a relationship: {surface_key}"
            )


def _endpoint_context(endpoint: EndpointRef, projection: SourceProjection) -> dict[str, Any]:
    atom = projection.records[endpoint.record_id]
    related: set[str] = {atom.id}
    related.update(str(item) for item in atom.payload.get("evidence_ids", []))
    moves = [
        record
        for record in projection.dossier.records_of_type("move")
        if atom.id in record.payload.get("inputs", [])
        or atom.id in record.payload.get("outputs", [])
    ]
    for move in moves:
        related.add(move.id)
        related.add(str(move.payload["thread_id"]))
        related.update(str(item) for item in move.payload.get("inputs", []))
        related.update(str(item) for item in move.payload.get("outputs", []))
        related.update(str(item) for item in move.payload.get("evidence_ids", []))
    records = [
        projection.records[identifier].to_dict()
        for identifier in sorted(related)
        if identifier in projection.records
    ]
    return {
        "source_id": projection.source_id,
        "endpoint": endpoint.to_dict(),
        "records": records,
        "context_envelope": projection.contexts.get(atom.id),
    }


def _partition_packets(
    items: list[dict[str, Any]], *, max_bytes: int
) -> tuple[list[dict[str, Any]], ...]:
    packets: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for item in items:
        if len(canonical_json_bytes({"candidates": [item]})) > max_bytes:
            raise CrossReferenceValidationError(
                "one candidate inspection context exceeds the deterministic packet bound"
            )
        proposed = [*current, item]
        if current and len(canonical_json_bytes({"candidates": proposed})) > max_bytes:
            packets.append(current)
            current = [item]
        else:
            current = proposed
    if current:
        packets.append(current)
    return tuple(packets)


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise CrossReferenceValidationError(f"expected a JSON object: {path}")
    return value


def _cross_reference_job_telemetry(
    *,
    state: StateRepository,
    cache_root: Path,
    batch_id: str,
    job_id: str,
) -> dict[str, Any]:
    """Load one shard's immutable worker telemetry from its receipt and event stream."""

    job = state.cross_reference_job_record(job_id)
    if job is None:
        return {
            "semantic_job_started": False,
            "duration_seconds": 0.0,
            "worker_events_emitted": 0,
            "token_usage": _empty_token_usage(),
        }
    if job["batch_id"] != batch_id:
        raise CrossReferenceValidationError(
            f"cross-reference telemetry job belongs to another batch: {job_id}"
        )
    expected_receipt_path = (
        cache_root / "cross-reference" / batch_id / "receipts" / f"{job_id}.json"
    )
    if job.get("receipt_path") != str(expected_receipt_path) or not expected_receipt_path.is_file():
        raise CrossReferenceValidationError(
            f"cross-reference telemetry receipt is missing or unbound: {job_id}"
        )
    try:
        receipt = read_receipt(expected_receipt_path)
    except (OSError, ValueError) as error:
        raise CrossReferenceValidationError(
            f"cross-reference telemetry receipt is invalid: {job_id}: {error}"
        ) from error
    process = receipt.get("process")
    events = receipt.get("events")
    duration = process.get("duration_seconds") if isinstance(process, dict) else None
    event_count = events.get("count") if isinstance(events, dict) else None
    if (
        receipt.get("kind") != "codex_job"
        or receipt.get("job_id") != job_id
        or receipt.get("source_id") != batch_id
        or not isinstance(duration, (int, float))
        or isinstance(duration, bool)
        or duration < 0
        or not isinstance(event_count, int)
        or isinstance(event_count, bool)
        or event_count < 0
    ):
        raise CrossReferenceValidationError(
            f"cross-reference telemetry receipt identity is invalid: {job_id}"
        )
    events_path = cache_root / "cross-reference" / batch_id / "events" / f"{job_id}.jsonl"
    if not events_path.is_file():
        raise CrossReferenceValidationError(
            f"cross-reference telemetry event stream is missing: {job_id}"
        )
    observed_events = 0
    usage = _empty_token_usage()
    for line in events_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        observed_events += 1
        raw_usage = event.get("usage")
        if raw_usage is None:
            continue
        if not isinstance(raw_usage, dict):
            raise CrossReferenceValidationError(
                f"cross-reference event token usage is invalid: {job_id}"
            )
        for key in usage:
            value = raw_usage.get(key, 0)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise CrossReferenceValidationError(
                    f"cross-reference event token usage is invalid: {job_id}"
                )
            usage[key] += value
    event_fingerprint = fingerprint(events_path)
    outputs = receipt.get("outputs")
    if (
        observed_events != event_count
        or not isinstance(outputs, list)
        or not any(
            isinstance(item, dict)
            and item.get("path") == str(events_path)
            and item.get("sha256") == event_fingerprint.sha256
            and item.get("size_bytes") == event_fingerprint.size_bytes
            for item in outputs
        )
    ):
        raise CrossReferenceValidationError(
            f"cross-reference telemetry event stream changed: {job_id}"
        )
    return {
        "semantic_job_started": True,
        "duration_seconds": float(duration),
        "worker_events_emitted": event_count,
        "token_usage": usage,
    }


def _aggregate_shard_telemetry(shards: list[dict[str, Any]]) -> dict[str, Any]:
    duration = 0.0
    events = 0
    usage = _empty_token_usage()
    for shard in shards:
        shard_duration = shard.get("duration_seconds")
        shard_events = shard.get("worker_events_emitted")
        shard_usage = shard.get("token_usage")
        if (
            not isinstance(shard.get("semantic_job_started"), bool)
            or not isinstance(shard_duration, (int, float))
            or isinstance(shard_duration, bool)
            or shard_duration < 0
            or not isinstance(shard_events, int)
            or isinstance(shard_events, bool)
            or shard_events < 0
            or not isinstance(shard_usage, dict)
            or set(shard_usage) != set(usage)
            or any(
                not isinstance(value, int) or isinstance(value, bool) or value < 0
                for value in shard_usage.values()
            )
        ):
            raise CrossReferenceValidationError("owned-pair shard telemetry is invalid")
        if not shard["semantic_job_started"] and (
            shard_duration != 0 or shard_events != 0 or any(shard_usage.values())
        ):
            raise CrossReferenceValidationError(
                "unstarted owned-pair shard contains worker telemetry"
            )
        duration += float(shard_duration)
        events += shard_events
        for key in usage:
            usage[key] += int(shard_usage[key])
    return {
        "duration_seconds": duration,
        "worker_events_emitted": events,
        "token_usage": usage,
    }


def _empty_token_usage() -> dict[str, int]:
    return {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
    }


def _valid_retained_output(receipt_path: Path, output_path: Path) -> bool:
    if not receipt_path.is_file() or not output_path.is_file():
        return False
    receipt = read_receipt(receipt_path)
    validation = receipt.get("validation")
    return isinstance(validation, dict) and validation.get("valid") is True


def _verify_projection_manifest(
    manifest: dict[str, Any],
    *,
    source_asset: dict[str, Any],
    artifacts: dict[str, Path],
) -> None:
    if manifest.get("asset_fingerprint") != source_asset:
        raise CrossReferenceValidationError("compiled projection asset fingerprint changed")
    raw_artifacts = manifest.get("artifacts")
    if not isinstance(raw_artifacts, list):
        raise CrossReferenceValidationError("compiled projection manifest lacks artifacts")
    by_kind = {str(item.get("kind")): item for item in raw_artifacts if isinstance(item, dict)}
    if set(by_kind) != set(artifacts):
        raise CrossReferenceValidationError("compiled projection artifact inventory changed")
    for kind, path in artifacts.items():
        observed = fingerprint(path)
        expected = by_kind[kind]
        if (
            expected.get("sha256") != observed.sha256
            or expected.get("size_bytes") != observed.size_bytes
        ):
            raise CrossReferenceValidationError(
                f"compiled projection artifact fingerprint changed: {kind}"
            )
