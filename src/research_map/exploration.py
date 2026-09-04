"""Read-only, provenance-verified search and one-hop graph exploration."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from research_map.cross_reference import validate_schema
from research_map.receipts import FileFingerprint, fingerprint
from research_map.records import Record
from research_map.relationship_markdown import load_relationship_markdown

_TOKEN = re.compile(r"[^\W_]+(?:'[^\W_]+)*", re.UNICODE)
_SNIPPET_LIMIT = 280


class GraphExplorationError(ValueError):
    """Raised when a graph cannot be explored without weakening provenance."""


class UnresolvedGraphObject(GraphExplorationError):
    """Raised when an explicit stable ID is absent from the selected graph."""


@dataclass(frozen=True, slots=True)
class SearchFilters:
    """Explicit, composable constraints for graph search."""

    source_ids: tuple[str, ...] = ()
    record_types: tuple[str, ...] = ()
    kinds: tuple[str, ...] = ()
    relationship_types: tuple[str, ...] = ()
    connectable: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_ids": list(self.source_ids),
            "record_types": list(self.record_types),
            "kinds": list(self.kinds),
            "relationship_types": list(self.relationship_types),
            "connectable": self.connectable,
        }


@dataclass(frozen=True, slots=True)
class _SearchDocument:
    object_id: str
    object_type: str
    record_type: str
    source_ids: tuple[str, ...]
    kind: str | None
    label: str
    evidence_ids: tuple[str, ...]
    connectable: bool | None
    fields: tuple[tuple[str, str, int], ...]


class GraphExplorer:
    """Validated in-memory view of one explicit source or corpus graph."""

    def __init__(
        self,
        *,
        graph_path: Path,
        schema_directory: Path | None = None,
    ) -> None:
        self.graph_path = graph_path.resolve()
        self.schema_directory = (
            schema_directory.resolve()
            if schema_directory is not None
            else Path(__file__).resolve().parents[2] / "schemas" / "research-map" / "v1"
        )
        self.graph = _load_object(self.graph_path, label="graph")
        self.graph_kind = "corpus" if "batch_id" in self.graph else "source"
        schema_name = (
            "corpus-graph.schema.json" if self.graph_kind == "corpus" else "graph.schema.json"
        )
        try:
            validate_schema(
                self.graph,
                schema_directory=self.schema_directory,
                schema_name=schema_name,
            )
        except ValueError as error:
            raise GraphExplorationError(f"invalid compiled graph: {error}") from error
        self.graph_fingerprint = _fingerprint(self.graph_path, label="graph")
        self.nodes = self._validated_nodes()
        self.edges = self._validated_edges()
        self.manifest_path = self.graph_path.with_name("manifest.json")
        self.manifest: dict[str, Any] | None = None
        self.manifest_fingerprint: FileFingerprint | None = None
        self.namespace_root: Path | None = None
        self.relationships: dict[str, dict[str, Any]] = {}
        self._load_manifest_and_relationships()
        self._validate_cross_source_edges()

    @property
    def graph_identity(self) -> dict[str, Any]:
        manifest = self.manifest_fingerprint
        return {
            "path": str(self.graph_path),
            "sha256": self.graph_fingerprint.sha256,
            "size_bytes": self.graph_fingerprint.size_bytes,
            "kind": self.graph_kind,
            "manifest_path": str(self.manifest_path) if self.manifest is not None else None,
            "manifest_sha256": manifest.sha256 if manifest is not None else None,
            "manifest_verified": self.manifest is not None,
            "namespace_root": (
                str(self.namespace_root) if self.namespace_root is not None else None
            ),
        }

    def search(
        self,
        query: str,
        *,
        filters: SearchFilters | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Rank matching existing objects without generating semantic content."""

        normalized_query = _normalize(query)
        query_terms = _terms(query)
        if not normalized_query or not query_terms:
            raise GraphExplorationError("search query must contain at least one word")
        if limit < 1:
            raise GraphExplorationError("search result limit must be positive")
        selected_filters = filters or SearchFilters()
        matches: list[dict[str, Any]] = []
        for document in self._search_documents():
            if not _included(document, selected_filters):
                continue
            ranked = _rank(document, normalized_query=normalized_query, query_terms=query_terms)
            if ranked is not None:
                matches.append(ranked)
        matches.sort(key=lambda item: (-int(item["score"]), str(item["object_id"])))
        returned = matches[:limit]
        return {
            "graph": self.graph_identity,
            "query": query,
            "normalized_query": normalized_query,
            "filters": selected_filters.to_dict(),
            "total_matches": len(matches),
            "returned_count": len(returned),
            "limit": limit,
            "truncated": len(matches) > len(returned),
            "results": returned,
        }

    def explore(self, object_id: str, *, neighbor_limit: int = 25) -> dict[str, Any]:
        """Resolve a stable ID and return bounded existing one-hop context."""

        if not object_id.strip():
            raise GraphExplorationError("exploration ID must be non-empty")
        if neighbor_limit < 1:
            raise GraphExplorationError("neighbor limit must be positive")
        node = self.nodes.get(object_id)
        relationship = self.relationships.get(object_id)
        if node is not None and relationship is not None:
            raise GraphExplorationError(f"stable ID is ambiguous: {object_id}")
        if node is not None:
            return {
                "graph": self.graph_identity,
                "requested_id": object_id,
                "object_type": "node",
                "object": _copy(node),
                "context": self._node_context(object_id, neighbor_limit=neighbor_limit),
            }
        if relationship is not None:
            left_id = str(relationship["left_endpoint"]["record_id"])
            right_id = str(relationship["right_endpoint"]["record_id"])
            return {
                "graph": self.graph_identity,
                "requested_id": object_id,
                "object_type": "relationship",
                "object": _copy(relationship),
                "endpoints": [_copy(self.nodes[left_id]), _copy(self.nodes[right_id])],
                "endpoint_contexts": [
                    self._node_context(left_id, neighbor_limit=neighbor_limit),
                    self._node_context(right_id, neighbor_limit=neighbor_limit),
                ],
            }
        raise UnresolvedGraphObject(f"stable ID does not resolve in graph: {object_id}")

    def _validated_nodes(self) -> dict[str, dict[str, Any]]:
        raw_nodes = self.graph.get("nodes")
        if not isinstance(raw_nodes, list):
            raise GraphExplorationError("compiled graph nodes are not a list")
        nodes: dict[str, dict[str, Any]] = {}
        for raw in raw_nodes:
            if not isinstance(raw, dict):
                raise GraphExplorationError("compiled graph node is not an object")
            payload = raw.get("payload")
            if not isinstance(payload, dict):
                raise GraphExplorationError("compiled graph node payload is not an object")
            record_value = {
                "id": raw.get("id"),
                "record_type": raw.get("record_type"),
                "revision": raw.get("revision"),
                **payload,
            }
            try:
                record = Record.from_mapping(record_value)
                validate_schema(
                    record.to_dict(),
                    schema_directory=self.schema_directory,
                    schema_name=f"{record.record_type}.schema.json",
                )
            except ValueError as error:
                raise GraphExplorationError(f"invalid graph node: {error}") from error
            if record.id in nodes:
                raise GraphExplorationError(f"duplicate graph node ID: {record.id}")
            source_id = raw.get("source_id", record.source_id)
            if source_id != record.source_id:
                raise GraphExplorationError(f"graph node source identity differs: {record.id}")
            nodes[record.id] = {
                "id": record.id,
                "record_type": record.record_type,
                "revision": record.revision,
                "source_id": record.source_id,
                "payload": _copy(payload),
            }
        return nodes

    def _validated_edges(self) -> tuple[dict[str, Any], ...]:
        raw_edges = self.graph.get("edges")
        if not isinstance(raw_edges, list):
            raise GraphExplorationError("compiled graph edges are not a list")
        edges: list[dict[str, Any]] = []
        for raw in raw_edges:
            if not isinstance(raw, dict):
                raise GraphExplorationError("compiled graph edge is not an object")
            source = raw.get("source")
            target = raw.get("target")
            if source not in self.nodes or target not in self.nodes:
                raise GraphExplorationError(
                    f"graph edge endpoint does not resolve: {source!r} -> {target!r}"
                )
            edges.append(_copy(raw))
        return tuple(sorted(edges, key=_edge_key))

    def _load_manifest_and_relationships(self) -> None:
        if not self.manifest_path.exists():
            return
        manifest = _load_object(self.manifest_path, label="graph manifest")
        graph_artifact = _graph_artifact(manifest, graph_kind=self.graph_kind)
        recorded_graph_path = _artifact_path(graph_artifact, label="manifest graph")
        namespace_root = _derive_namespace_root(self.graph_path, recorded_graph_path)
        _verify_artifact(graph_artifact, root=namespace_root, expected_path=self.graph_path)
        inputs = _manifest_inputs(manifest, graph_kind=self.graph_kind)
        verified_inputs: list[tuple[dict[str, Any], Path]] = []
        for artifact in inputs:
            verified_inputs.append((artifact, _verify_artifact(artifact, root=namespace_root)))
        self.manifest = manifest
        self.manifest_fingerprint = _fingerprint(self.manifest_path, label="graph manifest")
        self.namespace_root = namespace_root
        for artifact, path in verified_inputs:
            if artifact.get("kind") != "canonical_relationship_markdown":
                continue
            try:
                document = load_relationship_markdown(path)
            except ValueError as error:
                raise GraphExplorationError(
                    f"verified relationship input is invalid: {path}: {error}"
                ) from error
            for relationship in document.relationships:
                if relationship.relationship_id in self.relationships:
                    raise GraphExplorationError(
                        f"relationship ID appears in multiple verified inputs: "
                        f"{relationship.relationship_id}"
                    )
                try:
                    validate_schema(
                        relationship.to_dict(),
                        schema_directory=self.schema_directory,
                        schema_name="relationship.schema.json",
                    )
                except ValueError as error:
                    raise GraphExplorationError(
                        f"invalid verified relationship: {relationship.relationship_id}: {error}"
                    ) from error
                self.relationships[relationship.relationship_id] = relationship.to_dict()

    def _validate_cross_source_edges(self) -> None:
        for edge in self.edges:
            relationship_id = edge.get("relationship_id")
            if edge.get("origin") != "cross_source" and relationship_id is None:
                continue
            if not isinstance(relationship_id, str):
                raise GraphExplorationError("cross-source edge lacks a relationship ID")
            relationship = self.relationships.get(relationship_id)
            if relationship is None:
                raise GraphExplorationError(
                    f"relationship detail is unresolved by the verified manifest: {relationship_id}"
                )
            endpoints = {
                str(relationship["left_endpoint"]["record_id"]),
                str(relationship["right_endpoint"]["record_id"]),
            }
            if endpoints != {str(edge["source"]), str(edge["target"])}:
                raise GraphExplorationError(
                    f"relationship edge endpoints differ from canonical detail: {relationship_id}"
                )
            if edge.get("kind") != relationship.get("relation_type"):
                raise GraphExplorationError(
                    f"relationship edge type differs from canonical detail: {relationship_id}"
                )

    def _search_documents(self) -> Iterable[_SearchDocument]:
        for node in self.nodes.values():
            yield _node_document(node)
        for relationship in self.relationships.values():
            yield _relationship_document(relationship)

    def _node_context(self, object_id: str, *, neighbor_limit: int) -> dict[str, Any]:
        incident = [
            edge
            for edge in self.edges
            if edge["source"] == object_id or edge["target"] == object_id
        ]
        selected = incident[:neighbor_limit]
        neighbor_ids = sorted(
            {
                str(edge["target"] if edge["source"] == object_id else edge["source"])
                for edge in selected
            }
        )
        neighbors = [self.nodes[neighbor_id] for neighbor_id in neighbor_ids]
        move_nodes = [node for node in neighbors if node["record_type"] == "move"]
        evidence_ids = {
            neighbor["id"] for neighbor in neighbors if neighbor["record_type"] == "evidence"
        }
        payload_evidence = self.nodes[object_id]["payload"].get("evidence_ids", [])
        if isinstance(payload_evidence, list):
            evidence_ids.update(
                item for item in payload_evidence if isinstance(item, str) and item in self.nodes
            )
        for move in move_nodes:
            move_evidence = move["payload"].get("evidence_ids", [])
            if isinstance(move_evidence, list):
                evidence_ids.update(
                    item for item in move_evidence if isinstance(item, str) and item in self.nodes
                )
        thread_ids = {node["id"] for node in neighbors if node["record_type"] == "thread"}
        for move in move_nodes:
            thread_id = move["payload"].get("thread_id")
            if isinstance(thread_id, str) and thread_id in self.nodes:
                thread_ids.add(thread_id)
        cross_ids = sorted(
            {
                str(edge["relationship_id"])
                for edge in selected
                if edge.get("origin") == "cross_source"
                and isinstance(edge.get("relationship_id"), str)
            }
        )
        edge_groups: dict[str, list[dict[str, Any]]] = {
            "incoming_source_local": [],
            "outgoing_source_local": [],
            "incoming_cross_source": [],
            "outgoing_cross_source": [],
        }
        for edge in selected:
            direction = "incoming" if edge["target"] == object_id else "outgoing"
            origin = "cross_source" if edge.get("origin") == "cross_source" else "source_local"
            edge_groups[f"{direction}_{origin}"].append(_copy(edge))
        return {
            "total_incident_edges": len(incident),
            "returned_edge_count": len(selected),
            "neighbor_limit": neighbor_limit,
            "omitted_edge_count": len(incident) - len(selected),
            "truncated": len(incident) > len(selected),
            "edges": [_copy(edge) for edge in selected],
            "edge_groups": edge_groups,
            "neighbors": [_node_summary(node) for node in neighbors],
            "evidence": [_copy(self.nodes[value]) for value in sorted(evidence_ids)],
            "moves": [_copy(node) for node in move_nodes],
            "threads": [_copy(self.nodes[value]) for value in sorted(thread_ids)],
            "cross_source_relationships": [_copy(self.relationships[value]) for value in cross_ids],
        }


