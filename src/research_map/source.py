"""Resolution and verification of committed, immutable source registrations."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from research_map.ids import stable_id

SOURCE_REGISTRATION_VERSION = "1.0"
_FINGERPRINT = re.compile(r"SHA-256\s+([0-9a-f]{64});\s+(\d+)\s+bytes")
_PDF_DESCRIPTION = re.compile(
    r"(\d+)\s+readable,\s+(un)?encrypted\s+(?:PDF\s+)?pages", re.IGNORECASE
)


class SourceResolutionError(ValueError):
    """Raised when a requested committed source identity cannot be resolved."""


class SourceValidationError(ValueError):
    """Raised when source metadata and the immutable asset disagree."""


@dataclass(frozen=True, slots=True)
class PdfMetadata:
    page_count: int
    encrypted: bool


@dataclass(frozen=True, slots=True)
class RegisteredAsset:
    asset_id: str
    source_id: str
    role: str
    relative_path: str
    path: Path
    sha256: str
    size_bytes: int
    format: str
    pdf_metadata: PdfMetadata

    def to_record(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "source_id": self.source_id,
            "role": self.role,
            "path": self.relative_path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "metadata": {
                "format": self.format,
                "pdf": {
                    "pages": self.pdf_metadata.page_count,
                    "encrypted": self.pdf_metadata.encrypted,
                },
            },
        }


@dataclass(frozen=True, slots=True)
class RegisteredSource:
    source_id: str
    repository_root: Path
    source_yaml_path: Path
    source_yaml_sha256: str
    metadata: Mapping[str, Any]
    assets: tuple[RegisteredAsset, ...]
    schema_version: str = SOURCE_REGISTRATION_VERSION

    @property
    def main_asset(self) -> RegisteredAsset:
        """Return the one role-selected source asset used for semantic reading."""

        matches = tuple(asset for asset in self.assets if asset.role == "main")
        if len(matches) != 1:
            raise SourceValidationError(
                f"source requires exactly one main asset: {self.source_id}; observed={len(matches)}"
            )
        return matches[0]

    def to_record(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_id": self.source_id,
            "source_yaml_path": self.source_yaml_path.relative_to(self.repository_root).as_posix(),
            "source_yaml_sha256": self.source_yaml_sha256,
            "metadata": dict(self.metadata),
            "assets": [asset.to_record() for asset in self.assets],
        }

    def canonical_json(self) -> str:
        return json.dumps(self.to_record(), sort_keys=True, separators=(",", ":"))


def find_repository_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".project" / "PROJECT.md").is_file():
            return candidate
    raise SourceResolutionError(f"repository root not found from: {current}")


def resolve_source(
    source_id: str,
    *,
    repository_root: Path | None = None,
    metadata_probe: Callable[[Path], PdfMetadata] | None = None,
) -> RegisteredSource:
    """Resolve exactly one committed source and verify every immutable asset."""

    root = (repository_root or find_repository_root()).resolve()
    registrations = sorted((root / "sources" / "library").glob("*/source.yaml"))
    matches: list[tuple[Path, Mapping[str, Any]]] = []
    for path in registrations:
        payload = _load_yaml_mapping(path)
        if payload.get("library_id") == source_id:
            matches.append((path, payload))
    if not matches:
        raise SourceResolutionError(f"source is not registered in the corpus: {source_id}")
    if len(matches) > 1:
        raise SourceResolutionError(f"source identity is ambiguous: {source_id}")

    source_yaml_path, payload = matches[0]
    expected_pdf = _expected_pdf_metadata(payload)
    raw_assets = payload.get("assets")
    if not isinstance(raw_assets, list) or not raw_assets:
        raise SourceValidationError(f"source has no registered assets: {source_id}")

    probe = metadata_probe or probe_pdf_metadata
    assets = tuple(
        _resolve_asset(root, source_id, asset, expected_pdf=expected_pdf, metadata_probe=probe)
        for asset in raw_assets
    )
    source_yaml_sha256 = _sha256(source_yaml_path)
    metadata = {key: value for key, value in payload.items() if key != "assets"}
    source = RegisteredSource(
        source_id=source_id,
        repository_root=root,
        source_yaml_path=source_yaml_path,
        source_yaml_sha256=source_yaml_sha256,
        metadata=metadata,
        assets=assets,
    )
    _ = source.main_asset
    return source


def probe_pdf_metadata(path: Path, *, executable: str = "pdfinfo") -> PdfMetadata:
    """Read only the registration metadata needed to verify a PDF asset."""

    try:
        completed = subprocess.run(
            [executable, str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise SourceValidationError(f"PDF metadata probe unavailable: {error}") from error
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"exit {completed.returncode}"
        raise SourceValidationError(f"PDF metadata probe failed for {path}: {detail}")

    fields: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        key, separator, value = line.partition(":")
        if separator:
            fields[key.strip().lower()] = value.strip()
    try:
        pages = int(fields["pages"])
    except (KeyError, ValueError) as error:
        raise SourceValidationError(f"PDF page count missing for {path}") from error
    encrypted = fields.get("encrypted", "").lower().startswith("yes")
    return PdfMetadata(page_count=pages, encrypted=encrypted)


def verify_asset(asset: RegisteredAsset) -> None:
    """Recheck an immutable asset after workspace or worker activity."""

    if not asset.path.is_file():
        raise SourceValidationError(f"registered asset is missing: {asset.relative_path}")
    size = asset.path.stat().st_size
    digest = _sha256(asset.path)
    if size != asset.size_bytes or digest != asset.sha256:
        raise SourceValidationError(f"registered asset fingerprint changed: {asset.relative_path}")


def _resolve_asset(
    root: Path,
    source_id: str,
    raw: object,
    *,
    expected_pdf: PdfMetadata,
    metadata_probe: Callable[[Path], PdfMetadata],
) -> RegisteredAsset:
    if not isinstance(raw, dict):
        raise SourceValidationError(f"invalid asset entry for {source_id}")
    relative_path = raw.get("path")
    role = raw.get("asset_role")
    asset_format = raw.get("format")
    notes = raw.get("notes")
    if not isinstance(relative_path, str) or not relative_path:
        raise SourceValidationError(f"incomplete asset path for {source_id}")
    if not isinstance(role, str) or not role:
        raise SourceValidationError(f"incomplete asset role for {source_id}")
    if not isinstance(asset_format, str) or not asset_format:
        raise SourceValidationError(f"incomplete asset metadata for {source_id}")
    if not isinstance(notes, str):
        raise SourceValidationError(f"asset fingerprint note is missing for {source_id}")
    fingerprint = _FINGERPRINT.search(notes)
    if fingerprint is None:
        raise SourceValidationError(
            f"asset fingerprint is missing from source.yaml: {relative_path}"
        )
    expected_sha, expected_size_text = fingerprint.groups()
    expected_size = int(expected_size_text)

    path = (root / relative_path).resolve()
    library_root = (root / "sources" / "library").resolve()
    if not path.is_relative_to(library_root):
        raise SourceValidationError(f"asset escapes sources/library: {relative_path}")
    if not path.is_file() or path.is_symlink():
        raise SourceValidationError(
            f"registered asset is missing or not a regular file: {relative_path}"
        )
    if path.stat().st_size != expected_size or _sha256(path) != expected_sha:
        raise SourceValidationError(
            f"asset fingerprint does not match source.yaml: {relative_path}"
        )
    if asset_format != "pdf":
        raise SourceValidationError(f"unsupported registered asset format: {asset_format}")

    observed_pdf = metadata_probe(path)
    if observed_pdf != expected_pdf:
        raise SourceValidationError(f"PDF metadata does not match source.yaml: {relative_path}")
    return RegisteredAsset(
        asset_id=stable_id("asset", (source_id, role, expected_sha)),
        source_id=source_id,
        role=role,
        relative_path=relative_path,
        path=path,
        sha256=expected_sha,
        size_bytes=expected_size,
        format=asset_format,
        pdf_metadata=observed_pdf,
    )


def _expected_pdf_metadata(payload: Mapping[str, Any]) -> PdfMetadata:
    identification = payload.get("identification")
    evidence = identification.get("evidence") if isinstance(identification, dict) else None
    if not isinstance(evidence, list):
        raise SourceValidationError("PDF metadata evidence is missing from source.yaml")
    for item in evidence:
        if isinstance(item, str) and (match := _PDF_DESCRIPTION.search(item)):
            pages, unencrypted = match.groups()
            return PdfMetadata(page_count=int(pages), encrypted=unencrypted is None)
    raise SourceValidationError("PDF page/encryption metadata is missing from source.yaml")


def _load_yaml_mapping(path: Path) -> Mapping[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise SourceValidationError(f"cannot read {path}: {error}") from error
    if not isinstance(value, dict):
        raise SourceValidationError(f"source registration is not a mapping: {path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
