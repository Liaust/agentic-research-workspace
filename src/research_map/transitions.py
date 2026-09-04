"""Deterministic source and job state transitions."""

from __future__ import annotations

from collections.abc import Mapping

SOURCE_STATES = frozenset(
    {
        "registered",
        "oriented",
        "reading",
        "source_complete",
        "calibration_ready",
        "audit_failed",
        "audit_passed",
        "calibration_graph_inserted",
        "graph_inserted",
        "blocked",
    }
)

SOURCE_TRANSITIONS: Mapping[str | None, frozenset[str]] = {
    None: frozenset({"registered"}),
    "registered": frozenset({"oriented", "blocked"}),
    "oriented": frozenset({"reading", "blocked"}),
    "reading": frozenset({"source_complete", "audit_failed", "blocked"}),
    "source_complete": frozenset(
        {"reading", "audit_failed", "audit_passed", "calibration_ready", "blocked"}
    ),
    "calibration_ready": frozenset({"calibration_graph_inserted", "blocked"}),
    "audit_failed": frozenset({"reading", "blocked"}),
    "audit_passed": frozenset({"graph_inserted", "blocked"}),
    "calibration_graph_inserted": frozenset(),
    "graph_inserted": frozenset(),
    "blocked": frozenset({"registered", "oriented", "reading", "source_complete"}),
}

JOB_STATES = frozenset(
    {"pending", "running", "succeeded", "failed", "validation_failed", "superseded"}
)

JOB_TRANSITIONS: Mapping[str | None, frozenset[str]] = {
    None: frozenset({"pending"}),
    "pending": frozenset({"running", "superseded"}),
    "running": frozenset({"succeeded", "failed", "validation_failed", "superseded"}),
    "failed": frozenset({"running", "superseded"}),
    "validation_failed": frozenset({"running", "superseded"}),
    "succeeded": frozenset({"superseded"}),
    "superseded": frozenset(),
}

CROSS_REFERENCE_BATCH_STATES = frozenset(
    {
        "created",
        "discovering",
        "discovered",
        "inspecting",
        "inspected",
        "compiled",
        "stale",
    }
)

CROSS_REFERENCE_BATCH_TRANSITIONS: Mapping[str | None, frozenset[str]] = {
    None: frozenset({"created"}),
    "created": frozenset({"discovering", "stale"}),
    "discovering": frozenset({"discovered", "stale"}),
    "discovered": frozenset({"inspecting", "stale"}),
    "inspecting": frozenset({"inspected", "stale"}),
    "inspected": frozenset({"compiled", "stale"}),
    "compiled": frozenset({"stale"}),
    "stale": frozenset(),
}

CANDIDATE_STATES = frozenset(
    {
        "discovered",
        "inspecting",
        "relationship",
        "not_usefully_connected",
        "insufficient_extraction",
        "manual_review_candidate",
        "validation_failed",
    }
)

CANDIDATE_TRANSITIONS: Mapping[str | None, frozenset[str]] = {
    None: frozenset({"discovered"}),
    "discovered": frozenset({"inspecting", "validation_failed"}),
    "inspecting": frozenset(
        {
            "relationship",
            "not_usefully_connected",
            "insufficient_extraction",
            "manual_review_candidate",
            "validation_failed",
        }
    ),
    "relationship": frozenset(),
    "not_usefully_connected": frozenset(),
    "insufficient_extraction": frozenset(),
    "manual_review_candidate": frozenset(),
    "validation_failed": frozenset(),
}


class InvalidTransition(ValueError):
    """Raised when deterministic state rules reject a transition."""


def validate_transition(
    current: str | None,
    target: str,
    transitions: Mapping[str | None, frozenset[str]],
) -> bool:
    """Validate a transition, returning False when it is an idempotent replay."""

    if current == target:
        return False
    allowed = transitions.get(current)
    if allowed is None or target not in allowed:
        raise InvalidTransition(f"transition {current!r} -> {target!r} is not allowed")
    return True


def validate_source_transition(current: str | None, target: str) -> bool:
    if target not in SOURCE_STATES:
        raise InvalidTransition(f"unknown source state: {target!r}")
    return validate_transition(current, target, SOURCE_TRANSITIONS)


def validate_job_transition(current: str | None, target: str) -> bool:
    if target not in JOB_STATES:
        raise InvalidTransition(f"unknown job state: {target!r}")
    return validate_transition(current, target, JOB_TRANSITIONS)


def validate_cross_reference_batch_transition(current: str | None, target: str) -> bool:
    if target not in CROSS_REFERENCE_BATCH_STATES:
        raise InvalidTransition(f"unknown cross-reference batch state: {target!r}")
    return validate_transition(current, target, CROSS_REFERENCE_BATCH_TRANSITIONS)


def validate_candidate_transition(current: str | None, target: str) -> bool:
    if target not in CANDIDATE_STATES:
        raise InvalidTransition(f"unknown cross-reference candidate state: {target!r}")
    return validate_transition(current, target, CANDIDATE_TRANSITIONS)