def _node_document(node: Mapping[str, Any]) -> _SearchDocument:
    payload = _mapping(node.get("payload"), label="node payload")
    record_type = str(node["record_type"])
    specifications: dict[str, tuple[tuple[str, int], ...]] = {
        "atom": (
            ("label", 12),
            ("statement", 10),
            ("kind", 7),
            ("scope", 6),
            ("standalone_context", 6),
            ("epistemic_posture", 3),
            ("qualifications", 3),
            ("attribution", 2),
            ("type_gap", 2),
            ("fidelity", 2),
        ),
        "evidence": (("exact_span", 10), ("locator", 6), ("segments", 4)),
        "move": (
            ("label", 12),
            ("summary", 10),
            ("scientific_use", 8),
            ("role", 7),
            ("type_gap", 2),
        ),
        "thread": (
            ("title", 12),
            ("summary", 10),
            ("kind", 7),
            ("type_gap", 2),
        ),
    }
    fields: list[tuple[str, str, int]] = [
        ("id", str(node["id"]), 4),
        ("source_id", str(node["source_id"]), 4),
        ("record_type", record_type, 4),
    ]
    for name, weight in specifications[record_type]:
        text = _search_text(payload.get(name))
        if text:
            fields.append((name, text, weight))
    label = str(
        payload.get("label") or payload.get("title") or payload.get("locator") or node["id"]
    )
    evidence_ids = _string_tuple(payload.get("evidence_ids"))
    if record_type == "evidence":
        evidence_ids = (str(node["id"]),)
    kind = payload.get("kind", payload.get("role"))
    return _SearchDocument(
        object_id=str(node["id"]),
        object_type="node",
        record_type=record_type,
        source_ids=(str(node["source_id"]),),
        kind=str(kind) if isinstance(kind, str) else None,
        label=label,
        evidence_ids=evidence_ids,
        connectable=(payload.get("connectable") if record_type == "atom" else None),
        fields=tuple(fields),
    )


