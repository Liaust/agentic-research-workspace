"""Canonical single-file rendering for validated source-local WIP."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from research_map.markdown import parse_markdown, render_markdown
from research_map.proposals import dossier_from_wip
from research_map.records import Dossier, Record
from research_map.source import RegisteredSource


class CanonicalRenderError(ValueError):
    """Raised when validated WIP cannot round-trip as one canonical dossier."""


def dossier_from_validated_wip(
    source: RegisteredSource,
    records: Sequence[Mapping[str, Any]],
) -> Dossier:
    """Build a readable, deterministic record order without changing record payloads."""

    base = dossier_from_wip(
        source_id=source.source_id,
        title=_source_title(source),
        source_summary="Validated source-local research dossier.",
        records=tuple(dict(record) for record in records),
    )
    unordered = Dossier.create(
        source_id=base.source_id,
        title=base.title,
        summary=_source_summary(base),
        records=base.records,
    )
    return Dossier.create(
        source_id=unordered.source_id,
        title=unordered.title,
        summary=unordered.summary,
        records=_narrative_order(unordered),
    )


def render_canonical_dossier(dossier: Dossier) -> bytes:
    """Render and immediately prove a byte-equivalent Markdown round trip."""

    rendered = render_markdown(dossier).encode("utf-8")
    reparsed = parse_markdown(rendered.decode("utf-8")).dossier
    if reparsed != dossier:
        raise CanonicalRenderError("canonical Markdown changed record identity or payload")
    if render_markdown(reparsed).encode("utf-8") != rendered:
        raise CanonicalRenderError("canonical Markdown is not byte-equivalent on rebuild")
    return rendered


def _narrative_order(dossier: Dossier) -> tuple[Record, ...]:
    index = dossier.index()
    evidence = sorted(
        dossier.records_of_type("evidence"),
        key=lambda item: (
            int(item.payload.get("page_start", 0)),
            int(item.payload.get("page_end", 0)),
            item.id,
        ),
    )
    threads = _ordered_threads(dossier)
    ordered: list[Record] = []
    emitted: set[str] = set()

    def emit(identifier: str) -> None:
        record = index.get(identifier)
        if record is not None and identifier not in emitted:
            ordered.append(record)
            emitted.add(identifier)

    for record in evidence:
        emit(record.id)
    for thread in threads:
        emit(thread.id)
        for move_id in _string_list(thread.payload.get("move_ids")):
            move = index.get(move_id)
            if move is None or move.record_type != "move":
                continue
            for input_id in _string_list(move.payload.get("inputs")):
                emit(input_id)
            emit(move.id)
            for output_id in _string_list(move.payload.get("outputs")):
                emit(output_id)
    for record in sorted(dossier.records, key=lambda item: (item.record_type, item.id)):
        emit(record.id)
    return tuple(ordered)


def _thread_first_page(thread: Record, index: dict[str, Record]) -> int:
    pages: list[int] = []

    def collect(record: Record) -> None:
        for evidence_id in _string_list(record.payload.get("evidence_ids")):
            evidence = index.get(evidence_id)
            if evidence is not None and isinstance(evidence.payload.get("page_start"), int):
                pages.append(int(evidence.payload["page_start"]))

    collect(thread)
    for move_id in _string_list(thread.payload.get("move_ids")):
        move = index.get(move_id)
        if move is None:
            continue
        collect(move)
        for atom_id in (
            *_string_list(move.payload.get("inputs")),
            *_string_list(move.payload.get("outputs")),
        ):
            atom = index.get(atom_id)
            if atom is not None:
                collect(atom)
    return min(pages, default=10**9)


def _ordered_threads(dossier: Dossier) -> tuple[Record, ...]:
    index = dossier.index()
    threads = {thread.id: thread for thread in dossier.records_of_type("thread")}
    thread_by_move = {
        move_id: thread.id
        for thread in threads.values()
        for move_id in _string_list(thread.payload.get("move_ids"))
    }
    producer_thread: dict[str, str] = {}
    for move in dossier.records_of_type("move"):
        thread_id = thread_by_move.get(move.id)
        if thread_id is None:
            continue
        for output_id in _string_list(move.payload.get("outputs")):
            producer_thread[output_id] = thread_id
    dependencies: dict[str, set[str]] = {thread_id: set() for thread_id in threads}
    for move in dossier.records_of_type("move"):
        consumer_thread = thread_by_move.get(move.id)
        if consumer_thread is None:
            continue
        for input_id in _string_list(move.payload.get("inputs")):
            producer = producer_thread.get(input_id)
            if producer is not None and producer != consumer_thread:
                dependencies[consumer_thread].add(producer)

    def key(thread_id: str) -> tuple[int, str]:
        return (_thread_first_page(threads[thread_id], index), thread_id)

    ordered: list[Record] = []
    remaining = set(threads)
    emitted: set[str] = set()
    while remaining:
        ready = sorted(
            (thread_id for thread_id in remaining if dependencies[thread_id] <= emitted),
            key=key,
        )
        if not ready:
            ready = [min(remaining, key=key)]
        for thread_id in ready:
            ordered.append(threads[thread_id])
            emitted.add(thread_id)
            remaining.remove(thread_id)
    return tuple(ordered)


def _source_title(source: RegisteredSource) -> str:
    identity = source.metadata.get("identity")
    if isinstance(identity, dict) and isinstance(identity.get("title"), str):
        return str(identity["title"])
    return source.source_id


def _source_summary(dossier: Dossier) -> str:
    summaries = [
        str(thread.payload["summary"]).strip()
        for thread in _ordered_threads(dossier)
        if thread.payload.get("summary")
    ]
    return " ".join(summaries) or "Validated source-local research dossier."


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]
