"""The ingestion worker, and the check that would have caught it missing.

The worker crash-looped from the first deploy: the Dockerfile ran
`python -m agent_forge.knowledge.ingestion` and that package had no `__main__`. Every
test in the suite passed, because none of them ever asked whether the command the image
runs is a command that exists.

So the first test here is not about ingestion at all. It reads the Dockerfile.
"""

from __future__ import annotations

import importlib.util
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agent_forge.knowledge.ingestion.__main__ import _next_run, _sync_once, run

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "deploy" / "compose" / "Dockerfile"

pytestmark = pytest.mark.anyio


# ------------------------------------------------------- what the image actually runs


def _cmd_modules() -> list[str]:
    """Every `python -m <module>` the Dockerfile declares as a CMD."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    return re.findall(r'CMD \["python", "-m", "([a-z_.]+)"', text)


def test_every_module_the_image_runs_can_actually_be_run() -> None:
    """`python -m pkg` fails unless the package has __main__; nothing else checks this."""
    modules = _cmd_modules()
    assert modules, "no `python -m` CMD found; did the Dockerfile change shape?"

    for module in modules:
        spec = importlib.util.find_spec(f"{module}.__main__")
        assert spec is not None, f"the image runs `python -m {module}` and it has no __main__"


# --------------------------------------------------------------------- scheduling


def test_the_cron_from_the_profile_is_what_drives_the_schedule() -> None:
    """`sync_cron` was a profile field nothing read, and croniter a dependency nothing
    imported. Both existed for this."""
    after = datetime(2026, 9, 9, 10, 30, tzinfo=UTC)

    assert _next_run("0 * * * *", after=after) == datetime(2026, 9, 9, 11, 0, tzinfo=UTC)
    assert _next_run("0 */6 * * *", after=after) == datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


# ------------------------------------------------------------------- sync reporting


class _Report:
    def __init__(self, failed: int) -> None:
        self.failed = failed

    def to_dict(self) -> dict[str, Any]:
        return {"documents": 10, "chunks": 42, "failed": self.failed, "errors": ["secreto"]}


class _Knowledge:
    def __init__(self, report: _Report) -> None:
        self.report = report
        self.synced = 0

    async def sync(self, reader: Any) -> _Report:
        self.synced += 1
        return self.report

    async def aclose(self) -> None:
        pass


class _Source:
    type = "folder"
    sync_cron = "0 * * * *"
    path = "/data/corpus"


def _profile() -> Any:
    from agent_forge.profile import load_profile

    return load_profile(
        REPO_ROOT / "configs" / "agent.profile.poc.yaml",
        env={"GOVERNANCE_FAIL_MODE": "closed"},
    )


async def test_a_partial_sync_is_reported_as_a_problem_not_as_a_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nine of ten documents read is how an unreadable file goes unnoticed for a quarter."""
    events: list[tuple[str, dict[str, Any]]] = []

    class _Log:
        def info(self, event: str, **kw: Any) -> None:
            events.append((event, kw))

        def error(self, event: str, **kw: Any) -> None:
            events.append((event, kw))

    monkeypatch.setattr("agent_forge.knowledge.ingestion.__main__.log", _Log())
    monkeypatch.setattr(
        "agent_forge.knowledge.ingestion.__main__.build_reader", lambda *a, **k: object()
    )

    await _sync_once(_Knowledge(_Report(failed=1)), _profile(), _Source(), {})

    names = [name for name, _ in events]
    assert "ingestion.sync_complete" in names
    assert "ingestion.sync_partial" in names


async def test_the_sync_log_does_not_carry_the_error_texts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreadable document's error names it, and a filename is often the content."""
    logged: list[dict[str, Any]] = []

    class _Log:
        def info(self, event: str, **kw: Any) -> None:
            logged.append(kw)

        def error(self, event: str, **kw: Any) -> None:
            logged.append(kw)

    monkeypatch.setattr("agent_forge.knowledge.ingestion.__main__.log", _Log())
    monkeypatch.setattr(
        "agent_forge.knowledge.ingestion.__main__.build_reader", lambda *a, **k: object()
    )

    await _sync_once(_Knowledge(_Report(failed=0)), _profile(), _Source(), {})

    assert not any("errors" in entry for entry in logged)


async def test_a_profile_with_no_sources_exits_rather_than_idling(tmp_path: Path) -> None:
    """A worker that sits there logging nothing looks identical to one that is working."""
    profile = tmp_path / "empty.yaml"
    source = (REPO_ROOT / "configs" / "agent.profile.poc.yaml").read_text(encoding="utf-8")
    # Strip the sources list down to nothing.
    profile.write_text(
        re.sub(r"\n  sources:\n(?:.*\n)*?  rag:", "\n  sources: []\n  rag:", source),
        encoding="utf-8",
    )

    assert await run(profile, once=True) == 1
