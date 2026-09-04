"""Canonical insertion and deterministic source-local graph compilation."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from research_map.graph import GraphCompilation, compile_graph
from research_map.ids import job_id, run_id
from research_map.markdown import parse_markdown
from research_map.reading import validate_source_wip
from research_map.receipts import canonical_json_bytes, read_receipt, write_receipt
from research_map.render import dossier_from_validated_wip, render_canonical_dossier
from research_map.source import RegisteredSource, resolve_source, verify_asset
from research_map.state import StateRepository

COMPILER_VERSION = "1.0.0"
COMPILER_JOB_KIND = "graph_compile"
CALIBRATION_COMPILER_JOB_KIND = "calibration_graph_compile"


class CompilationError(ValueError):
    """Raised when canonical or graph insertion cannot validate deterministically."""


@dataclass(frozen=True, slots=True)
class CompilationResult:
    source_id: str
    run_id: str
    job_id: str
    state: str
    artifacts: tuple[dict[str, Any], ...]
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "artifacts": list(self.artifacts),
        }


@dataclass(frozen=True, slots=True)
class _Materialization:
    canonical_path: Path
    graph_path: Path
    context_path: Path
    manifest_path: Path
    canonical_bytes: bytes
    graph_bytes: bytes
    context_bytes: bytes
    manifest_bytes: bytes
    artifacts: tuple[dict[str, Any], ...]
    graph: GraphCompilation


class CompilationCoordinator:
    def __init__(
        self,
        *,
        repository_root: Path,
        state_path: Path,
        cache_root: Path | None = None,
        vault_root: Path | None = None,
        build_root: Path | None = None,
    ) -> None:
        self.repository_root = repository_root.resolve()
        self.state_path = state_path
        self.state = StateRepository(state_path)
        self.cache_root = cache_root or self.repository_root / ".cache" / "research-map"
        self.vault_root = vault_root or self.repository_root / "vault" / "sources"
        self.build_root = build_root or self.repository_root / "build" / "research-map"
        self.schema_directory = self.repository_root / "schemas" / "research-map" / "v1"

    def run_through_graph_inserted(
        self,
        source_id: str,
        *,
        model: str,
        replace_existing: bool = False,
    ) -> CompilationResult:
        if not model.strip():
            raise CompilationError("graph insertion requires an explicit model identity")
        self.state.initialize()
        source = resolve_source(source_id, repository_root=self.repository_root)
        verify_asset(source.main_asset)
        current_state = self.state.source_state(source_id)
        if current_state == "graph_inserted":
            return self._replay(source, model=model)
        if current_state != "audit_passed":
            raise CompilationError("source must be audit_passed before graph insertion")

        run_identifier = self._run_identifier(source_id, model)
        self.state.register_run(
            run_identifier,
            source_id=source_id,
            requested_through="graph_inserted",
            model=model,
        )
        job_identifier = job_id(run_identifier, COMPILER_JOB_KIND, 1)
        self._start_job(
            job_identifier,
            run_identifier=run_identifier,
            source_id=source_id,
            model=model,
        )
        receipt_path = (
            self.cache_root / "runs" / run_identifier / "receipts" / f"{job_identifier}.json"
        )
        try:
            materialization = self._materialization(
                source,
                run_identifier=run_identifier,
                job_identifier=job_identifier,
            )
            prior_artifacts: tuple[dict[str, Any], ...] = ()
            if replace_existing and any(
                path.exists() for path, _ in _materialized_files(materialization)
            ):
                prior_artifacts = self._verify_existing_projection(source_id)
                self._replace_materialization(materialization)
            else:
                self._write_materialization(materialization)
            receipt_fingerprint = write_receipt(
                receipt_path,
                {
                    "kind": "graph_compilation",
                    "source_id": source_id,
                    "run_id": run_identifier,
                    "job_id": job_identifier,
                    "model": model,
                    "asset_fingerprint": _asset_fingerprint(source),
                    "replacement_authorized": replace_existing,
                    "replaced_artifacts": list(prior_artifacts),
                    "artifacts": list(materialization.artifacts),
                    "validation": {
                        "canonical_round_trip": True,
                        "graph_schema": True,
                        "context_schema": True,
                        "byte_equivalent_rebuild": True,
                        "record_count": len(materialization.graph.graph["nodes"]),
                        "edge_count": len(materialization.graph.graph["edges"]),
                        "context_envelope_count": len(materialization.graph.context_envelopes),
                    },
                },
            )
            self.state.set_job_receipt(job_identifier, str(receipt_path))
            self.state.advance_job(
                job_identifier,
                "succeeded",
                run_identifier=run_identifier,
                source_id=source_id,
                job_kind=COMPILER_JOB_KIND,
                attempt=1,
                receipt={
                    "kind": "graph_compilation_succeeded",
                    "receipt": receipt_fingerprint.to_dict(),
                    "artifacts": list(materialization.artifacts),
                    "replacement_authorized": replace_existing,
                    "replaced_artifacts": list(prior_artifacts),
                },
            )
            self.state.advance_source(
                source_id,
                "graph_inserted",
                run_identifier=run_identifier,
                job_identifier=job_identifier,
                receipt={
                    "kind": "graph_inserted",
                    "canonical": materialization.artifacts[0],
                    "graph_manifest": materialization.artifacts[-1],
                    "byte_equivalent_rebuild": True,
                    "replacement_authorized": replace_existing,
                    "replaced_artifacts": list(prior_artifacts),
                },
            )
        except (OSError, ValueError) as error:
            current_job = self.state.job_record(job_identifier)
            if current_job is not None and current_job["state"] == "running":
                self.state.advance_job(
                    job_identifier,
                    "validation_failed",
                    run_identifier=run_identifier,
                    source_id=source_id,
                    job_kind=COMPILER_JOB_KIND,
                    attempt=1,
                    receipt={"kind": "graph_compilation_failed", "error": str(error)},
                )
            if isinstance(error, CompilationError):
                raise
            raise CompilationError(str(error)) from error

        verify_asset(source.main_asset)
        return CompilationResult(
            source_id=source_id,
            run_id=run_identifier,
            job_id=job_identifier,
            state=self.state.source_state(source_id) or "audit_passed",
            artifacts=materialization.artifacts,
        )

    def run_through_calibration_graph_inserted(
        self,
        source_id: str,
        *,
        model: str,
    ) -> CompilationResult:
        """Compile a mechanically valid WIP into an isolated provisional projection."""

        if not model.strip():
            raise CompilationError("calibration graph insertion requires a model identity")
        self.state.initialize()
        source = resolve_source(source_id, repository_root=self.repository_root)
        verify_asset(source.main_asset)
        current_state = self.state.source_state(source_id)
        if current_state == "calibration_graph_inserted":
            return self._calibration_replay(source, model=model)
        if current_state == "source_complete":
            self.state.advance_source(
                source_id,
                "calibration_ready",
                receipt={
                    "kind": "provisional_calibration_ready",
                    "canonical_promotion": False,
                    "source_audit_bypassed": True,
                },
            )
            current_state = "calibration_ready"
        if current_state != "calibration_ready":
            raise CompilationError(
                "source must be source_complete or calibration_ready before provisional "
                "graph insertion"
            )

        run_identifier = self._run_identifier(source_id, model, target="calibration_graph_inserted")
        self.state.register_run(
            run_identifier,
            source_id=source_id,
            requested_through="calibration_graph_inserted",
            model=model,
        )
        job_identifier = job_id(run_identifier, CALIBRATION_COMPILER_JOB_KIND, 1)
        self._start_job(
            job_identifier,
            run_identifier=run_identifier,
            source_id=source_id,
            model=model,
            job_kind=CALIBRATION_COMPILER_JOB_KIND,
            receipt_kind="provisional_graph_compilation",
        )
        receipt_path = (
            self.cache_root / "runs" / run_identifier / "receipts" / f"{job_identifier}.json"
        )
        try:
            materialization = self._materialization(
                source,
                run_identifier=run_identifier,
                job_identifier=job_identifier,
                projection_status="provisional_calibration",
            )
            self._write_materialization(materialization)
            receipt_fingerprint = write_receipt(
                receipt_path,
                {
                    "kind": "provisional_calibration_graph_compilation",
                    "source_id": source_id,
                    "run_id": run_identifier,
                    "job_id": job_identifier,
                    "model": model,
                    "projection_status": "provisional_calibration",
                    "canonical_promotion": False,
                    "asset_fingerprint": _asset_fingerprint(source),
                    "artifacts": list(materialization.artifacts),
                    "validation": {
                        "canonical_round_trip": True,
                        "graph_schema": True,
                        "context_schema": True,
                        "byte_equivalent_rebuild": True,
                        "record_count": len(materialization.graph.graph["nodes"]),
                        "edge_count": len(materialization.graph.graph["edges"]),
                        "context_envelope_count": len(materialization.graph.context_envelopes),
                    },
                },
            )
            self.state.set_job_receipt(job_identifier, str(receipt_path))
            self.state.advance_job(
                job_identifier,
                "succeeded",
                run_identifier=run_identifier,
                source_id=source_id,
                job_kind=CALIBRATION_COMPILER_JOB_KIND,
                attempt=1,
                receipt={
                    "kind": "provisional_graph_compilation_succeeded",
                    "receipt": receipt_fingerprint.to_dict(),
                    "artifacts": list(materialization.artifacts),
                },
            )
            self.state.advance_source(
                source_id,
                "calibration_graph_inserted",
                run_identifier=run_identifier,
                job_identifier=job_identifier,
                receipt={
                    "kind": "provisional_calibration_graph_inserted",
                    "canonical_promotion": False,
                    "source_audit_bypassed": True,
                    "projection_status": "provisional_calibration",
                    "projection": materialization.artifacts[0],
                    "graph_manifest": materialization.artifacts[-1],
                },
            )
        except (OSError, ValueError) as error:
            current_job = self.state.job_record(job_identifier)
            if current_job is not None and current_job["state"] == "running":
                self.state.advance_job(
                    job_identifier,
                    "validation_failed",
                    run_identifier=run_identifier,
                    source_id=source_id,
                    job_kind=CALIBRATION_COMPILER_JOB_KIND,
                    attempt=1,
                    receipt={
                        "kind": "provisional_graph_compilation_failed",
                        "error": str(error),
                    },
                )
            if isinstance(error, CompilationError):
                raise
            raise CompilationError(str(error)) from error

        verify_asset(source.main_asset)
        return CompilationResult(
            source_id=source_id,
            run_id=run_identifier,
            job_id=job_identifier,
            state="calibration_graph_inserted",
            artifacts=materialization.artifacts,
            warnings=("provisional calibration projection; source audit was not claimed or run",),
        )

    def _calibration_replay(self, source: RegisteredSource, *, model: str) -> CompilationResult:
        run_identifier = self._run_identifier(
            source.source_id, model, target="calibration_graph_inserted"
        )
        jobs = self.state.jobs_for_source(source.source_id, job_kind=CALIBRATION_COMPILER_JOB_KIND)
        matching = [
            job for job in jobs if job["run_id"] == run_identifier and job["state"] == "succeeded"
        ]
        if not matching:
            raise CompilationError(
                "provisional projection lacks a succeeded compiler job for this contract"
            )
        job = matching[-1]
        materialization = self._materialization(
            source,
            run_identifier=run_identifier,
            job_identifier=str(job["job_id"]),
            projection_status="provisional_calibration",
        )
        self._verify_materialization(materialization)
        verify_asset(source.main_asset)
        return CompilationResult(
            source_id=source.source_id,
            run_id=run_identifier,
            job_id=str(job["job_id"]),
            state="calibration_graph_inserted",
            artifacts=materialization.artifacts,
            warnings=("replayed provisional calibration projection",),
        )

    def _replay(self, source: RegisteredSource, *, model: str) -> CompilationResult:
        jobs = self.state.jobs_for_source(source.source_id, job_kind=COMPILER_JOB_KIND)
        succeeded = [job for job in jobs if job["state"] == "succeeded"]
        if not succeeded:
            raise CompilationError("graph_inserted source lacks a succeeded compiler job")
        current_run_id = self._run_identifier(source.source_id, model)
        current_jobs = [job for job in jobs if job["run_id"] == current_run_id]
        if current_jobs and current_jobs[-1]["state"] == "succeeded":
            return self._verified_replay(source, current_jobs[-1], model=model)
        prior_job = succeeded[-1]
        prior_run = self.state.run_record(str(prior_job["run_id"]))
        if prior_run is None or prior_run["model"] != model:
            raise CompilationError("model does not match the retained graph compilation")
        return self._rebuild_for_current_contract(
            source,
            model=model,
            run_identifier=current_run_id,
            prior_job=prior_job,
        )

    def _verified_replay(
        self,
        source: RegisteredSource,
        job: dict[str, Any],
        *,
        model: str,
    ) -> CompilationResult:
        run = self.state.run_record(str(job["run_id"]))
        if run is None or run["model"] != model:
            raise CompilationError("model does not match the retained graph compilation")
        materialization = self._materialization(
            source,
            run_identifier=str(job["run_id"]),
            job_identifier=str(job["job_id"]),
        )
        self._verify_materialization(materialization)
        verify_asset(source.main_asset)
        return CompilationResult(
            source_id=source.source_id,
            run_id=str(job["run_id"]),
            job_id=str(job["job_id"]),
            state="graph_inserted",
            artifacts=materialization.artifacts,
        )

    def _rebuild_for_current_contract(
        self,
        source: RegisteredSource,
        *,
        model: str,
        run_identifier: str,
        prior_job: dict[str, Any],
    ) -> CompilationResult:
        self._verify_prior_artifacts(source.source_id, prior_job)
        self.state.register_run(
            run_identifier,
            source_id=source.source_id,
            requested_through="graph_inserted",
            model=model,
        )
        job_identifier = job_id(run_identifier, COMPILER_JOB_KIND, 1)
        self._start_job(
            job_identifier,
            run_identifier=run_identifier,
            source_id=source.source_id,
            model=model,
        )
        receipt_path = (
            self.cache_root / "runs" / run_identifier / "receipts" / f"{job_identifier}.json"
        )
        try:
            materialization = self._materialization(
                source,
                run_identifier=run_identifier,
                job_identifier=job_identifier,
            )
            self._replace_materialization(materialization)
            receipt_fingerprint = write_receipt(
                receipt_path,
                {
                    "kind": "graph_compilation_rebuild",
                    "source_id": source.source_id,
                    "run_id": run_identifier,
                    "job_id": job_identifier,
                    "model": model,
                    "supersedes_job_id": prior_job["job_id"],
                    "asset_fingerprint": _asset_fingerprint(source),
                    "artifacts": list(materialization.artifacts),
                    "validation": {
                        "prior_artifacts_verified": True,
                        "canonical_round_trip": True,
                        "graph_schema": True,
                        "context_schema": True,
                        "byte_equivalent_rebuild": True,
                    },
                },
            )
            self.state.set_job_receipt(job_identifier, str(receipt_path))
            self.state.advance_job(
                job_identifier,
                "succeeded",
                run_identifier=run_identifier,
                source_id=source.source_id,
                job_kind=COMPILER_JOB_KIND,
                attempt=1,
                receipt={
                    "kind": "graph_compilation_rebuild_succeeded",
                    "supersedes_job_id": prior_job["job_id"],
                    "receipt": receipt_fingerprint.to_dict(),
                    "artifacts": list(materialization.artifacts),
                },
            )
            self.state.advance_source(
                source.source_id,
                "graph_inserted",
                run_identifier=run_identifier,
                job_identifier=job_identifier,
                receipt={
                    "kind": "graph_rebuilt",
                    "supersedes_job_id": prior_job["job_id"],
                    "canonical": materialization.artifacts[0],
                    "graph_manifest": materialization.artifacts[-1],
                    "byte_equivalent_rebuild": True,
                },
            )
        except (OSError, ValueError) as error:
            current_job = self.state.job_record(job_identifier)
            if current_job is not None and current_job["state"] == "running":
                self.state.advance_job(
                    job_identifier,
                    "validation_failed",
                    run_identifier=run_identifier,
                    source_id=source.source_id,
                    job_kind=COMPILER_JOB_KIND,
                    attempt=1,
                    receipt={"kind": "graph_compilation_rebuild_failed", "error": str(error)},
                )
            if isinstance(error, CompilationError):
                raise
            raise CompilationError(str(error)) from error
        verify_asset(source.main_asset)
        return CompilationResult(
            source_id=source.source_id,
            run_id=run_identifier,
            job_id=job_identifier,
            state="graph_inserted",
            artifacts=materialization.artifacts,
        )

    def _start_job(
        self,
        job_identifier: str,
        *,
        run_identifier: str,
        source_id: str,
        model: str,
        job_kind: str = COMPILER_JOB_KIND,
        receipt_kind: str = "graph_compilation",
    ) -> None:
        current = self.state.job_state(job_identifier)
        if current is None:
            self.state.advance_job(
                job_identifier,
                "pending",
                run_identifier=run_identifier,
                source_id=source_id,
                job_kind=job_kind,
                attempt=1,
                receipt={"kind": f"{receipt_kind}_created"},
            )
        elif current not in {"failed", "validation_failed"}:
            raise CompilationError(f"graph compilation job cannot start from {current}")
        self.state.advance_job(
            job_identifier,
            "running",
            run_identifier=run_identifier,
            source_id=source_id,
            job_kind=job_kind,
            attempt=1,
            receipt={"kind": f"{receipt_kind}_started", "model": model},
        )

    def _materialization(
        self,
        source: RegisteredSource,
        *,
        run_identifier: str,
        job_identifier: str,
        projection_status: str | None = None,
    ) -> _Materialization:
        validate_source_wip(self.state, source, schema_directory=self.schema_directory)
        dossier = dossier_from_validated_wip(
            source,
            self.state.latest_wip_records(source.source_id),
        )
        canonical_bytes = render_canonical_dossier(dossier)
        reparsed = parse_markdown(canonical_bytes.decode("utf-8")).dossier
        compilation = compile_graph(
            reparsed,
            schema_directory=self.schema_directory,
            expected_asset_sha256=source.main_asset.sha256,
            page_count=source.main_asset.pdf_metadata.page_count,
        )
        rebuilt = compile_graph(
            parse_markdown(render_canonical_dossier(reparsed).decode("utf-8")).dossier,
            schema_directory=self.schema_directory,
            expected_asset_sha256=source.main_asset.sha256,
            page_count=source.main_asset.pdf_metadata.page_count,
        )
        graph_bytes = compilation.graph_bytes()
        context_bytes = compilation.context_jsonl_bytes()
        if graph_bytes != rebuilt.graph_bytes() or context_bytes != rebuilt.context_jsonl_bytes():
            raise CompilationError("canonical graph rebuild is not byte-equivalent")

        canonical_path = self.vault_root / source.source_id / "paper.md"
        output_root = self.build_root / source.source_id
        graph_path = output_root / "graph.json"
        context_path = output_root / "context-envelopes.jsonl"
        manifest_path = output_root / "manifest.json"
        artifact_values = (
            _artifact("canonical_markdown", canonical_path, canonical_bytes, self.repository_root),
            _artifact("source_graph", graph_path, graph_bytes, self.repository_root),
            _artifact("context_envelopes", context_path, context_bytes, self.repository_root),
        )
        manifest = {
            "schema_version": "1.0",
            "source_id": source.source_id,
            "run_id": run_identifier,
            "job_id": job_identifier,
            "asset_fingerprint": _asset_fingerprint(source),
            "artifacts": list(artifact_values),
            "record_count": len(compilation.graph["nodes"]),
            "edge_count": len(compilation.graph["edges"]),
            "context_envelope_count": len(compilation.context_envelopes),
        }
        if projection_status is not None:
            manifest["projection_status"] = projection_status
            manifest["canonical_promotion"] = False
        manifest_bytes = canonical_json_bytes(manifest)
        manifest_artifact = _artifact(
            "graph_manifest", manifest_path, manifest_bytes, self.repository_root
        )
        return _Materialization(
            canonical_path=canonical_path,
            graph_path=graph_path,
            context_path=context_path,
            manifest_path=manifest_path,
            canonical_bytes=canonical_bytes,
            graph_bytes=graph_bytes,
            context_bytes=context_bytes,
            manifest_bytes=manifest_bytes,
            artifacts=(*artifact_values, manifest_artifact),
            graph=compilation,
        )

    def _write_materialization(self, value: _Materialization) -> None:
        for path, data in _materialized_files(value):
            _write_or_verify(path, data)
        self._verify_materialization(value)

    def _replace_materialization(self, value: _Materialization) -> None:
        for path, data in _materialized_files(value):
            _atomic_write(path, data)
        self._verify_materialization(value)

    def _verify_existing_projection(self, source_id: str) -> tuple[dict[str, Any], ...]:
        paths = {
            "canonical_markdown": self.vault_root / source_id / "paper.md",
            "source_graph": self.build_root / source_id / "graph.json",
            "context_envelopes": self.build_root / source_id / "context-envelopes.jsonl",
        }
        manifest_path = self.build_root / source_id / "manifest.json"
        all_paths = (*paths.values(), manifest_path)
        if not all(path.is_file() and not path.is_symlink() for path in all_paths):
            raise CompilationError("existing projection is incomplete or non-regular")
        try:
            manifest: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CompilationError("existing graph manifest is unreadable") from error
        if not isinstance(manifest, dict) or manifest.get("source_id") != source_id:
            raise CompilationError("existing graph manifest source identity does not match")
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, list):
            raise CompilationError("existing graph manifest lacks artifact fingerprints")
        observed: list[dict[str, Any]] = []
        observed_kinds: set[str] = set()
        for artifact in artifacts:
            if not isinstance(artifact, dict) or not isinstance(artifact.get("kind"), str):
                raise CompilationError("existing graph manifest has an invalid artifact")
            kind = str(artifact["kind"])
            path = paths.get(kind)
            if path is None:
                raise CompilationError(f"existing graph manifest has an unknown artifact: {kind}")
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != artifact.get("sha256") or len(
                data
            ) != artifact.get("size_bytes"):
                raise CompilationError(
                    f"existing projection fingerprint changed before replacement: {kind}"
                )
            observed.append(dict(artifact))
            observed_kinds.add(kind)
        if observed_kinds != set(paths):
            raise CompilationError("existing graph manifest artifact set is incomplete")
        manifest_bytes = manifest_path.read_bytes()
        observed.append(
            _artifact("graph_manifest", manifest_path, manifest_bytes, self.repository_root)
        )
        return tuple(observed)

    def _verify_prior_artifacts(self, source_id: str, job: dict[str, Any]) -> None:
        receipt_path = job.get("receipt_path")
        if not isinstance(receipt_path, str) or not Path(receipt_path).is_file():
            raise CompilationError("prior graph compilation receipt is missing")
        receipt = read_receipt(Path(receipt_path))
        artifacts = receipt.get("artifacts")
        if not isinstance(artifacts, list):
            raise CompilationError("prior graph compilation receipt lacks artifacts")
        paths = {
            "canonical_markdown": self.vault_root / source_id / "paper.md",
            "source_graph": self.build_root / source_id / "graph.json",
            "context_envelopes": self.build_root / source_id / "context-envelopes.jsonl",
            "graph_manifest": self.build_root / source_id / "manifest.json",
        }
        observed_kinds: set[str] = set()
        for artifact in artifacts:
            if not isinstance(artifact, dict) or not isinstance(artifact.get("kind"), str):
                raise CompilationError("prior graph artifact fingerprint is invalid")
            kind = str(artifact["kind"])
            path = paths.get(kind)
            if path is None or not path.is_file() or path.is_symlink():
                raise CompilationError(f"prior graph artifact is missing: {kind}")
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != artifact.get("sha256") or len(
                data
            ) != artifact.get("size_bytes"):
                raise CompilationError(f"prior graph artifact fingerprint changed: {kind}")
            observed_kinds.add(kind)
        if observed_kinds != set(paths):
            raise CompilationError("prior graph artifact set is incomplete")

    def _verify_materialization(self, value: _Materialization) -> None:
        for path, expected in _materialized_files(value):
            try:
                observed = path.read_bytes()
            except OSError as error:
                raise CompilationError(f"compiled artifact is missing: {path}") from error
            if observed != expected:
                raise CompilationError(f"compiled artifact differs from canonical rebuild: {path}")

    def _run_identifier(self, source_id: str, model: str, *, target: str = "graph_inserted") -> str:
        digest = hashlib.sha256()
        for path in (
            Path(__file__),
            Path(__file__).with_name("render.py"),
            Path(__file__).with_name("graph.py"),
            Path(__file__).with_name("markdown.py"),
            self.schema_directory / "graph.schema.json",
            self.schema_directory / "context-envelope.schema.json",
        ):
            digest.update(path.name.encode("utf-8"))
            digest.update(path.read_bytes())
        return run_id(
            source_id,
            f"{target}:{model}:{COMPILER_VERSION}:{digest.hexdigest()}",
        )


def _materialized_files(value: _Materialization) -> tuple[tuple[Path, bytes], ...]:
    return (
        (value.canonical_path, value.canonical_bytes),
        (value.graph_path, value.graph_bytes),
        (value.context_path, value.context_bytes),
        (value.manifest_path, value.manifest_bytes),
    )


def _write_or_verify(path: Path, data: bytes) -> None:
    if path.exists():
        if not path.is_file() or path.is_symlink():
            raise CompilationError(f"refusing non-regular artifact target: {path}")
        if path.read_bytes() != data:
            raise CompilationError(f"refusing to overwrite divergent artifact: {path}")
        return
    _atomic_write(path, data)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def _artifact(kind: str, path: Path, data: bytes, repository_root: Path) -> dict[str, Any]:
    digest = hashlib.sha256(data).hexdigest()
    display_path = (
        path.relative_to(repository_root).as_posix()
        if path.is_relative_to(repository_root)
        else str(path)
    )
    return {
        "kind": kind,
        "path": display_path,
        "sha256": digest,
        "size_bytes": len(data),
    }


def _asset_fingerprint(source: RegisteredSource) -> dict[str, Any]:
    asset = source.main_asset
    return {
        "asset_id": asset.asset_id,
        "sha256": asset.sha256,
        "size_bytes": asset.size_bytes,
    }
