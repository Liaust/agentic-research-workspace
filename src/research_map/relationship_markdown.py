"""Constrained canonical Markdown for mapper-proposed relationships."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from research_map.cross_reference import Relationship
from research_map.ids import canonical_source_pair

FENCE = "```research-map-relationship"


class RelationshipMarkdownError(ValueError):
    """Raised when canonical relationship Markdown is not well formed."""


@dataclass(frozen=True, slots=True)
class RelationshipDocument:
    source_pair: tuple[str, str]
    relationships: tuple[Relationship, ...]
    text: str


def parse_relationship_markdown(text: str) -> RelationshipDocument:
    lines = text.splitlines()
    pair = _title_pair(lines)
    relationships: list[Relationship] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.strip().startswith(FENCE) and line != FENCE:
            raise RelationshipMarkdownError("relationship fence must use the exact info string")
        if line != FENCE:
            index += 1
            continue
        start = index + 1
        index = start
        while index < len(lines) and lines[index] != "```":
            if lines[index].strip().startswith(FENCE):
                raise RelationshipMarkdownError("nested relationship fence")
            index += 1
        if index == len(lines):
            raise RelationshipMarkdownError(f"unclosed relationship fence at line {start}")
        try:
            payload: Any = yaml.safe_load("\n".join(lines[start:index]))
        except yaml.YAMLError as error:
            raise RelationshipMarkdownError(
                f"invalid relationship YAML at line {start}: {error}"
            ) from error
        if not isinstance(payload, dict):
            raise RelationshipMarkdownError(f"relationship block at line {start} is not a mapping")
        try:
            relationship = Relationship.from_mapping(payload)
        except ValueError as error:
            raise RelationshipMarkdownError(
                f"invalid relationship at line {start}: {error}"
            ) from error
        if _relationship_pair(relationship) != pair:
            raise RelationshipMarkdownError(
                "relationship endpoints do not match the document source pair"
            )
        relationships.append(relationship)
        index += 1

    identifiers = [item.relationship_id for item in relationships]
    if len(set(identifiers)) != len(identifiers):
        raise RelationshipMarkdownError("relationship document contains a duplicate ID")
    return RelationshipDocument(
        source_pair=pair,
        relationships=tuple(sorted(relationships, key=lambda item: item.relationship_id)),
        text=text,
    )


def load_relationship_markdown(path: Path) -> RelationshipDocument:
    try:
        return parse_relationship_markdown(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise RelationshipMarkdownError(
            f"cannot read canonical relationship Markdown: {path}"
        ) from error


def render_relationship_markdown(
    source_pair: tuple[str, str], relationships: tuple[Relationship, ...]
) -> str:
    pair = canonical_source_pair(*source_pair)
    ordered = tuple(sorted(relationships, key=lambda item: item.relationship_id))
    identifiers: set[str] = set()
    lines = [
        f"# Relationships: {pair[0]} ↔ {pair[1]}",
        "",
        "Potential cross-source relationships grounded in the canonical source maps. "
        "These records map the literature and do not adjudicate scientific truth.",
        "",
        "## Potential relationships",
        "",
    ]
    for relationship in ordered:
        if _relationship_pair(relationship) != pair:
            raise RelationshipMarkdownError(
                "relationship endpoints do not match the requested source pair"
            )
        if relationship.relationship_id in identifiers:
            raise RelationshipMarkdownError("relationship document contains a duplicate ID")
        identifiers.add(relationship.relationship_id)
        payload = relationship.to_dict()
        lines.extend(
            (
                f"### {payload['relation_type']}: "
                f"{payload['left_endpoint']['record_id']} ↔ "
                f"{payload['right_endpoint']['record_id']}",
                "",
                str(payload["rationale"]),
                "",
                FENCE,
                yaml.safe_dump(
                    payload,
                    allow_unicode=True,
                    default_flow_style=False,
                    sort_keys=False,
                    width=1000,
                ).rstrip(),
                "```",
                "",
            )
        )
    return "\n".join(lines).rstrip() + "\n"


def materialize_relationship_markdown(
    path: Path,
    *,
    source_pair: tuple[str, str],
    relationships: tuple[Relationship, ...],
) -> RelationshipDocument:
    """Merge exact records into one canonical pair document and write atomically."""

    pair = canonical_source_pair(*source_pair)
    existing: tuple[Relationship, ...] = ()
    if path.exists():
        document = load_relationship_markdown(path)
        if document.source_pair != pair:
            raise RelationshipMarkdownError("relationship file source pair changed")
        canonical_existing = render_relationship_markdown(pair, document.relationships)
        if document.text != canonical_existing:
            raise RelationshipMarkdownError(
                "existing relationship Markdown is not in canonical form"
            )
        existing = document.relationships
    merged = {item.relationship_id: item for item in existing}
    for relationship in relationships:
        prior = merged.get(relationship.relationship_id)
        if prior is not None and prior.to_dict() != relationship.to_dict():
            raise RelationshipMarkdownError(
                f"relationship revision identity differs: {relationship.relationship_id}"
            )
        merged[relationship.relationship_id] = relationship
    text = render_relationship_markdown(pair, tuple(merged.values()))
    reparsed = parse_relationship_markdown(text)
    if render_relationship_markdown(pair, reparsed.relationships) != text:
        raise RelationshipMarkdownError("relationship Markdown round trip changed bytes")
    data = text.encode("utf-8")
    if not path.exists() or path.read_bytes() != data:
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
    return RelationshipDocument(pair, reparsed.relationships, text)


def _title_pair(lines: list[str]) -> tuple[str, str]:
    titles = [line.removeprefix("# Relationships: ") for line in lines if line.startswith("# ")]
    if len(titles) != 1 or " ↔ " not in titles[0]:
        raise RelationshipMarkdownError(
            "relationship document requires one canonical source-pair title"
        )
    values = titles[0].split(" ↔ ")
    if len(values) != 2:
        raise RelationshipMarkdownError("relationship source-pair title is invalid")
    try:
        pair = canonical_source_pair(values[0], values[1])
    except ValueError as error:
        raise RelationshipMarkdownError(str(error)) from error
    if tuple(values) != pair:
        raise RelationshipMarkdownError("relationship source-pair title is not canonical")
    return pair


def _relationship_pair(relationship: Relationship) -> tuple[str, str]:
    left = relationship.payload["left_endpoint"]
    right = relationship.payload["right_endpoint"]
    return canonical_source_pair(str(left["source_id"]), str(right["source_id"]))
