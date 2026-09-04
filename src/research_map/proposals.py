"""Schema and provenance validation for Codex orientation/reading proposals."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from research_map.coverage import BatchPlan, LandmarkPlan, ScopePlan, initial_coverage
from research_map.records import Dossier, Record, RecordError
from research_map.validation import validate_record_vocabulary


class ProposalValidationError(ValueError):
    """Raised when worker output cannot enter deterministic WIP."""


class ManualReviewRequired(ProposalValidationError):
    """Raised when a worker explicitly requires human source review."""


@dataclass(frozen=True, slots=True)
class OrientationProposal:
    source_id: str
    source_summary: str
    scopes: tuple[ScopePlan, ...]
    landmarks: tuple[LandmarkPlan, ...]
    batches: tuple[BatchPlan, ...]
    thread_plan: tuple[dict[str, Any], ...]
    type_gaps: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...]
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ReadingProposal:
    source_id: str
    scope_id: str
    disposition: str
    scope_summary: str
    landmark_dispositions: tuple[dict[str, Any], ...]
    records: tuple[Record, ...]
    reopen_scope_ids: tuple[str, ...]
    manual_review_reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    payload: dict[str, Any]


def load_orientation_proposal(
    path: Path,
    *,
    schema_directory: Path,
    expected_source_id: str,
    page_count: int,
) -> OrientationProposal:
    payload = _load_and_validate(path, schema_directory / "orientation-proposal.schema.json")
    if payload["source_id"] != expected_source_id:
        raise ProposalValidationError("orientation proposal source identity does not match")
    if payload["manual_review"]:
        raise ManualReviewRequired("orientation requested manual review")
    scopes = tuple(
        ScopePlan.from_mapping(value, page_count=page_count) for value in payload["scopes"]
    )
    scope_ids = {scope.scope_id for scope in scopes}
    if len(scope_ids) != len(scopes):
        raise ProposalValidationError("orientation repeats a context scope ID")
    landmarks = tuple(
        LandmarkPlan.from_mapping(value, page_count=page_count) for value in payload["landmarks"]
    )
    landmark_by_id = {landmark.landmark_id: landmark for landmark in landmarks}
    if len(landmark_by_id) != len(landmarks):
        raise ProposalValidationError("orientation repeats a landmark ID")
    scope_by_id = {scope.scope_id: scope for scope in scopes}
    batches = tuple(
        BatchPlan.from_mapping(
            value,
            scopes=scope_by_id,
            landmarks=landmark_by_id,
        )
        for value in payload["batches"]
    )
    initial_coverage(page_count, scopes, landmarks, batches)
    for thread in payload["thread_plan"]:
        unknown = set(thread["scope_ids"]) - scope_ids
        if unknown:
            raise ProposalValidationError(
                f"orientation thread references unknown scopes: {sorted(unknown)}"
            )
        unknown_landmarks = set(thread["landmark_ids"]) - set(landmark_by_id)
        if unknown_landmarks:
            raise ProposalValidationError(
                f"orientation thread references unknown landmarks: {sorted(unknown_landmarks)}"
            )
    return OrientationProposal(
        source_id=expected_source_id,
        source_summary=str(payload["source_summary"]),
        scopes=scopes,
        landmarks=landmarks,
        batches=batches,
        thread_plan=tuple(payload["thread_plan"]),
        type_gaps=tuple(payload["type_gaps"]),
        warnings=tuple(payload["warnings"]),
        payload=payload,
    )


def load_reading_proposal(
    path: Path,
    *,
    schema_directory: Path,
    expected_source_id: str,
    expected_scope_id: str,
    expected_landmark_ids: tuple[str, ...] | None = None,
    expected_asset_sha256: str,
    page_count: int,
) -> ReadingProposal:
    payload = _load_and_validate(path, schema_directory / "reading-proposal.schema.json")
    if payload["source_id"] != expected_source_id:
        raise ProposalValidationError("reading proposal source identity does not match")
    if payload["scope_id"] != expected_scope_id:
        raise ProposalValidationError("reading proposal scope identity does not match")
    if payload["disposition"] == "manual_review" or payload["manual_review_reasons"]:
        reasons = "; ".join(payload["manual_review_reasons"]) or "worker requested review"
        raise ManualReviewRequired(reasons)
    landmark_dispositions = tuple(dict(item) for item in payload["landmark_dispositions"])
    disposition_ids = [str(item["landmark_id"]) for item in landmark_dispositions]
    if len(set(disposition_ids)) != len(disposition_ids):
        raise ProposalValidationError("reading proposal repeats a landmark disposition")
    if expected_landmark_ids is not None and set(disposition_ids) != set(expected_landmark_ids):
        missing = sorted(set(expected_landmark_ids) - set(disposition_ids))
        unexpected = sorted(set(disposition_ids) - set(expected_landmark_ids))
        raise ProposalValidationError(
            "reading proposal landmark dispositions do not match the assigned batch; "
            f"missing={missing}; unexpected={unexpected}"
        )
    for disposition in landmark_dispositions:
        record_ids = disposition["record_ids"]
        if disposition["disposition"] == "represented" and not record_ids:
            raise ProposalValidationError(
                f"represented landmark has no record IDs: {disposition['landmark_id']}"
            )
        if disposition["disposition"] == "not_substantive" and record_ids:
            raise ProposalValidationError(
                f"not-substantive landmark cites records: {disposition['landmark_id']}"
            )
        if disposition["disposition"] == "manual_review":
            raise ManualReviewRequired(
                f"landmark requires manual review: {disposition['landmark_id']}"
            )
    raw_records = tuple(
        record
        for field in ("evidence_records", "atom_records", "move_records", "thread_records")
        for record in payload[field]
    )
    try:
        records = tuple(Record.from_mapping(record) for record in raw_records)
    except RecordError as error:
        raise ProposalValidationError(str(error)) from error
    identifiers = [record.id for record in records]
    if len(set(identifiers)) != len(identifiers):
        raise ProposalValidationError("reading proposal repeats a record ID")
    for record in records:
        if record.source_id != expected_source_id:
            raise ProposalValidationError(f"cross-source record rejected: {record.id}")
        _validate_record_schema(record, schema_directory)
        if record.record_type == "evidence":
            if record.payload["asset_sha256"] != expected_asset_sha256:
                raise ProposalValidationError(f"evidence asset hash does not match: {record.id}")
            start = int(record.payload["page_start"])
            end = int(record.payload["page_end"])
            if start > end or end > page_count:
                raise ProposalValidationError(f"evidence page route is invalid: {record.id}")
            _validate_evidence_segments(record, page_count=page_count)
    vocabulary = validate_record_vocabulary(records)
    if not vocabulary.ok:
        raise ProposalValidationError("; ".join(vocabulary.errors))
    for record in records:
        if (
            record.record_type == "move"
            and record.payload.get("role") in {"derives", "concludes"}
            and not record.payload.get("inputs")
        ):
            raise ProposalValidationError(
                f"derivation/conclusion move has no declared inputs: {record.id}"
            )
    return ReadingProposal(
        source_id=expected_source_id,
        scope_id=expected_scope_id,
        disposition=str(payload["disposition"]),
        scope_summary=str(payload["scope_summary"]),
        landmark_dispositions=landmark_dispositions,
        records=records,
        reopen_scope_ids=tuple(payload["reopen_scope_ids"]),
        manual_review_reasons=tuple(payload["manual_review_reasons"]),
        warnings=tuple(payload["warnings"]),
        payload=payload,
    )


def dossier_from_wip(
    *, source_id: str, title: str, source_summary: str, records: tuple[dict[str, Any], ...]
) -> Dossier:
    try:
        return Dossier.create(
            source_id=source_id,
            title=title,
            summary=source_summary,
            records=records,
        )
    except RecordError as error:
        raise ProposalValidationError(str(error)) from error


def _load_and_validate(path: Path, schema_path: Path) -> dict[str, Any]:
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
        schema: Any = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProposalValidationError(f"proposal is unreadable: {error}") from error
    errors = sorted(
        Draft202012Validator(schema).iter_errors(payload), key=lambda item: list(item.path)
    )
    if errors:
        location = ".".join(str(item) for item in errors[0].path) or "proposal"
        raise ProposalValidationError(f"{location}: {errors[0].message}")
    if not isinstance(payload, dict):
        raise ProposalValidationError("proposal is not an object")
    return payload


def _validate_record_schema(record: Record, schema_directory: Path) -> None:
    schema = json.loads(
        (schema_directory / f"{record.record_type}.schema.json").read_text(encoding="utf-8")
    )
    errors = sorted(
        Draft202012Validator(schema).iter_errors(record.payload), key=lambda item: list(item.path)
    )
    if errors:
        location = ".".join(str(item) for item in errors[0].path) or "record"
        raise ProposalValidationError(f"{record.id}: {location}: {errors[0].message}")


def _validate_evidence_segments(record: Record, *, page_count: int) -> None:
    segments = record.payload.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ProposalValidationError(f"evidence has no explicit segments: {record.id}")
    routes: list[tuple[int, int, str, str]] = []
    for segment in segments:
        segment_start = int(segment["page_start"])
        segment_end = int(segment["page_end"])
        if segment_start > segment_end or segment_end > page_count:
            raise ProposalValidationError(f"evidence segment route is invalid: {record.id}")
        routes.append(
            (
                segment_start,
                segment_end,
                str(segment["locator"]),
                str(segment["transcription"]),
            )
        )
    if len(set(routes)) != len(routes):
        raise ProposalValidationError(f"evidence repeats an exact segment: {record.id}")
    if min(route[0] for route in routes) != int(record.payload["page_start"]) or max(
        route[1] for route in routes
    ) != int(record.payload["page_end"]):
        raise ProposalValidationError(
            f"evidence aggregate page route does not match its segments: {record.id}"
        )
