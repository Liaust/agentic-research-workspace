"""Deterministic page, batch, and semantic-landmark coverage accounting."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

COVERAGE_SCHEMA_VERSION = "1.1"
COVERAGE_DISPOSITIONS = frozenset(
    {"pending", "covered", "reopened", "not_substantive", "manual_review"}
)
TERMINAL_DISPOSITIONS = frozenset({"covered", "not_substantive"})


class CoverageError(ValueError):
    """Raised when coverage cannot prove complete source-local reading."""


@dataclass(frozen=True, slots=True)
class ScopePlan:
    scope_id: str
    label: str
    page_start: int
    page_end: int
    rationale: str
    thread_hints: tuple[str, ...]

    @classmethod
    def from_mapping(cls, value: dict[str, Any], *, page_count: int) -> ScopePlan:
        scope_id = value.get("scope_id")
        label = value.get("label")
        page_start = value.get("page_start")
        page_end = value.get("page_end")
        rationale = value.get("rationale")
        hints = value.get("thread_hints")
        _require_identity(scope_id, label, rationale, kind="scope")
        _require_page_route(page_start, page_end, page_count=page_count, identity=scope_id)
        if not isinstance(hints, list) or not all(isinstance(item, str) for item in hints):
            raise CoverageError(f"orientation scope thread hints are invalid: {scope_id}")
        assert isinstance(scope_id, str)
        assert isinstance(label, str)
        assert isinstance(page_start, int)
        assert isinstance(page_end, int)
        assert isinstance(rationale, str)
        return cls(scope_id, label, page_start, page_end, rationale, tuple(hints))

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_id": self.scope_id,
            "label": self.label,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "rationale": self.rationale,
            "thread_hints": list(self.thread_hints),
        }


@dataclass(frozen=True, slots=True)
class LandmarkPlan:
    landmark_id: str
    label: str
    kind: str
    page_start: int
    page_end: int
    rationale: str
    thread_hints: tuple[str, ...]

    @classmethod
    def from_mapping(cls, value: dict[str, Any], *, page_count: int) -> LandmarkPlan:
        landmark_id = value.get("landmark_id")
        label = value.get("label")
        kind = value.get("kind")
        page_start = value.get("page_start")
        page_end = value.get("page_end")
        rationale = value.get("rationale")
        hints = value.get("thread_hints")
        _require_identity(landmark_id, label, rationale, kind="landmark")
        if not isinstance(kind, str) or not kind.strip():
            raise CoverageError(f"orientation landmark kind is invalid: {landmark_id}")
        _require_page_route(page_start, page_end, page_count=page_count, identity=landmark_id)
        if not isinstance(hints, list) or not all(isinstance(item, str) for item in hints):
            raise CoverageError(f"orientation landmark thread hints are invalid: {landmark_id}")
        assert isinstance(landmark_id, str)
        assert isinstance(label, str)
        assert isinstance(page_start, int)
        assert isinstance(page_end, int)
        assert isinstance(rationale, str)
        return cls(
            landmark_id,
            label,
            kind,
            page_start,
            page_end,
            rationale,
            tuple(hints),
        )

    @property
    def coverage_id(self) -> str:
        return f"landmark:{self.landmark_id}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "landmark_id": self.landmark_id,
            "label": self.label,
            "kind": self.kind,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "rationale": self.rationale,
            "thread_hints": list(self.thread_hints),
        }


@dataclass(frozen=True, slots=True)
class BatchPlan:
    batch_id: str
    label: str
    scope_ids: tuple[str, ...]
    landmark_ids: tuple[str, ...]
    rationale: str
    page_start: int
    page_end: int

    @classmethod
    def from_mapping(
        cls,
        value: dict[str, Any],
        *,
        scopes: dict[str, ScopePlan],
        landmarks: dict[str, LandmarkPlan],
    ) -> BatchPlan:
        batch_id = value.get("batch_id")
        label = value.get("label")
        rationale = value.get("rationale")
        raw_scope_ids = value.get("scope_ids")
        raw_landmark_ids = value.get("landmark_ids")
        _require_identity(batch_id, label, rationale, kind="batch")
        if not isinstance(raw_scope_ids, list) or not raw_scope_ids:
            raise CoverageError(f"orientation batch has no context scopes: {batch_id}")
        if not isinstance(raw_landmark_ids, list) or not raw_landmark_ids:
            raise CoverageError(f"orientation batch has no landmarks: {batch_id}")
        scope_ids = tuple(str(item) for item in raw_scope_ids)
        landmark_ids = tuple(str(item) for item in raw_landmark_ids)
        if len(set(scope_ids)) != len(scope_ids) or len(set(landmark_ids)) != len(landmark_ids):
            raise CoverageError(f"orientation batch repeats an assignment: {batch_id}")
        if unknown := set(scope_ids) - set(scopes):
            raise CoverageError(f"orientation batch references unknown scopes: {sorted(unknown)}")
        if unknown := set(landmark_ids) - set(landmarks):
            raise CoverageError(
                f"orientation batch references unknown landmarks: {sorted(unknown)}"
            )
        assigned = [landmarks[landmark_id] for landmark_id in landmark_ids]
        assert isinstance(batch_id, str)
        assert isinstance(label, str)
        assert isinstance(rationale, str)
        return cls(
            batch_id,
            label,
            scope_ids,
            landmark_ids,
            rationale,
            min(item.page_start for item in assigned),
            max(item.page_end for item in assigned),
        )

    @property
    def scope_id(self) -> str:
        """Compatibility alias for the persisted reading-unit identity."""

        return self.batch_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "label": self.label,
            "scope_ids": list(self.scope_ids),
            "landmark_ids": list(self.landmark_ids),
            "rationale": self.rationale,
            "page_start": self.page_start,
            "page_end": self.page_end,
        }


@dataclass(frozen=True, slots=True)
class CoverageEntry:
    scope_id: str
    scope_kind: str
    disposition: str
    details: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CoverageReport:
    entries: tuple[CoverageEntry, ...]
    errors: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": COVERAGE_SCHEMA_VERSION,
            "complete": self.complete,
            "entries": [
                {
                    "scope_id": entry.scope_id,
                    "scope_kind": entry.scope_kind,
                    "disposition": entry.disposition,
                    "details": entry.details,
                }
                for entry in self.entries
            ],
            "errors": list(self.errors),
        }


def initial_coverage(
    page_count: int,
    scopes: tuple[ScopePlan, ...],
    landmarks: tuple[LandmarkPlan, ...],
    batches: tuple[BatchPlan, ...],
) -> tuple[CoverageEntry, ...]:
    if page_count < 1:
        raise CoverageError("source page count must be positive")
    if not scopes:
        raise CoverageError("orientation must declare at least one context scope")
    if not landmarks:
        raise CoverageError("orientation must declare at least one semantic landmark")
    if not batches:
        raise CoverageError("orientation must declare at least one reading batch")
    _require_unique((scope.scope_id for scope in scopes), kind="scope")
    _require_unique((landmark.landmark_id for landmark in landmarks), kind="landmark")
    _require_unique((batch.batch_id for batch in batches), kind="batch")
    known_scope_ids = {scope.scope_id for scope in scopes}
    known_landmark_ids = {landmark.landmark_id for landmark in landmarks}
    if unknown := {scope_id for batch in batches for scope_id in batch.scope_ids} - known_scope_ids:
        raise CoverageError(f"orientation batches reference unknown scopes: {sorted(unknown)}")
    if unassigned_scopes := known_scope_ids - {
        scope_id for batch in batches for scope_id in batch.scope_ids
    }:
        raise CoverageError(
            f"orientation context scopes have no reading batch: {sorted(unassigned_scopes)}"
        )
    if (
        unknown := {landmark_id for batch in batches for landmark_id in batch.landmark_ids}
        - known_landmark_ids
    ):
        raise CoverageError(f"orientation batches reference unknown landmarks: {sorted(unknown)}")
    covered_pages = {
        page for scope in scopes for page in range(scope.page_start, scope.page_end + 1)
    }
    missing = sorted(set(range(1, page_count + 1)) - covered_pages)
    if missing:
        raise CoverageError(f"orientation scopes leave pages uncovered: {missing}")
    assignment_counts = Counter(
        landmark_id for batch in batches for landmark_id in batch.landmark_ids
    )
    missing_landmarks = sorted(
        landmark_id for landmark_id in known_landmark_ids if assignment_counts[landmark_id] == 0
    )
    repeated_landmarks = sorted(
        landmark_id for landmark_id, count in assignment_counts.items() if count > 1
    )
    if missing_landmarks or repeated_landmarks:
        raise CoverageError(
            "orientation landmark assignments are not exactly once; "
            f"missing={missing_landmarks}; repeated={repeated_landmarks}"
        )
    page_entries = tuple(
        CoverageEntry(
            scope_id=f"page:{page:04d}",
            scope_kind="page",
            disposition="pending",
            details={
                "page": page,
                "context_scope_ids": [
                    scope.scope_id for scope in scopes if scope.page_start <= page <= scope.page_end
                ],
                "landmark_ids": [
                    landmark.landmark_id
                    for landmark in landmarks
                    if landmark.page_start <= page <= landmark.page_end
                ],
            },
        )
        for page in range(1, page_count + 1)
    )
    context_entries = tuple(
        CoverageEntry(
            scope_id=f"context:{scope.scope_id}",
            scope_kind="context_scope",
            disposition="pending",
            details=scope.to_dict(),
        )
        for scope in scopes
    )
    batch_entries = tuple(
        CoverageEntry(
            scope_id=batch.batch_id,
            scope_kind="reading_scope",
            disposition="pending",
            details=batch.to_dict(),
        )
        for batch in batches
    )
    batch_by_landmark = {
        landmark_id: batch.batch_id for batch in batches for landmark_id in batch.landmark_ids
    }
    landmark_entries = tuple(
        CoverageEntry(
            scope_id=landmark.coverage_id,
            scope_kind="landmark",
            disposition="pending",
            details={**landmark.to_dict(), "batch_id": batch_by_landmark[landmark.landmark_id]},
        )
        for landmark in landmarks
    )
    return page_entries + context_entries + batch_entries + landmark_entries


def validate_coverage(entries: tuple[CoverageEntry, ...], *, page_count: int) -> CoverageReport:
    errors: list[str] = []
    pages: dict[int, CoverageEntry] = {}
    by_kind: dict[str, list[CoverageEntry]] = {}
    for entry in entries:
        by_kind.setdefault(entry.scope_kind, []).append(entry)
        if entry.disposition not in COVERAGE_DISPOSITIONS:
            errors.append(f"{entry.scope_id}: unknown coverage disposition")
        if entry.scope_kind == "page":
            page = entry.details.get("page")
            if isinstance(page, int) and not isinstance(page, bool):
                pages[page] = entry
    missing_pages = sorted(set(range(1, page_count + 1)) - set(pages))
    if missing_pages:
        errors.append(f"coverage is missing pages: {missing_pages}")
    for page, entry in sorted(pages.items()):
        if entry.disposition not in TERMINAL_DISPOSITIONS:
            errors.append(f"page {page} is not terminal: {entry.disposition}")
    for kind, label in (
        ("context_scope", "context scope"),
        ("reading_scope", "reading batch"),
        ("landmark", "landmark"),
    ):
        rows = by_kind.get(kind, [])
        if not rows:
            errors.append(f"coverage has no worker-declared {label}s")
        for entry in rows:
            if entry.disposition not in TERMINAL_DISPOSITIONS:
                errors.append(f"{label} {entry.scope_id} is not terminal: {entry.disposition}")
    return CoverageReport(entries=entries, errors=tuple(errors))


def _require_identity(identity: object, label: object, rationale: object, *, kind: str) -> None:
    if not all(isinstance(item, str) and item.strip() for item in (identity, label, rationale)):
        raise CoverageError(f"orientation {kind} identity is incomplete")


def _require_page_route(
    page_start: object, page_end: object, *, page_count: int, identity: object
) -> None:
    if (
        not isinstance(page_start, int)
        or isinstance(page_start, bool)
        or not isinstance(page_end, int)
        or isinstance(page_end, bool)
        or page_start < 1
        or page_start > page_end
        or page_end > page_count
    ):
        raise CoverageError(f"orientation pages are invalid: {identity}")


def _require_unique(values: Any, *, kind: str) -> None:
    identifiers = list(values)
    if len(set(identifiers)) != len(identifiers):
        raise CoverageError(f"orientation {kind} IDs must be unique")
