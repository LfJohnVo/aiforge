"""Enforce a per-package coverage floor from coverage.xml.

`pytest --cov-fail-under` only checks the global number, which lets a well covered
peripheral package hide a thin `core/`. DoD item 12 requires 80% in `core/`,
`governance/` and `knowledge/` specifically, so the gate is per package.
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def package_coverage(report: Path, package: str) -> tuple[int, int]:
    """Return (covered_lines, total_lines) for every file under ``package``."""
    tree = ET.parse(report)  # noqa: S314 - our own coverage report, not untrusted input
    needle = package.replace("\\", "/").strip("/")
    covered = 0
    total = 0
    for cls in tree.iter("class"):
        filename = (cls.get("filename") or "").replace("\\", "/")
        if needle not in filename:
            continue
        for line in cls.iter("line"):
            total += 1
            if int(line.get("hits") or 0) > 0:
                covered += 1
    return covered, total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=Path("coverage.xml"))
    parser.add_argument("--min", type=float, default=80.0)
    parser.add_argument(
        "--package",
        action="append",
        default=[],
        help="path fragment to gate, e.g. agent_forge/core (repeatable)",
    )
    args = parser.parse_args(argv)

    if not args.report.is_file():
        print(f"coverage report not found: {args.report}", file=sys.stderr)
        return 1

    packages: list[str] = args.package or ["agent_forge"]
    failures: list[str] = []

    for package in packages:
        covered, total = package_coverage(args.report, package)
        if total == 0:
            failures.append(f"{package}: no measurable lines (package missing from report?)")
            continue
        percent = 100.0 * covered / total
        status = "OK " if percent >= args.min else "LOW"
        print(f"  [{status}] {package}: {percent:5.1f}% ({covered}/{total})")
        if percent < args.min:
            failures.append(f"{package}: {percent:.1f}% < {args.min:.1f}%")

    if failures:
        print("\ncoverage gate FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print(f"\ncoverage gate OK (min {args.min:.0f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
