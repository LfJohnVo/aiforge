"""Ragas metrics driven by the cell's own local model.

Ragas is the reference implementation of groundedness and answer relevancy, and its
judgements are better than the built-in token-overlap scorers. What it must not do is
choose its own model: its default factory reaches for OpenAI, and an eval run that ships
the tenant's answers to an external provider to grade them breaks the sovereignty
invariant in the name of measuring quality.

So the adapter points Ragas at the **same LiteLLM proxy the cell routes through**. That
proxy already enforces which backends exist and what may reach them, which means the
judge inherits the cell's routing rather than bypassing it.

Unavailable Ragas is not an error: ``build_refiner`` returns ``None`` and the harness keeps
its built-in scorers. The dependency is heavy, it is in an optional extra, and a laptop
must still be able to run the gate.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from agent_forge.observability.logging import get_logger

log = get_logger(__name__)

__all__ = ["RagasRefiner", "build_refiner"]


@dataclass(slots=True)
class RagasRefiner:
    """Scores one case with Ragas. Every metric degrades to ``None`` on failure.

    ``None`` rather than 0.0: a metric the judge could not compute is missing data, and
    recording it as zero would fail a gate for a reason that has nothing to do with the
    answer.
    """

    faithfulness: Any
    answer_relevancy: Any = None
    context_precision: Any = None

    async def score(
        self, *, question: str, answer: str, contexts: Sequence[str]
    ) -> dict[str, float]:
        scores: dict[str, float] = {}
        if contexts:
            value = await self._safe(
                "groundedness",
                self.faithfulness.ascore,
                user_input=question,
                response=answer,
                retrieved_contexts=list(contexts),
            )
            if value is not None:
                scores["groundedness"] = value
        if self.answer_relevancy is not None:
            value = await self._safe(
                "answer_relevancy",
                self.answer_relevancy.ascore,
                user_input=question,
                response=answer,
            )
            if value is not None:
                scores["answer_relevancy"] = value
        if self.context_precision is not None and contexts:
            value = await self._safe(
                "context_precision",
                self.context_precision.ascore,
                user_input=question,
                response=answer,
                retrieved_contexts=list(contexts),
            )
            if value is not None:
                scores["context_precision"] = value
        return scores

    @staticmethod
    async def _safe(name: str, fn: Any, **kwargs: Any) -> float | None:
        try:
            result = await fn(**kwargs)
        except Exception as exc:  # a judge that fails must not fail the run
            log.warning("evals.ragas_metric_failed", metric=name, detail=str(exc)[:200])
            return None
        value = getattr(result, "value", result)
        try:
            return float(value)
        except (TypeError, ValueError):
            log.warning("evals.ragas_unparseable", metric=name)
            return None


def build_refiner(
    *,
    model: str,
    base_url: str,
    api_key: str = "sk-local",
    embedding_model: str = "",
) -> RagasRefiner | None:
    """Wire Ragas to the cell's LiteLLM proxy, or return ``None``.

    ``model`` is a LiteLLM alias from ``configs/litellm.yaml`` -- the local quality model,
    normally. Passing an external alias here would defeat the point of the adapter, so the
    caller (``scripts/run_evals.py``) takes it from the profile's local aliases.
    """
    try:
        from openai import AsyncOpenAI
        from ragas.llms import LiteLLMStructuredLLM
        from ragas.metrics.collections import (
            AnswerRelevancy,
            ContextPrecisionWithoutReference,
            Faithfulness,
        )
    except ImportError as exc:
        log.info(
            "evals.ragas_unavailable",
            detail=f"{exc}; the built-in scorers will be used instead",
        )
        return None

    try:
        client = AsyncOpenAI(base_url=base_url.rstrip("/") + "/v1", api_key=api_key)
        llm = LiteLLMStructuredLLM(client=client, model=model, provider="openai")
        refiner = RagasRefiner(
            faithfulness=Faithfulness(llm=llm),
            context_precision=ContextPrecisionWithoutReference(llm=llm),
        )
        if embedding_model:
            # Answer relevancy needs embeddings as well as an LLM; without a local
            # embedding alias it is simply left to the built-in scorer.
            from ragas.embeddings import embedding_factory

            # `embedding_factory` is untyped upstream; the ignore is on the call and
            # not on the module, so the rest of the file keeps its checks.
            embeddings = embedding_factory(  # type: ignore[no-untyped-call]
                model=embedding_model, provider="openai", client=client
            )
            refiner.answer_relevancy = AnswerRelevancy(llm=llm, embeddings=embeddings)
    except Exception as exc:  # a misconfigured judge is not a reason to skip the gate
        log.warning("evals.ragas_setup_failed", detail=f"{type(exc).__name__}: {exc}"[:200])
        return None

    log.info(
        "evals.ragas_enabled",
        model=model,
        relevancy=refiner.answer_relevancy is not None,
    )
    return refiner
