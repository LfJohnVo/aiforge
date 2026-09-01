"""Build a knowledge graph of this repository from its AST.

Dual purpose (RF-12):
  a) the agent queries it at runtime through the ``repo_graph.query`` tool,
  b) Claude Code uses it as a development skill (``.claude/skills/repo-graph``).

Outputs ``docs/graphs/repo-graph.json``, ``repo-graph.graphml``, a Mermaid summary and a
regenerated ``docs/REPO_MAP.md``. Never edit those by hand.

Uses tree-sitter when available (it is a core dependency) and falls back to the stdlib
``ast`` module, so the script works even in a bare checkout.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import networkx as nx

if TYPE_CHECKING:
    # Nodes are module/symbol ids (str). networkx only ships the generic in its stubs;
    # `nx.DiGraph[str]` raises TypeError at runtime, hence the two-branch alias.
    type RepoGraph = nx.DiGraph[str]
else:
    RepoGraph = nx.DiGraph

SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "build",
        "dist",
        "instances",
        "htmlcov",
    }
)


@dataclass(slots=True)
class Symbol:
    """A class or function defined in a module."""

    name: str
    kind: str  # "class" | "function"
    lineno: int
    doc: str
    is_async: bool = False
    decorators: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Module:
    """One Python file and everything the graph needs to know about it."""

    path: str
    package: str
    doc: str
    loc: int
    imports: list[str] = field(default_factory=list)
    symbols: list[Symbol] = field(default_factory=list)


def _decorator_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    return "<expr>"


def _first_line(text: str | None) -> str:
    if not text:
        return ""
    return text.strip().splitlines()[0].strip()


def parse_module(path: Path, root: Path) -> Module | None:
    """Parse one file. Returns None when the file is not valid Python."""
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return None

    rel = path.relative_to(root).as_posix()
    module = Module(
        path=rel,
        package=path.relative_to(root).parent.as_posix(),
        doc=_first_line(ast.get_docstring(tree)),
        loc=source.count("\n") + 1,
    )

    for node in tree.body:
        if isinstance(node, ast.Import):
            module.imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            prefix = "." * node.level
            module.imports.append(f"{prefix}{node.module}")
        elif isinstance(node, ast.ClassDef):
            module.symbols.append(
                Symbol(
                    name=node.name,
                    kind="class",
                    lineno=node.lineno,
                    doc=_first_line(ast.get_docstring(node)),
                    decorators=[_decorator_name(d) for d in node.decorator_list],
                )
            )
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            module.symbols.append(
                Symbol(
                    name=node.name,
                    kind="function",
                    lineno=node.lineno,
                    doc=_first_line(ast.get_docstring(node)),
                    is_async=isinstance(node, ast.AsyncFunctionDef),
                    decorators=[_decorator_name(d) for d in node.decorator_list],
                )
            )

    return module


def discover(root: Path) -> list[Module]:
    """Walk the tree and parse every Python file that is not in a skipped directory."""
    modules: list[Module] = []
    for path in sorted(root.rglob("*.py")):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        parsed = parse_module(path, root)
        if parsed is not None:
            modules.append(parsed)
    return modules


def _module_id(rel_path: str) -> str:
    """Map ``src/agent_forge/core/graph.py`` to ``agent_forge.core.graph``."""
    stem = rel_path.removesuffix(".py").removeprefix("src/")
    parts = [p for p in stem.split("/") if p]
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def build_graph(modules: list[Module]) -> RepoGraph:
    """Nodes are modules and symbols; edges are imports and containment."""
    graph: RepoGraph = nx.DiGraph()
    known = {_module_id(m.path): m for m in modules}

    for module in modules:
        mid = _module_id(module.path)
        graph.add_node(
            mid,
            kind="module",
            path=module.path,
            package=module.package,
            doc=module.doc,
            loc=module.loc,
        )
        for symbol in module.symbols:
            sid = f"{mid}:{symbol.name}"
            graph.add_node(
                sid,
                kind=symbol.kind,
                path=module.path,
                lineno=symbol.lineno,
                doc=symbol.doc,
                is_async=symbol.is_async,
            )
            graph.add_edge(mid, sid, kind="defines")

    for module in modules:
        mid = _module_id(module.path)
        for imported in module.imports:
            target = _resolve_import(imported, mid, known)
            if target is not None and target != mid:
                graph.add_edge(mid, target, kind="imports")
            elif not imported.startswith("."):
                external = imported.split(".")[0]
                graph.add_node(external, kind="external")
                graph.add_edge(mid, external, kind="depends")

    return graph


def _resolve_import(imported: str, current: str, known: dict[str, Module]) -> str | None:
    """Resolve an import string to an internal module id, or None if external."""
    if imported.startswith("."):
        depth = len(imported) - len(imported.lstrip("."))
        base = current.split(".")[: -depth or None]
        candidate = ".".join([*base, imported.lstrip(".")]).strip(".")
    else:
        candidate = imported
    if candidate in known:
        return candidate
    # `from agent_forge.core.state import AgentState` resolves to the package module
    while "." in candidate:
        candidate = candidate.rsplit(".", 1)[0]
        if candidate in known:
            return candidate
    return None


def mermaid_summary(graph: RepoGraph, modules: list[Module]) -> str:
    """Package-level Mermaid diagram. Module level would be unreadable."""
    edges: Counter[tuple[str, str]] = Counter()
    for src, dst, data in graph.edges(data=True):
        if data.get("kind") != "imports":
            continue
        src_pkg = str(graph.nodes[src].get("package", ""))
        dst_pkg = str(graph.nodes[dst].get("package", ""))
        if src_pkg and dst_pkg and src_pkg != dst_pkg:
            edges[(src_pkg, dst_pkg)] += 1

    def alias(pkg: str) -> str:
        return pkg.replace("/", "_").replace("-", "_").replace(".", "_")

    packages = sorted({p for edge in edges for p in edge})
    lines = ["```mermaid", "flowchart LR"]
    for pkg in packages:
        loc = sum(m.loc for m in modules if m.package == pkg)
        lines.append(f'  {alias(pkg)}["{pkg}<br/>{loc} loc"]')
    for (src_pkg, dst_pkg), weight in sorted(edges.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {alias(src_pkg)} -->|{weight}| {alias(dst_pkg)}")
    lines.append("```")
    return "\n".join(lines)


def render_repo_map(graph: RepoGraph, modules: list[Module]) -> str:
    """Render docs/REPO_MAP.md. Generated file: never edit by hand."""
    total_loc = sum(m.loc for m in modules)
    n_classes = sum(1 for _, d in graph.nodes(data=True) if d.get("kind") == "class")
    n_funcs = sum(1 for _, d in graph.nodes(data=True) if d.get("kind") == "function")
    externals = sorted(
        {n for n, d in graph.nodes(data=True) if d.get("kind") == "external"},
    )

    out: list[str] = [
        "# Mapa del repositorio",
        "",
        "> **Archivo generado por `make repo-graph`.** No lo edites a mano: el próximo",
        "> `make repo-graph` sobrescribirá los cambios. La fuente de verdad es el código.",
        "",
        "## Resumen",
        "",
        f"- Módulos Python: **{len(modules)}**",
        f"- Líneas de código: **{total_loc}**",
        f"- Clases: **{n_classes}** · Funciones y métodos de nivel superior: **{n_funcs}**",
        f"- Dependencias externas importadas directamente: **{len(externals)}**",
        "",
        "## Dependencias entre paquetes",
        "",
        mermaid_summary(graph, modules),
        "",
        "## Módulos",
        "",
    ]

    by_package: dict[str, list[Module]] = {}
    for module in modules:
        by_package.setdefault(module.package, []).append(module)

    for package in sorted(by_package):
        out.append(f"### `{package or '.'}`")
        out.append("")
        out.append("| Módulo | Descripción | LOC | Símbolos |")
        out.append("|---|---|---:|---|")
        for module in sorted(by_package[package], key=lambda m: m.path):
            names = ", ".join(f"`{s.name}`" for s in module.symbols[:6])
            if len(module.symbols) > 6:
                names += f" (+{len(module.symbols) - 6})"
            doc = module.doc or "—"
            out.append(
                f"| [`{Path(module.path).name}`]({_rel_link(module.path)}) "
                f"| {doc} | {module.loc} | {names or '—'} |"
            )
        out.append("")

    out.extend(["## Dependencias externas", "", ", ".join(f"`{e}`" for e in externals), ""])
    return "\n".join(out)


def _rel_link(module_path: str) -> str:
    """Link from docs/REPO_MAP.md back to a source file."""
    return f"../{module_path}"


def export(graph: RepoGraph, modules: list[Module], out_dir: Path) -> dict[str, Any]:
    """Write JSON and GraphML. Returns the JSON payload."""
    out_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "version": 1,
        "modules": [asdict(m) for m in modules],
        "nodes": [{"id": n, **d} for n, d in graph.nodes(data=True)],
        "edges": [{"source": s, "target": t, **d} for s, t, d in graph.edges(data=True)],
    }
    (out_dir / "repo-graph.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # GraphML rejects None and non-scalar attributes, so normalise first.
    clean: RepoGraph = nx.DiGraph()
    for node, data in graph.nodes(data=True):
        clean.add_node(node, **{k: _scalar(v) for k, v in data.items()})
    for src, dst, data in graph.edges(data=True):
        clean.add_edge(src, dst, **{k: _scalar(v) for k, v in data.items()})
    nx.write_graphml(clean, out_dir / "repo-graph.graphml")

    (out_dir / "repo-graph.mermaid.md").write_text(
        mermaid_summary(graph, modules) + "\n", encoding="utf-8"
    )
    return payload


def _scalar(value: object) -> str | int | float | bool:
    if isinstance(value, str | int | float | bool):
        return value
    return str(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path())
    parser.add_argument("--out", type=Path, default=Path("docs/graphs"))
    parser.add_argument("--repo-map", type=Path, default=Path("docs/REPO_MAP.md"))
    args = parser.parse_args(argv)

    root = args.root.resolve()
    modules = discover(root)
    if not modules:
        print(f"no python modules found under {root}", file=sys.stderr)
        return 1

    graph = build_graph(modules)
    export(graph, modules, root / args.out)
    (root / args.repo_map).write_text(render_repo_map(graph, modules), encoding="utf-8")

    print(
        f"repo-graph: {len(modules)} modules, {graph.number_of_nodes()} nodes, "
        f"{graph.number_of_edges()} edges -> {args.out} and {args.repo_map}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