def _relationship_document(relationship: Mapping[str, Any]) -> _SearchDocument:
    left = _mapping(relationship.get("left_endpoint"), label="left endpoint")
    right = _mapping(relationship.get("right_endpoint"), label="right endpoint")
    relationship_id = str(relationship["relationship_id"])
    relation_type = str(relationship["relation_type"])
    fields = (
        ("relationship_id", relationship_id, 4),
        ("relation_type", relation_type, 9),
        ("comparison_surface", _search_text(relationship.get("comparison_surface")), 12),
        ("rationale", _search_text(relationship.get("rationale")), 10),
        ("scope_alignment", _search_text(relationship.get("scope_alignment")), 8),
        ("assumption_alignment", _search_text(relationship.get("assumption_alignment")), 8),
        ("qualifications", _search_text(relationship.get("qualifications")), 3),
        ("left_endpoint", str(left.get("record_id", "")), 4),
        ("right_endpoint", str(right.get("record_id", "")), 4),
    )
    evidence_ids = tuple(
        sorted(
            {
                *_string_tuple(relationship.get("left_evidence_ids")),
                *_string_tuple(relationship.get("right_evidence_ids")),
            }
        )
    )
    return _SearchDocument(
        object_id=relationship_id,
        object_type="relationship",
        record_type="relationship",
        source_ids=(str(left["source_id"]), str(right["source_id"])),
        kind=relation_type,
        label=f"{relation_type}: {left['record_id']} ↔ {right['record_id']}",
        evidence_ids=evidence_ids,
        connectable=None,
        fields=tuple(field for field in fields if field[1]),
    )


