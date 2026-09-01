"""Run the evaluation harness. ``make evals`` and ``make evals-ci``.

Builds a real cell -- graph, governance, retrieval, the local judge -- and runs the seed
datasets through it. The model backend is whatever the environment configures: Ollama on a
laptop and in CI, vLLM in a deployment. Nothing in this script talks to an external
provider, and that is not a convenience: the datasets contain C2 material and it does not
leave, not even to be evaluated.

Exit codes: 0 when every blocking threshold holds, 1 when one does not, 2 on a
configuration error. ``--enforce-thresholds`` is what CI passes; without it the report is
printed and the exit code stays 0, so a developer can see where they are without being
blocked.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent_forge.evals import (  # noqa: E402
    Harness,
    build_refiner,
    load_dataset,
    load_datasets,
    load_thresholds,
)
from agent_forge.observability.logging import configure_logging, get_logger  # noqa: E402

log = get_logger(__name__)

DATASETS = REPO_ROOT / "evals" / "datasets"
THRESHOLDS = REPO_ROOT / "evals" / "thresholds.yaml"
RESULTS = REPO_ROOT / "evals" / "results"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--enforce-thresholds",
        action="store_true",
        help="exit non-zero when a blocking threshold is breached (what CI passes)",
    )
    parser.add_argument("--datasets", type=Path, default=DATASETS)
    parser.add_argument("--thresholds", type=Path, default=THRESHOLDS)
    parser.add_argument("--out", type=Path, default=RESULTS / "latest.json")
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="run only these datasets (repeatable): generalist, security, ...",
    )
    parser.add_argument(
        "--ingest",
        action="store_true",
        default=True,
        help="ingest evals/datasets/corpus.jsonl before running (default)",
    )
    parser.add_argument("--no-ingest", dest="ingest", action="store_false")
    parser.add_argument(
        "--ragas",
        action="store_true",
        help="refine the quality metrics with Ragas, judged by the cell's own local model",
    )
    return parser.parse_args(argv)


async def build_cell(stack: AsyncExitStack) -> Any:
    """Assemble the runtime the harness will drive."""
    from agent_forge.runtime import Settings, build_runtime

    env = dict(os.environ)
    # An eval run must not write to the deployment's evidence chain or reuse its
    # checkpoints: both would mix a test run into production history.
    env.setdefault("AGENT_FORGE_INSTANCE", "evals")
    env.setdefault("LEDGER_PATH", str(RESULTS / "ledger"))
    env.pop("POSTGRES_DSN", None)
    settings = Settings.from_env(env)
    return await build_runtime(settings, stack, env=env)


def _read_corpus(path: Path) -> list[dict[str, Any]]:
    """Read the corpus off disk outside the async path, where blocking IO belongs."""
    return load_dataset(path) if path.is_file() else []


async def ingest_corpus(runtime: Any, path: Path) -> int:
    """Load the seed corpus so the grounded cases have something to be grounded in.

    Through the real ingestion pipeline -- chunking, classification, embedding, ACL --
    because a corpus inserted straight into the store would be graded against retrieval
    that never had to filter it.
    """
    rows = _read_corpus(path)
    if not rows:
        log.warning("evals.no_corpus", path=str(path))
        return 0
    from agent_forge.knowledge.documents import AccessControl, Document, SourceRef

    ingested = 0
    for row in rows:
        document = Document(
            source=SourceRef(kind="folder", locator=row["source_id"]),
            tenant_id=runtime.tenant_id,
            title=row["source_id"],
            text=row["text"],
            acl=AccessControl(groups=frozenset(row.get("acl_groups", ()))),
            classification=row["classification"],
            metadata={"eval_chunk_id": row["chunk_id"]},
        )
        await runtime.knowledge.pipeline.ingest_document(document)
        ingested += 1
    log.info("evals.corpus_ingested", documents=ingested)
    return ingested


async def main_async(args: argparse.Namespace) -> int:
    configure_logging(level=os.environ.get("LOG_LEVEL", "WARNING"), fmt="console")

    if not args.thresholds.is_file():
        print(f"thresholds file not found: {args.thresholds}", file=sys.stderr)
        return 2
    thresholds = load_thresholds(args.thresholds)
    datasets = load_datasets(args.datasets)
    corpus = datasets.pop("corpus", [])
    if args.only:
        datasets = {k: v for k, v in datasets.items() if k in set(args.only)}
    if not datasets:
        print(f"no datasets under {args.datasets}", file=sys.stderr)
        return 2

    async with AsyncExitStack() as stack:
        runtime = await build_cell(stack)
        if args.ingest:
            await ingest_corpus(runtime, args.datasets / "corpus.jsonl")

        harness = Harness(
            # The runtime's own graph call: the harness grades the cell a deployment
            # runs, not a pipeline assembled for the occasion.
            invoke=runtime.tasks.invoke,
            tenant_id=runtime.tenant_id,
            agent_name=runtime.profile.identity.agent_name,
            area=runtime.profile.identity.area,
            thresholds=thresholds,
            corpus=corpus,
            langfuse=runtime.observability.langfuse,
            refiner=_refiner(runtime) if args.ragas else None,
        )
        report = await harness.run(datasets)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report.to_dict(), indent=2, ensure_ascii=False), "utf-8")
    print(report.render())
    print(f"report: {args.out}")

    if report.breaches and args.enforce_thresholds:
        print(
            "\ngate FAILED. Lowering a threshold to make this pass requires an ADR: it is a\n"
            "decision to accept more risk or worse quality, not a configuration tweak.",
            file=sys.stderr,
        )
        return 1
    if report.breaches:
        print("\n(thresholds not enforced; pass --enforce-thresholds to gate on them)")
    return 0


def _refiner(runtime: Any) -> Any:
    """Ragas pointed at the cell's own proxy, using its local quality alias.

    The alias comes from the profile rather than a flag: letting the caller name an
    external model here would put the judge outside the perimeter, which is exactly what
    the adapter exists to prevent.
    """
    return build_refiner(
        model=runtime.profile.models.quality,
        base_url=os.environ.get("LITELLM_BASE_URL", "http://127.0.0.1:4000"),
        api_key=os.environ.get("LITELLM_MASTER_KEY", "sk-local"),
        embedding_model=runtime.profile.models.embeddings,
    )


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(main_async(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
