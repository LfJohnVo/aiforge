"""Shared pytest fixtures.

Unit tests must run with no network and no containers. Anything needing live
infrastructure belongs in ``tests/integration`` behind the ``integration`` marker.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# scripts/ is not an installed package; tests import from it directly.
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Absolute path to the repository root."""
    return REPO_ROOT


@pytest.fixture
def sample_package(tmp_path: Path) -> Iterator[Path]:
    """A tiny throwaway Python package used to exercise AST tooling."""
    pkg = tmp_path / "src" / "demo"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text('"""Demo package."""\n', encoding="utf-8")
    (pkg / "core.py").write_text(
        '"""Core of the demo."""\n'
        "from __future__ import annotations\n"
        "import json\n"
        "\n\n"
        "class Engine:\n"
        '    """Does the work."""\n'
        "\n"
        "    def run(self) -> str:\n"
        "        return json.dumps({})\n",
        encoding="utf-8",
    )
    (pkg / "client.py").write_text(
        '"""Client for the demo."""\n'
        "from __future__ import annotations\n"
        "from demo.core import Engine\n"
        "\n\n"
        "async def call() -> str:\n"
        '    """Call the engine."""\n'
        "    return Engine().run()\n",
        encoding="utf-8",
    )
    yield tmp_path


def pytest_asyncio_loop_factories() -> dict[str, Callable[[], asyncio.AbstractEventLoop]]:
    """Windows needs the selector loop for psycopg's async driver.

    Only affects developers running the suite natively on Windows; the product itself
    always runs in Linux containers. Documented in docs/RUNBOOK.md section 7.
    """
    if sys.platform == "win32":
        return {"selector": asyncio.SelectorEventLoop}
    return {"default": asyncio.new_event_loop}
