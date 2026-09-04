"""SQLite-backed operational state for the deterministic coordinator."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

from research_map.cross_reference import DiscoveryCandidate, Relationship
from research_map.ids import canonical_source_pair
from research_map.transitions import (
    validate_candidate_transition,
    validate_cross_reference_batch_transition,
    validate_job_transition,
    validate_source_transition,
)


@dataclass(frozen=True, slots=True)
class TransitionOutcome:
    previous_state: str | None
    current_state: str
    applied: bool
    transition_id: int


@dataclass(frozen=True, slots=True)
class SnapshotImportOutcome:
    """Result of one atomic, model-free source snapshot import."""

    source_id: str
    state: str
    applied: bool
    transition_count: int


class MissingIdentity(ValueError):
    """Raised when a requested source or run identity does not exist."""


class IdentityMismatch(ValueError):
    """Raised when immutable repository identity fields do not match."""


class StateRepository:
    """Small repository boundary around additive SQLite migrations and transitions."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        migration_root = files("research_map.migrations")
        with self.connect() as connection:
            connection.executescript(migration_root.joinpath("0001_initial.sql").read_text())
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)", (1,))
            applied = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version = 2"
            ).fetchone()
            if applied is None:
                connection.executescript(
                    migration_root.joinpath("0002_cross_reference.sql").read_text()
                )
                connection.execute("INSERT INTO schema_migrations(version) VALUES (?)", (2,))
            applied = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version = 3"
            ).fetchone()
            if applied is None:
                connection.executescript(
                    migration_root.joinpath("0003_holistic_cross_reference.sql").read_text()
                )
                connection.execute("INSERT INTO schema_migrations(version) VALUES (?)", (3,))

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def schema_versions(self) -> tuple[int, ...]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        return tuple(int(row["version"]) for row in rows)

    def source_state(self, source_id: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT state FROM sources WHERE source_id = ?", (source_id,)
            ).fetchone()
        return None if row is None else str(row["state"])

    def source_record(self, source_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT source_id, state, metadata_json FROM sources WHERE source_id = ?",
                (source_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "source_id": str(row["source_id"]),
            "state": str(row["state"]),
            "metadata": json.loads(str(row["metadata_json"])),
        }

    def assets_for(self, source_id: str) -> tuple[dict[str, Any], ...]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT asset_id, source_id, role, path, sha256, size_bytes, metadata_json "
                "FROM assets WHERE source_id = ? ORDER BY asset_id",
                (source_id,),
            ).fetchall()
        return tuple(
            {
                "asset_id": str(row["asset_id"]),
                "source_id": str(row["source_id"]),
                "role": str(row["role"]),
                "path": str(row["path"]),
                "sha256": str(row["sha256"]),
                "size_bytes": int(row["size_bytes"]),
                "metadata": json.loads(str(row["metadata_json"])),
            }
            for row in rows
        )

    def register_source(
        self,
        source_id: str,
        *,
        metadata: Mapping[str, Any],
        assets: Sequence[Mapping[str, Any]],
        receipt: Mapping[str, Any],
    ) -> TransitionOutcome:
        """Atomically persist one immutable source registration and its assets."""

        if not assets:
            raise ValueError("source registration requires at least one asset")
        receipt_json = _receipt_json(receipt)
        metadata_json = _canonical_json(metadata)
        normalized_assets = tuple(_normalize_asset(source_id, asset) for asset in assets)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT state, metadata_json FROM sources WHERE source_id = ?", (source_id,)
            ).fetchone()
            current = None if row is None else str(row["state"])
            if row is None:
                connection.execute(
                    "INSERT INTO sources(source_id, state, metadata_json) "
                    "VALUES (?, 'registered', ?)",
                    (source_id, metadata_json),
                )
                for asset in normalized_assets:
                    connection.execute(
                        "INSERT INTO assets(asset_id, source_id, role, path, sha256, size_bytes, "
                        "metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        asset,
                    )
                applied = True
                target = "registered"
            else:
                assert current is not None
                if str(row["metadata_json"]) != metadata_json:
                    raise IdentityMismatch(f"source registration does not match: {source_id}")
                stored_assets = connection.execute(
                    "SELECT asset_id, source_id, role, path, sha256, size_bytes, metadata_json "
                    "FROM assets WHERE source_id = ? ORDER BY asset_id",
                    (source_id,),
                ).fetchall()
                stored = tuple(tuple(asset) for asset in stored_assets)
                expected = tuple(sorted(normalized_assets, key=lambda asset: asset[0]))
                if stored != expected:
                    raise IdentityMismatch(f"source assets do not match: {source_id}")
                applied = False
                target = current

            cursor = connection.execute(
                "INSERT INTO transitions(entity_type, entity_id, from_state, to_state, applied, "
                "receipt_json) VALUES ('source', ?, ?, ?, ?, ?)",
                (source_id, current, target, int(applied), receipt_json),
            )
            if cursor.lastrowid is None:
                raise sqlite3.DatabaseError("transition insert did not return an identifier")
            transition_id = cursor.lastrowid
        return TransitionOutcome(current, target, applied, transition_id)

    def register_run(
        self,
        run_identifier: str,
        *,
        source_id: str,
        requested_through: str,
        model: str | None,
    ) -> bool:
        """Register one immutable run identity, returning False for an exact replay."""

        if not requested_through:
            raise ValueError("requested run target must not be empty")
        with self.connect() as connection:
            source = connection.execute(
                "SELECT 1 FROM sources WHERE source_id = ?", (source_id,)
            ).fetchone()
            if source is None:
                raise MissingIdentity(f"source is not registered: {source_id}")

            row = connection.execute(
                "SELECT source_id, requested_through, model FROM runs WHERE run_id = ?",
                (run_identifier,),
            ).fetchone()
            expected = (source_id, requested_through, model)
            if row is not None:
                stored = (str(row["source_id"]), str(row["requested_through"]), row["model"])
                if stored != expected:
                    raise IdentityMismatch(f"run identity does not match: {run_identifier}")
                return False

            connection.execute(
                "INSERT INTO runs(run_id, source_id, requested_through, model) VALUES (?, ?, ?, ?)",
                (run_identifier, source_id, requested_through, model),
            )
        return True

    def advance_source(
        self,
        source_id: str,
        target: str,
        *,
        receipt: Mapping[str, Any],
        run_identifier: str | None = None,
        job_identifier: str | None = None,
    ) -> TransitionOutcome:
        receipt_json = _receipt_json(receipt)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT state FROM sources WHERE source_id = ?", (source_id,)
            ).fetchone()
            current = None if row is None else str(row["state"])
            applied = validate_source_transition(current, target)
            if applied:
                if current is None:
                    connection.execute(
                        "INSERT INTO sources(source_id, state) VALUES (?, ?)",
                        (source_id, target),
                    )
                else:
                    connection.execute(
                        "UPDATE sources SET state = ?, updated_at = CURRENT_TIMESTAMP "
                        "WHERE source_id = ?",
                        (target, source_id),
                    )
            cursor = connection.execute(
                "INSERT INTO transitions(entity_type, entity_id, from_state, to_state, "
                "applied, run_id, job_id, receipt_json) VALUES ('source', ?, ?, ?, ?, ?, ?, ?)",
                (
                    source_id,
                    current,
                    target,
                    int(applied),
                    run_identifier,
                    job_identifier,
                    receipt_json,
                ),
            )
            if cursor.lastrowid is None:
                raise sqlite3.DatabaseError("transition insert did not return an identifier")
            transition_id = cursor.lastrowid
        return TransitionOutcome(current, target, applied, transition_id)

    def job_state(self, job_identifier: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT state FROM jobs WHERE job_id = ?", (job_identifier,)
            ).fetchone()
        return None if row is None else str(row["state"])

    def job_record(self, job_identifier: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT job_id, run_id, source_id, kind, attempt, state, receipt_path "
                "FROM jobs WHERE job_id = ?",
                (job_identifier,),
            ).fetchone()
        if row is None:
            return None
        return {
            "job_id": str(row["job_id"]),
            "run_id": str(row["run_id"]),
            "source_id": str(row["source_id"]),
            "kind": str(row["kind"]),
            "attempt": int(row["attempt"]),
            "state": str(row["state"]),
            "receipt_path": row["receipt_path"],
        }

    def jobs_for_run(self, run_identifier: str) -> tuple[dict[str, Any], ...]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT job_id, run_id, source_id, kind, attempt, state, receipt_path "
                "FROM jobs WHERE run_id = ? ORDER BY created_at, job_id",
                (run_identifier,),
            ).fetchall()
        return tuple(
            {
                "job_id": str(row["job_id"]),
                "run_id": str(row["run_id"]),
                "source_id": str(row["source_id"]),
                "kind": str(row["kind"]),
                "attempt": int(row["attempt"]),
                "state": str(row["state"]),
                "receipt_path": row["receipt_path"],
            }
            for row in rows
        )

    def run_record(self, run_identifier: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT run_id, source_id, requested_through, model, state "
                "FROM runs WHERE run_id = ?",
                (run_identifier,),
            ).fetchone()
        if row is None:
            return None
        return {
            "run_id": str(row["run_id"]),
            "source_id": str(row["source_id"]),
            "requested_through": str(row["requested_through"]),
            "model": row["model"],
            "state": str(row["state"]),
        }

    def register_cross_reference_batch(
        self,
        batch_identifier: str,
        *,
        source_ids: Sequence[str],
        input_fingerprints: Mapping[str, str],
        requested_through: str,
        model: str,
        strategy: str = "pairwise-v1",
        eligible_source_states: frozenset[str] | None = None,
    ) -> bool:
        """Register one immutable multi-source batch, returning False on exact replay."""

        sources = tuple(sorted(source_id.strip() for source_id in source_ids))
        if len(sources) < 2 or len(set(sources)) != len(sources):
            raise ValueError("cross-reference batch requires at least two unique sources")
        if set(input_fingerprints) != set(sources):
            raise IdentityMismatch("cross-reference input fingerprints do not match sources")
        if requested_through not in {"discovered", "inspected", "compiled"}:
            raise ValueError("cross-reference requested target is invalid")
        if not model.strip():
            raise ValueError("cross-reference batch model is required")
        if not strategy.strip():
            raise ValueError("cross-reference batch strategy is required")
        explicit_eligibility = eligible_source_states is not None
        eligible_states = eligible_source_states or frozenset({"graph_inserted"})
        if not eligible_states:
            raise ValueError("cross-reference eligible source states must not be empty")
        sources_json = json.dumps(sources, separators=(",", ":"))
        fingerprints_json = _canonical_json(input_fingerprints)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT source_id, state FROM sources WHERE source_id IN "
                f"({','.join('?' for _ in sources)}) ORDER BY source_id",
                sources,
            ).fetchall()
            observed = tuple((str(row["source_id"]), str(row["state"])) for row in rows)
            if tuple(source for source, _state in observed) != sources:
                raise MissingIdentity("cross-reference source is not registered")
            ineligible = [source for source, state in observed if state not in eligible_states]
            if ineligible:
                if not explicit_eligibility:
                    raise IdentityMismatch(
                        f"cross-reference sources are not graph_inserted: {ineligible}"
                    )
                raise IdentityMismatch(
                    "cross-reference sources are not in an eligible state: "
                    f"{ineligible}; eligible={sorted(eligible_states)}"
                )
            row = connection.execute(
                "SELECT sources_json, input_fingerprints_json, requested_through, model, "
                "strategy "
                "FROM cross_reference_batches WHERE batch_id = ?",
                (batch_identifier,),
            ).fetchone()
            expected = (sources_json, fingerprints_json, requested_through, model, strategy)
            if row is not None:
                stored = (
                    str(row["sources_json"]),
                    str(row["input_fingerprints_json"]),
                    str(row["requested_through"]),
                    str(row["model"]),
                    str(row["strategy"]),
                )
                if stored != expected:
                    raise IdentityMismatch(
                        f"cross-reference batch identity does not match: {batch_identifier}"
                    )
                return False
            connection.execute(
                "INSERT INTO cross_reference_batches("
                "batch_id, sources_json, input_fingerprints_json, requested_through, model, "
                "strategy, state) VALUES (?, ?, ?, ?, ?, ?, 'created')",
                (batch_identifier, *expected),
            )
            cursor = connection.execute(
                "INSERT INTO cross_reference_transitions("
                "entity_type, entity_id, from_state, to_state, applied, batch_id, receipt_json"
                ") VALUES ('batch', ?, NULL, 'created', 1, ?, ?)",
                (
                    batch_identifier,
                    batch_identifier,
                    _receipt_json({"reason": "batch registered"}),
                ),
            )
            if cursor.lastrowid is None:
                raise sqlite3.DatabaseError("cross-reference transition has no identifier")
        return True

    def cross_reference_batch_record(self, batch_identifier: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT batch_id, sources_json, input_fingerprints_json, requested_through, "
                "model, strategy, state FROM cross_reference_batches WHERE batch_id = ?",
                (batch_identifier,),
            ).fetchone()
        if row is None:
            return None
        return {
            "batch_id": str(row["batch_id"]),
            "source_ids": tuple(json.loads(str(row["sources_json"]))),
            "input_fingerprints": json.loads(str(row["input_fingerprints_json"])),
            "requested_through": str(row["requested_through"]),
            "model": str(row["model"]),
            "strategy": str(row["strategy"]),
            "state": str(row["state"]),
        }

    def advance_cross_reference_batch(
        self,
        batch_identifier: str,
        target: str,
        *,
        receipt: Mapping[str, Any],
    ) -> TransitionOutcome:
        receipt_json = _receipt_json(receipt)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT state FROM cross_reference_batches WHERE batch_id = ?",
                (batch_identifier,),
            ).fetchone()
            if row is None:
                raise MissingIdentity(
                    f"cross-reference batch is not registered: {batch_identifier}"
                )
            current = str(row["state"])
            applied = validate_cross_reference_batch_transition(current, target)
            if applied:
                connection.execute(
                    "UPDATE cross_reference_batches SET state = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE batch_id = ?",
                    (target, batch_identifier),
                )
            cursor = connection.execute(
                "INSERT INTO cross_reference_transitions("
                "entity_type, entity_id, from_state, to_state, applied, batch_id, receipt_json"
                ") VALUES ('batch', ?, ?, ?, ?, ?, ?)",
                (
                    batch_identifier,
                    current,
                    target,
                    int(applied),
                    batch_identifier,
                    receipt_json,
                ),
            )
            if cursor.lastrowid is None:
                raise sqlite3.DatabaseError("cross-reference transition has no identifier")
            transition_id = cursor.lastrowid
        return TransitionOutcome(current, target, applied, transition_id)

    def advance_cross_reference_job(
        self,
        job_identifier: str,
        target: str,
        *,
        batch_identifier: str,
        job_kind: str,
        source_a: str,
        source_b: str,
        attempt: int,
        receipt: Mapping[str, Any],
        source_ids: Sequence[str] | None = None,
    ) -> TransitionOutcome:
        if attempt < 1:
            raise ValueError("cross-reference job attempt must be positive")
        source_left, source_right = canonical_source_pair(source_a, source_b)
        job_sources = (
            (source_left, source_right)
            if source_ids is None
            else tuple(sorted(source_id.strip() for source_id in source_ids))
        )
        if len(job_sources) < 2 or len(set(job_sources)) != len(job_sources):
            raise ValueError("cross-reference job requires at least two unique sources")
        if not {source_left, source_right}.issubset(job_sources):
            raise IdentityMismatch("cross-reference job anchors are outside its source scope")
        scope_sources_json = json.dumps(job_sources, separators=(",", ":"))
        receipt_json = _receipt_json(receipt)
        with self.connect() as connection:
            batch = connection.execute(
                "SELECT sources_json FROM cross_reference_batches WHERE batch_id = ?",
                (batch_identifier,),
            ).fetchone()
            if batch is None:
                raise MissingIdentity(
                    f"cross-reference batch is not registered: {batch_identifier}"
                )
            batch_sources = set(json.loads(str(batch["sources_json"])))
            if not set(job_sources).issubset(batch_sources):
                raise IdentityMismatch("cross-reference job source scope is outside batch")
            row = connection.execute(
                "SELECT batch_id, kind, source_left, source_right, scope_sources_json, "
                "attempt, state "
                "FROM cross_reference_jobs WHERE job_id = ?",
                (job_identifier,),
            ).fetchone()
            expected_identity = (
                batch_identifier,
                job_kind,
                source_left,
                source_right,
                scope_sources_json,
                attempt,
            )
            if row is not None:
                stored_identity = (
                    str(row["batch_id"]),
                    str(row["kind"]),
                    str(row["source_left"]),
                    str(row["source_right"]),
                    str(
                        row["scope_sources_json"]
                        or json.dumps(
                            (str(row["source_left"]), str(row["source_right"])),
                            separators=(",", ":"),
                        )
                    ),
                    int(row["attempt"]),
                )
                if stored_identity != expected_identity:
                    raise IdentityMismatch(
                        f"cross-reference job identity does not match: {job_identifier}"
                    )
            current = None if row is None else str(row["state"])
            applied = validate_job_transition(current, target)
            if applied:
                if row is None:
                    connection.execute(
                        "INSERT INTO cross_reference_jobs("
                        "job_id, batch_id, kind, source_left, source_right, "
                        "scope_sources_json, attempt, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (job_identifier, *expected_identity, target),
                    )
                else:
                    connection.execute(
                        "UPDATE cross_reference_jobs SET state = ?, "
                        "updated_at = CURRENT_TIMESTAMP WHERE job_id = ?",
                        (target, job_identifier),
                    )
            cursor = connection.execute(
                "INSERT INTO cross_reference_transitions("
                "entity_type, entity_id, from_state, to_state, applied, batch_id, job_id, "
                "receipt_json) VALUES ('job', ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_identifier,
                    current,
                    target,
                    int(applied),
                    batch_identifier,
                    job_identifier,
                    receipt_json,
                ),
            )
            if cursor.lastrowid is None:
                raise sqlite3.DatabaseError("cross-reference transition has no identifier")
            transition_id = cursor.lastrowid
        return TransitionOutcome(current, target, applied, transition_id)

    def cross_reference_job_record(self, job_identifier: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT job_id, batch_id, kind, source_left, source_right, "
                "scope_sources_json, attempt, state, receipt_path "
                "FROM cross_reference_jobs WHERE job_id = ?",
                (job_identifier,),
            ).fetchone()
            transition = connection.execute(
                "SELECT receipt_json FROM cross_reference_transitions "
                "WHERE entity_type = 'job' AND entity_id = ? AND applied = 1 "
                "ORDER BY transition_id DESC LIMIT 1",
                (job_identifier,),
            ).fetchone()
        if row is None:
            return None
        source_pair = (str(row["source_left"]), str(row["source_right"]))
        source_scope = (
            tuple(json.loads(str(row["scope_sources_json"])))
            if row["scope_sources_json"] is not None
            else source_pair
        )
        transition_receipt: dict[str, Any] | None = None
        if transition is not None:
            raw_receipt = json.loads(str(transition["receipt_json"]))
            if not isinstance(raw_receipt, dict):
                raise sqlite3.DatabaseError("cross-reference transition receipt is invalid")
            transition_receipt = raw_receipt
        return {
            "job_id": str(row["job_id"]),
            "batch_id": str(row["batch_id"]),
            "kind": str(row["kind"]),
            "source_pair": source_pair,
            "source_ids": source_scope,
            "attempt": int(row["attempt"]),
            "state": str(row["state"]),
            "receipt_path": row["receipt_path"],
            "transition_receipt": transition_receipt,
        }

    def cross_reference_jobs_for_batch(self, batch_identifier: str) -> tuple[dict[str, Any], ...]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT job_id FROM cross_reference_jobs WHERE batch_id = ? "
                "ORDER BY kind, source_left, source_right, attempt, job_id",
                (batch_identifier,),
            ).fetchall()
        records = tuple(self.cross_reference_job_record(str(row["job_id"])) for row in rows)
        return tuple(record for record in records if record is not None)

    def set_cross_reference_job_receipt(self, job_identifier: str, receipt_path: str) -> None:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE cross_reference_jobs SET receipt_path = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE job_id = ?",
                (receipt_path, job_identifier),
            )
            if cursor.rowcount != 1:
                raise MissingIdentity(f"cross-reference job is not registered: {job_identifier}")

    def record_cross_reference_candidate(self, candidate: DiscoveryCandidate) -> bool:
        with self.connect() as connection:
            job = connection.execute(
                "SELECT batch_id, kind, source_left, source_right, scope_sources_json "
                "FROM cross_reference_jobs "
                "WHERE job_id = ?",
                (candidate.discovery_job_id,),
            ).fetchone()
            if job is None:
                raise MissingIdentity(
                    f"discovery job is not registered: {candidate.discovery_job_id}"
                )
            kind = str(job["kind"])
            scope = (
                set(json.loads(str(job["scope_sources_json"])))
                if job["scope_sources_json"] is not None
                else {str(job["source_left"]), str(job["source_right"])}
            )
            pair_matches = (
                str(job["source_left"]),
                str(job["source_right"]),
            ) == candidate.source_pair
            if (
                str(job["batch_id"]) != candidate.batch_id
                or (kind == "discovery" and not pair_matches)
                or (kind == "holistic" and not set(candidate.source_pair).issubset(scope))
                or kind not in {"discovery", "holistic"}
            ):
                raise IdentityMismatch("candidate does not belong to discovery job")
            row = connection.execute(
                "SELECT batch_id, source_left, source_right, left_endpoint_id, left_revision, "
                "left_record_sha256, right_endpoint_id, right_revision, right_record_sha256, "
                "comparison_surface, discovery_job_id FROM cross_reference_candidates "
                "WHERE candidate_id = ?",
                (candidate.candidate_id,),
            ).fetchone()
            expected = (
                candidate.batch_id,
                *candidate.source_pair,
                candidate.left_endpoint.record_id,
                candidate.left_endpoint.revision,
                candidate.left_endpoint.record_sha256,
                candidate.right_endpoint.record_id,
                candidate.right_endpoint.revision,
                candidate.right_endpoint.record_sha256,
                candidate.comparison_surface,
                candidate.discovery_job_id,
            )
            if row is not None:
                stored = (
                    row["batch_id"],
                    row["source_left"],
                    row["source_right"],
                    row["left_endpoint_id"],
                    row["left_revision"],
                    row["left_record_sha256"],
                    row["right_endpoint_id"],
                    row["right_revision"],
                    row["right_record_sha256"],
                    row["comparison_surface"],
                    row["discovery_job_id"],
                )
                if stored != expected:
                    raise IdentityMismatch(
                        f"candidate identity does not match: {candidate.candidate_id}"
                    )
                return False
            connection.execute(
                "INSERT INTO cross_reference_candidates("
                "candidate_id, batch_id, source_left, source_right, left_endpoint_id, "
                "left_revision, left_record_sha256, right_endpoint_id, right_revision, "
                "right_record_sha256, comparison_surface, state, discovery_job_id"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'discovered', ?)",
                (candidate.candidate_id, *expected),
            )
            connection.execute(
                "INSERT INTO cross_reference_transitions("
                "entity_type, entity_id, from_state, to_state, applied, batch_id, job_id, "
                "receipt_json) VALUES ('candidate', ?, NULL, 'discovered', 1, ?, ?, ?)",
                (
                    candidate.candidate_id,
                    candidate.batch_id,
                    candidate.discovery_job_id,
                    _receipt_json({"reason": "candidate discovered"}),
                ),
            )
        return True

    def cross_reference_candidate_record(self, candidate_identifier: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM cross_reference_candidates WHERE candidate_id = ?",
                (candidate_identifier,),
            ).fetchone()
        if row is None:
            return None
        return _cross_reference_candidate_row(row)

    def cross_reference_candidates_for_batch(
        self, batch_identifier: str
    ) -> tuple[dict[str, Any], ...]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM cross_reference_candidates WHERE batch_id = ? "
                "ORDER BY source_left, source_right, candidate_id",
                (batch_identifier,),
            ).fetchall()
        return tuple(_cross_reference_candidate_row(row) for row in rows)

    def advance_cross_reference_candidate(
        self,
        candidate_identifier: str,
        target: str,
        *,
        receipt: Mapping[str, Any],
        inspection_job_identifier: str | None = None,
        outcome: Mapping[str, Any] | None = None,
    ) -> TransitionOutcome:
        receipt_json = _receipt_json(receipt)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT batch_id, source_left, source_right, state, inspection_job_id, "
                "outcome_json FROM cross_reference_candidates WHERE candidate_id = ?",
                (candidate_identifier,),
            ).fetchone()
            if row is None:
                raise MissingIdentity(
                    f"cross-reference candidate is not registered: {candidate_identifier}"
                )
            batch_identifier = str(row["batch_id"])
            current = str(row["state"])
            if inspection_job_identifier is not None:
                job = connection.execute(
                    "SELECT batch_id, kind, source_left, source_right, scope_sources_json FROM "
                    "cross_reference_jobs WHERE job_id = ?",
                    (inspection_job_identifier,),
                ).fetchone()
                valid_job = False
                if job is not None and str(job["batch_id"]) == batch_identifier:
                    kind = str(job["kind"])
                    pair = (str(row["source_left"]), str(row["source_right"]))
                    if kind == "inspection":
                        valid_job = (str(job["source_left"]), str(job["source_right"])) == pair
                    elif kind == "holistic":
                        scope = set(json.loads(str(job["scope_sources_json"])))
                        valid_job = set(pair).issubset(scope)
                if not valid_job:
                    raise IdentityMismatch("candidate inspection job does not match")
            if target == "inspecting" and inspection_job_identifier is None:
                raise ValueError("inspecting candidate requires an inspection job")
            terminal = {
                "relationship",
                "not_usefully_connected",
                "insufficient_extraction",
                "manual_review_candidate",
            }
            if target in terminal:
                if outcome is None or outcome.get("outcome") != target:
                    raise ValueError("terminal candidate outcome does not match target state")
                if inspection_job_identifier is None:
                    inspection_job_identifier = row["inspection_job_id"]
                if inspection_job_identifier is None:
                    raise ValueError("terminal candidate requires an inspection job")
            outcome_json = None if outcome is None else _canonical_json(outcome)
            applied = validate_candidate_transition(current, target)
            if applied:
                connection.execute(
                    "UPDATE cross_reference_candidates SET state = ?, inspection_job_id = "
                    "COALESCE(?, inspection_job_id), outcome_json = COALESCE(?, outcome_json), "
                    "updated_at = CURRENT_TIMESTAMP WHERE candidate_id = ?",
                    (
                        target,
                        inspection_job_identifier,
                        outcome_json,
                        candidate_identifier,
                    ),
                )
            cursor = connection.execute(
                "INSERT INTO cross_reference_transitions("
                "entity_type, entity_id, from_state, to_state, applied, batch_id, job_id, "
                "receipt_json) VALUES ('candidate', ?, ?, ?, ?, ?, ?, ?)",
                (
                    candidate_identifier,
                    current,
                    target,
                    int(applied),
                    batch_identifier,
                    inspection_job_identifier,
                    receipt_json,
                ),
            )
            if cursor.lastrowid is None:
                raise sqlite3.DatabaseError("cross-reference transition has no identifier")
            transition_id = cursor.lastrowid
        return TransitionOutcome(current, target, applied, transition_id)

    def record_cross_reference_relationship(
        self,
        relationship: Relationship,
        *,
        inspection_job_identifier: str,
    ) -> bool:
        payload_json = _canonical_json(relationship.payload)
        with self.connect() as connection:
            candidate = connection.execute(
                "SELECT batch_id, state, inspection_job_id FROM cross_reference_candidates "
                "WHERE candidate_id = ?",
                (relationship.candidate_id,),
            ).fetchone()
            if candidate is None:
                raise MissingIdentity(
                    f"relationship candidate is not registered: {relationship.candidate_id}"
                )
            expected = (
                relationship.batch_id,
                "relationship",
                inspection_job_identifier,
            )
            observed = (
                str(candidate["batch_id"]),
                str(candidate["state"]),
                candidate["inspection_job_id"],
            )
            if observed != expected:
                raise IdentityMismatch("relationship candidate state or job does not match")
            row = connection.execute(
                "SELECT candidate_id, batch_id, revision, relation_type, payload_json, "
                "inspection_job_id FROM cross_reference_relationships "
                "WHERE relationship_id = ?",
                (relationship.relationship_id,),
            ).fetchone()
            stored_expected = (
                relationship.candidate_id,
                relationship.batch_id,
                relationship.revision,
                relationship.relation_type,
                payload_json,
                inspection_job_identifier,
            )
            if row is not None:
                stored = (
                    row["candidate_id"],
                    row["batch_id"],
                    row["revision"],
                    row["relation_type"],
                    row["payload_json"],
                    row["inspection_job_id"],
                )
                if stored != stored_expected:
                    raise IdentityMismatch(
                        f"relationship identity does not match: {relationship.relationship_id}"
                    )
                return False
            connection.execute(
                "INSERT INTO cross_reference_relationships("
                "relationship_id, candidate_id, batch_id, revision, relation_type, state, "
                "payload_json, inspection_job_id) VALUES (?, ?, ?, ?, ?, 'accepted', ?, ?)",
                (relationship.relationship_id, *stored_expected),
            )
        return True

    def cross_reference_relationship_record(
        self, relationship_identifier: str
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT payload_json, state FROM cross_reference_relationships "
                "WHERE relationship_id = ?",
                (relationship_identifier,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row["payload_json"]))
        if not isinstance(payload, dict):
            raise sqlite3.DatabaseError("cross-reference relationship payload is invalid")
        payload["state"] = str(row["state"])
        return dict(payload)

    def cross_reference_relationships_for_batch(
        self, batch_identifier: str
    ) -> tuple[dict[str, Any], ...]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT relationship_id FROM cross_reference_relationships "
                "WHERE batch_id = ? ORDER BY relationship_id",
                (batch_identifier,),
            ).fetchall()
        records = tuple(
            self.cross_reference_relationship_record(str(row["relationship_id"])) for row in rows
        )
        return tuple(record for record in records if record is not None)

    def register_cross_reference_artifact(
        self,
        artifact_identifier: str,
        *,
        batch_identifier: str,
        job_identifier: str | None,
        kind: str,
        path: str,
        sha256: str,
        size_bytes: int,
        metadata: Mapping[str, Any],
    ) -> bool:
        if len(sha256) != 64 or any(character not in "0123456789abcdef" for character in sha256):
            raise ValueError("cross-reference artifact sha256 is invalid")
        if size_bytes < 0 or not kind.strip() or not path.strip():
            raise ValueError("cross-reference artifact identity is invalid")
        metadata_json = _canonical_json(metadata)
        with self.connect() as connection:
            batch = connection.execute(
                "SELECT 1 FROM cross_reference_batches WHERE batch_id = ?",
                (batch_identifier,),
            ).fetchone()
            if batch is None:
                raise MissingIdentity(
                    f"cross-reference batch is not registered: {batch_identifier}"
                )
            if job_identifier is not None:
                job = connection.execute(
                    "SELECT batch_id FROM cross_reference_jobs WHERE job_id = ?",
                    (job_identifier,),
                ).fetchone()
                if job is None or str(job["batch_id"]) != batch_identifier:
                    raise IdentityMismatch("cross-reference artifact job does not match batch")
            row = connection.execute(
                "SELECT batch_id, job_id, kind, path, sha256, size_bytes, metadata_json "
                "FROM cross_reference_artifacts WHERE artifact_id = ?",
                (artifact_identifier,),
            ).fetchone()
            expected = (
                batch_identifier,
                job_identifier,
                kind,
                path,
                sha256,
                size_bytes,
                metadata_json,
            )
            if row is not None:
                stored = (
                    row["batch_id"],
                    row["job_id"],
                    row["kind"],
                    row["path"],
                    row["sha256"],
                    row["size_bytes"],
                    row["metadata_json"],
                )
                if stored != expected:
                    stored_without_path = (*stored[:3], *stored[4:])
                    expected_without_path = (*expected[:3], *expected[4:])
                    if stored_without_path == expected_without_path:
                        connection.execute(
                            "UPDATE cross_reference_artifacts SET path = ? WHERE artifact_id = ?",
                            (path, artifact_identifier),
                        )
                        return False
                    raise IdentityMismatch(
                        f"cross-reference artifact identity does not match: {artifact_identifier}"
                    )
                return False
            connection.execute(
                "INSERT INTO cross_reference_artifacts("
                "artifact_id, batch_id, job_id, kind, path, sha256, size_bytes, metadata_json"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (artifact_identifier, *expected),
            )
        return True

    def cross_reference_transitions_for(
        self, entity_type: str, entity_id: str
    ) -> tuple[sqlite3.Row, ...]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM cross_reference_transitions "
                "WHERE entity_type = ? AND entity_id = ? ORDER BY transition_id",
                (entity_type, entity_id),
            ).fetchall()
        return tuple(rows)

    def jobs_for_source(
        self, source_id: str, *, job_kind: str | None = None
    ) -> tuple[dict[str, Any], ...]:
        query = (
            "SELECT job_id, run_id, source_id, kind, attempt, state, receipt_path "
            "FROM jobs WHERE source_id = ?"
        )
        parameters: tuple[object, ...] = (source_id,)
        if job_kind is not None:
            query += " AND kind = ?"
            parameters = (source_id, job_kind)
        query += " ORDER BY created_at, attempt, job_id"
        with self.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return tuple(
            {
                "job_id": str(row["job_id"]),
                "run_id": str(row["run_id"]),
                "source_id": str(row["source_id"]),
                "kind": str(row["kind"]),
                "attempt": int(row["attempt"]),
                "state": str(row["state"]),
                "receipt_path": row["receipt_path"],
            }
            for row in rows
        )

    def set_job_receipt(self, job_identifier: str, receipt_path: str) -> None:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE jobs SET receipt_path = ?, updated_at = CURRENT_TIMESTAMP WHERE job_id = ?",
                (receipt_path, job_identifier),
            )
            if cursor.rowcount != 1:
                raise MissingIdentity(f"job is not registered: {job_identifier}")

    def import_source_snapshot(
        self,
        source_id: str,
        *,
        metadata: Mapping[str, Any],
        assets: Sequence[Mapping[str, Any]],
        records: Sequence[Mapping[str, Any]],
        coverage: Sequence[Mapping[str, Any]],
        run_identifier: str,
        job_identifier: str,
        receipt_path: str,
        origin_receipt: Mapping[str, Any],
    ) -> SnapshotImportOutcome:
        """Atomically import one verified active proposal as ``source_complete``.

        This is the public deterministic transfer boundary used by downstream
        corpus work. It intentionally imports only active source-local meaning;
        the complete origin identity and fingerprints remain in the import
        receipt. Exact replay performs no writes.
        """

        if not assets:
            raise ValueError("source snapshot import requires at least one asset")
        if not records:
            raise ValueError("source snapshot import requires active WIP records")
        if not receipt_path.strip() or not origin_receipt:
            raise ValueError("source snapshot import requires an origin receipt")
        metadata_json = _canonical_json(metadata)
        normalized_assets = tuple(
            sorted(
                (_normalize_asset(source_id, asset) for asset in assets), key=lambda item: item[0]
            )
        )
        normalized_records = _normalize_snapshot_records(source_id, records)
        normalized_coverage = _normalize_snapshot_coverage(coverage)
        origin_receipt_json = _receipt_json(origin_receipt)
        source_receipts = (
            _receipt_json({"kind": "corpus_snapshot_registered", "origin": origin_receipt}),
            _receipt_json({"kind": "corpus_snapshot_oriented", "semantic_jobs_started": 0}),
            _receipt_json({"kind": "corpus_snapshot_reading", "semantic_jobs_started": 0}),
            _receipt_json(
                {
                    "kind": "corpus_snapshot_imported",
                    "origin": origin_receipt,
                    "semantic_jobs_started": 0,
                }
            ),
        )
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT state, metadata_json FROM sources WHERE source_id = ?", (source_id,)
            ).fetchone()
            if existing is not None:
                _verify_snapshot_replay(
                    connection,
                    source_id=source_id,
                    metadata_json=metadata_json,
                    assets=normalized_assets,
                    records=normalized_records,
                    coverage=normalized_coverage,
                    run_identifier=run_identifier,
                    job_identifier=job_identifier,
                    receipt_path=receipt_path,
                    origin_receipt_json=origin_receipt_json,
                )
                return SnapshotImportOutcome(
                    source_id=source_id,
                    state="source_complete",
                    applied=False,
                    transition_count=0,
                )

            connection.execute(
                "INSERT INTO sources(source_id, state, metadata_json) VALUES (?, 'registered', ?)",
                (source_id, metadata_json),
            )
            for asset in normalized_assets:
                connection.execute(
                    "INSERT INTO assets(asset_id, source_id, role, path, sha256, size_bytes, "
                    "metadata_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    asset,
                )
            connection.execute(
                "INSERT INTO transitions(entity_type, entity_id, from_state, to_state, applied, "
                "receipt_json) VALUES ('source', ?, NULL, 'registered', 1, ?)",
                (source_id, source_receipts[0]),
            )
            connection.execute(
                "INSERT INTO runs(run_id, source_id, requested_through, model) "
                "VALUES (?, ?, 'source_complete_import', NULL)",
                (run_identifier, source_id),
            )
            connection.execute(
                "INSERT INTO jobs(job_id, run_id, source_id, kind, attempt, state, receipt_path) "
                "VALUES (?, ?, ?, 'corpus_snapshot_import', 1, 'succeeded', ?)",
                (job_identifier, run_identifier, source_id, receipt_path),
            )
            for previous, target, job_receipt in (
                (None, "pending", {"kind": "corpus_snapshot_import_pending"}),
                ("pending", "running", {"kind": "corpus_snapshot_import_running"}),
                (
                    "running",
                    "succeeded",
                    {
                        "kind": "corpus_snapshot_import_succeeded",
                        "origin": origin_receipt,
                        "semantic_jobs_started": 0,
                    },
                ),
            ):
                connection.execute(
                    "INSERT INTO transitions(entity_type, entity_id, from_state, to_state, "
                    "applied, run_id, job_id, receipt_json) "
                    "VALUES ('job', ?, ?, ?, 1, ?, ?, ?)",
                    (
                        job_identifier,
                        previous,
                        target,
                        run_identifier,
                        job_identifier,
                        _receipt_json(job_receipt),
                    ),
                )
            for record_id, revision, record_type, payload_json in normalized_records:
                connection.execute(
                    "INSERT INTO wip_records(source_id, record_id, revision, record_type, "
                    "payload_json, status, proposal_job_id) "
                    "VALUES (?, ?, ?, ?, ?, 'active', ?)",
                    (
                        source_id,
                        record_id,
                        revision,
                        record_type,
                        payload_json,
                        job_identifier,
                    ),
                )
            for scope_id, scope_kind, disposition, details_json in normalized_coverage:
                connection.execute(
                    "INSERT INTO coverage(source_id, scope_id, scope_kind, disposition, "
                    "details_json, updated_by_job_id) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        source_id,
                        scope_id,
                        scope_kind,
                        disposition,
                        details_json,
                        job_identifier,
                    ),
                )
            current = "registered"
            for target, receipt_json in zip(
                ("oriented", "reading", "source_complete"), source_receipts[1:], strict=True
            ):
                validate_source_transition(current, target)
                connection.execute(
                    "UPDATE sources SET state = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE source_id = ?",
                    (target, source_id),
                )
                connection.execute(
                    "INSERT INTO transitions(entity_type, entity_id, from_state, to_state, "
                    "applied, run_id, job_id, receipt_json) "
                    "VALUES ('source', ?, ?, ?, 1, ?, ?, ?)",
                    (
                        source_id,
                        current,
                        target,
                        run_identifier,
                        job_identifier,
                        receipt_json,
                    ),
                )
                current = target
        return SnapshotImportOutcome(
            source_id=source_id,
            state="source_complete",
            applied=True,
            transition_count=7,
        )

    def apply_wip_records(
        self,
        source_id: str,
        *,
        job_identifier: str,
        records: Sequence[Mapping[str, Any]],
    ) -> tuple[str, ...]:
        """Apply validated proposal records while retaining revision history."""

        applied: list[str] = []
        with self.connect() as connection:
            job = connection.execute(
                "SELECT source_id FROM jobs WHERE job_id = ?", (job_identifier,)
            ).fetchone()
            if job is None:
                raise MissingIdentity(f"job is not registered: {job_identifier}")
            if str(job["source_id"]) != source_id:
                raise IdentityMismatch(f"job does not belong to source: {source_id}")
            for record in records:
                if record.get("source_id") != source_id:
                    raise IdentityMismatch(f"WIP record does not belong to source: {source_id}")
                record_id = str(record["id"])
                revision = int(record["revision"])
                record_type = str(record["record_type"])
                payload_json = _canonical_json(record)
                existing = connection.execute(
                    "SELECT record_type, payload_json FROM wip_records "
                    "WHERE source_id = ? AND record_id = ? AND revision = ?",
                    (source_id, record_id, revision),
                ).fetchone()
                if existing is not None:
                    if (
                        str(existing["record_type"]) != record_type
                        or str(existing["payload_json"]) != payload_json
                    ):
                        raise IdentityMismatch(
                            f"WIP revision identity does not match: {record_id}@{revision}"
                        )
                    continue
                latest = connection.execute(
                    "SELECT MAX(revision) AS revision FROM wip_records "
                    "WHERE source_id = ? AND record_id = ?",
                    (source_id, record_id),
                ).fetchone()
                latest_revision = None if latest is None else latest["revision"]
                expected_revision = 1 if latest_revision is None else int(latest_revision) + 1
                if revision != expected_revision:
                    raise IdentityMismatch(
                        f"WIP revision is not sequential: {record_id}@{revision}"
                    )
                connection.execute(
                    "UPDATE wip_records SET status = 'superseded' "
                    "WHERE source_id = ? AND record_id = ? AND status = 'active'",
                    (source_id, record_id),
                )
                connection.execute(
                    "INSERT INTO wip_records(source_id, record_id, revision, record_type, "
                    "payload_json, status, proposal_job_id) VALUES (?, ?, ?, ?, ?, 'active', ?)",
                    (
                        source_id,
                        record_id,
                        revision,
                        record_type,
                        payload_json,
                        job_identifier,
                    ),
                )
                applied.append(f"{record_id}@{revision}")
        return tuple(applied)

    def latest_wip_records(self, source_id: str) -> tuple[dict[str, Any], ...]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM wip_records "
                "WHERE source_id = ? AND status = 'active' ORDER BY record_id",
                (source_id,),
            ).fetchall()
        return tuple(json.loads(str(row["payload_json"])) for row in rows)

    def wip_record_revisions(self, source_id: str) -> tuple[dict[str, Any], ...]:
        """Return every retained WIP revision for provenance verification."""

        with self.connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM wip_records WHERE source_id = ? "
                "ORDER BY record_id, revision",
                (source_id,),
            ).fetchall()
        return tuple(json.loads(str(row["payload_json"])) for row in rows)

    def upsert_coverage(
        self,
        source_id: str,
        *,
        scope_id: str,
        scope_kind: str,
        disposition: str,
        details: Mapping[str, Any],
        job_identifier: str,
    ) -> None:
        with self.connect() as connection:
            job = connection.execute(
                "SELECT source_id FROM jobs WHERE job_id = ?", (job_identifier,)
            ).fetchone()
            if job is None:
                raise MissingIdentity(f"job is not registered: {job_identifier}")
            if str(job["source_id"]) != source_id:
                raise IdentityMismatch(f"job does not belong to source: {source_id}")
            connection.execute(
                "INSERT INTO coverage(source_id, scope_id, scope_kind, disposition, "
                "details_json, updated_by_job_id) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(source_id, scope_id) DO UPDATE SET "
                "scope_kind = excluded.scope_kind, disposition = excluded.disposition, "
                "details_json = excluded.details_json, "
                "updated_by_job_id = excluded.updated_by_job_id, "
                "updated_at = CURRENT_TIMESTAMP",
                (
                    source_id,
                    scope_id,
                    scope_kind,
                    disposition,
                    _canonical_json(details),
                    job_identifier,
                ),
            )

    def coverage_for(self, source_id: str) -> tuple[dict[str, Any], ...]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT scope_id, scope_kind, disposition, details_json, updated_by_job_id "
                "FROM coverage WHERE source_id = ? ORDER BY scope_kind, scope_id",
                (source_id,),
            ).fetchall()
        return tuple(
            {
                "scope_id": str(row["scope_id"]),
                "scope_kind": str(row["scope_kind"]),
                "disposition": str(row["disposition"]),
                "details": json.loads(str(row["details_json"])),
                "updated_by_job_id": row["updated_by_job_id"],
            }
            for row in rows
        )

    def record_findings(
        self,
        source_id: str,
        *,
        audit_job_identifier: str,
        findings: Sequence[Mapping[str, Any]],
    ) -> tuple[str, ...]:
        applied: list[str] = []
        with self.connect() as connection:
            job = connection.execute(
                "SELECT source_id FROM jobs WHERE job_id = ?", (audit_job_identifier,)
            ).fetchone()
            if job is None:
                raise MissingIdentity(f"job is not registered: {audit_job_identifier}")
            if str(job["source_id"]) != source_id:
                raise IdentityMismatch(f"job does not belong to source: {source_id}")
            for finding in findings:
                finding_id = str(finding["finding_id"])
                kind = str(finding["kind"])
                payload_json = _canonical_json(finding)
                existing = connection.execute(
                    "SELECT source_id, audit_job_id, kind, payload_json FROM findings "
                    "WHERE finding_id = ?",
                    (finding_id,),
                ).fetchone()
                if existing is not None:
                    stored = (
                        str(existing["source_id"]),
                        str(existing["audit_job_id"]),
                        str(existing["kind"]),
                        str(existing["payload_json"]),
                    )
                    expected = (source_id, audit_job_identifier, kind, payload_json)
                    if stored != expected:
                        raise IdentityMismatch(f"finding identity does not match: {finding_id}")
                    continue
                connection.execute(
                    "INSERT INTO findings(finding_id, source_id, audit_job_id, kind, status, "
                    "payload_json) VALUES (?, ?, ?, ?, 'open', ?)",
                    (finding_id, source_id, audit_job_identifier, kind, payload_json),
                )
                applied.append(finding_id)
        return tuple(applied)

    def findings_for(
        self, source_id: str, *, status: str | None = None
    ) -> tuple[dict[str, Any], ...]:
        query = (
            "SELECT finding_id, audit_job_id, kind, status, payload_json, resolved_at "
            "FROM findings WHERE source_id = ?"
        )
        parameters: tuple[object, ...] = (source_id,)
        if status is not None:
            query += " AND status = ?"
            parameters = (source_id, status)
        query += " ORDER BY created_at, finding_id"
        with self.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return tuple(
            {
                "finding_id": str(row["finding_id"]),
                "audit_job_id": str(row["audit_job_id"]),
                "kind": str(row["kind"]),
                "status": str(row["status"]),
                "payload": json.loads(str(row["payload_json"])),
                "resolved_at": row["resolved_at"],
            }
            for row in rows
        )

    def resolve_open_findings(self, source_id: str) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE findings SET status = 'resolved', resolved_at = CURRENT_TIMESTAMP "
                "WHERE source_id = ? AND status = 'open'",
                (source_id,),
            )
        return cursor.rowcount

    def advance_job(
        self,
        job_identifier: str,
        target: str,
        *,
        run_identifier: str,
        job_kind: str,
        source_id: str,
        attempt: int,
        receipt: Mapping[str, Any],
    ) -> TransitionOutcome:
        if attempt < 1:
            raise ValueError("job attempt must be positive")
        receipt_json = _receipt_json(receipt)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT run_id, source_id, kind, attempt, state FROM jobs WHERE job_id = ?",
                (job_identifier,),
            ).fetchone()
            expected_identity = (run_identifier, source_id, job_kind, attempt)
            if row is not None:
                stored_identity = (
                    str(row["run_id"]),
                    str(row["source_id"]),
                    str(row["kind"]),
                    int(row["attempt"]),
                )
                if stored_identity != expected_identity:
                    raise IdentityMismatch(f"job identity does not match: {job_identifier}")

            run = connection.execute(
                "SELECT source_id FROM runs WHERE run_id = ?", (run_identifier,)
            ).fetchone()
            if run is None:
                raise MissingIdentity(f"run is not registered: {run_identifier}")
            if str(run["source_id"]) != source_id:
                raise IdentityMismatch(
                    f"run {run_identifier} does not belong to source {source_id}"
                )

            current = None if row is None else str(row["state"])
            applied = validate_job_transition(current, target)
            if applied:
                if current is None:
                    connection.execute(
                        "INSERT INTO jobs(job_id, run_id, source_id, kind, attempt, state) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (job_identifier, run_identifier, source_id, job_kind, attempt, target),
                    )
                else:
                    connection.execute(
                        "UPDATE jobs SET state = ?, updated_at = CURRENT_TIMESTAMP "
                        "WHERE job_id = ?",
                        (target, job_identifier),
                    )
            cursor = connection.execute(
                "INSERT INTO transitions(entity_type, entity_id, from_state, to_state, "
                "applied, run_id, job_id, receipt_json) VALUES ('job', ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_identifier,
                    current,
                    target,
                    int(applied),
                    run_identifier,
                    job_identifier,
                    receipt_json,
                ),
            )
            if cursor.lastrowid is None:
                raise sqlite3.DatabaseError("transition insert did not return an identifier")
            transition_id = cursor.lastrowid
        return TransitionOutcome(current, target, applied, transition_id)

    def transitions_for(self, entity_type: str, entity_id: str) -> tuple[sqlite3.Row, ...]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM transitions WHERE entity_type = ? AND entity_id = ? "
                "ORDER BY transition_id",
                (entity_type, entity_id),
            ).fetchall()
        return tuple(rows)

    def source_transitions_for_job(self, job_identifier: str) -> tuple[sqlite3.Row, ...]:
        """Return every ordered source transition that cites one job."""

        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM transitions WHERE entity_type = 'source' AND job_id = ? "
                "ORDER BY transition_id",
                (job_identifier,),
            ).fetchall()
        return tuple(rows)


