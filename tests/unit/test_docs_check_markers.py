"""Regression tests for the two false positives the docs gate hit on its first run.

Both are specific to this repository: the documentation is in Spanish, and it forbids
placeholder markers by naming them.
"""

from __future__ import annotations

from pathlib import Path

import docs_check

FILLER = "Contenido real y suficientemente largo para pasar el minimo. " * 12


def _doc(tmp_path: Path, body: str, name: str = "X.md") -> Path:
    (tmp_path / "docs").mkdir(exist_ok=True)
    path = tmp_path / "docs" / name
    path.write_text(body, encoding="utf-8")
    return path


def test_spanish_word_todo_is_not_a_placeholder(tmp_path: Path) -> None:
    """`TODO` is a marker; `todo` is the Spanish word for "everything"."""
    _doc(tmp_path, f"# T\n\nEsto cubre todo el flujo y Todo lo demas.\n{FILLER}")

    assert docs_check.check_file(tmp_path, "docs/X.md") == []


def test_uppercase_todo_in_prose_is_still_a_placeholder(tmp_path: Path) -> None:
    _doc(tmp_path, f"# T\n\n{FILLER}\nTODO: terminar esta seccion.\n")

    problems = docs_check.check_file(tmp_path, "docs/X.md")

    assert any("placeholder marker" in p for p in problems)


def test_marker_inside_inline_code_is_a_reference_not_a_marker(tmp_path: Path) -> None:
    """A rule that says "nada de `TODO`" is forbidding the marker, not carrying one."""
    _doc(tmp_path, f"# T\n\nNada de `TODO`, ni `FIXME`.\n{FILLER}")

    assert docs_check.check_file(tmp_path, "docs/X.md") == []


def test_marker_inside_fenced_block_is_ignored(tmp_path: Path) -> None:
    _doc(tmp_path, f"# T\n\n```python\n# TODO: ejemplo de lo prohibido\n```\n{FILLER}")

    assert docs_check.check_file(tmp_path, "docs/X.md") == []


def test_stripping_code_preserves_line_numbers(tmp_path: Path) -> None:
    body = "# T\n\n```\nuno\ndos\n```\n\nTODO real\n" + FILLER
    _doc(tmp_path, body)

    problems = docs_check.check_file(tmp_path, "docs/X.md")

    assert problems == ["docs/X.md:8: placeholder marker 'TODO'"]


def test_yaml_frontmatter_counts_as_having_a_heading(tmp_path: Path) -> None:
    """MADR ADRs open with frontmatter; that is not a missing heading."""
    _doc(tmp_path, f"---\nstatus: accepted\n---\n# ADR-999\n\n{FILLER}")

    assert docs_check.check_file(tmp_path, "docs/X.md") == []


def test_frontmatter_without_heading_still_fails(tmp_path: Path) -> None:
    _doc(tmp_path, f"---\nstatus: accepted\n---\nSin encabezado.\n{FILLER}")

    problems = docs_check.check_file(tmp_path, "docs/X.md")

    assert any("no top-level heading" in p for p in problems)
