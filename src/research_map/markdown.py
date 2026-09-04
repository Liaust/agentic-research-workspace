"""Strict fenced-record parsing inside one coherent Markdown dossier."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from research_map.records import Dossier, Record, RecordError

FENCE = "```research-map"


class MarkdownRecordError(ValueError):
    """Raised when canonical Markdown contains an invalid research-map fence."""


@dataclass(frozen=True, slots=True)
class MarkdownDocument:
    dossier: Dossier
    text: str


def parse_markdown(text: str) -> MarkdownDocument:
    lines = text.splitlines()
    title = _title(lines)
    summary = _summary(lines)
    records: list[Record] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.strip().startswith(FENCE) and line != FENCE:
            raise MarkdownRecordError("research-map fence must use the exact info string")
        if line != FENCE:
            index += 1
            continue
        start = index + 1
        index = start
        while index < len(lines) and lines[index] != "```":
            if lines[index].strip().startswith(FENCE):
                raise MarkdownRecordError("nested research-map fence")
            index += 1
        if index == len(lines):
            raise MarkdownRecordError(f"unclosed research-map fence at line {start}")
        yaml_text = "\n".join(lines[start:index])
        try:
            payload: Any = yaml.safe_load(yaml_text)
        except yaml.YAMLError as error:
            raise MarkdownRecordError(
                f"invalid research-map YAML at line {start}: {error}"
            ) from error
        if not isinstance(payload, dict):
            raise MarkdownRecordError(f"research-map block at line {start} is not a mapping")
        try:
            records.append(Record.from_mapping(payload))
        except RecordError as error:
            raise MarkdownRecordError(f"invalid record at line {start}: {error}") from error
        index += 1

    if not records:
        raise MarkdownRecordError("canonical dossier contains no research-map records")
    source_ids = {record.source_id for record in records}
    if len(source_ids) != 1:
        raise MarkdownRecordError("canonical dossier is not source-local")
    dossier = Dossier.create(
        source_id=source_ids.pop(),
        title=title,
        summary=summary,
        records=records,
    )
    return MarkdownDocument(dossier=dossier, text=text)


def load_markdown(path: Path) -> MarkdownDocument:
    try:
        return parse_markdown(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise MarkdownRecordError(f"cannot read canonical Markdown: {path}") from error


def render_markdown(dossier: Dossier) -> str:
    """Render a single readable paper file; v1 never emits file-per-record output."""

    lines = [f"# {dossier.title}", "", dossier.summary, "", "## Source-local research map", ""]
    for record in dossier.records:
        lines.extend((_record_prose(record), "", FENCE))
        dumped = yaml.safe_dump(
            record.to_dict(),
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
            width=1000,
        ).rstrip()
        lines.extend((dumped, "```", ""))
    return "\n".join(lines).rstrip() + "\n"


def _title(lines: list[str]) -> str:
    titles = [line[2:].strip() for line in lines if line.startswith("# ")]
    if len(titles) != 1 or not titles[0]:
        raise MarkdownRecordError("canonical dossier requires exactly one level-one title")
    return titles[0]


def _summary(lines: list[str]) -> str:
    title_index = next(index for index, line in enumerate(lines) if line.startswith("# "))
    paragraph: list[str] = []
    started = False
    for line in lines[title_index + 1 :]:
        if line == FENCE or line.startswith("## "):
            break
        if not line.strip():
            if started:
                break
            continue
        started = True
        paragraph.append(line.strip())
    summary = " ".join(paragraph)
    if not summary:
        raise MarkdownRecordError("canonical dossier requires an introductory summary")
    return summary


def _record_prose(record: Record) -> str:
    payload = record.payload
    if record.record_type == "evidence":
        return f"Evidence at {payload['locator']} anchors the records that follow."
    if record.record_type == "atom":
        return f"### {payload['label']}\n\n{payload['statement']}"
    if record.record_type == "move":
        return f"### {payload['label']}\n\n{payload['summary']}"
    return f"### {payload['title']}\n\n{payload['summary']}"
