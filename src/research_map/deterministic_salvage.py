"""Zero-call salvage of validator-accepted rows from one retained failed shard."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from research_map.cross_reference import (
    Relationship,
    validate_relationship_against_records,
    validate_schema,
)
from research_map.cross_reference_admission import compile_holistic_admission
from research_map.cross_reference_recovery import (
    _graph_records,
    _load_frozen_records,
    _pair_outcomes,
)
from research_map.exploration import GraphExplorer
from research_map.multi_surface_contract import (
    diagnose_multi_surface_proposal,
    multi_surface_granular_admission_projection,
)
from research_map.receipts import canonical_json_bytes, fingerprint, read_receipt
from research_map.relationship_markdown import materialize_relationship_markdown

SALVAGE_VERSION = "1.0.0"
GRAPH_MANIFEST_SCHEMA = "deterministic-recovery-salvage-graph-manifest.schema.json"
REPORT_SCHEMA = "deterministic-recovery-salvage-report.schema.json"


class DeterministicSalvageValidationError(ValueError):
    """Raised when retained lineage cannot prove a safe deterministic salvage."""


@dataclass(frozen=True, slots=True)
class DeterministicSalvageResult:
    salvage_id: str
    root: Path
    state: str
    semantic_jobs_started: int
    graph_path: Path
    report_path: Path
    counts: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "salvage_id": self.salvage_id,
            "root": str(self.root),
            "state": self.state,
            "semantic_jobs_started": self.semantic_jobs_started,
            "graph_path": str(self.graph_path),
            "report_path": str(self.report_path),
            "counts": dict(self.counts),
        }


class DeterministicSalvageCoordinator:
    """Compile an already-accepted safe-row projection into an additive graph."""

    def __init__(
        self,
        *,
        repository_root: Path,
        origin_recovery_root: Path,
        destination_run_root: Path,
        shard_id: str,
        schema_directory: Path | None = None,
    ) -> None:
        self.repository_root = repository_root.resolve()
        self.origin_root = origin_recovery_root.resolve()
        self.root = destination_run_root.resolve()
        self.shard_id = shard_id
        self.schema_directory = (
            schema_directory.resolve()
            if schema_directory is not None
            else self.repository_root / "schemas" / "research-map" / "v1"
        )
        self.salvage_id = self.root.name
        self.graph_path = self.root / "graph" / "graph.json"
        self.graph_manifest_path = self.root / "graph" / "manifest.json"
        self.plan_path = self.root / "salvage" / "plan.json"
        self.admission_path = self.root / "salvage" / "admission.json"
        self.report_path = self.root / "report.json"

    def run(self) -> DeterministicSalvageResult:
        if self.root == self.origin_root or self.root.is_relative_to(self.origin_root):
            raise DeterministicSalvageValidationError(
                "salvage destination must be separate from the immutable origin"
            )
        origin = self._verified_origin()
        shard = self._verified_failed_shard(origin)
        retained = self._verified_retained_inputs(shard)

        proposal = retained["proposal"]
        records, evidence = _load_frozen_records(
            retained["frozen_records_path"],
            batch_id=str(shard["batch_id"]),
            source_ids=tuple(str(value) for value in shard["source_ids"]),
        )
        owned_pairs = tuple((str(value[0]), str(value[1])) for value in shard["owned_pairs"])
        report = diagnose_multi_surface_proposal(
            proposal,
            batch_id=str(shard["batch_id"]),
            source_ids=tuple(str(value) for value in shard["source_ids"]),
            records=records,
            evidence_ids_by_source=evidence,
            expected_pairs=owned_pairs,
        )
        recorded_report = _object(
            _object(shard["original"], "failed shard original").get("validation_report"),
            "failed shard original validation report",
        )
        if report.to_dict() != dict(recorded_report):
            raise DeterministicSalvageValidationError(
                "recomputed original validation report differs from retained receipt"
            )
        if report.has_fatal_findings:
            raise DeterministicSalvageValidationError(
                "fatal original findings prevent deterministic row salvage"
            )

        origin_explorer = origin["explorer"]
        existing_relationships = tuple(
            Relationship.from_mapping(value) for value in origin_explorer.relationships.values()
        )
        projection = multi_surface_granular_admission_projection(
            proposal,
            validation_report=report,
            existing_relationship_ids=tuple(
                relationship.relationship_id for relationship in existing_relationships
            ),
        )
        original_receipt = retained["original_receipt"]
        protocol_sha256 = _receipt_input_sha(original_receipt, "AGENTS.md")
        admission = compile_holistic_admission(
            projection,
            batch_id=str(shard["batch_id"]),
            job_id=str(shard["original_job_id"]),
            raw_proposal_sha256=retained["proposal_sha256"],
            records=records,
            evidence_ids_by_source=evidence,
            existing_relationships=existing_relationships,
            owned_pairs=frozenset(owned_pairs),
            model=str(original_receipt["model"]),
            input_sha256=retained["frozen_records_sha256"],
            protocol_sha256=protocol_sha256,
            relationship_schema_sha256=fingerprint(
                self.schema_directory / "relationship.schema.json"
            ).sha256,
            schema_directory=self.schema_directory,
        )
        if admission.counts != {
            "admitted": len(report.accepted_relationship_indexes),
            "quarantined": 0,
            "duplicate": 0,
            "existing_noop": 0,
        }:
            raise DeterministicSalvageValidationError(
                f"safe-row admission was not exact: {admission.counts}"
            )
        salvaged = tuple(
            item.relationship for item in admission.admitted if item.relationship is not None
        )
        if len(salvaged) != len(admission.admitted):
            raise DeterministicSalvageValidationError(
                "an admitted safe row lacks its canonical relationship"
            )

        pair_outcomes = _pair_outcomes(proposal, report, owned_pairs=owned_pairs)
        pair_partitions = self._pair_partitions(origin["report"], pair_outcomes)
        plan = {
            "schema_version": "1.0",
            "kind": "deterministic_recovery_salvage_plan",
            "coordinator_version": SALVAGE_VERSION,
            "salvage_id": self.salvage_id,
            "origin_recovery_id": str(origin["report"]["recovery_id"]),
            "shard_id": self.shard_id,
            "origin_graph_sha256": origin["graph_sha256"],
            "failed_shard_receipt_sha256": origin["shard_receipt_sha256"],
            "original_proposal_sha256": retained["proposal_sha256"],
            "frozen_records_sha256": retained["frozen_records_sha256"],
            "original_worker_receipt_sha256": retained["original_receipt_sha256"],
            "accepted_relationship_indexes": list(report.accepted_relationship_indexes),
            "rejected_relationship_indexes": list(report.rejected_relationship_indexes),
            "semantic_jobs_started": 0,
        }
        _write_json_stable(self.plan_path, plan)
        plan_sha256 = fingerprint(self.plan_path).sha256

        admission_receipt = {
            "schema_version": "1.0",
            "kind": "deterministic_recovery_salvage_admission",
            "salvage_id": self.salvage_id,
            "shard_id": self.shard_id,
            "plan_sha256": plan_sha256,
            "original_validation_report": report.to_dict(),
            "admission": admission.receipt_payload(
                raw_proposal_path="lineage/original-proposal.json"
            ),
            "admitted_relationships": [value.to_dict() for value in salvaged],
            "retained_rejected_rows": {
                "surface_indexes": list(report.rejected_surface_indexes),
                "relationship_indexes": list(report.rejected_relationship_indexes),
                "findings": [finding.to_dict() for finding in report.findings],
            },
            "pair_outcomes": pair_outcomes,
        }
        _write_json_stable(self.admission_path, admission_receipt)

        graph, all_relationships = self._build_graph(
            origin_graph=origin["graph"],
            existing_relationships=existing_relationships,
            salvaged=salvaged,
        )
        _write_json_stable(self.graph_path, graph)
        relationship_paths = _materialize_relationships_stable(
            self.root / "relationships", all_relationships
        )
        lineage_paths = self._materialize_lineage(origin, retained)
        manifest_inputs = (
            [_descriptor("salvage_lineage", path, relative_to=self.root) for path in lineage_paths]
            + [
                _descriptor(
                    "deterministic_salvage_admission", self.admission_path, relative_to=self.root
                )
            ]
            + [
                _descriptor("canonical_relationship_markdown", path, relative_to=self.root)
                for path in relationship_paths
            ]
        )
        origin_manifest = origin["manifest"]
        graph_manifest = {
            "schema_version": "1.0",
            "kind": "deterministic_recovery_salvage_graph_manifest",
            "salvage_id": self.salvage_id,
            "origin_recovery_id": str(origin["report"]["recovery_id"]),
            "origin_run_id": str(origin["report"]["origin_run_id"]),
            "batch_id": str(graph["batch_id"]),
            "sources": list(graph["sources"]),
            "plan_sha256": plan_sha256,
            "origin_graph_sha256": origin["graph_sha256"],
            "inputs": manifest_inputs,
            "corpus_graph": _descriptor("corpus_graph", self.graph_path, relative_to=self.root),
            "node_count": len(graph["nodes"]),
            "source_local_edge_count": sum(
                edge["origin"] == "source_local" for edge in graph["edges"]
            ),
            "origin_relationship_count": len(existing_relationships),
            "salvaged_relationship_count": len(salvaged),
            "cross_source_edge_count": sum(
                edge["origin"] == "cross_source" for edge in graph["edges"]
            ),
            "source_quality": origin_manifest.get("source_quality", []),
            "quality_label_counts": origin_manifest.get("quality_label_counts", {}),
            "selected_origin_counts": origin_manifest.get("selected_origin_counts", {}),
            "structurally_invalid_sources": origin_manifest.get("structurally_invalid_sources", []),
        }
        _write_json_stable(self.graph_manifest_path, graph_manifest)
        validate_schema(
            graph_manifest,
            schema_directory=self.schema_directory,
            schema_name=GRAPH_MANIFEST_SCHEMA,
        )
        explorer = GraphExplorer(graph_path=self.graph_path, schema_directory=self.schema_directory)
        if not explorer.graph_identity["manifest_verified"] or set(explorer.relationships) != {
            relationship.relationship_id for relationship in all_relationships
        }:
            raise DeterministicSalvageValidationError(
                "salvaged graph manifest does not verify the relationship union"
            )

        counts = {
            "records": len(graph["nodes"]),
            "source_local_edges": sum(edge["origin"] == "source_local" for edge in graph["edges"]),
            "origin_relationships": len(existing_relationships),
            "salvaged_relationships": len(salvaged),
            "relationships": len(all_relationships),
            "pairs": sum(len(values) for values in pair_partitions.values()),
        }
        terminal_report = {
            "schema_version": "1.0",
            "kind": "deterministic_recovery_salvage_report",
            "salvage_id": self.salvage_id,
            "origin_recovery_id": str(origin["report"]["recovery_id"]),
            "origin_run_id": str(origin["report"]["origin_run_id"]),
            "shard_id": self.shard_id,
            "state": "complete" if not pair_partitions["remaining_unresolved"] else "partial",
            "plan_sha256": plan_sha256,
            "counts": counts,
            "pair_partitions": pair_partitions,
            "dispositions": {
                "admitted": len(salvaged),
                "retained_rejected_relationships": len(report.rejected_relationship_indexes),
                "retained_rejected_surfaces": len(report.rejected_surface_indexes),
            },
            "telemetry": {
                "semantic_jobs_started": 0,
                "duration_seconds": 0,
                "worker_events_emitted": 0,
                "token_usage": {
                    "input_tokens": 0,
                    "cached_input_tokens": 0,
                    "output_tokens": 0,
                },
            },
            "graph": _descriptor("corpus_graph", self.graph_path, relative_to=self.root),
            "graph_manifest": _descriptor(
                "deterministic_recovery_salvage_graph_manifest",
                self.graph_manifest_path,
                relative_to=self.root,
            ),
            "admission": _descriptor(
                "deterministic_salvage_admission", self.admission_path, relative_to=self.root
            ),
            "retrieval": {
                "graph_path": str(self.graph_path),
                "search_command": f"research-map search --graph {self.graph_path}",
                "explore_command": f"research-map explore --graph {self.graph_path}",
                "manifest_verified": True,
            },
        }
        _write_json_stable(self.report_path, terminal_report)
        validate_schema(
            terminal_report,
            schema_directory=self.schema_directory,
            schema_name=REPORT_SCHEMA,
        )
        return DeterministicSalvageResult(
            salvage_id=self.salvage_id,
            root=self.root,
            state=str(terminal_report["state"]),
            semantic_jobs_started=0,
            graph_path=self.graph_path,
            report_path=self.report_path,
            counts=counts,
        )

    def report(self) -> dict[str, Any]:
        if not self.report_path.is_file():
            raise DeterministicSalvageValidationError("salvage report does not exist")
        report = read_receipt(self.report_path)
        validate_schema(
            report,
            schema_directory=self.schema_directory,
            schema_name=REPORT_SCHEMA,
        )
        explorer = GraphExplorer(graph_path=self.graph_path, schema_directory=self.schema_directory)
        if not explorer.graph_identity["manifest_verified"]:
            raise DeterministicSalvageValidationError("salvage graph manifest did not verify")
        if len(explorer.relationships) != int(_object(report["counts"], "counts")["relationships"]):
            raise DeterministicSalvageValidationError(
                "salvage report relationship count differs from verified graph"
            )
        return report

    def _verified_origin(self) -> dict[str, Any]:
        report_path = self.origin_root / "report.json"
        graph_path = self.origin_root / "graph" / "graph.json"
        manifest_path = self.origin_root / "graph" / "manifest.json"
        launch_plan_path = self.origin_root / "lineage" / "recovery-launch-plan.json"
        for path in (report_path, graph_path, manifest_path, launch_plan_path):
            if not path.is_file():
                raise DeterministicSalvageValidationError(
                    f"required immutable origin artifact is missing: {path}"
                )
        report = read_receipt(report_path)
        if report.get("kind") != "cross_reference_recovery_report" or report.get("state") not in {
            "complete",
            "partial",
        }:
            raise DeterministicSalvageValidationError("origin recovery report is not terminal")
        explorer = GraphExplorer(graph_path=graph_path, schema_directory=self.schema_directory)
        if not explorer.graph_identity["manifest_verified"]:
            raise DeterministicSalvageValidationError(
                "origin recovery graph manifest did not verify"
            )
        graph_descriptor = _object(report.get("graph"), "origin report graph")
        if graph_descriptor.get("sha256") != fingerprint(graph_path).sha256:
            raise DeterministicSalvageValidationError("origin report graph binding changed")
        manifest_descriptor = _object(report.get("graph_manifest"), "origin report manifest")
        if manifest_descriptor.get("sha256") != fingerprint(manifest_path).sha256:
            raise DeterministicSalvageValidationError("origin report manifest binding changed")
        return {
            "report": report,
            "graph": _load_object(graph_path),
            "manifest": read_receipt(manifest_path),
            "explorer": explorer,
            "graph_path": graph_path,
            "graph_sha256": fingerprint(graph_path).sha256,
            "manifest_path": manifest_path,
            "report_path": report_path,
            "launch_plan_path": launch_plan_path,
        }

    def _verified_failed_shard(self, origin: dict[str, Any]) -> dict[str, Any]:
        report = origin["report"]
        matches = [
            value
            for value in _objects(report.get("shard_outcomes"), "origin shard outcomes")
            if value.get("shard_id") == self.shard_id
        ]
        if len(matches) != 1 or matches[0].get("outcome") != "validation_failed":
            raise DeterministicSalvageValidationError(
                "selected shard is not one retained validation-failed origin shard"
            )
        descriptor = _object(matches[0].get("receipt"), "failed shard descriptor")
        path = self.origin_root / str(descriptor.get("path", ""))
        _verify_hash(path, str(descriptor.get("sha256", "")), "failed shard receipt")
        shard = read_receipt(path)
        if shard.get("shard_id") != self.shard_id or shard.get("outcome") != "validation_failed":
            raise DeterministicSalvageValidationError("failed shard receipt identity changed")
        launch = read_receipt(origin["launch_plan_path"])
        plans = [
            value
            for value in _objects(launch.get("failed_shards"), "origin failed-shard plan")
            if value.get("shard_id") == self.shard_id
        ]
        if len(plans) != 1:
            raise DeterministicSalvageValidationError("failed shard launch-plan entry is ambiguous")
        for key in ("batch_id", "job_id", "source_ids", "owned_pairs"):
            shard_key = "original_job_id" if key == "job_id" else key
            if shard.get(shard_key) != plans[0].get(key):
                raise DeterministicSalvageValidationError(
                    f"failed shard receipt differs from launch plan: {key}"
                )
        origin["shard_receipt_path"] = path
        origin["shard_receipt_sha256"] = fingerprint(path).sha256
        origin["shard_plan"] = plans[0]
        return shard

    def _verified_retained_inputs(self, shard: Mapping[str, Any]) -> dict[str, Any]:
        launch = read_receipt(self.origin_root / "lineage" / "recovery-launch-plan.json")
        plan = next(
            value
            for value in _objects(launch.get("failed_shards"), "origin failed-shard plan")
            if value.get("shard_id") == self.shard_id
        )
        proposal_path, proposal_sha = _verified_descriptor(plan, "original_proposal")
        records_path, records_sha = _verified_descriptor(plan, "frozen_records")
        receipt_path, receipt_sha = _verified_descriptor(plan, "original_receipt")
        original = _object(shard.get("original"), "failed shard original")
        if (
            _object(original.get("proposal"), "shard original proposal").get("sha256")
            != proposal_sha
        ):
            raise DeterministicSalvageValidationError("original proposal lineage changed")
        if _object(original.get("receipt"), "shard original receipt").get("sha256") != receipt_sha:
            raise DeterministicSalvageValidationError("original worker receipt lineage changed")
        worker_receipt = read_receipt(receipt_path)
        if worker_receipt.get("job_id") != shard.get("original_job_id"):
            raise DeterministicSalvageValidationError("original worker job identity changed")
        if _receipt_input_sha(worker_receipt, "input/corpus-records.json") != records_sha:
            raise DeterministicSalvageValidationError("worker frozen-record input binding changed")
        return {
            "proposal": _load_object(proposal_path),
            "proposal_path": proposal_path,
            "proposal_sha256": proposal_sha,
            "frozen_records_path": records_path,
            "frozen_records_sha256": records_sha,
            "original_receipt": worker_receipt,
            "original_receipt_path": receipt_path,
            "original_receipt_sha256": receipt_sha,
        }

    def _build_graph(
        self,
        *,
        origin_graph: Mapping[str, Any],
        existing_relationships: Sequence[Relationship],
        salvaged: Sequence[Relationship],
    ) -> tuple[dict[str, Any], tuple[Relationship, ...]]:
        origin_by_id = {value.relationship_id: value for value in existing_relationships}
        added_by_id: dict[str, Relationship] = {}
        for relationship in salvaged:
            if relationship.relationship_id in origin_by_id:
                raise DeterministicSalvageValidationError(
                    "safe-row salvage unexpectedly collides with the origin graph"
                )
            prior = added_by_id.get(relationship.relationship_id)
            if prior is not None and prior.to_dict() != relationship.to_dict():
                raise DeterministicSalvageValidationError(
                    "salvaged relationship identity has conflicting payloads"
                )
            added_by_id[relationship.relationship_id] = relationship
        graph = copy.deepcopy(dict(origin_graph))
        edges = graph.get("edges")
        if not isinstance(edges, list):
            raise DeterministicSalvageValidationError("origin graph edges are invalid")
        graph_records, graph_evidence = _graph_records(origin_graph)
        for relationship in added_by_id.values():
            validate_relationship_against_records(
                relationship,
                records=graph_records,
                evidence_ids_by_source=graph_evidence,
            )
        for relationship in sorted(added_by_id.values(), key=lambda value: value.relationship_id):
            left = relationship.payload["left_endpoint"]
            right = relationship.payload["right_endpoint"]
            source, target = (
                (str(right["record_id"]), str(left["record_id"]))
                if relationship.payload["direction"] == "right_to_left"
                else (str(left["record_id"]), str(right["record_id"]))
            )
            edges.append(
                {
                    "source": source,
                    "target": target,
                    "kind": relationship.relation_type,
                    "origin": "cross_source",
                    "relationship_id": relationship.relationship_id,
                }
            )
        if graph.get("nodes") != origin_graph.get("nodes") or edges[
            : len(origin_graph["edges"])
        ] != list(origin_graph["edges"]):
            raise DeterministicSalvageValidationError("salvage changed an origin node or edge")
        validate_schema(
            graph,
            schema_directory=self.schema_directory,
            schema_name="corpus-graph.schema.json",
        )
        return graph, tuple((*existing_relationships, *added_by_id.values()))

    def _pair_partitions(
        self, origin_report: Mapping[str, Any], outcomes: Sequence[Mapping[str, Any]]
    ) -> dict[str, list[list[str]]]:
        raw = _object(origin_report.get("pair_partitions"), "origin pair partitions")
        original_successful = {_pair(value) for value in raw.get("original_successful", [])}
        recovered = {_pair(value) for value in raw.get("recovered_inspected", [])}
        unresolved = {_pair(value) for value in raw.get("remaining_unresolved", [])}
        origin_inspected = original_successful | recovered
        salvaged = {
            _pair(value["source_ids"]) for value in outcomes if value.get("status") == "inspected"
        }
        if not salvaged.issubset(unresolved):
            raise DeterministicSalvageValidationError(
                "salvaged pair outcomes are not a subset of origin unresolved pairs"
            )
        remaining = unresolved - salvaged
        partitions = (origin_inspected, salvaged, remaining)
        if any(
            partitions[left] & partitions[right]
            for left in range(3)
            for right in range(left + 1, 3)
        ):
            raise DeterministicSalvageValidationError("salvage pair partitions overlap")
        return {
            "origin_inspected": [list(value) for value in sorted(origin_inspected)],
            "salvaged_inspected": [list(value) for value in sorted(salvaged)],
            "remaining_unresolved": [list(value) for value in sorted(remaining)],
        }

    def _materialize_lineage(
        self, origin: Mapping[str, Any], retained: Mapping[str, Any]
    ) -> tuple[Path, ...]:
        artifacts = {
            "origin-report.json": origin["report_path"],
            "origin-graph.json": origin["graph_path"],
            "origin-graph-manifest.json": origin["manifest_path"],
            "origin-quality-graph-manifest.json": self.origin_root
            / "lineage"
            / "origin-graph-manifest.json",
            "origin-failed-shard-receipt.json": origin["shard_receipt_path"],
            "original-proposal.json": retained["proposal_path"],
            "original-frozen-records.json": retained["frozen_records_path"],
            "original-worker-receipt.json": retained["original_receipt_path"],
            "salvage-plan.json": self.plan_path,
        }
        paths: list[Path] = []
        for name, source in artifacts.items():
            destination = self.root / "lineage" / name
            _write_bytes_stable(destination, Path(source).read_bytes())
            paths.append(destination)
        return tuple(paths)


def _verified_descriptor(mapping: Mapping[str, Any], key: str) -> tuple[Path, str]:
    descriptor = _object(mapping.get(key), f"{key} descriptor")
    path = Path(str(descriptor.get("path", ""))).resolve()
    sha256 = str(descriptor.get("sha256", ""))
    _verify_hash(path, sha256, key)
    return path, sha256


def _verify_hash(path: Path, expected: str, label: str) -> None:
    if not path.is_file() or fingerprint(path).sha256 != expected:
        raise DeterministicSalvageValidationError(f"retained {label} is missing or changed")


def _receipt_input_sha(receipt: Mapping[str, Any], suffix: str) -> str:
    matches = [
        value
        for value in _objects(receipt.get("inputs"), "worker inputs")
        if str(value.get("path", "")).endswith(suffix)
    ]
    if len(matches) != 1:
        raise DeterministicSalvageValidationError(
            f"original worker receipt does not bind exactly one {suffix} input"
        )
    return str(matches[0].get("sha256", ""))


def _materialize_relationships_stable(
    root: Path, relationships: Sequence[Relationship]
) -> tuple[Path, ...]:
    grouped: dict[tuple[str, str], list[Relationship]] = {}
    for relationship in relationships:
        left = str(relationship.payload["left_endpoint"]["source_id"])
        right = str(relationship.payload["right_endpoint"]["source_id"])
        pair = tuple(sorted((left, right)))
        grouped.setdefault((pair[0], pair[1]), []).append(relationship)
    expected: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="research-map-salvage-") as temp:
        temp_root = Path(temp)
        for pair, values in sorted(grouped.items()):
            relative = Path(f"{pair[0]}--{pair[1]}") / "relationships.md"
            generated = temp_root / relative
            materialize_relationship_markdown(
                generated,
                source_pair=pair,
                relationships=tuple(values),
            )
            destination = root / relative
            _write_bytes_stable(destination, generated.read_bytes())
            expected.append(destination)
    existing = set(root.glob("*--*/relationships.md")) if root.exists() else set()
    if existing != set(expected):
        raise DeterministicSalvageValidationError(
            "existing salvage relationship projection has unexpected files"
        )
    return tuple(expected)


def _descriptor(kind: str, path: Path, *, relative_to: Path) -> dict[str, Any]:
    value = fingerprint(path, relative_to=relative_to)
    return {"kind": kind, **value.to_dict()}


def _write_json_stable(path: Path, payload: Mapping[str, Any]) -> None:
    _write_bytes_stable(path, canonical_json_bytes(dict(payload)))


def _write_bytes_stable(path: Path, data: bytes) -> None:
    if path.exists():
        if path.is_file() and path.read_bytes() == data:
            return
        raise DeterministicSalvageValidationError(
            f"existing salvage artifact differs from deterministic rebuild: {path}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DeterministicSalvageValidationError(
            f"cannot read retained JSON object: {path}"
        ) from error
    return dict(_object(value, str(path)))


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise DeterministicSalvageValidationError(f"{label} must be an object")
    return value


def _objects(value: Any, label: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise DeterministicSalvageValidationError(f"{label} must be an array of objects")
    return list(value)


def _pair(value: Any) -> tuple[str, str]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise DeterministicSalvageValidationError("source pair must contain two source IDs")
    pair = (str(value[0]), str(value[1]))
    if pair[0] >= pair[1]:
        raise DeterministicSalvageValidationError("source pair must be canonical")
    return pair


def stable_salvage_plan_sha256(payload: Mapping[str, Any]) -> str:
    """Expose the canonical plan digest for focused deterministic tests."""

    return hashlib.sha256(canonical_json_bytes(dict(payload))).hexdigest()