def _rank(
    document: _SearchDocument,
    *,
    normalized_query: str,
    query_terms: tuple[str, ...],
) -> dict[str, Any] | None:
    normalized_fields = [
        (name, text, weight, _normalize(text), frozenset(_terms(text)))
        for name, text, weight in document.fields
    ]
    matched_terms = tuple(
        term
        for term in query_terms
        if any(term in field_terms for *_, field_terms in normalized_fields)
    )
    if len(matched_terms) != len(query_terms):
        return None
    score = 0
    matched_fields: list[dict[str, Any]] = []
    for name, text, weight, normalized, field_terms in normalized_fields:
        terms = [term for term in query_terms if term in field_terms]
        phrase = normalized_query in normalized
        if not terms and not phrase:
            continue
        field_score = weight * len(terms) + (weight * len(query_terms) * 2 if phrase else 0)
        score += field_score
        matched_fields.append(
            {
                "field": name,
                "matched_terms": terms,
                "exact_phrase": phrase,
                "field_score": field_score,
                "snippet": _snippet(text, normalized_query=normalized_query, terms=query_terms),
            }
        )
    matched_fields.sort(key=lambda item: (-int(item["field_score"]), str(item["field"])))
    return {
        "object_id": document.object_id,
        "object_type": document.object_type,
        "record_type": document.record_type,
        "source_ids": list(document.source_ids),
        "kind": document.kind,
        "label": document.label,
        "score": score,
        "matched_terms": list(matched_terms),
        "matched_fields": matched_fields,
        "snippet": matched_fields[0]["snippet"],
        "evidence_ids": list(document.evidence_ids),
    }


