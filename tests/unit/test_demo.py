from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from research_map.demo import build_demo
from research_map.exploration import GraphExplorer

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "schemas/research-map/v1"
EXAMPLE = ROOT / "examples/synthetic-study"


def test_source_to_graph_is_repeatable_and_explorable(tmp_path: Path) -> None:
    output = tmp_path / "demo"
    first = build_demo(example=EXAMPLE, schema_directory=SCHEMAS, output=output)
    assert first == build_demo(example=EXAMPLE, schema_directory=SCHEMAS, output=output)
    assert (first["records"], first["edges"], first["model_calls"]) == (6, 9, 0)
    explorer = GraphExplorer(graph_path=output / "graph.json", schema_directory=SCHEMAS)
    assert explorer.search("causation")["returned_count"] > 0
    result = explorer.explore("LIB-903:atom:causal-limit")
    assert result["object"]["payload"]["kind"] == "limitation"
    assert result["context"]["evidence"]
    assert result["context"]["moves"]
    assert result["context"]["threads"]


@pytest.mark.parametrize("changed_file", ["source.md", "dossier.md"])
def test_tampered_source_or_evidence_fails_before_writing(
    tmp_path: Path, changed_file: str
) -> None:
    example = tmp_path / "example"
    shutil.copytree(EXAMPLE, example)
    path = example / changed_file
    path.write_text(path.read_text().replace("Three fictional trials", "Five fictional trials"))
    output = tmp_path / "output"
    with pytest.raises(ValueError):
        build_demo(example=example, schema_directory=SCHEMAS, output=output)
    assert not output.exists()


def test_changed_output_is_not_overwritten(tmp_path: Path) -> None:
    output = tmp_path / "output"
    build_demo(example=EXAMPLE, schema_directory=SCHEMAS, output=output)
    target = output / "graph.json"
    target.write_text("User changes must remain.")
    with pytest.raises(ValueError, match="differs"):
        build_demo(example=EXAMPLE, schema_directory=SCHEMAS, output=output)
    assert target.read_text() == "User changes must remain."


def test_symlink_output_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    output = tmp_path / "link"
    output.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic"):
        build_demo(example=EXAMPLE, schema_directory=SCHEMAS, output=output)
    assert not list(target.iterdir())
