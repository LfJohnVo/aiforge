"""Tests for the documentation completeness gate.

This gate is what enforces the "no aspirational documentation" anti-goal, so it has to
actually fail when a document is a stub.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import coverage_gate
import docs_check

REAL_DOC = "# Título\n\n" + ("Contenido real y suficientemente largo. " * 20)


def test_this_repository_passes_the_gate(repo_root: Path) -> None:
    assert docs_check.main(["--root", str(repo_root)]) == 0


def test_missing_file_is_reported(tmp_path: Path) -> None:
    problems = docs_check.check_file(tmp_path, "docs/NOPE.md")
    assert problems == ["docs/NOPE.md: missing"]


def test_stub_is_reported(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "X.md").write_text("# X\n", encoding="utf-8")

    problems = docs_check.check_file(tmp_path, "docs/X.md")

    assert any("stub" in p for p in problems)


@pytest.mark.parametrize("marker", ["lorem ipsum", "TODO", "TBD", "<placeholder>"])
def test_placeholder_markers_are_rejected(tmp_path: Path, marker: str) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "X.md").write_text(f"{REAL_DOC}\n\n{marker} algo\n", encoding="utf-8")

    problems = docs_check.check_file(tmp_path, "docs/X.md")

    assert any("placeholder marker" in p for p in problems)


def test_missing_heading_is_reported(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "X.md").write_text("Sin encabezado. " * 40, encoding="utf-8")

    problems = docs_check.check_file(tmp_path, "docs/X.md")

    assert any("no top-level heading" in p for p in problems)


def test_fewer_than_four_accepted_adrs_fails(tmp_path: Path) -> None:
    adr = tmp_path / "docs" / "adr"
    adr.mkdir(parents=True)
    (adr / "ADR-000-template.md").write_text("status: template\n", encoding="utf-8")
    (adr / "ADR-001-x.md").write_text("---\nstatus: accepted\n---\n# x\n", encoding="utf-8")

    problems = docs_check.check_adrs(tmp_path)

    assert len(problems) == 1
    assert "at least 4" in problems[0]


def test_session_note_without_log_entry_fails(tmp_path: Path) -> None:
    memory = tmp_path / "docs" / "memory"
    (memory / "SESSION_NOTES").mkdir(parents=True)
    (memory / "DECISIONS_LOG.md").write_text("# Bitácora\n\n## 2026-09-01 · F0 · x\n", "utf-8")
    (memory / "SESSION_NOTES" / "2026-09-01-fase-0.md").write_text("# f0\n", encoding="utf-8")
    (memory / "SESSION_NOTES" / "2026-09-02-fase-1.md").write_text("# f1\n", encoding="utf-8")

    problems = docs_check.check_decisions_log_per_phase(tmp_path)

    assert problems == ["DECISIONS_LOG.md: no entry for phase(s) F1"]


def _coverage_xml(filename: str, hits: list[int]) -> str:
    lines = "".join(f'<line number="{i}" hits="{h}"/>' for i, h in enumerate(hits, 1))
    return (
        '<?xml version="1.0"?><coverage><packages><package><classes>'
        f'<class filename="{filename}"><lines>{lines}</lines></class>'
        "</classes></package></packages></coverage>"
    )


def test_coverage_gate_passes_above_threshold(tmp_path: Path) -> None:
    report = tmp_path / "coverage.xml"
    report.write_text(_coverage_xml("src/agent_forge/core/graph.py", [1, 1, 1, 1, 0]), "utf-8")

    code = coverage_gate.main(["--report", str(report), "--min", "80", "--package", "core"])

    assert code == 0


def test_coverage_gate_fails_below_threshold(tmp_path: Path) -> None:
    report = tmp_path / "coverage.xml"
    report.write_text(_coverage_xml("src/agent_forge/core/graph.py", [1, 0, 0, 0, 0]), "utf-8")

    code = coverage_gate.main(["--report", str(report), "--min", "80", "--package", "core"])

    assert code == 1


def test_coverage_gate_fails_when_package_absent(tmp_path: Path) -> None:
    """A package missing from the report must fail, not silently pass at 0 lines."""
    report = tmp_path / "coverage.xml"
    report.write_text(_coverage_xml("src/agent_forge/api/app.py", [1, 1]), encoding="utf-8")

    code = coverage_gate.main(["--report", str(report), "--package", "agent_forge/governance"])

    assert code == 1


def test_coverage_gate_fails_without_report(tmp_path: Path) -> None:
    assert coverage_gate.main(["--report", str(tmp_path / "nope.xml")]) == 1
