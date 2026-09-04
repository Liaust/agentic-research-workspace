"""Deterministic visual transport for source-local PDF reading."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from research_map.receipts import canonical_json_bytes, fingerprint

VISUAL_TRANSPORT_VERSION = "1.0"


class VisualTransportError(RuntimeError):
    """Raised when ordered page images cannot be prepared faithfully."""


def prepare_page_images(
    pdf_path: Path,
    *,
    asset_sha256: str,
    page_count: int,
    destination: Path,
    executable: str = "pdftoppm",
    dpi: int = 150,
) -> tuple[Path, ...]:
    """Render or verify one immutable, ordered page-image set."""

    if page_count < 1:
        raise ValueError("page count must be positive")
    if dpi < 72:
        raise ValueError("visual render DPI must be at least 72")
    receipt_path = destination / "render-receipt.json"
    if destination.is_dir():
        return _verify_render(
            receipt_path,
            expected_asset_sha256=asset_sha256,
            expected_page_count=page_count,
            expected_dpi=dpi,
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    try:
        prefix = temporary / "rendered"
        arguments = (
            executable,
            "-png",
            "-r",
            str(dpi),
            "-f",
            "1",
            "-l",
            str(page_count),
            str(pdf_path),
            str(prefix),
        )
        try:
            completed = subprocess.run(arguments, check=False, capture_output=True, text=True)
        except OSError as error:
            raise VisualTransportError(f"page renderer is unavailable: {executable}") from error
        if completed.returncode != 0:
            raise VisualTransportError(
                f"page rendering failed with status {completed.returncode}: "
                f"{completed.stderr.strip()}"
            )
        generated = sorted(temporary.glob("rendered-*.png"), key=_rendered_page_number)
        if len(generated) != page_count:
            raise VisualTransportError(
                f"page rendering produced {len(generated)} images; expected {page_count}"
            )
        page_records: list[dict[str, Any]] = []
        for page, generated_path in enumerate(generated, start=1):
            destination_path = temporary / f"page-{page:03d}.png"
            generated_path.replace(destination_path)
            item = fingerprint(destination_path, relative_to=temporary)
            page_records.append(
                {
                    "page": page,
                    "path": item.path,
                    "sha256": item.sha256,
                    "size_bytes": item.size_bytes,
                }
            )
        receipt_path = temporary / "render-receipt.json"
        receipt_path.write_bytes(
            canonical_json_bytes(
                {
                    "schema_version": VISUAL_TRANSPORT_VERSION,
                    "asset_sha256": asset_sha256,
                    "page_count": page_count,
                    "dpi": dpi,
                    "renderer": executable,
                    "arguments": list(arguments[1:-2]) + ["<immutable-pdf>", "<prefix>"],
                    "pages": page_records,
                }
            )
        )
        os.replace(temporary, destination)
    except Exception:
        for path in sorted(temporary.rglob("*"), reverse=True):
            if path.is_file() or path.is_symlink():
                path.unlink(missing_ok=True)
            elif path.is_dir():
                path.rmdir()
        temporary.rmdir()
        raise
    return _verify_render(
        destination / "render-receipt.json",
        expected_asset_sha256=asset_sha256,
        expected_page_count=page_count,
        expected_dpi=dpi,
    )


def extract_page_text(
    pdf_path: Path,
    *,
    page_count: int,
    executable: str = "pdftotext",
) -> Mapping[int, str | None]:
    """Extract page text only for post-generation exact-prose verification."""

    resolved_executable = resolve_poppler_executable(executable)
    result: dict[int, str | None] = {}
    for page in range(1, page_count + 1):
        try:
            completed = subprocess.run(
                (
                    resolved_executable,
                    "-f",
                    str(page),
                    "-l",
                    str(page),
                    "-raw",
                    str(pdf_path),
                    "-",
                ),
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as error:
            raise VisualTransportError(f"page text checker is unavailable: {executable}") from error
        result[page] = completed.stdout if completed.returncode == 0 else None
    return result


def resolve_poppler_executable(executable: str) -> str:
    """Resolve Poppler companions exposed by the bundled Codex runtime."""

    resolved = shutil.which(executable)
    if resolved is not None:
        return resolved
    if executable != "pdftotext":
        return executable
    renderer = shutil.which("pdftoppm")
    if renderer is None:
        return executable
    renderer_path = Path(renderer).resolve()
    candidates = (
        renderer_path.parent / executable,
        renderer_path.parents[2] / "native" / "poppler" / "poppler" / "bin" / executable,
    )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return executable


def _resolve_poppler_executable(executable: str) -> str:
    """Backward-compatible alias for the original internal resolver."""

    return resolve_poppler_executable(executable)


def _verify_render(
    receipt_path: Path,
    *,
    expected_asset_sha256: str,
    expected_page_count: int,
    expected_dpi: int,
) -> tuple[Path, ...]:
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise VisualTransportError("page-image receipt is unreadable") from error
    expected = (
        VISUAL_TRANSPORT_VERSION,
        expected_asset_sha256,
        expected_page_count,
        expected_dpi,
    )
    observed = (
        receipt.get("schema_version"),
        receipt.get("asset_sha256"),
        receipt.get("page_count"),
        receipt.get("dpi"),
    )
    if observed != expected:
        raise VisualTransportError("page-image receipt identity does not match")
    raw_pages = receipt.get("pages")
    if not isinstance(raw_pages, list) or len(raw_pages) != expected_page_count:
        raise VisualTransportError("page-image receipt has an invalid page inventory")
    paths: list[Path] = []
    for expected_page, item in enumerate(raw_pages, start=1):
        if not isinstance(item, dict) or item.get("page") != expected_page:
            raise VisualTransportError("page-image order is invalid")
        path = receipt_path.parent / f"page-{expected_page:03d}.png"
        observed_page = fingerprint(path, relative_to=receipt_path.parent)
        if (
            item.get("path") != observed_page.path
            or item.get("sha256") != observed_page.sha256
            or item.get("size_bytes") != observed_page.size_bytes
        ):
            raise VisualTransportError(f"page-image fingerprint changed: page {expected_page}")
        paths.append(path)
    return tuple(paths)


def _rendered_page_number(path: Path) -> int:
    try:
        return int(path.stem.rsplit("-", 1)[1])
    except (IndexError, ValueError) as error:
        raise VisualTransportError(
            f"renderer produced an unexpected filename: {path.name}"
        ) from error