def _included(document: _SearchDocument, filters: SearchFilters) -> bool:
    if filters.source_ids and not set(document.source_ids).intersection(filters.source_ids):
        return False
    if filters.record_types and document.record_type not in filters.record_types:
        return False
    if filters.kinds and document.kind not in filters.kinds:
        return False
    if filters.relationship_types and (
        document.object_type != "relationship" or document.kind not in filters.relationship_types
    ):
        return False
    return filters.connectable is None or document.connectable is filters.connectable


def _manifest_inputs(manifest: Mapping[str, Any], *, graph_kind: str) -> list[dict[str, Any]]:
    key = "inputs" if graph_kind == "corpus" else "artifacts"
    raw = manifest.get(key, [])
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise GraphExplorationError(f"graph manifest {key} are invalid")
    graph_kind_name = "corpus_graph" if graph_kind == "corpus" else "source_graph"
    return [dict(item) for item in raw if item.get("kind") != graph_kind_name]


def _graph_artifact(manifest: Mapping[str, Any], *, graph_kind: str) -> dict[str, Any]:
    if graph_kind == "corpus":
        raw = manifest.get("corpus_graph")
        if not isinstance(raw, dict):
            raise GraphExplorationError("corpus manifest has no graph artifact")
        return dict(raw)
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise GraphExplorationError("source manifest has no artifacts")
    candidates = [
        item for item in artifacts if isinstance(item, dict) and item.get("kind") == "source_graph"
    ]
    if len(candidates) != 1:
        raise GraphExplorationError("source manifest must identify exactly one source graph")
    return dict(candidates[0])


