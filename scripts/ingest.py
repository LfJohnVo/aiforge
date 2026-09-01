"""Run a knowledge sync from the command line.

Used by ``make ingest``, by the maintenance cron and by an operator debugging a corpus.
Runs the same pipeline the worker does, so what it reports is what production would do.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from agent_forge.core.errors import AgentForgeError, ProfileError  # noqa: E402
from agent_forge.knowledge import build_knowledge, build_reader  # noqa: E402
from agent_forge.observability.logging import configure_logging, get_logger  # noqa: E402
from agent_forge.profile import format_profile_error, load_profile  # noqa: E402

log = get_logger("scripts.ingest")


async def run(profile_path: Path, *, only: str | None, dry_run: bool) -> int:
    """Sync every configured source, or just one. Returns a process exit code."""
    env = dict(os.environ)
    try:
        profile = load_profile(profile_path, env=env)
    except ProfileError as exc:
        print(format_profile_error(exc), file=sys.stderr)
        return 2

    sources = [s for s in profile.knowledge.sources if only is None or s.type == only]
    if not sources:
        print(
            f"no sources of type {only!r} in {profile_path}"
            if only
            else f"the profile {profile_path} declares no knowledge sources",
            file=sys.stderr,
        )
        return 1

    # No gateway: ingestion goes through the worker's own embedding backend, and the CLI
    # is expected to run where that backend is reachable. Without one it degrades to
    # hashing embeddings and says so.
    knowledge = build_knowledge(profile=profile, gateway=None, env=env)

    if dry_run:
        print(json.dumps({"would_sync": [s.type for s in sources]}, indent=2))
        return 0

    failures = 0
    reports: list[dict[str, Any]] = []
    try:
        for entry in sources:
            reader = build_reader(
                entry,
                tenant_id=profile.identity.tenant_id,
                env=env,
                default=profile.knowledge.default_classification,
            )
            report = await knowledge.sync(reader)
            reports.append(report.to_dict())
            failures += report.failed
    except AgentForgeError as exc:
        print(f"ingest failed: {exc}", file=sys.stderr)
        return 1
    finally:
        await knowledge.aclose()

    print(json.dumps({"reports": reports}, indent=2, ensure_ascii=False))
    # A sync that could not read some documents is a partial success, and CI or cron
    # should see that in the exit code rather than in a log nobody reads.
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        type=Path,
        default=Path(os.environ.get("AGENT_FORGE_PROFILE", "configs/agent.profile.example.yaml")),
    )
    parser.add_argument("--source", choices=["folder", "sharepoint", "s3"], default=None)
    parser.add_argument("--dry-run", action="store_true", help="list sources, sync nothing")
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)

    configure_logging(level=args.log_level, fmt="console", service="ingest")
    return asyncio.run(run(args.profile, only=args.source, dry_run=args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
