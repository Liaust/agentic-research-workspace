"""Deterministic semantic and structural validation for source-local dossiers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from research_map.records import Dossier, Record

RECOMMENDED_ATOM_KINDS = frozenset(
    {
        "claim",
        "definition",
        "assumption",
        "concept",
        "equation",
        "method",
        "result",
        "question",
        "objection",
        "response",
        "limitation",
        "figure",
    }
)
RECOMMENDED_MOVE_ROLES = frozenset(
    {
        "introduces",
        "defines",
        "assumes",
        "derives",
        "supports",
        "justifies",
        "applies",
        "qualifies",
        "contrasts",
        "interprets",
        "objects",
        "responds",
        "concludes",
    }
)
RECOMMENDED_THREAD_KINDS = frozenset(
    {
        "argument",
        "derivation",
        "model-construction",
        "question-method-result",
        "definition-development",
        "objection-response",
        "comparison",
    }
)


@dataclass(frozen=True, slots=True)
class ValidationReport:
    errors: tuple[str, ...]
    type_gaps: tuple[dict[str, str], ...]

    @property
    def ok(self) -> bool:
        return not self.errors


class DossierValidationError(ValueError):
    """Raised when a dossier cannot enter canonical or graph state."""


def validate_dossier(
    dossier: Dossier,
    *,
    schema_directory: Path,
    expected_asset_sha256: str,
    page_count: int,
) -> ValidationReport:
    errors: list[str] = []
    type_gaps: list[dict[str, str]] = []
    evidence = {record.id: record for record in dossier.records_of_type("evidence")}
    atoms = {record.id: record for record in dossier.records_of_type("atom")}
    moves = {record.id: record for record in dossier.records_of_type("move")}
    threads = {record.id: record for record in dossier.records_of_type("thread")}

    vocabulary = validate_record_vocabulary(dossier.records)
    errors.extend(vocabulary.errors)
    type_gaps.extend(vocabulary.type_gaps)

    for record in dossier.records:
        _validate_record_schema(record, schema_directory, errors)
        if record.source_id != dossier.source_id or not record.id.startswith(
            f"{dossier.source_id}:{record.record_type}:"
        ):
            errors.append(f"{record.id}: source-scoped identity does not match dossier")

    for record in evidence.values():
        payload = record.payload
        if payload.get("asset_sha256") != expected_asset_sha256:
            errors.append(f"{record.id}: evidence asset fingerprint does not match")
        start = payload.get("page_start")
        end = payload.get("page_end")
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or start > end
            or end > page_count
        ):
            errors.append(f"{record.id}: evidence page route is outside the source")

    for record in atoms.values():
        _validate_evidence_links(record, evidence, errors)
    for record in moves.values():
        _validate_evidence_links(record, evidence, errors)
    for record in threads.values():
        _validate_evidence_links(record, evidence, errors)

    move_membership: dict[str, list[str]] = {identifier: [] for identifier in moves}
    for thread in threads.values():
        move_ids = _string_list(thread.payload.get("move_ids"))
        ordered_moves = [moves[move_id] for move_id in move_ids if move_id in moves]
        presentation_indices = [move.payload.get("presentation_index") for move in ordered_moves]
        if any(index is not None for index in presentation_indices):
            if not all(
                isinstance(index, int) and not isinstance(index, bool)
                for index in presentation_indices
            ):
                errors.append(f"{thread.id}: every ordered move needs a presentation index")
            else:
                integer_indices = [cast(int, index) for index in presentation_indices]
                if integer_indices != sorted(integer_indices):
                    errors.append(f"{thread.id}: move order disagrees with presentation indices")
                elif len(set(integer_indices)) != len(integer_indices):
                    errors.append(f"{thread.id}: presentation indices must be unique")
        for move_id in move_ids:
            if move_id not in moves:
                errors.append(f"{thread.id}: unknown move reference {move_id}")
            else:
                move_membership[move_id].append(thread.id)
        for entry in _string_list(thread.payload.get("entry_points")):
            if entry not in atoms and entry not in moves:
                errors.append(f"{thread.id}: unknown entry point {entry}")
        for branch in thread.payload.get("branches", []):
            if not isinstance(branch, dict):
                continue
            start = branch.get("from_move")
            end = branch.get("to_move")
            if start not in move_ids or end not in move_ids:
                errors.append(f"{thread.id}: branch endpoints must be ordered thread moves")

    atom_connections: dict[str, int] = {identifier: 0 for identifier in atoms}
    producer_by_atom: dict[str, list[str]] = {identifier: [] for identifier in atoms}
    for move in moves.values():
        thread_id = move.payload.get("thread_id")
        if thread_id not in threads:
            errors.append(f"{move.id}: unknown containing thread {thread_id}")
        elif move.id not in _string_list(threads[str(thread_id)].payload.get("move_ids")):
            errors.append(f"{move.id}: containing thread does not list move")
        if not move_membership.get(move.id):
            errors.append(f"{move.id}: move is not ordered in a thread")
        inputs = _string_list(move.payload.get("inputs"))
        outputs = _string_list(move.payload.get("outputs"))
        if move.payload.get("role") in {"derives", "concludes"} and not inputs:
            errors.append(f"{move.id}: derivation/conclusion has no declared inputs")
        for input_id in inputs:
            if input_id not in atoms and input_id not in moves:
                errors.append(f"{move.id}: unknown input {input_id}")
            if input_id in atoms:
                atom_connections[input_id] += 1
        for output_id in outputs:
            if output_id not in atoms:
                errors.append(f"{move.id}: output is not an atom {output_id}")
            else:
                atom_connections[output_id] += 1
                producer_by_atom[output_id].append(move.id)

    for atom in atoms.values():
        standalone = atom.payload.get("standalone_context")
        if atom_connections[atom.id] == 0 and not (
            isinstance(standalone, str) and standalone.strip()
        ):
            errors.append(f"{atom.id}: unexplained orphan atom")

    for move in moves.values():
        if move.payload.get("role") != "concludes":
            continue
        for output_id in _string_list(move.payload.get("outputs")):
            trace = trace_upstream(dossier, output_id)
            if not trace or not any(identifier in evidence for identifier in trace):
                errors.append(f"{output_id}: conclusion has no backward trace to evidence")

    return ValidationReport(tuple(dict.fromkeys(errors)), tuple(type_gaps))


def validate_record_vocabulary(records: tuple[Record, ...]) -> ValidationReport:
    """Validate open-vocabulary use before proposal records enter persistent WIP."""

    errors: list[str] = []
    type_gaps: list[dict[str, str]] = []
    for record in records:
        if record.record_type == "atom":
            _record_type_gap(record, "kind", RECOMMENDED_ATOM_KINDS, type_gaps, errors)
        elif record.record_type == "move":
            _record_type_gap(record, "role", RECOMMENDED_MOVE_ROLES, type_gaps, errors)
        elif record.record_type == "thread":
            _record_type_gap(record, "kind", RECOMMENDED_THREAD_KINDS, type_gaps, errors)
    return ValidationReport(tuple(dict.fromkeys(errors)), tuple(type_gaps))


def require_valid_dossier(
    dossier: Dossier,
    *,
    schema_directory: Path,
    expected_asset_sha256: str,
    page_count: int,
) -> ValidationReport:
    report = validate_dossier(
        dossier,
        schema_directory=schema_directory,
        expected_asset_sha256=expected_asset_sha256,
        page_count=page_count,
    )
    if not report.ok:
        raise DossierValidationError("; ".join(report.errors))
    return report


def trace_upstream(dossier: Dossier, record_id: str) -> tuple[str, ...]:
    """Return a deterministic backward trace through producing moves and evidence."""

    index = dossier.index()
    producers: dict[str, list[Record]] = {}
    for move in dossier.records_of_type("move"):
        for output in _string_list(move.payload.get("outputs")):
            producers.setdefault(output, []).append(move)
    visited: set[str] = set()

    def visit(identifier: str) -> None:
        if identifier in visited or identifier not in index:
            return
        visited.add(identifier)
        record = index[identifier]
        for evidence_id in _string_list(record.payload.get("evidence_ids")):
            visit(evidence_id)
        if record.record_type == "atom":
            for producer in sorted(producers.get(identifier, []), key=lambda item: item.id):
                visit(producer.id)
        if record.record_type == "move":
            for input_id in _string_list(record.payload.get("inputs")):
                visit(input_id)

    visit(record_id)
    return tuple(sorted(visited))


def _validate_record_schema(record: Record, schema_directory: Path, errors: list[str]) -> None:
    schema_path = schema_directory / f"{record.record_type}.schema.json"
    try:
        schema: Any = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as read_error:
        errors.append(f"{record.id}: cannot load schema: {read_error}")
        return
    validator = Draft202012Validator(schema)
    for schema_error in sorted(
        validator.iter_errors(record.payload), key=lambda item: list(item.path)
    ):
        location = ".".join(str(part) for part in schema_error.path) or "record"
        errors.append(f"{record.id}: {location}: {schema_error.message}")


def _validate_evidence_links(
    record: Record, evidence: dict[str, Record], errors: list[str]
) -> None:
    for evidence_id in _string_list(record.payload.get("evidence_ids")):
        if evidence_id not in evidence:
            errors.append(f"{record.id}: unknown evidence reference {evidence_id}")


def _record_type_gap(
    record: Record,
    field: str,
    recommended: frozenset[str],
    type_gaps: list[dict[str, str]],
    errors: list[str],
) -> None:
    slug = record.payload.get(field)
    if slug in recommended:
        return
    gap = record.payload.get("type_gap")
    if not isinstance(gap, dict) or gap.get("slug") != slug or not gap.get("rationale"):
        errors.append(f"{record.id}: unknown {field} requires an explicit type_gap")
        return
    type_gaps.append(
        {"record_id": record.id, "slug": str(slug), "rationale": str(gap["rationale"])}
    )


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]