def _receipt_json(receipt: Mapping[str, Any]) -> str:
    if not receipt:
        raise ValueError("transition receipt must not be empty")
    return _canonical_json(receipt)


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"))


def _normalize_asset(source_id: str, asset: Mapping[str, Any]) -> tuple[Any, ...]:
    required = ("asset_id", "source_id", "role", "path", "sha256", "size_bytes", "metadata")
    if any(key not in asset for key in required):
        raise ValueError("asset registration is incomplete")
    if asset["source_id"] != source_id:
        raise IdentityMismatch(f"asset does not belong to source: {source_id}")
    return (
        str(asset["asset_id"]),
        source_id,
        str(asset["role"]),
        str(asset["path"]),
        str(asset["sha256"]),
        int(asset["size_bytes"]),
        _canonical_json(asset["metadata"]),
    )


def _normalize_snapshot_records(
    source_id: str, records: Sequence[Mapping[str, Any]]
) -> tuple[tuple[str, int, str, str], ...]:
    normalized: list[tuple[str, int, str, str]] = []
    observed: set[str] = set()
    for record in records:
        if record.get("source_id") != source_id:
            raise IdentityMismatch(f"WIP record does not belong to source: {source_id}")
        record_id = str(record.get("id", ""))
        record_type = str(record.get("record_type", ""))
        revision = record.get("revision")
        if (
            not record_id
            or record_id in observed
            or not record_type
            or not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 1
        ):
            raise ValueError("source snapshot contains invalid or duplicate WIP records")
        observed.add(record_id)
        normalized.append((record_id, revision, record_type, _canonical_json(record)))
    return tuple(sorted(normalized, key=lambda item: item[0]))


