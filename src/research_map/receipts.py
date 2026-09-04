"""Canonical, inspectable receipts for workspace and worker operations."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RECEIPT_SCHEMA_VERSION = "1.0"


@dataclass(frozen=True, slots=True)
class FileFingerprint:
    path: str
    sha256: str
    size_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256, "size_bytes": self.size_bytes}


def fingerprint(path: Path, *, relative_to: Path | None = None) -> FileFingerprint:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    display_path = path.relative_to(relative_to).as_posix() if relative_to else str(path)
    return FileFingerprint(display_path, digest.hexdigest(), path.stat().st_size)


def canonical_json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def write_receipt(path: Path, payload: Mapping[str, Any]) -> FileFingerprint:
    """Write a versioned receipt atomically and return its resulting fingerprint."""

    document = {"schema_version": RECEIPT_SCHEMA_VERSION, **dict(payload)}
    path.parent.mkdir(parents=True, exist_ok=True)
    data = canonical_json_bytes(document)
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
    return fingerprint(path)


def read_receipt(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        raise ValueError(f"unsupported receipt: {path}")
    return value
