"""Compile an authored synthetic dossier without models, credentials or private inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from research_map.graph import compile_graph
from research_map.markdown import load_markdown, parse_markdown, render_markdown
from research_map.receipts import canonical_json_bytes


def build_demo(*, example: Path, schema_directory: Path, output: Path) -> dict[str, Any]:
    source = (example / "source.md").read_bytes()
    source_text = source.decode("utf-8")
    source_hash = hashlib.sha256(source).hexdigest()
    document = load_markdown(example / "dossier.md")
    for record in document.dossier.records_of_type("evidence"):
        if record.payload["exact_span"] not in source_text:
            raise ValueError(f"evidence span is absent from the synthetic source: {record.id}")
    compilation = compile_graph(
        document.dossier,
        schema_directory=schema_directory,
        expected_asset_sha256=source_hash,
        page_count=1,
    )
    rendered = render_markdown(document.dossier)
    if parse_markdown(rendered).dossier.to_dict() != document.dossier.to_dict():
        raise ValueError("Markdown round trip changed the authored dossier")
    files = {
        "source.md": source,
        "dossier.md": rendered.encode("utf-8"),
        "graph.json": compilation.graph_bytes(),
        "context.jsonl": compilation.context_jsonl_bytes(),
    }
    manifest = {
        "schema_version": "1.0",
        "kind": "synthetic_walkthrough_receipt",
        "source_id": document.dossier.source_id,
        "source_sha256": source_hash,
        "records": len(document.dossier.records),
        "edges": len(compilation.graph["edges"]),
        "model_calls": 0,
        "semantic_origin": "manually_authored_synthetic_fixture",
        "scientific_acceptance": "not_applicable",
        "files": [
            {"path": name, "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}
            for name, raw in sorted(files.items())
        ],
    }
    files["receipt.json"] = canonical_json_bytes(manifest)
    # Never overwrite a changed output or traverse output symlinks. Exact reruns
    # are read-only and idempotent, including the deterministic receipt.
    if any(path.is_symlink() for path in (output, *output.parents)):
        raise ValueError("demo output must not contain symbolic links in its path")
    if output.exists():
        if not output.is_dir():
            raise ValueError("demo output must be a directory")
        existing = list(output.iterdir())
        if existing:
            if {path.name for path in existing} != set(files) or any(
                path.is_symlink() or not path.is_file() or path.read_bytes() != files[path.name]
                for path in existing
            ):
                raise ValueError("demo output differs; choose a new output directory")
            return manifest
    output.mkdir(parents=True, exist_ok=True)
    for name, raw in files.items():
        with (output / name).open("xb") as handle:
            handle.write(raw)
    return manifest


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--example", type=Path, default=root / "examples/synthetic-study")
    parser.add_argument("--schema-directory", type=Path, default=root / "schemas/research-map/v1")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        receipt = build_demo(
            example=args.example, schema_directory=args.schema_directory, output=args.output
        )
    except (OSError, ValueError) as error:
        print(
            json.dumps({"ok": False, "error": str(error)}) if args.json else f"Demo failed: {error}"
        )
        return 1
    if args.json:
        print(json.dumps({"ok": True, **receipt}, sort_keys=True))
    else:
        print(f"Compiled {receipt['records']} synthetic records and {receipt['edges']} links.")
        print("No model calls. No real experiment. Source, dossier, graph and receipt are in:")
        print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
