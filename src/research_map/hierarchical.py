"""Validation for the experimental hierarchical whole-paper proposal.

The model reads the PDF and page images. This module runs after generation and
accepts already-extracted page text only to check verbatim prose provenance; it
does not parse a paper or provide semantic input to the reader.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from research_map.records import Record, RecordError
from research_map.validation import (
    RECOMMENDED_ATOM_KINDS,
    RECOMMENDED_MOVE_ROLES,
    RECOMMENDED_THREAD_KINDS,
)

FindingDisposition = Literal["passed", "failed", "uncheckable"]


@dataclass(frozen=True, slots=True)
class HierarchicalFinding:
    """One inspectable deterministic or provenance result."""

    code: str
    disposition: FindingDisposition
    subject_id: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "disposition": self.disposition,
            "subject_id": self.subject_id,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class HierarchicalValidationReport:
    """Complete external validation report for one unchanged proposal."""

    findings: tuple[HierarchicalFinding, ...]

    @property
    def ok(self) -> bool:
        return not any(item.disposition == "failed" for item in self.findings)

    @property
    def has_uncheckable(self) -> bool:
        return any(item.disposition == "uncheckable" for item in self.findings)

    def findings_with_code(self, code: str) -> tuple[HierarchicalFinding, ...]:
        return tuple(item for item in self.findings if item.code == code)

    def to_dict(self) -> dict[str, Any]:
        counts = Counter(item.disposition for item in self.findings)
        return {
            "ok": self.ok,
            "has_uncheckable": self.has_uncheckable,
            "counts": {
                "passed": counts["passed"],
                "failed": counts["failed"],
                "uncheckable": counts["uncheckable"],
            },
            "findings": [item.to_dict() for item in self.findings],
        }


def normalize_prose_for_matching(value: str) -> str:
    """Conservatively normalize layout artifacts without paraphrasing text."""

    normalized = unicodedata.normalize("NFKC", value).replace("\u00ad", "")
    for source, replacement in (
        ("\u2018", "'"),
        ("\u2019", "'"),
        ("\u201c", '"'),
        ("\u201d", '"'),
        ("\u2013", "-"),
        ("\u2014", "-"),
    ):
        normalized = normalized.replace(source, replacement)
    normalized = re.sub(r"(?<=\w)-[ \t]*\r?\n[ \t]*(?=\w)", "", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def administrative_type_gap_annotations(
    payload: Mapping[str, Any],
) -> tuple[dict[str, str], ...]:
    """Describe missing vocabulary metadata that calibration may add mechanically."""

    annotations: list[dict[str, str]] = []
    contracts = (
        ("atom_records", "kind", RECOMMENDED_ATOM_KINDS),
        ("move_records", "role", RECOMMENDED_MOVE_ROLES),
        ("thread_records", "kind", RECOMMENDED_THREAD_KINDS),
    )
    for collection, field, recommended in contracts:
        for raw in payload.get(collection, []):
            if not isinstance(raw, Mapping):
                continue
            slug = raw.get(field)
            gap = raw.get("type_gap")
            if (
                isinstance(slug, str)
                and slug not in recommended
                and not (
                    isinstance(gap, Mapping) and gap.get("slug") == slug and gap.get("rationale")
                )
            ):
                annotations.append(
                    {
                        "record_id": str(raw.get("id", "unknown-record")),
                        "field": field,
                        "slug": slug,
                        "rationale": (
                            "Calibration coordinator recorded source-native model vocabulary "
                            "that omitted its required administrative type-gap declaration."
                        ),
                    }
                )
    return tuple(annotations)


def adapted_hierarchical_records(
    payload: Mapping[str, Any], *, annotate_missing_type_gaps: bool = False
) -> tuple[Record, ...]:
    """Adapt the segment-rich whole-paper proposal to the accepted v1 dossier."""

    records: list[Record] = []
    for raw in payload.get("evidence_records", []):
        if not isinstance(raw, dict):
            raise RecordError("evidence record must be an object")
        evidence = deepcopy(raw)
        segments = evidence.get("segments", [])
        evidence["exact_span"] = "\n\n".join(
            str(segment["transcription"])
            for segment in segments
            if isinstance(segment, dict) and "transcription" in segment
        )
        records.append(Record.from_mapping(evidence))
    annotations = {
        item["record_id"]: item
        for item in administrative_type_gap_annotations(payload)
        if annotate_missing_type_gaps
    }
    for collection in ("atom_records", "move_records", "thread_records"):
        for raw in payload.get(collection, []):
            if not isinstance(raw, dict):
                raise RecordError(f"{collection} item must be an object")
            normalized = deepcopy(raw)
            annotation = annotations.get(str(normalized.get("id")))
            if annotation is not None:
                normalized["type_gap"] = {
                    "slug": annotation["slug"],
                    "rationale": annotation["rationale"],
                }
            records.append(Record.from_mapping(normalized))
    return tuple(records)


def validate_hierarchical_proposal_file(
    proposal_path: Path,
    *,
    schema_path: Path,
    expected_source_id: str,
    expected_asset_sha256: str,
    page_count: int,
    page_text_by_page: Mapping[int, str | None],
) -> HierarchicalValidationReport:
    payload = json.loads(proposal_path.read_text(encoding="utf-8"))
    return validate_hierarchical_proposal(
        payload,
        schema_path=schema_path,
        expected_source_id=expected_source_id,
        expected_asset_sha256=expected_asset_sha256,
        page_count=page_count,
        page_text_by_page=page_text_by_page,
    )


def validate_hierarchical_proposal(
    payload: Mapping[str, Any],
    *,
    schema_path: Path,
    expected_source_id: str,
    expected_asset_sha256: str,
    page_count: int,
    page_text_by_page: Mapping[int, str | None],
) -> HierarchicalValidationReport:
    """Validate structure and externally checkable source provenance."""

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema_errors = sorted(
        Draft202012Validator(schema).iter_errors(dict(payload)),
        key=lambda item: [str(part) for part in item.absolute_path],
    )
    if schema_errors:
        return HierarchicalValidationReport(
            tuple(
                HierarchicalFinding(
                    "schema_error",
                    "failed",
                    ".".join(str(part) for part in error.absolute_path) or "proposal",
                    error.message,
                )
                for error in schema_errors
            )
        )

    findings: list[HierarchicalFinding] = []
    source_id = str(payload["source_id"])
    if source_id != expected_source_id:
        _fail(
            findings,
            "source_identity_mismatch",
            source_id,
            f"expected {expected_source_id}",
        )

    blocks = list(payload["argument_blocks"])
    evidence = list(payload["evidence_records"])
    atoms = list(payload["atom_records"])
    moves = list(payload["move_records"])
    threads = list(payload["thread_records"])
    source_objects = list(payload["source_objects"])

    collections = (blocks, evidence, atoms, moves, threads, source_objects)
    all_items = [item for collection in collections for item in collection]
    all_ids = [str(item["id"]) for item in all_items]
    duplicate_ids = sorted(item_id for item_id, count in Counter(all_ids).items() if count > 1)
    for item_id in duplicate_ids:
        _fail(findings, "duplicate_id", item_id, "identifier appears more than once")

    for item in all_items:
        item_id = str(item["id"])
        if str(item["source_id"]) != expected_source_id:
            _fail(
                findings,
                "record_source_mismatch",
                item_id,
                f"record source_id is not {expected_source_id}",
            )

    block_by_id = _index(blocks)
    evidence_by_id = _index(evidence)
    atom_by_id = _index(atoms)
    move_by_id = _index(moves)
    thread_by_id = _index(threads)

    _validate_blocks(
        findings,
        blocks,
        atom_ids=set(atom_by_id),
        move_ids=set(move_by_id),
        page_count=page_count,
    )
    _validate_evidence(
        findings,
        evidence,
        expected_asset_sha256=expected_asset_sha256,
        page_count=page_count,
        page_text_by_page=page_text_by_page,
    )
    _validate_semantic_references(
        findings,
        atoms=atoms,
        moves=moves,
        threads=threads,
        evidence_ids=set(evidence_by_id),
        atom_ids=set(atom_by_id),
        move_ids=set(move_by_id),
        thread_ids=set(thread_by_id),
    )
    _validate_source_objects(
        findings,
        source_objects=source_objects,
        evidence_by_id=evidence_by_id,
        atom_by_id=atom_by_id,
        move_ids=set(move_by_id),
        page_count=page_count,
    )

    if not any(item.disposition == "failed" for item in findings):
        findings.append(
            HierarchicalFinding(
                "structure_valid",
                "passed",
                source_id,
                (
                    f"validated {len(block_by_id)} blocks, {len(evidence_by_id)} evidence "
                    f"records, {len(atom_by_id)} atoms, {len(move_by_id)} moves, "
                    f"{len(thread_by_id)} threads, and {len(source_objects)} source objects"
                ),
            )
        )
    return HierarchicalValidationReport(tuple(findings))


def _validate_blocks(
    findings: list[HierarchicalFinding],
    blocks: list[Mapping[str, Any]],
    *,
    atom_ids: set[str],
    move_ids: set[str],
    page_count: int,
) -> None:
    sequences = [int(block["sequence"]) for block in blocks]
    if sequences != list(range(1, len(blocks) + 1)):
        _fail(
            findings,
            "block_sequence_invalid",
            "argument_blocks",
            "block array must use consecutive source-order sequences starting at one",
        )

    atom_membership: Counter[str] = Counter()
    move_membership: Counter[str] = Counter()
    previous_start = 0
    for block in blocks:
        block_id = str(block["id"])
        page_start = int(block["page_start"])
        page_end = int(block["page_end"])
        if not 1 <= page_start <= page_end <= page_count:
            _fail(findings, "block_page_route_invalid", block_id, "page range is invalid")
        if page_start < previous_start:
            _fail(
                findings,
                "block_source_order_invalid",
                block_id,
                "block page start moves backwards in source order",
            )
        previous_start = page_start
        for atom_id in block["atom_ids"]:
            atom_membership[str(atom_id)] += 1
            if str(atom_id) not in atom_ids:
                _fail(findings, "block_atom_reference_unknown", block_id, str(atom_id))
        for move_id in block["move_ids"]:
            move_membership[str(move_id)] += 1
            if str(move_id) not in move_ids:
                _fail(findings, "block_move_reference_unknown", block_id, str(move_id))

    for atom_id in sorted(atom_ids):
        if atom_membership[atom_id] != 1:
            _fail(
                findings,
                "atom_block_membership_invalid",
                atom_id,
                f"expected exactly one block, found {atom_membership[atom_id]}",
            )
    for move_id in sorted(move_ids):
        if move_membership[move_id] != 1:
            _fail(
                findings,
                "move_block_membership_invalid",
                move_id,
                f"expected exactly one block, found {move_membership[move_id]}",
            )


def _validate_evidence(
    findings: list[HierarchicalFinding],
    evidence: list[Mapping[str, Any]],
    *,
    expected_asset_sha256: str,
    page_count: int,
    page_text_by_page: Mapping[int, str | None],
) -> None:
    for record in evidence:
        record_id = str(record["id"])
        if str(record["asset_sha256"]) != expected_asset_sha256:
            _fail(findings, "evidence_asset_mismatch", record_id, "asset hash is incorrect")
        page_start = int(record["page_start"])
        page_end = int(record["page_end"])
        if not 1 <= page_start <= page_end <= page_count:
            _fail(findings, "evidence_page_route_invalid", record_id, "page range is invalid")
        for index, segment in enumerate(record["segments"], start=1):
            subject_id = f"{record_id}#segment-{index}"
            segment_start = int(segment["page_start"])
            segment_end = int(segment["page_end"])
            if segment_start != segment_end:
                _fail(
                    findings,
                    "evidence_segment_crosses_pages",
                    subject_id,
                    "a contiguous segment must remain on one page",
                )
                continue
            if not page_start <= segment_start <= page_end or not 1 <= segment_start <= page_count:
                _fail(
                    findings,
                    "evidence_segment_route_invalid",
                    subject_id,
                    "segment page is outside the evidence record route",
                )
                continue
            mode = str(segment["transcription_mode"])
            if mode != "verbatim_text":
                findings.append(
                    HierarchicalFinding(
                        "visual_review_required",
                        "uncheckable",
                        subject_id,
                        f"{mode} requires direct PDF/page-image review",
                    )
                )
                continue
            page_text = page_text_by_page.get(segment_start)
            if page_text is None or not normalize_prose_for_matching(page_text):
                findings.append(
                    HierarchicalFinding(
                        "page_text_unavailable",
                        "uncheckable",
                        subject_id,
                        f"no usable extracted text for PDF page {segment_start}",
                    )
                )
                continue
            transcription = normalize_prose_for_matching(str(segment["transcription"]))
            normalized_page = normalize_prose_for_matching(page_text)
            if transcription and transcription in normalized_page:
                findings.append(
                    HierarchicalFinding(
                        "verbatim_segment_matched",
                        "passed",
                        subject_id,
                        f"verbatim prose occurs on claimed PDF page {segment_start}",
                    )
                )
            else:
                _fail(
                    findings,
                    "verbatim_segment_not_found",
                    subject_id,
                    f"prose does not occur on claimed PDF page {segment_start}",
                )


def _validate_semantic_references(
    findings: list[HierarchicalFinding],
    *,
    atoms: list[Mapping[str, Any]],
    moves: list[Mapping[str, Any]],
    threads: list[Mapping[str, Any]],
    evidence_ids: set[str],
    atom_ids: set[str],
    move_ids: set[str],
    thread_ids: set[str],
) -> None:
    connected_atom_ids: set[str] = set()
    for atom in atoms:
        atom_id = str(atom["id"])
        _check_references(
            findings,
            subject_id=atom_id,
            code="atom_evidence_reference_unknown",
            references=atom["evidence_ids"],
            allowed=evidence_ids,
        )

    moves_by_thread: defaultdict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for move in moves:
        move_id = str(move["id"])
        inputs = [str(value) for value in move["inputs"]]
        outputs = [str(value) for value in move["outputs"]]
        connected_atom_ids.update(inputs)
        connected_atom_ids.update(outputs)
        _check_references(
            findings,
            subject_id=move_id,
            code="move_atom_reference_unknown",
            references=inputs,
            allowed=atom_ids,
        )
        _check_references(
            findings,
            subject_id=move_id,
            code="move_atom_reference_unknown",
            references=outputs,
            allowed=atom_ids,
        )
        _check_references(
            findings,
            subject_id=move_id,
            code="move_evidence_reference_unknown",
            references=move["evidence_ids"],
            allowed=evidence_ids,
        )
        thread_id = str(move["thread_id"])
        if thread_id not in thread_ids:
            _fail(findings, "move_thread_reference_unknown", move_id, thread_id)
        moves_by_thread[thread_id].append(move)

    for atom in atoms:
        atom_id = str(atom["id"])
        standalone = atom["standalone_context"]
        if atom_id not in connected_atom_ids and not (
            isinstance(standalone, str) and standalone.strip()
        ):
            _fail(
                findings,
                "atom_has_no_reasoning_use",
                atom_id,
                "atom is absent from all moves and has no standalone context",
            )

    thread_move_membership: Counter[str] = Counter()
    for thread in threads:
        thread_id = str(thread["id"])
        listed_move_ids = [str(value) for value in thread["move_ids"]]
        _check_references(
            findings,
            subject_id=thread_id,
            code="thread_move_reference_unknown",
            references=listed_move_ids,
            allowed=move_ids,
        )
        _check_references(
            findings,
            subject_id=thread_id,
            code="thread_entry_reference_unknown",
            references=thread["entry_points"],
            allowed=atom_ids | set(listed_move_ids),
        )
        _check_references(
            findings,
            subject_id=thread_id,
            code="thread_evidence_reference_unknown",
            references=thread["evidence_ids"],
            allowed=evidence_ids,
        )
        for branch in thread["branches"]:
            _check_references(
                findings,
                subject_id=thread_id,
                code="thread_branch_reference_unknown",
                references=(branch["from_move"], branch["to_move"]),
                allowed=set(listed_move_ids),
            )
        for move_id in listed_move_ids:
            thread_move_membership[move_id] += 1
        actual_moves = moves_by_thread.get(thread_id, [])
        actual_ids = [str(move["id"]) for move in actual_moves]
        if set(actual_ids) != set(listed_move_ids):
            _fail(
                findings,
                "thread_move_membership_mismatch",
                thread_id,
                "thread list does not equal moves that name this thread",
            )
        indexes = [int(move["presentation_index"]) for move in actual_moves]
        expected_ids = [
            str(move["id"])
            for move in sorted(actual_moves, key=lambda item: int(item["presentation_index"]))
        ]
        if len(set(indexes)) != len(indexes) or expected_ids != listed_move_ids:
            _fail(
                findings,
                "thread_presentation_order_invalid",
                thread_id,
                "move_ids must follow unique increasing presentation indexes",
            )

    for move_id in sorted(move_ids):
        if thread_move_membership[move_id] != 1:
            _fail(
                findings,
                "move_thread_membership_invalid",
                move_id,
                f"expected exactly one thread, found {thread_move_membership[move_id]}",
            )


def _validate_source_objects(
    findings: list[HierarchicalFinding],
    *,
    source_objects: list[Mapping[str, Any]],
    evidence_by_id: Mapping[str, Mapping[str, Any]],
    atom_by_id: Mapping[str, Mapping[str, Any]],
    move_ids: set[str],
    page_count: int,
) -> None:
    labels: Counter[tuple[str, str]] = Counter(
        (str(item["kind"]), str(item["source_label"])) for item in source_objects
    )
    for (kind, label), count in labels.items():
        if count > 1:
            _fail(
                findings,
                "source_object_label_duplicate",
                f"{kind}:{label}",
                "source-native object label appears more than once",
            )

    objects_by_atom: defaultdict[str, list[Mapping[str, Any]]] = defaultdict(list)
    atoms_with_object: set[str] = set()
    for item in source_objects:
        object_id = str(item["id"])
        page = int(item["page"])
        if not 1 <= page <= page_count:
            _fail(findings, "source_object_page_invalid", object_id, "page is invalid")
        _check_references(
            findings,
            subject_id=object_id,
            code="source_object_evidence_reference_unknown",
            references=item["evidence_ids"],
            allowed=set(evidence_by_id),
        )
        _check_references(
            findings,
            subject_id=object_id,
            code="source_object_atom_reference_unknown",
            references=item["atom_ids"],
            allowed=set(atom_by_id),
        )
        _check_references(
            findings,
            subject_id=object_id,
            code="source_object_move_reference_unknown",
            references=item["move_ids"],
            allowed=move_ids,
        )
        required_mode = "displayed_math" if item["kind"] == "equation" else "figure_region"
        matching_segment = False
        for evidence_id in item["evidence_ids"]:
            record = evidence_by_id.get(str(evidence_id))
            if record is None:
                continue
            matching_segment = matching_segment or any(
                segment["transcription_mode"] == required_mode
                and int(segment["page_start"]) == page
                and int(segment["page_end"]) == page
                for segment in record["segments"]
            )
        if not matching_segment:
            _fail(
                findings,
                "source_object_fidelity_evidence_missing",
                object_id,
                f"requires {required_mode} evidence on PDF page {page}",
            )
        for atom_id_value in item["atom_ids"]:
            atom_id = str(atom_id_value)
            atoms_with_object.add(atom_id)
            objects_by_atom[atom_id].append(item)
            atom = atom_by_id.get(atom_id)
            if (
                atom is not None
                and item["kind"] in {"equation", "figure"}
                and str(atom["kind"]) != str(item["kind"])
            ):
                _fail(
                    findings,
                    "source_object_atom_kind_mismatch",
                    object_id,
                    f"{item['kind']} object references {atom['kind']} atom {atom_id}",
                )

    for atom_id, atom in atom_by_id.items():
        if atom["kind"] in {"equation", "figure"} and atom_id not in atoms_with_object:
            _fail(
                findings,
                "atom_source_object_missing",
                atom_id,
                f"{atom['kind']} atom has no source-object route",
            )

    for atom_id, objects in objects_by_atom.items():
        if len(objects) < 2:
            continue
        roles = {str(item["role"]) for item in objects}
        if len(roles) > 1:
            _fail(
                findings,
                "mixed_source_roles_collapsed",
                atom_id,
                f"one atom represents distinct source roles: {sorted(roles)}",
            )
        if any(
            not isinstance(item["grouping_rationale"], str)
            or not str(item["grouping_rationale"]).strip()
            for item in objects
        ):
            _fail(
                findings,
                "source_object_grouping_unjustified",
                atom_id,
                "multiple source objects share one atom without a grouping rationale",
            )


def _index(items: list[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    return {str(item["id"]): item for item in items}


def _check_references(
    findings: list[HierarchicalFinding],
    *,
    subject_id: str,
    code: str,
    references: Any,
    allowed: set[str],
) -> None:
    values = [str(value) for value in references]
    duplicates = sorted(value for value, count in Counter(values).items() if count > 1)
    if duplicates:
        _fail(
            findings,
            "duplicate_reference",
            subject_id,
            f"duplicate references: {duplicates}",
        )
    unknown = sorted(set(values) - allowed)
    if unknown:
        _fail(findings, code, subject_id, f"unknown references: {unknown}")


def _fail(
    findings: list[HierarchicalFinding],
    code: str,
    subject_id: str,
    message: str,
) -> None:
    findings.append(HierarchicalFinding(code, "failed", subject_id, message))
