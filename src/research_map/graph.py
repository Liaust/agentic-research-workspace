"""Deterministic source-local graph and endpoint-context compilation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from research_map.receipts import canonical_json_bytes
from research_map.records import Dossier, Record
from research_map.validation import require_valid_dossier


@dataclass(frozen=True, slots=True)
class GraphCompilation:
    graph: dict[str, Any]
    context_envelopes: tuple[dict[str, Any], ...]

    def graph_bytes(self) -> bytes:
        return canonical_json_bytes(self.graph)

    def context_jsonl_bytes(self) -> bytes:
        return b"".join(canonical_json_bytes(item) for item in self.context_envelopes)


def compile_graph(
    dossier: Dossier,
    *,
    schema_directory: Path,
    expected_asset_sha256: str,
    page_count: int,
) -> GraphCompilation:
    require_valid_dossier(
        dossier,
        schema_directory=schema_directory,
        expected_asset_sha256=expected_asset_sha256,
        page_count=page_count,
    )
    nodes = [
        {
            "id": record.id,
            "record_type": record.record_type,
            "revision": record.revision,
            "payload": {
                key: value
                for key, value in record.to_dict().items()
                if key not in {"id", "record_type", "revision"}
            },
        }
        for record in sorted(dossier.records, key=lambda item: item.id)
    ]
    edges = _edges(dossier)
    graph = {
        "schema_version": 1,
        "source_id": dossier.source_id,
        "nodes": nodes,
        "edges": edges,
    }
    envelopes = tuple(
        _context_envelope(dossier, atom)
        for atom in sorted(dossier.records_of_type("atom"), key=lambda item: item.id)
        if atom.payload.get("connectable") is True
    )
    _validate_compilation(graph, envelopes, schema_directory)
    return GraphCompilation(graph=graph, context_envelopes=envelopes)


def _edges(dossier: Dossier) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    for record in dossier.records:
        for evidence_id in _string_list(record.payload.get("evidence_ids")):
            edges.append({"source": record.id, "target": evidence_id, "kind": "supported_by"})
        if record.record_type == "move":
            for position, input_id in enumerate(_string_list(record.payload.get("inputs"))):
                edges.append(
                    {
                        "source": input_id,
                        "target": record.id,
                        "kind": "input_to",
                        "position": position,
                    }
                )
            for position, output_id in enumerate(_string_list(record.payload.get("outputs"))):
                edges.append(
                    {
                        "source": record.id,
                        "target": output_id,
                        "kind": "produces",
                        "position": position,
                    }
                )
            edges.append(
                {
                    "source": record.id,
                    "target": str(record.payload["thread_id"]),
                    "kind": "member_of",
                }
            )
        if record.record_type == "thread":
            move_ids = _string_list(record.payload.get("move_ids"))
            for position, (start, end) in enumerate(zip(move_ids, move_ids[1:], strict=False)):
                edges.append(
                    {
                        "source": start,
                        "target": end,
                        "kind": "precedes",
                        "position": position,
                    }
                )
            branches = record.payload.get("branches", [])
            if isinstance(branches, list):
                for branch in branches:
                    if isinstance(branch, dict):
                        edges.append(
                            {
                                "source": str(branch["from_move"]),
                                "target": str(branch["to_move"]),
                                "kind": "branch",
                                "label": str(branch["label"]),
                            }
                        )
    return sorted(
        edges,
        key=lambda item: (
            item["source"],
            item["target"],
            item["kind"],
            item.get("position", -1),
            item.get("label", ""),
        ),
    )


def _context_envelope(dossier: Dossier, atom: Record) -> dict[str, Any]:
    containing_moves = [
        move
        for move in dossier.records_of_type("move")
        if atom.id in _string_list(move.payload.get("inputs"))
        or atom.id in _string_list(move.payload.get("outputs"))
    ]
    thread_ids = sorted({str(move.payload["thread_id"]) for move in containing_moves})
    evidence_ids = set(_string_list(atom.payload.get("evidence_ids")))
    upstream: set[str] = set()
    downstream: set[str] = set()
    for move in containing_moves:
        evidence_ids.update(_string_list(move.payload.get("evidence_ids")))
        if atom.id in _string_list(move.payload.get("outputs")):
            upstream.update(_string_list(move.payload.get("inputs")))
        if atom.id in _string_list(move.payload.get("inputs")):
            downstream.update(_string_list(move.payload.get("outputs")))
    upstream.discard(atom.id)
    downstream.discard(atom.id)
    return {
        "schema_version": 1,
        "source_id": dossier.source_id,
        "atom_id": atom.id,
        "evidence_ids": sorted(evidence_ids),
        "move_ids": sorted(move.id for move in containing_moves),
        "thread_ids": thread_ids,
        "upstream_ids": sorted(upstream),
        "downstream_ids": sorted(downstream),
        "scope": str(atom.payload["scope"]),
        "qualifications": list(atom.payload["qualifications"]),
    }


def _validate_compilation(
    graph: dict[str, Any], envelopes: tuple[dict[str, Any], ...], schema_directory: Path
) -> None:
    for schema_name, instances in (
        ("graph.schema.json", (graph,)),
        ("context-envelope.schema.json", envelopes),
    ):
        schema = json.loads((schema_directory / schema_name).read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)
        for instance in instances:
            errors = sorted(validator.iter_errors(instance), key=lambda error: list(error.path))
            if errors:
                raise ValueError(errors[0].message)


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]
