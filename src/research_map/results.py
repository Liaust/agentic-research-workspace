"""Versioned command results shared by human and JSON renderers."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

RESULT_SCHEMA_VERSION = "1.0"
RESULT_CLASSIFICATION_VERSION = "1.0"


class ResultClassification(StrEnum):
    SUCCESS = "success"
    UNRESOLVED_IDENTITY = "unresolved_identity"
    INVALID_TRANSITION = "invalid_transition"
    VALIDATION_FAILURE = "validation_failure"
    WORKER_FAILURE = "worker_failure"


EXIT_CODES: dict[ResultClassification, int] = {
    ResultClassification.SUCCESS: 0,
    ResultClassification.UNRESOLVED_IDENTITY: 2,
    ResultClassification.INVALID_TRANSITION: 3,
    ResultClassification.VALIDATION_FAILURE: 4,
    ResultClassification.WORKER_FAILURE: 5,
}


@dataclass(frozen=True, slots=True)
class CommandResult:
    command: str
    ok: bool
    classification: ResultClassification = ResultClassification.SUCCESS
    identifiers: dict[str, str] = field(default_factory=dict)
    current_state: str | None = None
    artifacts: tuple[dict[str, Any], ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    data: dict[str, Any] = field(default_factory=dict)
    schema_version: str = RESULT_SCHEMA_VERSION
    classification_version: str = RESULT_CLASSIFICATION_VERSION

    def __post_init__(self) -> None:
        if self.ok != (self.classification is ResultClassification.SUCCESS):
            raise ValueError("result ok flag and classification disagree")

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.classification]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["classification"] = self.classification.value
        payload["exit_code"] = self.exit_code
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    def to_human(self) -> str:
        status = "ok" if self.ok else "error"
        lines = [
            f"{self.command}: {status}",
            f"classification: {self.classification.value}",
            f"exit_code: {self.exit_code}",
        ]
        lines.extend(f"{key}: {value}" for key, value in sorted(self.identifiers.items()))
        if self.current_state is not None:
            lines.append(f"state: {self.current_state}")
        lines.extend(f"warning: {warning}" for warning in self.warnings)
        lines.extend(f"error: {error}" for error in self.errors)
        return "\n".join(lines)
