"""Evaluation harness: datasets, scorers, thresholds and the CI gate.

Quality and safety **block the merge** here; they are not observed and discussed. The
four layers in ``docs/EVALS.md`` map onto this package as: scorers (quality), the security
dataset (safety), the access-control pass (invariants evaluated as policy), and pytest
for the invariants that admit no threshold at all.
"""

from __future__ import annotations

from agent_forge.evals.harness import (
    EvalReport,
    Harness,
    Threshold,
    load_dataset,
    load_datasets,
    load_thresholds,
)
from agent_forge.evals.ragas_adapter import RagasRefiner, build_refiner
from agent_forge.evals.scorers import (
    CaseResult,
    MetricSummary,
    answer_relevancy,
    citation_rate,
    context_precision,
    groundedness,
    summarise,
)

__all__ = [
    "CaseResult",
    "EvalReport",
    "Harness",
    "MetricSummary",
    "RagasRefiner",
    "Threshold",
    "answer_relevancy",
    "build_refiner",
    "citation_rate",
    "context_precision",
    "groundedness",
    "load_dataset",
    "load_datasets",
    "load_thresholds",
    "summarise",
]
