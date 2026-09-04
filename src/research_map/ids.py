"""Stable identifiers for coordinator-owned objects."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

_PREFIX = re.compile(r"^[a-z][a-z0-9_]*$")


def stable_id(prefix: str, parts: Iterable[str], *, length: int = 24) -> str:
    """Return a deterministic, readable identifier from canonical string parts."""

    if not _PREFIX.fullmatch(prefix):
        raise ValueError(f"invalid identifier prefix: {prefix!r}")
    if length < 12 or length > 64:
        raise ValueError("identifier digest length must be between 12 and 64")
    normalized = tuple(str(part).strip() for part in parts)
    if not normalized or any(not part for part in normalized):
        raise ValueError("identifier parts must be non-empty")
    digest = hashlib.sha256("\x1f".join(normalized).encode()).hexdigest()[:length]
    return f"{prefix}_{digest}"


def run_id(source_id: str, request_key: str) -> str:
    return stable_id("run", (source_id, request_key))


def job_id(run_identifier: str, job_kind: str, attempt: int) -> str:
    if attempt < 1:
        raise ValueError("job attempt must be positive")
    return stable_id("job", (run_identifier, job_kind, str(attempt)))


def artifact_id(source_id: str, artifact_kind: str, sha256: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError("artifact sha256 must be a lowercase hexadecimal digest")
    return stable_id("artifact", (source_id, artifact_kind, sha256))


def canonical_source_pair(source_a: str, source_b: str) -> tuple[str, str]:
    normalized = sorted((source_a.strip(), source_b.strip()))
    if not all(normalized) or normalized[0] == normalized[1]:
        raise ValueError("cross-reference source pair requires two distinct sources")
    return normalized[0], normalized[1]


def cross_reference_batch_id(
    source_fingerprints: Iterable[tuple[str, str]],
    *,
    requested_through: str,
    model: str,
    strategy: str = "pairwise-v1",
) -> str:
    normalized = tuple(
        sorted((source.strip(), fingerprint.strip()) for source, fingerprint in source_fingerprints)
    )
    if len(normalized) < 2 or len({source for source, _ in normalized}) != len(normalized):
        raise ValueError("cross-reference batch requires at least two unique sources")
    if any(not source or not fingerprint for source, fingerprint in normalized):
        raise ValueError("cross-reference source fingerprints must be non-empty")
    return stable_id(
        "xref_batch",
        (
            strategy,
            requested_through,
            model,
            *(f"{source}:{fingerprint}" for source, fingerprint in normalized),
        ),
    )


def cross_reference_job_id(
    batch_identifier: str,
    job_kind: str,
    source_a: str,
    source_b: str,
    attempt: int,
) -> str:
    if attempt < 1:
        raise ValueError("cross-reference job attempt must be positive")
    source_left, source_right = canonical_source_pair(source_a, source_b)
    return stable_id(
        "xref_job",
        (batch_identifier, job_kind, source_left, source_right, str(attempt)),
    )


def holistic_cross_reference_job_id(
    batch_identifier: str,
    source_ids: Iterable[str],
    attempt: int,
) -> str:
    if attempt < 1:
        raise ValueError("cross-reference job attempt must be positive")
    sources = tuple(sorted(source_id.strip() for source_id in source_ids))
    if len(sources) < 2 or len(set(sources)) != len(sources) or not all(sources):
        raise ValueError("holistic cross-reference job requires unique sources")
    return stable_id("xref_job", (batch_identifier, "holistic", *sources, str(attempt)))


def cross_reference_candidate_id(
    batch_identifier: str,
    left_endpoint: tuple[str, int, str],
    right_endpoint: tuple[str, int, str],
) -> str:
    endpoints = tuple(sorted((left_endpoint, right_endpoint), key=lambda value: value[0]))
    if endpoints[0][0] == endpoints[1][0]:
        raise ValueError("cross-reference candidate endpoints must be distinct")
    parts = [batch_identifier]
    for record_id, revision, sha256 in endpoints:
        if revision < 1 or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("candidate endpoint revision or fingerprint is invalid")
        parts.extend((record_id, str(revision), sha256))
    return stable_id("xref_candidate", parts)


def cross_reference_relationship_id(
    left_endpoint: tuple[str, int, str],
    right_endpoint: tuple[str, int, str],
    *,
    relation_type: str,
    direction: str,
) -> str:
    endpoints: tuple[tuple[str, int, str], tuple[str, int, str]] = (
        left_endpoint,
        right_endpoint,
    )
    if direction == "symmetric":
        ordered = sorted(endpoints, key=lambda value: value[0])
        endpoints = (ordered[0], ordered[1])
    parts = [relation_type, direction]
    for record_id, revision, sha256 in endpoints:
        if revision < 1 or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("relationship endpoint revision or fingerprint is invalid")
        parts.extend((record_id, str(revision), sha256))
    return stable_id("xref_rel", parts)