def _normalize_snapshot_coverage(
    coverage: Sequence[Mapping[str, Any]],
) -> tuple[tuple[str, str, str, str], ...]:
    normalized: list[tuple[str, str, str, str]] = []
    observed: set[str] = set()
    for item in coverage:
        scope_id = str(item.get("scope_id", ""))
        scope_kind = str(item.get("scope_kind", ""))
        disposition = str(item.get("disposition", ""))
        details = item.get("details")
        if (
            not scope_id
            or scope_id in observed
            or not scope_kind
            or not disposition
            or not isinstance(details, dict)
        ):
            raise ValueError("source snapshot contains invalid or duplicate coverage")
        observed.add(scope_id)
        normalized.append((scope_id, scope_kind, disposition, _canonical_json(details)))
    return tuple(sorted(normalized, key=lambda item: (item[1], item[0])))


def _verify_snapshot_replay(
    connection: sqlite3.Connection,
    *,
    source_id: str,
    metadata_json: str,
    assets: tuple[tuple[Any, ...], ...],
    records: tuple[tuple[str, int, str, str], ...],
    coverage: tuple[tuple[str, str, str, str], ...],
    run_identifier: str,
    job_identifier: str,
    receipt_path: str,
    origin_receipt_json: str,
) -> None:
    source = connection.execute(
        "SELECT state, metadata_json FROM sources WHERE source_id = ?", (source_id,)
    ).fetchone()
    if source is None or (str(source["state"]), str(source["metadata_json"])) != (
        "source_complete",
        metadata_json,
    ):
        raise IdentityMismatch(f"destination source snapshot does not match: {source_id}")
    stored_assets = connection.execute(
        "SELECT asset_id, source_id, role, path, sha256, size_bytes, metadata_json "
        "FROM assets WHERE source_id = ? ORDER BY asset_id",
        (source_id,),
    ).fetchall()
    if tuple(tuple(row) for row in stored_assets) != assets:
        raise IdentityMismatch(f"destination source assets do not match: {source_id}")
    stored_records = connection.execute(
        "SELECT record_id, revision, record_type, payload_json FROM wip_records "
        "WHERE source_id = ? AND status = 'active' ORDER BY record_id",
        (source_id,),
    ).fetchall()
    if tuple(tuple(row) for row in stored_records) != records:
        raise IdentityMismatch(f"destination WIP snapshot does not match: {source_id}")
    stored_coverage = connection.execute(
        "SELECT scope_id, scope_kind, disposition, details_json FROM coverage "
        "WHERE source_id = ? ORDER BY scope_kind, scope_id",
        (source_id,),
    ).fetchall()
    if tuple(tuple(row) for row in stored_coverage) != coverage:
        raise IdentityMismatch(f"destination coverage snapshot does not match: {source_id}")
    run = connection.execute(
        "SELECT source_id, requested_through, model FROM runs WHERE run_id = ?",
        (run_identifier,),
    ).fetchone()
    if run is None or tuple(run) != (source_id, "source_complete_import", None):
        raise IdentityMismatch(f"destination import run does not match: {source_id}")
    job = connection.execute(
        "SELECT run_id, source_id, kind, attempt, state, receipt_path FROM jobs WHERE job_id = ?",
        (job_identifier,),
    ).fetchone()
    if job is None or tuple(job) != (
        run_identifier,
        source_id,
        "corpus_snapshot_import",
        1,
        "succeeded",
        receipt_path,
    ):
        raise IdentityMismatch(f"destination import job does not match: {source_id}")
    transitions = connection.execute(
        "SELECT receipt_json FROM transitions WHERE entity_type = 'source' "
        "AND entity_id = ? AND to_state = 'source_complete' AND applied = 1 "
        "ORDER BY transition_id",
        (source_id,),
    ).fetchall()
    import_receipts = []
    for transition in transitions:
        receipt = json.loads(str(transition["receipt_json"]))
        if isinstance(receipt, dict) and receipt.get("kind") == "corpus_snapshot_imported":
            import_receipts.append(receipt)
    if len(import_receipts) != 1:
        raise IdentityMismatch(f"destination import receipt is missing: {source_id}")
    final_receipt = import_receipts[0]
    if (
        not isinstance(final_receipt, dict)
        or final_receipt.get("kind") != "corpus_snapshot_imported"
        or _canonical_json(final_receipt.get("origin", {})) != origin_receipt_json
    ):
        raise IdentityMismatch(f"destination import origin does not match: {source_id}")


def _cross_reference_candidate_row(row: sqlite3.Row) -> dict[str, Any]:
    outcome_json = row["outcome_json"]
    return {
        "candidate_id": str(row["candidate_id"]),
        "batch_id": str(row["batch_id"]),
        "source_pair": (str(row["source_left"]), str(row["source_right"])),
        "left_endpoint": {
            "source_id": str(row["source_left"]),
            "record_id": str(row["left_endpoint_id"]),
            "revision": int(row["left_revision"]),
            "record_sha256": str(row["left_record_sha256"]),
        },
        "right_endpoint": {
            "source_id": str(row["source_right"]),
            "record_id": str(row["right_endpoint_id"]),
            "revision": int(row["right_revision"]),
            "record_sha256": str(row["right_record_sha256"]),
        },
        "comparison_surface": str(row["comparison_surface"]),
        "state": str(row["state"]),
        "discovery_job_id": str(row["discovery_job_id"]),
        "inspection_job_id": row["inspection_job_id"],
        "outcome": None if outcome_json is None else json.loads(str(outcome_json)),
    }