def _derive_namespace_root(actual_path: Path, recorded_path: Path) -> Path:
    if recorded_path.is_absolute():
        if actual_path != recorded_path.resolve():
            raise GraphExplorationError("manifest graph path differs from the explicit graph path")
        return Path(recorded_path.anchor)
    parts = recorded_path.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise GraphExplorationError("manifest graph path is not a safe relative path")
    if len(actual_path.parts) < len(parts) or actual_path.parts[-len(parts) :] != parts:
        raise GraphExplorationError(
            "cannot derive one namespace root from the explicit and recorded graph paths"
        )
    root_parts = actual_path.parts[: -len(parts)]
    return Path(*root_parts) if root_parts else Path(actual_path.anchor)


def _verify_artifact(
    artifact: Mapping[str, Any],
    *,
    root: Path,
    expected_path: Path | None = None,
) -> Path:
    recorded = _artifact_path(artifact, label="manifest artifact")
    path = recorded.resolve() if recorded.is_absolute() else (root / recorded).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as error:
        raise GraphExplorationError(
            f"manifest artifact escapes its namespace: {recorded}"
        ) from error
    if expected_path is not None and path != expected_path:
        raise GraphExplorationError("manifest graph path differs from the explicit graph path")
    if not path.is_file():
        raise GraphExplorationError(f"manifest artifact does not exist: {path}")
    value = _fingerprint(path, label="manifest artifact")
    sha256 = artifact.get("sha256")
    size_bytes = artifact.get("size_bytes")
    if value.sha256 != sha256 or value.size_bytes != size_bytes:
        raise GraphExplorationError(f"manifest artifact fingerprint differs: {path}")
    return path


def _artifact_path(artifact: Mapping[str, Any], *, label: str) -> Path:
    value = artifact.get("path")
    if not isinstance(value, str) or not value:
        raise GraphExplorationError(f"{label} path is invalid")
    return Path(value)


def _load_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GraphExplorationError(f"cannot read {label}: {path}: {error}") from error
    if not isinstance(value, dict):
        raise GraphExplorationError(f"{label} is not an object: {path}")
    return value


def _fingerprint(path: Path, *, label: str) -> FileFingerprint:
    try:
        return fingerprint(path)
    except OSError as error:
        raise GraphExplorationError(f"cannot fingerprint {label}: {path}: {error}") from error


def _node_summary(node: Mapping[str, Any]) -> dict[str, Any]:
    payload = _mapping(node.get("payload"), label="node payload")
    label = payload.get("label") or payload.get("title") or payload.get("locator") or node["id"]
    return {
        "id": node["id"],
        "record_type": node["record_type"],
        "source_id": node["source_id"],
        "kind": payload.get("kind", payload.get("role")),
        "label": label,
        "evidence_ids": list(_string_tuple(payload.get("evidence_ids"))),
    }


def _edge_key(edge: Mapping[str, Any]) -> tuple[str, str, str, str, str, int, str]:
    return (
        str(edge.get("source", "")),
        str(edge.get("target", "")),
        str(edge.get("kind", "")),
        str(edge.get("origin", "")),
        str(edge.get("relationship_id", "")),
        int(edge.get("position", -1)),
        str(edge.get("label", "")),
    )


def _normalize(value: str) -> str:
    return " ".join(_terms(value))


def _terms(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", value)
    return tuple(dict.fromkeys(match.group(0).casefold() for match in _TOKEN.finditer(normalized)))


def _search_text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return " ".join(_search_text(item) for item in value if _search_text(item))
    if isinstance(value, dict):
        parts: list[str] = []
        for key in sorted(value):
            text = _search_text(value[key])
            if text:
                parts.append(text)
        return " ".join(parts)
    return ""


def _snippet(text: str, *, normalized_query: str, terms: Sequence[str]) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= _SNIPPET_LIMIT:
        return collapsed
    lower = collapsed.casefold()
    index = lower.find(normalized_query)
    if index < 0:
        positions = [lower.find(term) for term in terms if lower.find(term) >= 0]
        index = min(positions, default=0)
    start = max(0, index - (_SNIPPET_LIMIT // 3))
    end = min(len(collapsed), start + _SNIPPET_LIMIT)
    prefix = "…" if start else ""
    suffix = "…" if end < len(collapsed) else ""
    return prefix + collapsed[start:end] + suffix


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise GraphExplorationError(f"{label} is not an object")
    return value


def _copy(value: Mapping[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(json.dumps(dict(value))))
