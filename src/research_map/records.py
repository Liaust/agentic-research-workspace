"""Typed boundaries for source-local research-map records."""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

RECORD_TYPES = frozenset({"evidence", "atom", "move", "thread"})


class RecordError(ValueError):
    """Raised when a record or dossier violates the basic v1 shape."""


@dataclass(frozen=True, slots=True)
class Record:
    payload: dict[str, Any]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> Record:
        payload = copy.deepcopy(dict(value))
        if payload.get("schema_version") != 1:
            raise RecordError("record schema_version must be 1")
        record_type = payload.get("record_type")
        if record_type not in RECORD_TYPES:
            raise RecordError(f"unsupported record_type: {record_type!r}")
        identifier = payload.get("id")
        if not isinstance(identifier, str) or not identifier:
            raise RecordError("record id must be non-empty")
        revision = payload.get("revision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise RecordError("record revision must be a positive integer")
        return cls(payload)

    @property
    def id(self) -> str:
        return str(self.payload["id"])

    @property
    def record_type(self) -> str:
        return str(self.payload["record_type"])

    @property
    def revision(self) -> int:
        return int(self.payload["revision"])

    @property
    def source_id(self) -> str:
        return str(self.payload.get("source_id", ""))

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self.payload)


@dataclass(frozen=True, slots=True)
class Dossier:
    source_id: str
    title: str
    summary: str
    records: tuple[Record, ...]
    schema_version: int = 1

    @classmethod
    def create(
        cls,
        *,
        source_id: str,
        title: str,
        summary: str,
        records: Iterable[Record | Mapping[str, Any]],
    ) -> Dossier:
        normalized = tuple(
            record if isinstance(record, Record) else Record.from_mapping(record)
            for record in records
        )
        if not source_id or not title.strip() or not summary.strip():
            raise RecordError("dossier source, title, and summary must be non-empty")
        identifiers = [record.id for record in normalized]
        if len(set(identifiers)) != len(identifiers):
            raise RecordError("canonical dossier contains duplicate stable record ids")
        if any(record.source_id != source_id for record in normalized):
            raise RecordError("dossier contains a cross-source record")
        return cls(source_id, title, summary, normalized)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> Dossier:
        if value.get("schema_version") != 1:
            raise RecordError("dossier schema_version must be 1")
        raw_records = value.get("records")
        if not isinstance(raw_records, list):
            raise RecordError("dossier records must be a list")
        source_id = value.get("source_id")
        title = value.get("title")
        summary = value.get("summary")
        if not all(isinstance(item, str) for item in (source_id, title, summary)):
            raise RecordError("dossier identity fields must be strings")
        assert isinstance(source_id, str)
        assert isinstance(title, str)
        assert isinstance(summary, str)
        mappings: list[Mapping[str, Any]] = []
        for raw in raw_records:
            if not isinstance(raw, dict):
                raise RecordError("dossier record must be a mapping")
            mappings.append(raw)
        return cls.create(
            source_id=source_id,
            title=title,
            summary=summary,
            records=mappings,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_id": self.source_id,
            "title": self.title,
            "summary": self.summary,
            "records": [record.to_dict() for record in self.records],
        }

    def index(self) -> dict[str, Record]:
        return {record.id: record for record in self.records}

    def records_of_type(self, record_type: str) -> tuple[Record, ...]:
        return tuple(record for record in self.records if record.record_type == record_type)
