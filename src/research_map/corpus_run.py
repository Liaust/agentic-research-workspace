"""Hash-approved, canary-first orchestration for the frozen paper corpus."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from research_map.calibration import (
    CANARY_STOP_FAILURE_CLASSES,
    PROVISIONAL_SOURCE_STATE,
    CalibrationCoordinator,
    CalibrationSourceResult,
)
from research_map.codex import CodexCapabilityError, check_capabilities
from research_map.receipts import fingerprint, read_receipt, write_receipt
from research_map.source import (
    RegisteredSource,
    SourceResolutionError,
    SourceValidationError,
    resolve_source,
    verify_asset,
)
from research_map.visual import resolve_poppler_executable

CORPUS_RUNNER_VERSION = "1.0"
MANIFEST_SCHEMA = Path("schemas/research-map/v1/corpus-run-manifest.schema.json")
READER_PROTOCOL = Path("protocols/research-map/experiments/hierarchical-visual-reader.md")
READER_PROPOSAL_SCHEMA = Path(
    "schemas/research-map/experiments/hierarchical-reading-proposal.schema.json"
)


class CorpusRunValidationError(ValueError):
    """Raised when the frozen launch contract or requested mode is invalid."""


class CorpusRunExecutionError(RuntimeError):
    """Raised when launch identity or shared runtime state makes execution unsafe."""


@dataclass(frozen=True, slots=True)
class ExcludedSource:
    source_id: str
    page_count: int
    reason: str


@dataclass(frozen=True, slots=True)
class CorpusRunManifest:
    path: Path
    payload: dict[str, Any]
    run_id: str
    projection_status: str
    terminal_stage: str
    reader_mode: str
    model: str
    canary_source_id: str
    source_workers: int
    source_timeout_seconds: int
    semantic_attempts_per_source: int
    automatic_retries: int
    generic_review_calls_per_source: int
    source_ids: tuple[str, ...]
    expected_source_count: int
    expected_asset_count: int
    expected_page_count: int
    excluded_sources: tuple[ExcludedSource, ...]


@dataclass(frozen=True, slots=True)
class CorpusRunResult:
    run_id: str
    root: Path
    launch_plan_path: Path
    launch_plan_sha256: str
    preflight_receipt_path: Path
    source_count: int
    asset_count: int
    page_count: int
    status: str = "ready_for_approval"
    source_results: tuple[CalibrationSourceResult, ...] = ()
    reused_source_ids: tuple[str, ...] = ()
    canary: dict[str, Any] | None = None
    peak_source_concurrency: int = 0
    semantic_jobs_started: int = 0
    worker_events_emitted: int = 0
    summary_path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "projection_status": "provisional_source_map",
            "canonical_promotion": False,
            "status": self.status,
            "root": str(self.root),
            "launch_plan": {
                "path": str(self.launch_plan_path),
                "sha256": self.launch_plan_sha256,
            },
            "preflight_receipt": str(self.preflight_receipt_path),
            "source_count": self.source_count,
            "asset_count": self.asset_count,
            "page_count": self.page_count,
            "source_results": [item.to_dict() for item in self.source_results],
            "reused_source_ids": list(self.reused_source_ids),
            "canary": self.canary,
            "peak_source_concurrency": self.peak_source_concurrency,
            "semantic_jobs_started": self.semantic_jobs_started,
            "worker_events_emitted": self.worker_events_emitted,
            "summary_path": None if self.summary_path is None else str(self.summary_path),
        }


def load_corpus_run_manifest(path: Path, *, repository_root: Path) -> CorpusRunManifest:
    """Load the accepted v1 manifest and enforce its cross-field invariants."""

    manifest_path = path.resolve()
    schema_path = (repository_root / MANIFEST_SCHEMA).resolve()
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CorpusRunValidationError(f"corpus-run manifest is unreadable: {error}") from error
    if not isinstance(payload, dict) or not isinstance(schema, dict):
        raise CorpusRunValidationError("corpus-run manifest and schema must be objects")
    errors = sorted(
        Draft202012Validator(schema).iter_errors(payload), key=lambda item: item.json_path
    )
    if errors:
        first = errors[0]
        raise CorpusRunValidationError(
            f"corpus-run manifest violates {first.json_path}: {first.message}"
        )

    source_ids = tuple(str(value) for value in payload["source_ids"])
    excluded = tuple(
        ExcludedSource(
            source_id=str(item["source_id"]),
            page_count=int(item["page_count"]),
            reason=str(item["reason"]),
        )
        for item in payload["excluded_sources"]
    )
    if source_ids[0] != payload["canary_source_id"]:
        raise CorpusRunValidationError("corpus-run canary must be first in manifest order")
    if len(source_ids) != int(payload["expected_source_count"]):
        raise CorpusRunValidationError("corpus-run expected source count does not match source IDs")
    if len(set(source_ids)) != len(source_ids):
        raise CorpusRunValidationError("corpus-run source IDs must be unique")
    excluded_ids = {item.source_id for item in excluded}
    if excluded_ids.intersection(source_ids):
        raise CorpusRunValidationError("included and excluded corpus sources overlap")
    return CorpusRunManifest(
        path=manifest_path,
        payload=payload,
        run_id=str(payload["run_id"]),
        projection_status=str(payload["projection_status"]),
        terminal_stage=str(payload["terminal_stage"]),
        reader_mode=str(payload["reader_mode"]),
        model=str(payload["model"]),
        canary_source_id=str(payload["canary_source_id"]),
        source_workers=int(payload["source_workers"]),
        source_timeout_seconds=int(payload["source_timeout_seconds"]),
        semantic_attempts_per_source=int(payload["semantic_attempts_per_source"]),
        automatic_retries=int(payload["automatic_retries"]),
        generic_review_calls_per_source=int(payload["generic_review_calls_per_source"]),
        source_ids=source_ids,
        expected_source_count=int(payload["expected_source_count"]),
        expected_asset_count=int(payload["expected_asset_count"]),
        expected_page_count=int(payload["expected_page_count"]),
        excluded_sources=excluded,
    )


class CorpusRunCoordinator:
    """Build the stable launch envelope and enforce approval before source work."""

    def __init__(
        self,
        *,
        repository_root: Path,
        manifest_path: Path,
        codex_executable: str = "codex",
        page_renderer: str = "pdftoppm",
        text_extractor: str = "pdftotext",
        metadata_probe: str = "pdfinfo",
        source_runner: Callable[[str, str], CalibrationSourceResult] | None = None,
    ) -> None:
        self.repository_root = repository_root.resolve()
        self.manifest = load_corpus_run_manifest(
            manifest_path, repository_root=self.repository_root
        )
        self.root = (
            self.repository_root / ".cache" / "research-map" / "corpus-runs" / self.manifest.run_id
        )
        self.launch_plan_path = self.root / "launch-plan.json"
        self.preflight_receipt_path = self.root / "preflight.json"
        self.codex_executable = codex_executable
        self.page_renderer = page_renderer
        self.text_extractor = text_extractor
        self.metadata_probe = metadata_probe
        self.summary_path = self.root / "summary.json"
        self.run_identity_path = self.root / "run-identity.json"
        self._calibration = CalibrationCoordinator(
            repository_root=self.repository_root,
            calibration_id=self.manifest.run_id,
            codex_executable=self.codex_executable,
            page_renderer=self.page_renderer,
            text_extractor=self.text_extractor,
            source_timeout_seconds=self.manifest.source_timeout_seconds,
            run_root=self.root,
        )
        self._source_runner = source_runner or self._run_source

    def run(
        self,
        *,
        preflight_only: bool,
        approved_launch_plan_sha256: str | None = None,
    ) -> CorpusRunResult:
        result = self.preflight()
        if preflight_only:
            if approved_launch_plan_sha256 is not None:
                raise CorpusRunValidationError("preflight-only does not accept an approval SHA-256")
            return result
        if approved_launch_plan_sha256 is None:
            raise CorpusRunValidationError(
                "execution requires --approved-launch-plan-sha256 from preflight"
            )
        if re.fullmatch(r"[0-9a-f]{64}", approved_launch_plan_sha256) is None:
            raise CorpusRunValidationError("approved launch-plan SHA-256 is malformed")
        if approved_launch_plan_sha256 != result.launch_plan_sha256:
            raise CorpusRunValidationError(
                "approved launch-plan SHA-256 does not match the current launch envelope"
            )
        return self._execute_approved(result)

    def preflight(self) -> CorpusRunResult:
        started_at = datetime.now(UTC)
        try:
            launch_plan = self._build_launch_plan()
            launch_fingerprint = self._persist_launch_plan(launch_plan)
        except (
            CodexCapabilityError,
            CorpusRunExecutionError,
            SourceResolutionError,
            SourceValidationError,
            OSError,
            ValueError,
        ) as error:
            write_receipt(
                self.preflight_receipt_path,
                {
                    "kind": "corpus_run_preflight",
                    "run_id": self.manifest.run_id,
                    "status": "failed",
                    "started_at": started_at.isoformat(),
                    "finished_at": datetime.now(UTC).isoformat(),
                    "semantic_jobs_started": 0,
                    "worker_events_emitted": 0,
                    "errors": [str(error)],
                },
            )
            raise CorpusRunExecutionError(f"corpus-run preflight failed: {error}") from error

        receipt = {
            "kind": "corpus_run_preflight",
            "run_id": self.manifest.run_id,
            "status": "passed",
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
            "launch_plan_path": str(self.launch_plan_path),
            "launch_plan_sha256": launch_fingerprint.sha256,
            "source_count": launch_plan["source_count"],
            "asset_count": launch_plan["asset_count"],
            "page_count": launch_plan["page_count"],
            "excluded_source_ids": [item["source_id"] for item in launch_plan["excluded_sources"]],
            "source_workers": self.manifest.source_workers,
            "semantic_jobs_started": 0,
            "worker_events_emitted": 0,
            "errors": [],
        }
        write_receipt(self.preflight_receipt_path, receipt)
        return CorpusRunResult(
            run_id=self.manifest.run_id,
            root=self.root,
            launch_plan_path=self.launch_plan_path,
            launch_plan_sha256=launch_fingerprint.sha256,
            preflight_receipt_path=self.preflight_receipt_path,
            source_count=int(launch_plan["source_count"]),
            asset_count=int(launch_plan["asset_count"]),
            page_count=int(launch_plan["page_count"]),
        )

    def _build_launch_plan(self) -> dict[str, Any]:
        registered_ids = _registered_source_ids(self.repository_root)
        planned_ids = set(self.manifest.source_ids).union(
            item.source_id for item in self.manifest.excluded_sources
        )
        if registered_ids != planned_ids:
            raise CorpusRunExecutionError(
                "manifest does not partition the complete registered corpus: "
                f"planned={sorted(planned_ids)} registered={sorted(registered_ids)}"
            )

        included = tuple(self._resolved_source(source_id) for source_id in self.manifest.source_ids)
        excluded = tuple(
            self._resolved_source(item.source_id) for item in self.manifest.excluded_sources
        )
        asset_count = sum(len(source.assets) for source in included)
        page_count = sum(source.main_asset.pdf_metadata.page_count for source in included)
        if asset_count != self.manifest.expected_asset_count:
            raise CorpusRunExecutionError(
                f"included asset count changed: expected={self.manifest.expected_asset_count} "
                f"observed={asset_count}"
            )
        if page_count != self.manifest.expected_page_count:
            raise CorpusRunExecutionError(
                f"included page count changed: expected={self.manifest.expected_page_count} "
                f"observed={page_count}"
            )
        excluded_by_id = {source.source_id: source for source in excluded}
        for item in self.manifest.excluded_sources:
            observed_pages = excluded_by_id[item.source_id].main_asset.pdf_metadata.page_count
            if observed_pages != item.page_count:
                raise CorpusRunExecutionError(
                    f"excluded source page count changed: {item.source_id}: "
                    f"expected={item.page_count} observed={observed_pages}"
                )

        capabilities = check_capabilities(self.codex_executable)
        contracts = tuple(
            _stable_file_fingerprint(self.repository_root, relative_path)
            for relative_path in (MANIFEST_SCHEMA, READER_PROTOCOL, READER_PROPOSAL_SCHEMA)
        )
        return {
            "kind": "corpus_run_launch_plan",
            "contract_version": CORPUS_RUNNER_VERSION,
            "run_id": self.manifest.run_id,
            "projection_status": self.manifest.projection_status,
            "canonical_promotion": False,
            "terminal_stage": self.manifest.terminal_stage,
            "reader_mode": self.manifest.reader_mode,
            "model": self.manifest.model,
            "canary_source_id": self.manifest.canary_source_id,
            "source_workers": self.manifest.source_workers,
            "source_timeout_seconds": self.manifest.source_timeout_seconds,
            "semantic_attempts_per_source": self.manifest.semantic_attempts_per_source,
            "automatic_retries": self.manifest.automatic_retries,
            "generic_review_calls_per_source": self.manifest.generic_review_calls_per_source,
            "cross_reference": self.manifest.payload["cross_reference"],
            "manifest": _stable_file_fingerprint(
                self.repository_root,
                self.manifest.path.relative_to(self.repository_root),
            ),
            "contracts": list(contracts),
            "tools": {
                "codex": {
                    "command": self.codex_executable,
                    "version": capabilities.version,
                    "required_options": sorted(capabilities.options),
                },
                "metadata_probe": _tool_descriptor(self.metadata_probe),
                "page_renderer": _tool_descriptor(self.page_renderer),
                "text_extractor": _tool_descriptor(self.text_extractor),
            },
            "source_count": len(included),
            "asset_count": asset_count,
            "page_count": page_count,
            "source_ids": list(self.manifest.source_ids),
            "sources": [_stable_source_record(source, self.repository_root) for source in included],
            "excluded_sources": [
                {
                    **_stable_source_record(source, self.repository_root),
                    "reason": next(
                        item.reason
                        for item in self.manifest.excluded_sources
                        if item.source_id == source.source_id
                    ),
                }
                for source in excluded
            ],
        }

    def _resolved_source(self, source_id: str) -> RegisteredSource:
        source = resolve_source(source_id, repository_root=self.repository_root)
        for asset in source.assets:
            verify_asset(asset)
        return source

    def _execute_approved(self, preflight: CorpusRunResult) -> CorpusRunResult:
        launch_sha256 = preflight.launch_plan_sha256
        with self._calibration._calibration_lease():
            preflight = self.preflight()
            if preflight.launch_plan_sha256 != launch_sha256:
                raise CorpusRunExecutionError(
                    "launch envelope drifted after approval and before the canary"
                )
            self._bind_run_identity(launch_sha256)
            existing = self._load_existing_results(launch_sha256)
            reused = tuple(
                source_id for source_id in self.manifest.source_ids if source_id in existing
            )
            invocation = _InvocationCounters()

            canary_result = existing.get(self.manifest.canary_source_id)
            if canary_result is None:
                try:
                    canary_result = self._invoke_source(
                        self.manifest.canary_source_id,
                        invocation=invocation,
                    )
                except Exception as error:
                    canary = {
                        "source_id": self.manifest.canary_source_id,
                        "status": "failed",
                        "source_outcome": "system_failure",
                        "failure_class": "source_execution_failed",
                        "transport_passed": False,
                    }
                    result = self._execution_result(
                        preflight,
                        existing=existing,
                        reused=reused,
                        canary=canary,
                        invocation=invocation,
                        status="source_canary_stopped",
                    )
                    self._write_summary(result, errors=(str(error),))
                    raise CorpusRunExecutionError(
                        "source transport canary failed before a terminal result: "
                        f"{self.manifest.canary_source_id}: {error}"
                    ) from error
                self._write_source_result(canary_result, launch_sha256)
                existing[canary_result.source_id] = canary_result
            canary = _canary_summary(canary_result)
            if canary["status"] == "failed":
                result = self._execution_result(
                    preflight,
                    existing=existing,
                    reused=reused,
                    canary=canary,
                    invocation=invocation,
                    status="source_canary_stopped",
                )
                self._write_summary(
                    result,
                    errors=(
                        "source transport canary failed; remaining semantic jobs were not started",
                    ),
                )
                raise CorpusRunExecutionError(
                    "source transport canary failed; remaining semantic jobs were not started: "
                    f"{canary_result.source_id}: {canary_result.failure_class}"
                )

            pending = tuple(
                source_id for source_id in self.manifest.source_ids[1:] if source_id not in existing
            )
            corpus_error = self._run_post_canary(
                pending,
                existing=existing,
                launch_sha256=launch_sha256,
                invocation=invocation,
                preflight=preflight,
                reused=reused,
                canary=canary,
            )
            status = "source_maps_complete" if corpus_error is None else "source_maps_stopped"
            result = self._execution_result(
                preflight,
                existing=existing,
                reused=reused,
                canary=canary,
                invocation=invocation,
                status=status,
            )
            self._write_summary(result, errors=() if corpus_error is None else (str(corpus_error),))
            if corpus_error is not None:
                raise CorpusRunExecutionError(
                    f"corpus run stopped: {corpus_error}"
                ) from corpus_error
            return result

    def _run_post_canary(
        self,
        pending_source_ids: tuple[str, ...],
        *,
        existing: dict[str, CalibrationSourceResult],
        launch_sha256: str,
        invocation: _InvocationCounters,
        preflight: CorpusRunResult,
        reused: tuple[str, ...],
        canary: dict[str, Any],
    ) -> Exception | None:
        if not pending_source_ids:
            return None
        source_order = {
            source_id: index for index, source_id in enumerate(self.manifest.source_ids)
        }
        source_iterator = iter(pending_source_ids)
        corpus_error: Exception | None = None
        futures: dict[Future[CalibrationSourceResult], str] = {}
        with ThreadPoolExecutor(
            max_workers=self.manifest.source_workers,
            thread_name_prefix="research-map-source",
        ) as executor:
            for _ in range(self.manifest.source_workers):
                source_id = next(source_iterator, None)
                if source_id is not None:
                    futures[
                        executor.submit(self._invoke_source, source_id, invocation=invocation)
                    ] = source_id
            while futures:
                done, _ = wait(futures, return_when=FIRST_COMPLETED)
                for future in sorted(done, key=lambda item: source_order[futures[item]]):
                    source_id = futures.pop(future)
                    try:
                        source_result = future.result()
                        self._validate_source_result(source_result, expected_source_id=source_id)
                    except Exception as error:
                        corpus_error = error
                        continue
                    self._write_source_result(source_result, launch_sha256)
                    existing[source_id] = source_result
                    in_progress = self._execution_result(
                        preflight,
                        existing=existing,
                        reused=reused,
                        canary=canary,
                        invocation=invocation,
                        status="source_maps_in_progress",
                    )
                    self._write_summary(in_progress, errors=())
                if corpus_error is None:
                    while len(futures) < self.manifest.source_workers:
                        source_id = next(source_iterator, None)
                        if source_id is None:
                            break
                        futures[
                            executor.submit(
                                self._invoke_source,
                                source_id,
                                invocation=invocation,
                            )
                        ] = source_id
        return corpus_error

    def _invoke_source(
        self,
        source_id: str,
        *,
        invocation: _InvocationCounters,
    ) -> CalibrationSourceResult:
        with invocation.source_job():
            result = self._source_runner(source_id, self.manifest.model)
        invocation.record_worker_events(result)
        self._validate_source_result(result, expected_source_id=source_id)
        return result

    def _run_source(self, source_id: str, model: str) -> CalibrationSourceResult:
        return self._calibration._run_source(source_id, model=model)

    def _bind_run_identity(self, launch_sha256: str) -> None:
        payload = {
            "kind": "corpus_run_identity",
            "run_id": self.manifest.run_id,
            "launch_plan_sha256": launch_sha256,
            "projection_status": self.manifest.projection_status,
            "canonical_promotion": False,
        }
        if self.run_identity_path.exists():
            if read_receipt(self.run_identity_path) != {"schema_version": "1.0", **payload}:
                raise CorpusRunExecutionError("corpus run identity does not match prior execution")
            return
        write_receipt(self.run_identity_path, payload)

    def _load_existing_results(self, launch_sha256: str) -> dict[str, CalibrationSourceResult]:
        results: dict[str, CalibrationSourceResult] = {}
        result_root = self.root / "source-results"
        if not result_root.exists():
            return results
        expected_ids = set(self.manifest.source_ids)
        for path in sorted(result_root.glob("*.json")):
            try:
                receipt = read_receipt(path)
                source_id = str(receipt["source_id"])
                if (
                    receipt.get("kind") != "corpus_run_source_result"
                    or receipt.get("run_id") != self.manifest.run_id
                    or receipt.get("launch_plan_sha256") != launch_sha256
                    or source_id not in expected_ids
                    or path.name != f"{source_id}.json"
                ):
                    raise CorpusRunExecutionError(f"source result identity is invalid: {path}")
                result = _source_result_from_receipt(receipt)
                self._validate_source_result(result, expected_source_id=source_id)
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
                raise CorpusRunExecutionError(
                    f"source result is unreadable or unsafe to resume: {path}: {error}"
                ) from error
            if source_id in results:
                raise CorpusRunExecutionError(f"duplicate terminal source result: {source_id}")
            results[source_id] = result
        return results

    def _write_source_result(
        self,
        result: CalibrationSourceResult,
        launch_sha256: str,
    ) -> None:
        path = self.root / "source-results" / f"{result.source_id}.json"
        payload = {
            "kind": "corpus_run_source_result",
            "run_id": self.manifest.run_id,
            "launch_plan_sha256": launch_sha256,
            **result.to_dict(),
        }
        if path.exists():
            if read_receipt(path) != {"schema_version": "1.0", **payload}:
                raise CorpusRunExecutionError(
                    f"terminal source result changed on replay: {result.source_id}"
                )
            return
        write_receipt(path, payload)

    def _validate_source_result(
        self,
        result: CalibrationSourceResult,
        *,
        expected_source_id: str,
    ) -> None:
        if result.source_id != expected_source_id:
            raise CorpusRunExecutionError(
                f"source runner returned the wrong identity: expected={expected_source_id} "
                f"observed={result.source_id}"
            )
        if result.outcome not in {"provisional_map", "source_failed"}:
            raise CorpusRunExecutionError(
                f"source runner returned a nonterminal outcome: {result.source_id}: "
                f"{result.outcome}"
            )
        if result.record_count < 0 or result.edge_count < 0:
            raise CorpusRunExecutionError(f"source result counts are invalid: {result.source_id}")
        if result.outcome == "provisional_map" and result.state != PROVISIONAL_SOURCE_STATE:
            raise CorpusRunExecutionError(
                f"provisional source result has the wrong state: {result.source_id}: {result.state}"
            )
        if result.outcome == "source_failed" and not result.failure_class:
            raise CorpusRunExecutionError(
                f"failed source result has no stable failure class: {result.source_id}"
            )
        if result.retry_allowed:
            raise CorpusRunExecutionError(
                f"source result incorrectly permits automatic retry: {result.source_id}"
            )

    def _execution_result(
        self,
        preflight: CorpusRunResult,
        *,
        existing: dict[str, CalibrationSourceResult],
        reused: tuple[str, ...],
        canary: dict[str, Any],
        invocation: _InvocationCounters,
        status: str,
    ) -> CorpusRunResult:
        ordered_results = tuple(
            existing[source_id] for source_id in self.manifest.source_ids if source_id in existing
        )
        return CorpusRunResult(
            run_id=preflight.run_id,
            root=preflight.root,
            launch_plan_path=preflight.launch_plan_path,
            launch_plan_sha256=preflight.launch_plan_sha256,
            preflight_receipt_path=preflight.preflight_receipt_path,
            source_count=preflight.source_count,
            asset_count=preflight.asset_count,
            page_count=preflight.page_count,
            status=status,
            source_results=ordered_results,
            reused_source_ids=reused,
            canary=canary,
            peak_source_concurrency=invocation.peak,
            semantic_jobs_started=invocation.started,
            worker_events_emitted=invocation.worker_events,
            summary_path=self.summary_path,
        )

    def _write_summary(
        self,
        result: CorpusRunResult,
        *,
        errors: tuple[str, ...],
    ) -> None:
        result_by_source = {item.source_id: item for item in result.source_results}
        write_receipt(
            self.summary_path,
            {
                "kind": "provisional_corpus_run",
                "run_id": result.run_id,
                "projection_status": self.manifest.projection_status,
                "canonical_promotion": False,
                "status": result.status,
                "launch_plan_sha256": result.launch_plan_sha256,
                "requested_source_ids": list(self.manifest.source_ids),
                "terminal_source_ids": [
                    source_id
                    for source_id in self.manifest.source_ids
                    if source_id in result_by_source
                ],
                "remaining_source_ids": [
                    source_id
                    for source_id in self.manifest.source_ids
                    if source_id not in result_by_source
                ],
                "source_results": [item.to_dict() for item in result.source_results],
                "source_outcome_counts": _counts(item.outcome for item in result.source_results),
                "reused_source_ids": list(result.reused_source_ids),
                "semantic_jobs_started_this_invocation": result.semantic_jobs_started,
                "peak_source_concurrency_this_invocation": result.peak_source_concurrency,
                "source_workers": self.manifest.source_workers,
                "canary": result.canary,
                "cross_reference": self.manifest.payload["cross_reference"],
                "errors": list(errors),
            },
        )

    def _persist_launch_plan(self, launch_plan: dict[str, Any]) -> Any:
        if self.launch_plan_path.exists():
            existing = read_receipt(self.launch_plan_path)
            expected = {"schema_version": "1.0", **launch_plan}
            if existing != expected:
                raise CorpusRunExecutionError(
                    "immutable corpus run ID already has a different launch plan"
                )
            return fingerprint(self.launch_plan_path)
        return write_receipt(self.launch_plan_path, launch_plan)


def _registered_source_ids(repository_root: Path) -> set[str]:
    source_ids: set[str] = set()
    for path in sorted((repository_root / "sources" / "library").glob("*/source.yaml")):
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as error:
            raise CorpusRunExecutionError(f"source registration is unreadable: {path}") from error
        source_id = payload.get("library_id") if isinstance(payload, dict) else None
        if not isinstance(source_id, str) or not source_id:
            raise CorpusRunExecutionError(f"source registration has no library ID: {path}")
        if source_id in source_ids:
            raise CorpusRunExecutionError(f"duplicate source registration: {source_id}")
        source_ids.add(source_id)
    return source_ids


def _stable_source_record(source: RegisteredSource, repository_root: Path) -> dict[str, Any]:
    return {
        "source_id": source.source_id,
        "source_yaml": {
            "path": source.source_yaml_path.relative_to(repository_root).as_posix(),
            "sha256": source.source_yaml_sha256,
        },
        "main_asset_id": source.main_asset.asset_id,
        "main_page_count": source.main_asset.pdf_metadata.page_count,
        "assets": [
            {
                "asset_id": asset.asset_id,
                "role": asset.role,
                "path": asset.relative_path,
                "sha256": asset.sha256,
                "size_bytes": asset.size_bytes,
                "page_count": asset.pdf_metadata.page_count,
                "encrypted": asset.pdf_metadata.encrypted,
            }
            for asset in source.assets
        ],
    }


def _stable_file_fingerprint(repository_root: Path, relative_path: Path) -> dict[str, Any]:
    path = (repository_root / relative_path).resolve()
    return fingerprint(path, relative_to=repository_root).to_dict()


def _tool_descriptor(executable: str) -> dict[str, str]:
    resolved = shutil.which(resolve_poppler_executable(executable))
    if resolved is None:
        raise CorpusRunExecutionError(f"required executable is unavailable: {executable}")
    completed = subprocess.run([resolved, "-v"], check=False, capture_output=True, text=True)
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
    version = next((line.strip() for line in output.splitlines() if line.strip()), "")
    if completed.returncode != 0 or not version:
        raise CorpusRunExecutionError(f"required executable version check failed: {executable}")
    return {"command": executable, "version": version}


class _InvocationCounters:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.started = 0
        self.worker_events = 0

    def source_job(self) -> _SourceJobCounter:
        return _SourceJobCounter(self)

    def record_worker_events(self, result: CalibrationSourceResult) -> None:
        observed = sum(
            int(event_count)
            for job in result.job_telemetry
            if isinstance((event_count := job.get("event_count")), int)
            and not isinstance(event_count, bool)
            and event_count >= 0
        )
        with self._lock:
            self.worker_events += observed


class _SourceJobCounter:
    def __init__(self, counters: _InvocationCounters) -> None:
        self.counters = counters

    def __enter__(self) -> None:
        with self.counters._lock:
            self.counters.active += 1
            self.counters.started += 1
            self.counters.peak = max(self.counters.peak, self.counters.active)

    def __exit__(self, *_args: object) -> None:
        with self.counters._lock:
            self.counters.active -= 1


def _canary_summary(result: CalibrationSourceResult) -> dict[str, Any]:
    stopped = result.failure_class in CANARY_STOP_FAILURE_CLASSES
    return {
        "source_id": result.source_id,
        "status": "failed" if stopped else "passed",
        "source_outcome": result.outcome,
        "failure_class": result.failure_class,
        "transport_passed": not stopped,
    }


def _source_result_from_receipt(receipt: dict[str, Any]) -> CalibrationSourceResult:
    source_id = _required_string(receipt, "source_id")
    outcome = _required_string(receipt, "outcome")
    state_value = receipt.get("state")
    if state_value is not None and not isinstance(state_value, str):
        raise TypeError("source result state must be a string or null")
    record_count = _nonnegative_integer(receipt, "record_count")
    edge_count = _nonnegative_integer(receipt, "edge_count")
    warnings = _string_list(receipt, "warnings")
    errors = _string_list(receipt, "errors")
    job_ids = _string_list(receipt, "job_ids")
    stage = _required_string(receipt, "stage")
    failure_value = receipt.get("failure_class")
    if failure_value is not None and not isinstance(failure_value, str):
        raise TypeError("source result failure class must be a string or null")
    retry_allowed = receipt.get("retry_allowed")
    if not isinstance(retry_allowed, bool):
        raise TypeError("source result retry posture must be boolean")
    duration_value = receipt.get("duration_seconds")
    if duration_value is not None and (
        not isinstance(duration_value, (int, float))
        or isinstance(duration_value, bool)
        or duration_value < 0
    ):
        raise TypeError("source result duration must be a nonnegative number or null")
    telemetry_value = receipt.get("job_telemetry")
    if not isinstance(telemetry_value, list) or not all(
        isinstance(item, dict) for item in telemetry_value
    ):
        raise TypeError("source result telemetry must be an object array")
    return CalibrationSourceResult(
        source_id=source_id,
        outcome=outcome,
        state=state_value,
        record_count=record_count,
        edge_count=edge_count,
        warnings=warnings,
        errors=errors,
        job_ids=job_ids,
        stage=stage,
        failure_class=failure_value,
        retry_allowed=retry_allowed,
        duration_seconds=None if duration_value is None else float(duration_value),
        job_telemetry=tuple(dict(item) for item in telemetry_value),
    )


def _required_string(receipt: dict[str, Any], key: str) -> str:
    value = receipt.get(key)
    if not isinstance(value, str) or not value:
        raise TypeError(f"source result {key} must be a non-empty string")
    return value


def _nonnegative_integer(receipt: dict[str, Any], key: str) -> int:
    value = receipt.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise TypeError(f"source result {key} must be a nonnegative integer")
    return value


def _string_list(receipt: dict[str, Any], key: str) -> tuple[str, ...]:
    value = receipt.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError(f"source result {key} must be a string array")
    return tuple(value)


def _counts(values: Iterable[object]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        key = str(value)
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))
