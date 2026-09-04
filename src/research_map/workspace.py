"""Source-local, manifest-backed workspaces for bounded Codex jobs."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from research_map.receipts import canonical_json_bytes, fingerprint
from research_map.source import RegisteredSource, verify_asset

WORKSPACE_MANIFEST_VERSION = "1.0"


class WorkspaceError(ValueError):
    """Raised when a workspace would violate locality or immutability."""


@dataclass(frozen=True, slots=True)
class WorkspaceInput:
    source_id: str
    destination: str
    kind: str
    area: str = "input"
    source_path: Path | None = None
    content: bytes | None = None

    def __post_init__(self) -> None:
        if (self.source_path is None) == (self.content is None):
            raise ValueError("workspace input requires exactly one content source")
        if self.area not in {"input", "templates", "tools", "root"}:
            raise WorkspaceError(f"unsupported workspace input area: {self.area}")
        destination = PurePosixPath(self.destination)
        if destination.is_absolute() or ".." in destination.parts or not destination.parts:
            raise WorkspaceError(f"unsafe workspace input path: {self.destination}")
        if self.area == "root" and len(destination.parts) != 1:
            raise WorkspaceError("root authority input must be one file")


@dataclass(frozen=True, slots=True)
class Workspace:
    path: Path
    manifest_path: Path
    manifest: dict[str, Any]


class WorkspaceBuilder:
    def __init__(self, run_root: Path, *, run_id: str, source: RegisteredSource) -> None:
        self.run_root = run_root
        self.run_id = run_id
        self.source = source

    def reading(self, *, extra_inputs: tuple[WorkspaceInput, ...] = ()) -> Workspace:
        path = self.run_root / "reading"
        inputs = self._base_inputs() + extra_inputs
        if path.exists():
            workspace = validate_workspace(path, expected_source_id=self.source.source_id)
            expected = self._input_descriptors(inputs)
            observed = [
                (
                    item["source_id"],
                    item["path"],
                    item["kind"],
                    item["area"],
                    item["sha256"],
                    item["size_bytes"],
                )
                for item in workspace.manifest["inputs"]
            ]
            if observed != expected:
                raise WorkspaceError("persistent reading workspace inputs do not match")
            return workspace
        return self._create(path, kind="reading", attempt=None, inputs=inputs)

    def audit(
        self,
        attempt: int,
        *,
        prior_state_inputs: tuple[WorkspaceInput, ...] = (),
    ) -> Workspace:
        if attempt < 1:
            raise ValueError("audit attempt must be positive")
        path = self.run_root / "audit" / str(attempt)
        if path.exists():
            raise WorkspaceError(f"audit workspace already exists: {path}")
        return self._create(
            path,
            kind="audit",
            attempt=attempt,
            inputs=self._base_inputs() + prior_state_inputs,
        )

    def finding_verification(
        self,
        attempt: int,
        *,
        prior_state_inputs: tuple[WorkspaceInput, ...] = (),
    ) -> Workspace:
        if attempt < 1:
            raise ValueError("finding-verification attempt must be positive")
        path = self.run_root / "finding-verification" / str(attempt)
        if path.exists():
            raise WorkspaceError(f"finding-verification workspace already exists: {path}")
        return self._create(
            path,
            kind="finding_verification",
            attempt=attempt,
            inputs=self._base_inputs() + prior_state_inputs,
        )

    def _base_inputs(self) -> tuple[WorkspaceInput, ...]:
        asset = self.source.main_asset
        return (
            WorkspaceInput(
                source_id=self.source.source_id,
                destination="paper.pdf",
                kind="source_asset",
                source_path=asset.path,
            ),
            WorkspaceInput(
                source_id=self.source.source_id,
                destination="source.json",
                kind="source_metadata",
                content=canonical_json_bytes(self.source.to_record()),
            ),
        )

    def _input_descriptors(
        self, inputs: tuple[WorkspaceInput, ...]
    ) -> list[tuple[str, str, str, str, str, int]]:
        descriptors: list[tuple[str, str, str, str, str, int]] = []
        for item in inputs:
            if item.source_path is not None:
                item_fingerprint = fingerprint(item.source_path)
                digest = item_fingerprint.sha256
                size = item_fingerprint.size_bytes
            else:
                content = item.content or b""
                digest = hashlib.sha256(content).hexdigest()
                size = len(content)
            descriptors.append(
                (
                    item.source_id,
                    _workspace_relative_path(item),
                    item.kind,
                    item.area,
                    digest,
                    size,
                )
            )
        return descriptors

    def _create(
        self,
        path: Path,
        *,
        kind: str,
        attempt: int | None,
        inputs: tuple[WorkspaceInput, ...],
    ) -> Workspace:
        if not inputs:
            raise WorkspaceError("workspace must contain declared inputs")
        if any(item.source_id != self.source.source_id for item in inputs):
            raise WorkspaceError("cross-source workspace input rejected")
        destinations = [item.destination for item in inputs]
        if len(set(destinations)) != len(destinations):
            raise WorkspaceError("duplicate workspace input destination")

        for asset in self.source.assets:
            verify_asset(asset)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{path.name}.", dir=path.parent))
        try:
            input_root = temporary / "input"
            input_root.mkdir()
            (temporary / "templates").mkdir()
            (temporary / "tools").mkdir()
            (temporary / "scratch").mkdir()
            (temporary / "output").mkdir()
            manifest_inputs: list[dict[str, Any]] = []
            for item in inputs:
                destination = temporary / _workspace_relative_path(item)
                destination.parent.mkdir(parents=True, exist_ok=True)
                if item.source_path is not None:
                    shutil.copyfile(item.source_path, destination)
                else:
                    destination.write_bytes(item.content or b"")
                destination.chmod(0o444)
                item_fingerprint = fingerprint(destination, relative_to=temporary)
                manifest_inputs.append(
                    {
                        "source_id": item.source_id,
                        "path": item_fingerprint.path,
                        "kind": item.kind,
                        "area": item.area,
                        "sha256": item_fingerprint.sha256,
                        "size_bytes": item_fingerprint.size_bytes,
                        "immutable": True,
                    }
                )
            for directory in sorted(input_root.rglob("*"), reverse=True):
                if directory.is_dir():
                    directory.chmod(0o555)
            input_root.chmod(0o555)
            (temporary / "templates").chmod(0o555)
            (temporary / "tools").chmod(0o555)
            manifest: dict[str, Any] = {
                "schema_version": WORKSPACE_MANIFEST_VERSION,
                "run_id": self.run_id,
                "source_id": self.source.source_id,
                "workspace_kind": kind,
                "attempt": attempt,
                "inputs": manifest_inputs,
                "writable_paths": ["scratch", "output"],
                "network_access": False,
            }
            manifest_path = temporary / "manifest.json"
            manifest_path.write_bytes(canonical_json_bytes(manifest))
            os.replace(temporary, path)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

        for asset in self.source.assets:
            verify_asset(asset)
        workspace = validate_workspace(path, expected_source_id=self.source.source_id)
        self._write_run_manifest()
        return workspace

    def _write_run_manifest(self) -> None:
        workspaces: list[dict[str, Any]] = []
        for manifest_path in sorted(self.run_root.glob("**/manifest.json")):
            if manifest_path == self.run_root / "manifest.json":
                continue
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            workspaces.append(
                {
                    "path": manifest_path.parent.relative_to(self.run_root).as_posix(),
                    "kind": manifest["workspace_kind"],
                    "attempt": manifest["attempt"],
                    "manifest_sha256": fingerprint(manifest_path).sha256,
                }
            )
        run_manifest = {
            "schema_version": WORKSPACE_MANIFEST_VERSION,
            "run_id": self.run_id,
            "source_id": self.source.source_id,
            "workspaces": workspaces,
        }
        self.run_root.mkdir(parents=True, exist_ok=True)
        (self.run_root / "manifest.json").write_bytes(canonical_json_bytes(run_manifest))


class CrossReferenceWorkspaceBuilder:
    """Build one immutable-input workspace for a multi-source semantic job."""

    def __init__(
        self,
        path: Path,
        *,
        batch_id: str,
        job_id: str,
        source_ids: tuple[str, ...],
    ) -> None:
        if len(source_ids) < 2 or tuple(sorted(set(source_ids))) != source_ids:
            raise WorkspaceError("cross-reference sources must be unique and canonical")
        self.path = path
        self.batch_id = batch_id
        self.job_id = job_id
        self.source_ids = source_ids

    def build(self, *, kind: str, inputs: tuple[WorkspaceInput, ...]) -> Workspace:
        if kind not in {
            "cross_reference_discovery",
            "cross_reference_inspection",
            "cross_reference_holistic",
            "cross_reference_benchmark_audit",
            "cross_reference_benchmark_blind_probe",
            "cross_reference_benchmark_comparison",
            "cross_reference_recovery",
        }:
            raise WorkspaceError(f"unsupported cross-reference workspace kind: {kind}")
        if not inputs or any(item.source_id != self.batch_id for item in inputs):
            raise WorkspaceError("cross-reference workspace inputs must bind the batch")
        if self.path.exists():
            workspace = validate_workspace(self.path, expected_source_id=self.batch_id)
            if workspace.manifest.get("workspace_kind") != kind:
                raise WorkspaceError("cross-reference workspace kind changed")
            expected = _descriptors_for_inputs(inputs)
            observed = [
                (
                    item["source_id"],
                    item["path"],
                    item["kind"],
                    item["area"],
                    item["sha256"],
                    item["size_bytes"],
                )
                for item in workspace.manifest["inputs"]
            ]
            if observed != expected:
                raise WorkspaceError("cross-reference workspace inputs do not match")
            return workspace

        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{self.path.name}.", dir=self.path.parent))
        try:
            for directory_name in ("input", "templates", "tools", "scratch", "output"):
                (temporary / directory_name).mkdir()
            manifest_inputs: list[dict[str, Any]] = []
            for item in inputs:
                destination = temporary / _workspace_relative_path(item)
                destination.parent.mkdir(parents=True, exist_ok=True)
                if item.source_path is not None:
                    shutil.copyfile(item.source_path, destination)
                else:
                    destination.write_bytes(item.content or b"")
                destination.chmod(0o444)
                item_fingerprint = fingerprint(destination, relative_to=temporary)
                manifest_inputs.append(
                    {
                        "source_id": item.source_id,
                        "path": item_fingerprint.path,
                        "kind": item.kind,
                        "area": item.area,
                        "sha256": item_fingerprint.sha256,
                        "size_bytes": item_fingerprint.size_bytes,
                        "immutable": True,
                    }
                )
            for area in ("input", "templates", "tools"):
                for directory_path in sorted((temporary / area).rglob("*"), reverse=True):
                    if directory_path.is_dir():
                        directory_path.chmod(0o555)
                (temporary / area).chmod(0o555)
            manifest = {
                "schema_version": WORKSPACE_MANIFEST_VERSION,
                "run_id": self.job_id,
                "source_id": self.batch_id,
                "workspace_kind": kind,
                "attempt": None,
                "inputs": manifest_inputs,
                "writable_paths": ["scratch", "output"],
                "network_access": False,
                "authorized_source_ids": list(self.source_ids),
            }
            (temporary / "manifest.json").write_bytes(canonical_json_bytes(manifest))
            os.replace(temporary, self.path)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        return validate_workspace(self.path, expected_source_id=self.batch_id)


def validate_workspace(path: Path, *, expected_source_id: str) -> Workspace:
    manifest_path = path / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkspaceError(f"workspace manifest is unreadable: {path}") from error
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != WORKSPACE_MANIFEST_VERSION
    ):
        raise WorkspaceError("unsupported workspace manifest")
    if manifest.get("source_id") != expected_source_id:
        raise WorkspaceError("workspace source identity does not match")
    inputs = manifest.get("inputs")
    if not isinstance(inputs, list) or not inputs:
        raise WorkspaceError("workspace manifest has no inputs")
    for item in inputs:
        if not isinstance(item, dict) or item.get("source_id") != expected_source_id:
            raise WorkspaceError("cross-source workspace input rejected")
        relative = PurePosixPath(str(item.get("path", "")))
        area = item.get("area")
        if area not in {"input", "templates", "tools", "root"}:
            raise WorkspaceError("workspace input has an invalid area")
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise WorkspaceError("workspace input path is unsafe")
        if area in {"input", "templates", "tools"} and relative.parts[0] != area:
            raise WorkspaceError(f"workspace input path is outside {area}/")
        if area == "root" and len(relative.parts) != 1:
            raise WorkspaceError("root authority input is not at workspace root")
        input_path = path.joinpath(*relative.parts)
        if not input_path.is_file() or input_path.is_symlink():
            raise WorkspaceError(f"workspace input is missing: {relative}")
        observed = fingerprint(input_path, relative_to=path)
        if observed.sha256 != item.get("sha256") or observed.size_bytes != item.get("size_bytes"):
            raise WorkspaceError(f"workspace input fingerprint changed: {relative}")
        if input_path.stat().st_mode & 0o222:
            raise WorkspaceError(f"workspace input is writable: {relative}")
    authorized_source_ids = manifest.get("authorized_source_ids")
    if authorized_source_ids is not None and (
        not isinstance(authorized_source_ids, list)
        or len(authorized_source_ids) < 2
        or not all(isinstance(item, str) for item in authorized_source_ids)
        or sorted(set(authorized_source_ids)) != authorized_source_ids
    ):
        raise WorkspaceError("cross-reference authorized source IDs are invalid")
    return Workspace(path=path, manifest_path=manifest_path, manifest=manifest)


def _workspace_relative_path(item: WorkspaceInput) -> str:
    if item.area == "root":
        return item.destination
    return f"{item.area}/{item.destination}"


def _descriptors_for_inputs(
    inputs: tuple[WorkspaceInput, ...],
) -> list[tuple[str, str, str, str, str, int]]:
    descriptors: list[tuple[str, str, str, str, str, int]] = []
    for item in inputs:
        if item.source_path is not None:
            item_fingerprint = fingerprint(item.source_path)
            digest = item_fingerprint.sha256
            size = item_fingerprint.size_bytes
        else:
            content = item.content or b""
            digest = hashlib.sha256(content).hexdigest()
            size = len(content)
        descriptors.append(
            (
                item.source_id,
                _workspace_relative_path(item),
                item.kind,
                item.area,
                digest,
                size,
            )
        )
    return descriptors
