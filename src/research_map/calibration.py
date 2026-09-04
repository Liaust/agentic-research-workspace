"""Fail-continuing provisional corpus calibration coordination."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from research_map.codex import CodexCapabilityError, CodexRunner
from research_map.compiler import CompilationCoordinator, CompilationError
from research_map.cross_reference import CrossReferenceValidationError
from research_map.cross_reference_loop import (
    CrossReferenceCoordinator,
    CrossReferenceExecutionError,
)
from research_map.map_first import MapFirstReadingCoordinator
from research_map.proposals import ManualReviewRequired, ProposalValidationError
from research_map.reading import ReadingValidationError, WorkerExecutionError
from research_map.receipts import canonical_json_bytes, fingerprint, read_receipt, write_receipt
from research_map.records import RecordError
from research_map.source import (
    SourceResolutionError,
    SourceValidationError,
    resolve_source,
    verify_asset,
)
from research_map.state import IdentityMismatch, MissingIdentity, StateRepository
from research_map.transitions import InvalidTransition
from research_map.validation import DossierValidationError
from research_map.visual import VisualTransportError

CALIBRATION_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
PROVISIONAL_SOURCE_STATE = "calibration_graph_inserted"
CANARY_STOP_FAILURE_CLASSES = frozenset(
    {
        "worker_timeout",
        "worker_start_failed",
        "worker_exit",
        "worker_terminal_failure",
        "worker_execution_failed",
        "event_stream_invalid",
        "transport_without_terminal_event",
        "authoritative_artifact_missing",
        "artifact_schema_invalid",
        "job_receipt_unreadable",
    }
)


class CalibrationValidationError(ValueError):
    """Raised when a calibration command or isolated source result is invalid."""


class CalibrationExecutionError(RuntimeError):
    """Raised when shared runtime or durable state makes continuation unsafe."""


@dataclass(frozen=True, slots=True)
class CalibrationSourceResult:
    source_id: str
    outcome: str
    state: str | None
    record_count: int
    edge_count: int
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    job_ids: tuple[str, ...] = ()
    stage: str = "source_map"
    failure_class: str | None = None
    retry_allowed: bool = False
    duration_seconds: float | None = None
    job_telemetry: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "outcome": self.outcome,
            "state": self.state,
            "record_count": self.record_count,
            "edge_count": self.edge_count,
            "warnings": list(self.warnings),
            "errors": list(self.errors),
            "job_ids": list(self.job_ids),
            "stage": self.stage,
            "failure_class": self.failure_class,
            "retry_allowed": self.retry_allowed,
            "duration_seconds": self.duration_seconds,
            "job_telemetry": list(self.job_telemetry),
        }


@dataclass(frozen=True, slots=True)
class CalibrationRunResult:
    calibration_id: str
    through: str
    root: Path
    state_path: Path
    source_results: tuple[CalibrationSourceResult, ...]
    eligible_source_ids: tuple[str, ...]
    cross_reference: dict[str, Any] | None
    preflight: dict[str, Any]
    canary: dict[str, Any]
    cross_reference_input_manifest: dict[str, Any] | None = None
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "calibration_id": self.calibration_id,
            "projection_status": "provisional_calibration",
            "canonical_promotion": False,
            "through": self.through,
            "root": str(self.root),
            "state_path": str(self.state_path),
            "source_results": [item.to_dict() for item in self.source_results],
            "eligible_source_ids": list(self.eligible_source_ids),
            "source_outcome_counts": _counts(item.outcome for item in self.source_results),
            "cross_reference": self.cross_reference,
            "preflight": self.preflight,
            "canary": self.canary,
            "cross_reference_input_manifest": self.cross_reference_input_manifest,
            "warnings": list(self.warnings),
        }


class CalibrationCoordinator:
    """Run one source-local call per paper, isolate failures, then map usable records."""

    def __init__(
        self,
        *,
        repository_root: Path,
        calibration_id: str,
        codex_executable: str = "codex",
        page_renderer: str = "pdftoppm",
        text_extractor: str = "pdftotext",
        source_timeout_seconds: float = 5400,
        cross_reference_timeout_seconds: float = 5400,
        run_root: Path | None = None,
    ) -> None:
        if not CALIBRATION_ID_PATTERN.fullmatch(calibration_id):
            raise CalibrationValidationError(
                "calibration ID must be a lowercase alphanumeric hyphenated slug"
            )
        self.repository_root = repository_root.resolve()
        self.calibration_id = calibration_id
        default_root = (
            self.repository_root / ".cache" / "research-map" / "calibrations" / calibration_id
        )
        self.root = (run_root or default_root).resolve()
        allowed_root = (self.repository_root / ".cache" / "research-map").resolve()
        if not self.root.is_relative_to(allowed_root):
            raise CalibrationValidationError(
                "calibration run root must remain under .cache/research-map"
            )
        self.state_path = self.root / "state.sqlite3"
        self.cache_root = self.root / "cache"
        self.vault_root = self.root / "vault" / "sources"
        self.relationship_root = self.root / "vault" / "relationships"
        self.build_root = self.root / "build"
        self.codex_executable = codex_executable
        self.page_renderer = page_renderer
        self.text_extractor = text_extractor
        if source_timeout_seconds <= 0 or cross_reference_timeout_seconds <= 0:
            raise CalibrationValidationError("calibration timeouts must be positive")
        self.source_timeout_seconds = source_timeout_seconds
        self.cross_reference_timeout_seconds = cross_reference_timeout_seconds

    def run(
        self,
        source_ids: tuple[str, ...],
        *,
        through: str,
        model: str,
    ) -> CalibrationRunResult:
        sources = tuple(source_id.strip() for source_id in source_ids)
        if not sources or any(not source_id for source_id in sources):
            raise CalibrationValidationError("calibration requires at least one source")
        if len(set(sources)) != len(sources):
            raise CalibrationValidationError("calibration source IDs must be unique")
        if through not in {"source-maps", "cross-reference"}:
            raise CalibrationValidationError("unsupported calibration terminal stage")
        if not model.strip():
            raise CalibrationValidationError("calibration model is required")

        with self._calibration_lease():
            return self._run_locked(sources, through=through, model=model)

    def _run_locked(
        self,
        sources: tuple[str, ...],
        *,
        through: str,
        model: str,
    ) -> CalibrationRunResult:
        try:
            preflight = self._preflight(sources, model=model)
        except CalibrationExecutionError as error:
            self._write_summary(
                through="preflight-failed",
                requested_sources=sources,
                source_results=(),
                eligible=(),
                cross_reference={
                    "outcome": "system_failure",
                    "failure_class": "preflight_failed",
                    "error": str(error),
                },
                warnings=(),
                preflight=self._preflight_summary(),
                canary={"source_id": sources[0], "status": "not_started"},
                cross_reference_input_manifest=None,
            )
            raise

        observed_results: list[CalibrationSourceResult] = []
        canary: dict[str, Any] = {"source_id": sources[0], "status": "pending"}
        for source_index, source_id in enumerate(sources):
            try:
                source_result = self._run_source(source_id, model=model)
                observed_results.append(source_result)
                self._write_source_result(source_result)
            except CalibrationExecutionError as error:
                self._write_summary(
                    through="source-maps-stopped",
                    requested_sources=sources,
                    source_results=tuple(observed_results),
                    eligible=tuple(
                        item.source_id
                        for item in observed_results
                        if item.state == PROVISIONAL_SOURCE_STATE
                        and item.outcome == "provisional_map"
                    ),
                    cross_reference={
                        "outcome": "system_failure",
                        "failure_class": "source_state_failed",
                        "error": str(error),
                    },
                    warnings=(),
                    preflight=preflight,
                    canary=canary,
                    cross_reference_input_manifest=None,
                )
                raise
            if source_index == 0:
                canary = self._canary_result(source_result)
                if canary["status"] == "failed":
                    canary_error = CalibrationExecutionError(
                        "source transport canary failed; remaining semantic calls were not "
                        f"started: {source_id}: {source_result.failure_class}"
                    )
                    self._write_summary(
                        through="source-canary-stopped",
                        requested_sources=sources,
                        source_results=tuple(observed_results),
                        eligible=(),
                        cross_reference={
                            "outcome": "system_failure",
                            "failure_class": source_result.failure_class,
                            "error": str(canary_error),
                        },
                        warnings=(),
                        preflight=preflight,
                        canary=canary,
                        cross_reference_input_manifest=None,
                    )
                    raise canary_error
            self._write_summary(
                through="source-maps-in-progress",
                requested_sources=sources,
                source_results=tuple(observed_results),
                eligible=tuple(
                    item.source_id
                    for item in observed_results
                    if item.state == PROVISIONAL_SOURCE_STATE and item.outcome == "provisional_map"
                ),
                cross_reference=None,
                warnings=(),
                preflight=preflight,
                canary=canary,
                cross_reference_input_manifest=None,
            )
        source_results = tuple(observed_results)
        eligible = tuple(
            item.source_id
            for item in source_results
            if item.state == PROVISIONAL_SOURCE_STATE and item.outcome == "provisional_map"
        )
        warnings: list[str] = []
        cross_reference: dict[str, Any] | None = None
        cross_reference_input_manifest: dict[str, Any] | None = None
        if through == "cross-reference":
            if len(eligible) < 2:
                warnings.append(
                    "holistic cross-reference not started: fewer than two provisional maps"
                )
            else:
                try:
                    cross_reference_input_manifest = self._freeze_cross_reference_inputs(eligible)
                    xref = CrossReferenceCoordinator(
                        repository_root=self.repository_root,
                        state_path=self.state_path,
                        cache_root=self.cache_root,
                        vault_root=self.vault_root,
                        relationship_root=self.relationship_root,
                        build_root=self.build_root,
                        codex_executable=self.codex_executable,
                        eligible_source_states=frozenset({PROVISIONAL_SOURCE_STATE}),
                        job_timeout_seconds=self.cross_reference_timeout_seconds,
                        allow_failed_proposal_revalidation=True,
                    ).run(eligible, through="compiled", model=model)
                    cross_reference = xref.to_dict()
                    cross_reference["batch_id"] = xref.batch_id
                    cross_reference["state"] = xref.state
                except (
                    CalibrationExecutionError,
                    CrossReferenceExecutionError,
                    CrossReferenceValidationError,
                    IdentityMismatch,
                    MissingIdentity,
                    InvalidTransition,
                    OSError,
                    sqlite3.Error,
                ) as error:
                    self._write_summary(
                        through=through,
                        requested_sources=sources,
                        source_results=source_results,
                        eligible=eligible,
                        cross_reference={
                            "outcome": "system_failure",
                            "failure_class": "cross_reference_failed",
                            "error": str(error),
                        },
                        warnings=tuple(warnings),
                        preflight=preflight,
                        canary=canary,
                        cross_reference_input_manifest=cross_reference_input_manifest,
                    )
                    raise CalibrationExecutionError(
                        f"holistic provisional cross-reference stopped: {error}"
                    ) from error

        result = CalibrationRunResult(
            calibration_id=self.calibration_id,
            through=through,
            root=self.root,
            state_path=self.state_path,
            source_results=source_results,
            eligible_source_ids=eligible,
            cross_reference=cross_reference,
            preflight=preflight,
            canary=canary,
            cross_reference_input_manifest=cross_reference_input_manifest,
            warnings=tuple(warnings),
        )
        self._write_summary(
            through=through,
            requested_sources=sources,
            source_results=source_results,
            eligible=eligible,
            cross_reference=cross_reference,
            warnings=tuple(warnings),
            preflight=preflight,
            canary=canary,
            cross_reference_input_manifest=cross_reference_input_manifest,
        )
        return result

    @contextmanager
    def _calibration_lease(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / ".calibration.lock"
        handle = lock_path.open("a+b")
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                holder = _read_lock_metadata(handle)
                raise CalibrationExecutionError(
                    "calibration is already running: "
                    f"{self.calibration_id}; holder={json.dumps(holder, sort_keys=True)}"
                ) from error
            metadata: dict[str, Any] = {
                "kind": "calibration_lease",
                "calibration_id": self.calibration_id,
                "status": "active",
                "pid": os.getpid(),
                "acquired_at": datetime.now(UTC).isoformat(),
            }
            _write_lock_metadata(handle, metadata)
            try:
                yield
            finally:
                metadata["status"] = "released"
                metadata["released_at"] = datetime.now(UTC).isoformat()
                try:
                    _write_lock_metadata(handle, metadata)
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def _preflight(self, sources: tuple[str, ...], *, model: str) -> dict[str, Any]:
        started_at = datetime.now(UTC)
        checks: list[dict[str, Any]] = []
        resolved_sources: list[dict[str, Any]] = []
        receipt_path = self.root / "preflight.json"
        try:
            repository = StateRepository(self.state_path)
            repository.initialize()
            checks.append({"name": "state_database", "status": "passed"})

            for source_id in sources:
                source = resolve_source(source_id, repository_root=self.repository_root)
                assets: list[dict[str, Any]] = []
                for asset in source.assets:
                    verify_asset(asset)
                    assets.append(fingerprint(asset.path).to_dict())
                resolved_sources.append({"source_id": source_id, "assets": assets})
            checks.append(
                {
                    "name": "source_assets",
                    "status": "passed",
                    "source_count": len(resolved_sources),
                    "asset_count": sum(len(item["assets"]) for item in resolved_sources),
                }
            )

            executable_checks = {
                "metadata_probe": _require_executable("pdfinfo"),
                "page_renderer": _require_executable(self.page_renderer),
                "text_extractor": _require_executable(self.text_extractor),
            }
            checks.append({"name": "local_executables", "status": "passed", **executable_checks})

            required_paths = (
                self.repository_root
                / "protocols"
                / "research-map"
                / "experiments"
                / "hierarchical-visual-reader.md",
                self.repository_root
                / "schemas"
                / "research-map"
                / "experiments"
                / "hierarchical-reading-proposal.schema.json",
            )
            missing_paths = [str(path) for path in required_paths if not path.is_file()]
            if missing_paths:
                raise CalibrationExecutionError(
                    "required reader contract is missing: " + ", ".join(missing_paths)
                )
            checks.append(
                {
                    "name": "reader_contracts",
                    "status": "passed",
                    "paths": [fingerprint(path).to_dict() for path in required_paths],
                }
            )

            self.cache_root.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.cache_root, prefix=".preflight-"):
                pass
            disk = shutil.disk_usage(self.root)
            checks.append(
                {
                    "name": "calibration_storage",
                    "status": "passed",
                    "free_bytes": disk.free,
                    "total_bytes": disk.total,
                }
            )

            capabilities = CodexRunner(self.codex_executable).capabilities
            checks.append(
                {
                    "name": "codex_capabilities",
                    "status": "passed",
                    "executable": capabilities.executable,
                    "version": capabilities.version,
                    "model": model,
                }
            )
        except (
            CalibrationExecutionError,
            CodexCapabilityError,
            SourceResolutionError,
            SourceValidationError,
            OSError,
            sqlite3.Error,
            ValueError,
        ) as error:
            failure_payload: dict[str, Any] = {
                "kind": "calibration_preflight",
                "calibration_id": self.calibration_id,
                "status": "failed",
                "failure_class": "preflight_failed",
                "started_at": started_at.isoformat(),
                "finished_at": datetime.now(UTC).isoformat(),
                "requested_source_ids": list(sources),
                "checks": checks,
                "resolved_sources": resolved_sources,
                "errors": [str(error)],
            }
            write_receipt(receipt_path, failure_payload)
            raise CalibrationExecutionError(f"calibration preflight failed: {error}") from error

        success_payload: dict[str, Any] = {
            "kind": "calibration_preflight",
            "calibration_id": self.calibration_id,
            "status": "passed",
            "failure_class": None,
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
            "requested_source_ids": list(sources),
            "checks": checks,
            "resolved_sources": resolved_sources,
            "errors": [],
        }
        write_receipt(receipt_path, success_payload)
        return self._preflight_summary()

    def _preflight_summary(self) -> dict[str, Any]:
        path = self.root / "preflight.json"
        try:
            receipt = read_receipt(path)
        except (OSError, ValueError, json.JSONDecodeError):
            return {"status": "unavailable", "path": str(path)}
        return {
            "status": receipt.get("status"),
            "failure_class": receipt.get("failure_class"),
            "path": str(path),
            "sha256": fingerprint(path).sha256,
        }

    def _canary_result(self, result: CalibrationSourceResult) -> dict[str, Any]:
        stopped = result.failure_class in CANARY_STOP_FAILURE_CLASSES
        return {
            "source_id": result.source_id,
            "status": "failed" if stopped else "passed",
            "source_outcome": result.outcome,
            "failure_class": result.failure_class,
            "transport_passed": not stopped,
        }

    def _run_source(self, source_id: str, *, model: str) -> CalibrationSourceResult:
        started = time.monotonic()
        repository = StateRepository(self.state_path)
        try:
            source = resolve_source(source_id, repository_root=self.repository_root)
            for asset in source.assets:
                verify_asset(asset)
            reading = MapFirstReadingCoordinator(
                repository_root=self.repository_root,
                state_path=self.state_path,
                cache_root=self.cache_root,
                codex_executable=self.codex_executable,
                page_renderer=self.page_renderer,
                text_extractor=self.text_extractor,
                job_timeout_seconds=self.source_timeout_seconds,
            ).run_through_source_complete(
                source_id,
                model=model,
                calibration_tolerant=True,
            )
            compiled = CompilationCoordinator(
                repository_root=self.repository_root,
                state_path=self.state_path,
                cache_root=self.cache_root,
                vault_root=self.vault_root,
                build_root=self.build_root,
            ).run_through_calibration_graph_inserted(source_id, model=model)
            record_count, edge_count = self._projection_counts(source_id)
            telemetry = self._job_telemetry(repository, source_id)
            return CalibrationSourceResult(
                source_id=source_id,
                outcome="provisional_map",
                state=compiled.state,
                record_count=record_count,
                edge_count=edge_count,
                warnings=tuple(dict.fromkeys((*reading.warnings, *compiled.warnings))),
                job_ids=tuple(str(job["job_id"]) for job in repository.jobs_for_source(source_id)),
                duration_seconds=time.monotonic() - started,
                job_telemetry=telemetry,
            )
        except WorkerExecutionError as error:
            return self._failed_source_result(
                repository,
                source_id,
                error,
                started=started,
                stage="source_reading",
                default_failure_class="worker_execution_failed",
            )
        except (
            SourceValidationError,
            IdentityMismatch,
            MissingIdentity,
            InvalidTransition,
            OSError,
            sqlite3.Error,
        ) as error:
            raise CalibrationExecutionError(
                f"source state or immutable identity failed: {source_id}: {error}"
            ) from error
        except (
            ProposalValidationError,
            ReadingValidationError,
            KeyError,
            ValueError,
        ) as error:
            return self._failed_source_result(
                repository,
                source_id,
                error,
                started=started,
                stage="source_validation",
                default_failure_class="deterministic_source_validation_failed",
            )
        except ManualReviewRequired as error:
            return self._failed_source_result(
                repository,
                source_id,
                error,
                started=started,
                stage="source_validation",
                default_failure_class="manual_review_required",
            )
        except VisualTransportError as error:
            return self._failed_source_result(
                repository,
                source_id,
                error,
                started=started,
                stage="source_preparation",
                default_failure_class="visual_transport_failed",
            )
        except SourceResolutionError as error:
            return self._failed_source_result(
                repository,
                source_id,
                error,
                started=started,
                stage="source_resolution",
                default_failure_class="source_resolution_failed",
            )
        except (CompilationError, DossierValidationError, RecordError) as error:
            return self._failed_source_result(
                repository,
                source_id,
                error,
                started=started,
                stage="compilation",
                default_failure_class="compilation_failed",
            )

    def _failed_source_result(
        self,
        repository: StateRepository,
        source_id: str,
        error: Exception,
        *,
        started: float,
        stage: str,
        default_failure_class: str,
    ) -> CalibrationSourceResult:
        telemetry = self._job_telemetry(repository, source_id)
        failure_class = _latest_job_failure_class(telemetry) or default_failure_class
        return CalibrationSourceResult(
            source_id=source_id,
            outcome="source_failed",
            state=repository.source_state(source_id),
            record_count=len(repository.latest_wip_records(source_id)),
            edge_count=0,
            errors=(str(error),),
            job_ids=tuple(str(job["job_id"]) for job in repository.jobs_for_source(source_id)),
            stage=stage,
            failure_class=failure_class,
            retry_allowed=False,
            duration_seconds=time.monotonic() - started,
            job_telemetry=telemetry,
        )

    def _job_telemetry(
        self, repository: StateRepository, source_id: str
    ) -> tuple[dict[str, Any], ...]:
        telemetry: list[dict[str, Any]] = []
        for job in repository.jobs_for_source(source_id):
            item: dict[str, Any] = {
                "job_id": str(job["job_id"]),
                "kind": str(job["kind"]),
                "state": str(job["state"]),
                "receipt_path": job["receipt_path"],
            }
            receipt_path = job["receipt_path"]
            if receipt_path:
                try:
                    receipt = read_receipt(Path(str(receipt_path)))
                except (OSError, ValueError, json.JSONDecodeError) as error:
                    item["failure_class"] = "job_receipt_unreadable"
                    item["receipt_error"] = str(error)
                else:
                    process = receipt.get("process", {})
                    events = receipt.get("events", {})
                    validation = receipt.get("validation", {})
                    item.update(
                        {
                            "returncode": process.get("returncode"),
                            "timed_out": process.get("timed_out"),
                            "duration_seconds": process.get("duration_seconds"),
                            "event_count": events.get("count"),
                            "terminal_event_seen": events.get("terminal_event_seen"),
                            "terminal_event_type": events.get("terminal_event_type"),
                            "failure_class": validation.get("failure_class"),
                            "output_valid": validation.get("valid"),
                        }
                    )
            telemetry.append(item)
        return tuple(telemetry)

    def _write_source_result(self, result: CalibrationSourceResult) -> None:
        write_receipt(
            self.root / "source-results" / f"{result.source_id}.json",
            {
                "kind": "calibration_source_result",
                "calibration_id": self.calibration_id,
                **result.to_dict(),
            },
        )

    def _projection_counts(self, source_id: str) -> tuple[int, int]:
        manifest_path = self.build_root / source_id / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CalibrationExecutionError(
                f"provisional graph manifest is unreadable: {source_id}"
            ) from error
        if (
            not isinstance(manifest, dict)
            or manifest.get("source_id") != source_id
            or manifest.get("projection_status") != "provisional_calibration"
            or manifest.get("canonical_promotion") is not False
        ):
            raise CalibrationExecutionError(
                f"provisional graph manifest identity is invalid: {source_id}"
            )
        return int(manifest["record_count"]), int(manifest["edge_count"])

    def _freeze_cross_reference_inputs(self, eligible: tuple[str, ...]) -> dict[str, Any]:
        projections: list[dict[str, Any]] = []
        for source_id in eligible:
            manifest_path = self.build_root / source_id / "manifest.json"
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise CalibrationExecutionError(
                    f"cross-reference projection manifest is unreadable: {source_id}"
                ) from error
            if (
                not isinstance(manifest, dict)
                or manifest.get("source_id") != source_id
                or manifest.get("projection_status") != "provisional_calibration"
                or manifest.get("canonical_promotion") is not False
            ):
                raise CalibrationExecutionError(
                    f"cross-reference projection identity is invalid: {source_id}"
                )
            artifacts = manifest.get("artifacts")
            if not isinstance(artifacts, list) or not artifacts:
                raise CalibrationExecutionError(
                    f"cross-reference projection artifacts are missing: {source_id}"
                )
            record_count = manifest.get("record_count")
            edge_count = manifest.get("edge_count")
            if (
                not isinstance(record_count, int)
                or isinstance(record_count, bool)
                or record_count < 0
                or not isinstance(edge_count, int)
                or isinstance(edge_count, bool)
                or edge_count < 0
            ):
                raise CalibrationExecutionError(
                    f"cross-reference projection counts are invalid: {source_id}"
                )
            verified_artifacts: list[dict[str, Any]] = []
            for artifact in artifacts:
                if not isinstance(artifact, dict) or not isinstance(artifact.get("path"), str):
                    raise CalibrationExecutionError(
                        f"cross-reference projection artifact is invalid: {source_id}"
                    )
                artifact_path = Path(str(artifact["path"]))
                if not artifact_path.is_absolute():
                    artifact_path = self.repository_root / artifact_path
                artifact_path = artifact_path.resolve()
                try:
                    artifact_path.relative_to(self.repository_root)
                except ValueError as error:
                    raise CalibrationExecutionError(
                        f"cross-reference projection artifact is outside the repository: "
                        f"{source_id}: {artifact['path']}"
                    ) from error
                try:
                    observed = fingerprint(artifact_path)
                except OSError as error:
                    raise CalibrationExecutionError(
                        f"cross-reference projection artifact is unreadable: {source_id}: "
                        f"{artifact['path']}"
                    ) from error
                if observed.sha256 != artifact.get("sha256") or observed.size_bytes != artifact.get(
                    "size_bytes"
                ):
                    raise CalibrationExecutionError(
                        f"cross-reference projection artifact changed: {source_id}: "
                        f"{artifact['path']}"
                    )
                verified_artifacts.append(
                    {
                        "kind": artifact.get("kind"),
                        "path": str(artifact["path"]),
                        "sha256": observed.sha256,
                        "size_bytes": observed.size_bytes,
                    }
                )
            projections.append(
                {
                    "source_id": source_id,
                    "record_count": record_count,
                    "edge_count": edge_count,
                    "projection_manifest": fingerprint(manifest_path).to_dict(),
                    "artifacts": verified_artifacts,
                }
            )

        input_sha256 = hashlib.sha256(canonical_json_bytes(projections)).hexdigest()
        path = self.root / "cross-reference-input-manifest.json"
        write_receipt(
            path,
            {
                "kind": "calibration_cross_reference_input",
                "calibration_id": self.calibration_id,
                "source_ids": list(eligible),
                "source_count": len(eligible),
                "record_count": sum(item["record_count"] for item in projections),
                "edge_count": sum(item["edge_count"] for item in projections),
                "input_sha256": input_sha256,
                "projections": projections,
            },
        )
        return {
            "path": str(path),
            "sha256": fingerprint(path).sha256,
            "input_sha256": input_sha256,
            "source_ids": list(eligible),
            "source_count": len(eligible),
        }

    def _write_summary(
        self,
        *,
        through: str,
        requested_sources: tuple[str, ...],
        source_results: tuple[CalibrationSourceResult, ...],
        eligible: tuple[str, ...],
        cross_reference: dict[str, Any] | None,
        warnings: tuple[str, ...],
        preflight: dict[str, Any],
        canary: dict[str, Any],
        cross_reference_input_manifest: dict[str, Any] | None,
    ) -> None:
        write_receipt(
            self.root / "summary.json",
            {
                "kind": "provisional_corpus_calibration",
                "calibration_id": self.calibration_id,
                "projection_status": "provisional_calibration",
                "canonical_promotion": False,
                "through": through,
                "requested_source_ids": list(requested_sources),
                "attempted_source_ids": [item.source_id for item in source_results],
                "remaining_source_ids": [
                    source_id
                    for source_id in requested_sources
                    if source_id not in {item.source_id for item in source_results}
                ],
                "source_results": [item.to_dict() for item in source_results],
                "eligible_source_ids": list(eligible),
                "source_outcome_counts": _counts(item.outcome for item in source_results),
                "cross_reference": cross_reference,
                "preflight": preflight,
                "canary": canary,
                "cross_reference_input_manifest": cross_reference_input_manifest,
                "warnings": list(warnings),
            },
        )


def _counts(values: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        key = str(value)
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _require_executable(executable: str) -> str:
    candidate = Path(executable)
    if candidate.is_absolute() or os.sep in executable:
        resolved = candidate.expanduser().resolve()
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise CalibrationExecutionError(f"required executable is unavailable: {executable}")
        return str(resolved)
    resolved_name = shutil.which(executable)
    if resolved_name is None:
        raise CalibrationExecutionError(f"required executable is unavailable: {executable}")
    return resolved_name


def _read_lock_metadata(handle: Any) -> dict[str, Any]:
    handle.seek(0)
    try:
        value = json.loads(handle.read().decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"status": "active", "metadata": "unreadable"}
    return value if isinstance(value, dict) else {"status": "active", "metadata": "invalid"}


def _write_lock_metadata(handle: Any, payload: dict[str, Any]) -> None:
    handle.seek(0)
    handle.truncate()
    handle.write(canonical_json_bytes({"schema_version": "1.0", **payload}))
    handle.flush()
    os.fsync(handle.fileno())


def _latest_job_failure_class(telemetry: tuple[dict[str, Any], ...]) -> str | None:
    for job in reversed(telemetry):
        failure_class = job.get("failure_class")
        if isinstance(failure_class, str) and failure_class:
            return failure_class
    return None


def calibration_summary(path: Path) -> dict[str, Any]:
    """Read one versioned summary for agent or human inspection."""

    return read_receipt(path)
