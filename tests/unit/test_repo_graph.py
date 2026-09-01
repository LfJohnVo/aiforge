"""Tests for the repository graph builder (RF-12).

The graph is what lets the agent answer questions about itself and what lets us check
the "core knows no domain" invariant mechanically, so it has to be right.
"""

from __future__ import annotations

import json
from pathlib import Path

import repo_graph


def test_discover_skips_noise(sample_package: Path) -> None:
    (sample_package / "src" / "demo" / "__pycache__").mkdir()
    (sample_package / "src" / "demo" / "__pycache__" / "core.py").write_text("x=1")
    (sample_package / ".venv").mkdir()
    (sample_package / ".venv" / "lib.py").write_text("y=2")

    modules = repo_graph.discover(sample_package)
    paths = {m.path for m in modules}

    assert paths == {"src/demo/__init__.py", "src/demo/core.py", "src/demo/client.py"}


def test_parse_module_extracts_symbols_and_docs(sample_package: Path) -> None:
    module = repo_graph.parse_module(sample_package / "src" / "demo" / "core.py", sample_package)

    assert module is not None
    assert module.doc == "Core of the demo."
    assert module.imports == ["__future__", "json"]
    assert [(s.name, s.kind) for s in module.symbols] == [("Engine", "class")]


def test_parse_module_marks_async_functions(sample_package: Path) -> None:
    module = repo_graph.parse_module(sample_package / "src" / "demo" / "client.py", sample_package)

    assert module is not None
    (symbol,) = module.symbols
    assert symbol.name == "call"
    assert symbol.is_async is True


def test_parse_module_returns_none_on_syntax_error(tmp_path: Path) -> None:
    broken = tmp_path / "broken.py"
    broken.write_text("def (:\n", encoding="utf-8")

    assert repo_graph.parse_module(broken, tmp_path) is None


def test_module_id_strips_src_and_init() -> None:
    assert repo_graph._module_id("src/agent_forge/core/graph.py") == "agent_forge.core.graph"
    assert repo_graph._module_id("src/agent_forge/core/__init__.py") == "agent_forge.core"
    assert repo_graph._module_id("scripts/repo_graph.py") == "scripts.repo_graph"


def test_build_graph_links_internal_imports(sample_package: Path) -> None:
    graph = repo_graph.build_graph(repo_graph.discover(sample_package))

    assert graph.has_edge("demo.client", "demo.core")
    assert graph.edges["demo.client", "demo.core"]["kind"] == "imports"
    # External dependencies become their own nodes so we can inventory them.
    assert graph.nodes["json"]["kind"] == "external"
    assert graph.has_edge("demo.core", "json")


def test_build_graph_defines_edges_for_symbols(sample_package: Path) -> None:
    graph = repo_graph.build_graph(repo_graph.discover(sample_package))

    assert graph.has_edge("demo.core", "demo.core:Engine")
    assert graph.nodes["demo.core:Engine"]["kind"] == "class"


def test_export_writes_all_three_artefacts(sample_package: Path, tmp_path: Path) -> None:
    modules = repo_graph.discover(sample_package)
    graph = repo_graph.build_graph(modules)
    out = tmp_path / "graphs"

    payload = repo_graph.export(graph, modules, out)

    assert (out / "repo-graph.json").is_file()
    assert (out / "repo-graph.graphml").is_file()
    assert (out / "repo-graph.mermaid.md").is_file()
    assert payload["version"] == 1
    # GraphML cannot serialise None or lists; export must have normalised them.
    graphml = (out / "repo-graph.graphml").read_text(encoding="utf-8")
    assert "None" not in graphml or "<data" in graphml


def test_repo_map_is_generated_and_marked_as_such(sample_package: Path) -> None:
    modules = repo_graph.discover(sample_package)
    graph = repo_graph.build_graph(modules)

    text = repo_graph.render_repo_map(graph, modules)

    assert text.startswith("# Mapa del repositorio")
    assert "generado por `make repo-graph`" in text
    assert "`Engine`" in text
    assert "```mermaid" in text


def test_main_runs_end_to_end_on_this_repository(repo_root: Path, tmp_path: Path) -> None:
    out = tmp_path / "graphs"
    repo_map = tmp_path / "REPO_MAP.md"

    code = repo_graph.main(
        ["--root", str(repo_root), "--out", str(out), "--repo-map", str(repo_map)]
    )

    assert code == 0
    payload = json.loads((out / "repo-graph.json").read_text(encoding="utf-8"))
    ids = {node["id"] for node in payload["nodes"]}
    assert "scripts.repo_graph" in ids
    assert repo_map.read_text(encoding="utf-8").startswith("# Mapa del repositorio")


def test_main_fails_when_there_is_nothing_to_parse(tmp_path: Path) -> None:
    assert repo_graph.main(["--root", str(tmp_path), "--out", str(tmp_path / "o")]) == 1
