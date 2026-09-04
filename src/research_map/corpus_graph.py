"""Deterministic compilation of source-local graphs and canonical relationships."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from research_map.cross_reference import (
    EndpointRef,
    Relationship,
    validate_relationship_against_records,
    validate_schema,
)
from research_map.receipts import FileFingerprint, canonical_json_bytes, fingerprint
from research_map.records import Record
from research_map.relationship_markdown import load_relationship_markdown


class CorpusGraphError(ValueError):
    """Raised when canonical inputs cannot produce a trustworthy corpus graph."""


@dataclass(frozen=True, slots=True)
class CorpusGraphMaterialization:
    batch_id: str
    graph_path: Path
    manifest_path: Path
    graph: dict[str, Any]
    manifest: dict[str, Any]
    artifacts: tuple[dict[str, Any], ...]


def compile_corpus_graph(
    *,
    batch_id: str,
    source_graphs: Mapping[str, Mapping[str, Any]],
    relationships: tuple[Relationship, ...],
    schema_directory: Path,
) -> dict[str, Any]:
    sources = tuple(sorted(source_graphs))
    if len(sources) < 2:
        raise CorpusGraphError("corpus graph requires at least two source graphs")
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    records: dict[str, Record] = {}
    evidence_ids_by_source: dict[str, set[str]] = {}
    source_node_counts: dict[str, int] = {}
    source_edge_counts: dict[str, int] = {}
    for source_id in sources:
        graph = dict(source_graphs[source_id])
        validate_schema(
            graph,
            schema_directory=schema_directory,
            schema_name="graph.schema.json",
        )
        if graph.get("source_id") != source_id:
            raise CorpusGraphError(f"source graph identity changed: {source_id}")
        raw_nodes = graph["nodes"]
        raw_edges = graph["edges"]
        source_node_counts[source_id] = len(raw_nodes)
        source_edge_counts[source_id] = len(raw_edges)
        evidence_ids_by_source[source_id] = set()
        for raw_node in raw_nodes:
            record = Record.from_mapping(
                {
                    "id": raw_node["id"],
                    "record_type": raw_node["record_type"],
                    "revision": raw_node["revision"],
                    **raw_node["payload"],
                }
            )
            if record.source_id != source_id or record.id in records:
                raise CorpusGraphError("source-local node identity is invalid or duplicated")
            records[record.id] = record
            if record.record_type == "evidence":
                evidence_ids_by_source[source_id].add(record.id)
            nodes.append(
                {
                    "id": raw_node["id"],
                    "source_id": source_id,
                    "record_type": raw_node["record_type"],
                    "revision": raw_node["revision"],
                    "payload": raw_node["payload"],
                }
            )
        for raw_edge in raw_edges:
            edge = {
                "source": raw_edge["source"],
                "target": raw_edge["target"],
                "kind": raw_edge["kind"],
                "origin": "source_local",
                "relationship_id": None,
            }
            for optional in ("position", "label"):
                if optional in raw_edge:
                    edge[optional] = raw_edge[optional]
            edges.append(edge)

    relationship_ids: set[str] = set()
    for relationship in sorted(relationships, key=lambda item: item.relationship_id):
        if relationship.relationship_id in relationship_ids:
            raise CorpusGraphError("corpus graph relationship is duplicated")
        relationship_ids.add(relationship.relationship_id)
        validate_schema(
            relationship.to_dict(),
            schema_directory=schema_directory,
            schema_name="relationship.schema.json",
        )
        validate_relationship_against_records(
            relationship,
            records=records,
            evidence_ids_by_source=evidence_ids_by_source,
        )
        left = EndpointRef.from_mapping(relationship.payload["left_endpoint"])
        right = EndpointRef.from_mapping(relationship.payload["right_endpoint"])
        direction = relationship.payload["direction"]
        source, target = (
            (right.record_id, left.record_id)
            if direction == "right_to_left"
            else (left.record_id, right.record_id)
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

    graph = {
        "schema_version": 1,
        "batch_id": batch_id,
        "sources": list(sources),
        "nodes": sorted(nodes, key=lambda item: str(item["id"])),
        "edges": sorted(
            edges,
            key=lambda item: (
                str(item["source"]),
                str(item["target"]),
                str(item["kind"]),
                str(item["origin"]),
                str(item["relationship_id"]),
                int(item.get("position", -1)),
                str(item.get("label", "")),
            ),
        ),
    }
    validate_schema(
        graph,
        schema_directory=schema_directory,
        schema_name="corpus-graph.schema.json",
    )
    for source_id in sources:
        if (
            sum(1 for node in graph["nodes"] if node["source_id"] == source_id)
            != (source_node_counts[source_id])
        ):
            raise CorpusGraphError("source-local nodes changed during corpus compilation")
        if (
            sum(
                1
                for edge in graph["edges"]
                if edge["origin"] == "source_local"
                and str(edge["source"]).startswith(f"{source_id}:")
            )
            != source_edge_counts[source_id]
        ):
            raise CorpusGraphError("source-local edges changed during corpus compilation")
    return graph


class CorpusGraphCompiler:
    def __init__(
        self,
        *,
        repository_root: Path,
        build_root: Path,
        schema_directory: Path,
    ) -> None:
        self.repository_root = repository_root.resolve()
        self.build_root = build_root
        self.schema_directory = schema_directory

    def materialize(
        self,
        *,
        batch_id: str,
        source_graph_paths: Mapping[str, Path],
        relationship_paths: tuple[Path, ...],
        replace_existing: bool = False,
    ) -> CorpusGraphMaterialization:
        source_graphs = {
            source_id: _load_object(path) for source_id, path in sorted(source_graph_paths.items())
        }
        relationships: list[Relationship] = []
        for path in sorted(relationship_paths):
            document = load_relationship_markdown(path)
            relationships.extend(document.relationships)
        graph = compile_corpus_graph(
            batch_id=batch_id,
            source_graphs=source_graphs,
            relationships=tuple(relationships),
            schema_directory=self.schema_directory,
        )
        graph_bytes = canonical_json_bytes(graph)
        output_root = self.build_root / "corpus" / batch_id
        graph_path = output_root / "graph.json"
        manifest_path = output_root / "manifest.json"
        graph_artifact = _artifact(
            "corpus_graph", graph_path, graph_bytes, relative_to=self.repository_root
        )
        source_artifacts = [
            _fingerprint_artifact("source_graph", path, relative_to=self.repository_root)
            for _source_id, path in sorted(source_graph_paths.items())
        ]
        relationship_artifacts = [
            _fingerprint_artifact(
                "canonical_relationship_markdown", path, relative_to=self.repository_root
            )
            for path in sorted(relationship_paths)
        ]
        manifest = {
            "schema_version": "1.0",
            "batch_id": batch_id,
            "sources": sorted(source_graph_paths),
            "inputs": [*source_artifacts, *relationship_artifacts],
            "corpus_graph": graph_artifact,
            "node_count": len(graph["nodes"]),
            "source_local_edge_count": sum(
                1 for item in graph["edges"] if item["origin"] == "source_local"
            ),
            "cross_source_edge_count": sum(
                1 for item in graph["edges"] if item["origin"] == "cross_source"
            ),
        }
        manifest_bytes = canonical_json_bytes(manifest)
        manifest_artifact = _artifact(
            "corpus_manifest", manifest_path, manifest_bytes, relative_to=self.repository_root
        )
        _write_or_verify(graph_path, graph_bytes, replace_existing=replace_existing)
        _write_or_verify(manifest_path, manifest_bytes, replace_existing=replace_existing)
        if graph_path.read_bytes() != graph_bytes or manifest_path.read_bytes() != manifest_bytes:
            raise CorpusGraphError("corpus projection differs from canonical rebuild")
        return CorpusGraphMaterialization(
            batch_id=batch_id,
            graph_path=graph_path,
            manifest_path=manifest_path,
            graph=graph,
            manifest=manifest,
            artifacts=(graph_artifact, manifest_artifact),
        )


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CorpusGraphError(f"cannot read source graph: {path}") from error
    if not isinstance(value, dict):
        raise CorpusGraphError(f"source graph is not an object: {path}")
    return value


def _fingerprint_artifact(kind: str, path: Path, *, relative_to: Path) -> dict[str, Any]:
    value = fingerprint(path, relative_to=_relative_base(path, relative_to))
    return {"kind": kind, **value.to_dict()}


def _artifact(kind: str, path: Path, data: bytes, *, relative_to: Path) -> dict[str, Any]:
    value = FileFingerprint(
        path=_display_path(path, relative_to),
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
    )
    return {"kind": kind, **value.to_dict()}


def _relative_base(path: Path, repository_root: Path) -> Path | None:
    try:
        path.resolve().relative_to(repository_root)
    except ValueError:
        return None
    return repository_root


def _display_path(path: Path, repository_root: Path) -> str:
    base = _relative_base(path, repository_root)
    return path.resolve().relative_to(base).as_posix() if base else str(path)


def _write_or_verify(path: Path, data: bytes, *, replace_existing: bool = False) -> None:
    if path.exists():
        if not path.is_file():
            raise CorpusGraphError(f"existing compiled artifact differs from rebuild: {path}")
        if path.read_bytes() == data:
            return
        if not replace_existing:
            raise CorpusGraphError(f"existing compiled artifact differs from rebuild: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise
