"""The ingestion worker: keeps the corpus in step with its sources.

``deploy/compose/Dockerfile`` has run ``python -m agent_forge.knowledge.ingestion`` since
F8 and this module did not exist, so the worker crash-looped from the first deploy. The
intent was declared in three other places -- ``croniter`` in the dependencies, ``sync_cron``
in the profile, the ``ingestion-worker`` service in the compose -- and only the code was
missing.

What it does: one schedule per source, taken from that source's ``sync_cron``. A source
with no cron syncs once at start and never again, which is right for a corpus somebody
loads by hand.

Why a loop and not a CronJob: the sources are per profile, so the schedule belongs to the
profile too. Splitting it into Kubernetes CronJobs would mean the profile and the cluster
had to agree, and the two drift. ``--once`` exists for the deployments that would rather
own the scheduling themselves -- the Helm chart's Job runs it that way.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import signal
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from croniter import croniter

from agent_forge.core.errors import AgentForgeError, ProfileError
from agent_forge.knowledge import build_knowledge, build_reader
from agent_forge.observability.logging import configure_logging, get_logger
from agent_forge.profile import AgentProfile, format_profile_error, load_profile
from agent_forge.runtime import build_gateway

log = get_logger("agent_forge.ingestion.worker")

# How long a source with no cron waits before its one and only sync, so the stores it
# needs have time to accept connections. The alternative -- retrying a failed first sync
# forever -- hides a misconfigured source behind what looks like a slow start.
INITIAL_DELAY_SECONDS = 5


def _next_run(expression: str, *, after: datetime) -> datetime:
    return croniter(expression, after).get_next(datetime)  # type: ignore[no-any-return]


async def _sync_once(
    knowledge: Any, profile: AgentProfile, source: Any, env: dict[str, str]
) -> None:
    reader = build_reader(
        source,
        tenant_id=profile.identity.tenant_id,
        env=env,
        default=profile.knowledge.default_classification,
    )
    report = await knowledge.sync(reader)
    # `failed` is logged separately from the counts: a sync that read nine documents and
    # failed on one is a partial success, and a run that reports only "9 documents" is how
    # a permanently unreadable file goes unnoticed for a quarter.
    log.info(
        "ingestion.sync_complete",
        source=source.type,
        **{k: v for k, v in report.to_dict().items() if k != "errors"},
    )
    if report.failed:
        log.error(
            "ingestion.sync_partial",
            source=source.type,
            failed=report.failed,
            detail="some documents could not be read; the rest were indexed",
        )


async def _schedule(
    knowledge: Any, profile: AgentProfile, source: Any, env: dict[str, str]
) -> None:
    """Run one source forever, on its own cron."""
    await asyncio.sleep(INITIAL_DELAY_SECONDS)
    try:
        await _sync_once(knowledge, profile, source, env)
    except AgentForgeError as exc:
        log.error("ingestion.sync_failed", source=source.type, detail=str(exc))

    if not source.sync_cron:
        log.info(
            "ingestion.source_is_manual",
            source=source.type,
            detail="no sync_cron; synced once and will not run again",
        )
        return

    while True:
        now = datetime.now(UTC)
        delay = max(1.0, (_next_run(source.sync_cron, after=now) - now).total_seconds())
        log.info("ingestion.sleeping", source=source.type, seconds=round(delay))
        await asyncio.sleep(delay)
        try:
            await _sync_once(knowledge, profile, source, env)
        except AgentForgeError as exc:
            # Keep the schedule. One failed sync is an incident; a worker that exits on it
            # is an outage, and the corpus then goes stale silently.
            log.error("ingestion.sync_failed", source=source.type, detail=str(exc))


async def run(profile_path: Path, *, once: bool) -> int:
    env = dict(os.environ)
    try:
        profile = load_profile(profile_path, env=env)
    except ProfileError as exc:
        log.error("ingestion.profile_invalid", detail=format_profile_error(exc))
        return 2

    sources = list(profile.knowledge.sources)
    if not sources:
        log.error("ingestion.no_sources", profile=str(profile_path))
        return 1

    # A real gateway, not None: embedding through the policy is what keeps a C3 chunk away
    # from a hosted embedding endpoint, and it is also the difference between a semantic
    # index and a lexical one.
    knowledge = build_knowledge(profile=profile, gateway=build_gateway(profile, env), env=env)
    log.info(
        "ingestion.worker_started",
        sources=[s.type for s in sources],
        mode="once" if once else "scheduled",
    )

    try:
        if once:
            for source in sources:
                await _sync_once(knowledge, profile, source, env)
            return 0

        tasks = [
            asyncio.create_task(_schedule(knowledge, profile, source, env)) for source in sources
        ]
        stop = asyncio.Event()
        _install_signal_handlers(stop)
        await stop.wait()

        log.info("ingestion.worker_stopping")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        return 0
    except AgentForgeError as exc:
        log.error("ingestion.worker_failed", detail=str(exc))
        return 1
    finally:
        await knowledge.aclose()


def _install_signal_handlers(stop: asyncio.Event) -> None:
    """SIGTERM is how Docker and Kubernetes ask; without this the worker is SIGKILLed."""
    loop = asyncio.get_running_loop()
    for name in ("SIGTERM", "SIGINT"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        with contextlib.suppress(NotImplementedError):  # Windows has no add_signal_handler
            loop.add_signal_handler(signum, stop.set)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Agent Forge ingestion worker")
    parser.add_argument(
        "--profile",
        type=Path,
        default=Path(os.environ.get("AGENT_FORGE_PROFILE", "configs/agent.profile.example.yaml")),
    )
    parser.add_argument(
        "--once", action="store_true", help="sync every source once and exit (for a Job)"
    )
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)

    configure_logging(
        level=args.log_level,
        fmt=os.environ.get("LOG_FORMAT", "json"),
        service="ingestion-worker",
        instance=os.environ.get("AGENT_FORGE_INSTANCE", "default"),
    )
    return asyncio.run(run(args.profile, once=args.once))


if __name__ == "__main__":
    raise SystemExit(main())
