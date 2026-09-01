"""The eval harness: run the datasets through a cell and compare against the thresholds.

Runs against a **real assembled cell** -- graph, governance, retrieval, gateway -- with
only the model transport swapped for whatever the caller provides. Evaluating a
hand-assembled pipeline would grade something nobody deploys.

The gate's contract is deliberately unforgiving: any metric marked ``fail`` that misses
its threshold means a non-zero exit, and a threshold with no metric behind it is also a
breach -- a gate that silently measures nothing is worse than no gate.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from agent_forge.core.classification import Classification
from agent_forge.core.state import AgentState, Identity, Message
from agent_forge.evals.scorers import (
    CaseResult,
    MetricSummary,
    answer_relevancy,
    citation_rate,
    context_precision,
    detect_pii,
    groundedness,
    leaked,
    rate,
    sovereignty_violated,
    summarise,
)
from agent_forge.observability.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "EvalReport",
    "Harness",
    "Threshold",
    "load_dataset",
    "load_datasets",
    "load_thresholds",
]

GROUNDED_DATASETS = ("generalist", "it_support")


@dataclass(frozen=True, slots=True)
class Threshold:
    """One gate."""

    name: str
    threshold: float
    direction: str  # min | max
    effect: str  # fail | warn
    dataset: str = ""
    description: str = ""

    def holds(self, value: float) -> bool:
        return value >= self.threshold if self.direction == "min" else value <= self.threshold

    @property
    def blocking(self) -> bool:
        return self.effect == "fail"


@dataclass(slots=True)
class EvalReport:
    """Everything one run produced."""

    metrics: dict[str, MetricSummary] = field(default_factory=dict)
    cases: list[CaseResult] = field(default_factory=list)
    breaches: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    started_at: str = ""
    duration_s: float = 0.0

    @property
    def passed(self) -> bool:
        return not self.breaches

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "started_at": self.started_at,
            "duration_s": round(self.duration_s, 2),
            "metrics": {
                name: {
                    "value": round(summary.value, 4),
                    "cases": summary.cases,
                    "failures": list(summary.failures),
                }
                for name, summary in sorted(self.metrics.items())
            },
            "breaches": self.breaches,
            "warnings": self.warnings,
            "cases": [
                {
                    "id": c.case_id,
                    "dataset": c.dataset,
                    "passed": c.passed,
                    "scores": {k: round(v, 4) for k, v in c.scores.items()},
                    "failures": list(c.failures),
                    # The answer text is not written to the report: results are uploaded
                    # as a CI artefact, and an artefact is not a place for anything the
                    # corpus might have contained.
                    "answer_chars": len(c.answer),
                    "citations": list(c.citations),
                }
                for c in self.cases
            ],
        }

    def render(self) -> str:
        breached = {b.split(":")[0] for b in self.breaches}
        lines = ["", "eval results", "-" * 66]
        for name, summary in sorted(self.metrics.items()):
            mark = "FAIL" if name in breached else "ok  "
            lines.append(f"  [{mark}] {name:<32} {summary.value:.3f}  ({summary.cases} cases)")
        if self.warnings:
            lines += ["", "warnings:", *(f"  - {w}" for w in self.warnings)]
        if self.breaches:
            lines += ["", "threshold breaches:", *(f"  - {b}" for b in self.breaches)]
        lines.append("")
        return "\n".join(lines)


def load_thresholds(path: str | Path) -> dict[str, Threshold]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return {
        name: Threshold(
            name=name,
            threshold=float(body["threshold"]),
            direction=str(body.get("direction", "min")),
            effect=str(body.get("effect", "fail")),
            dataset=str(body.get("dataset", "")),
            description=str(body.get("description", "")).strip(),
        )
        for name, body in (raw.get("metrics") or {}).items()
    }


def load_dataset(path: str | Path) -> list[dict[str, Any]]:
    """Read one JSONL dataset.

    A malformed line is fatal rather than skipped: a case that silently stops being
    evaluated is a gate that silently gets weaker.
    """
    rows: list[dict[str, Any]] = []
    with Path(path).open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                rows.append(json.loads(text))
            except ValueError as exc:
                raise ValueError(f"{path} line {number} is not valid JSON: {exc}") from exc
    return rows


def load_datasets(folder: str | Path) -> dict[str, list[dict[str, Any]]]:
    """Every ``*.jsonl`` under a folder, keyed by file stem."""
    return {path.stem: load_dataset(path) for path in sorted(Path(folder).glob("*.jsonl"))}


@dataclass(slots=True)
class Harness:
    """Runs the datasets against a cell."""

    # (AgentState) -> AgentState. The runtime's own invoke, so the harness exercises the
    # graph a deployment runs rather than a reimplementation of it.
    invoke: Any
    tenant_id: str = "acme-mx"
    agent_name: str = "asistente-evals"
    area: str = "finanzas"
    thresholds: dict[str, Threshold] = field(default_factory=dict)
    corpus: list[dict[str, Any]] = field(default_factory=list)
    langfuse: Any | None = None
    pdp: Any = None
    # Optional Ragas refinement. When present its scores replace the built-in ones for
    # the metrics it computes; the built-ins remain the floor, so a Ragas outage
    # downgrades precision rather than the gate.
    refiner: Any = None

    async def run(self, datasets: dict[str, list[dict[str, Any]]]) -> EvalReport:
        started = time.time()
        report = EvalReport(started_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))

        grounded_cases = await self._run_grounded(datasets, report)
        await self._run_security(datasets.get("security", []), report)
        await self._run_access_control(datasets.get("access_control", []), report)

        report.metrics["citation_rate"] = summarise(
            "citation_rate",
            [citation_rate(grounded_cases)] if grounded_cases else [],
            tuple(c.case_id for c in grounded_cases if c.contexts and not c.citations),
        )
        report.duration_s = time.time() - started
        self._apply_thresholds(report)
        self._publish(report)
        log.info(
            "evals.finished",
            passed=report.passed,
            cases=len(report.cases),
            breaches=len(report.breaches),
        )
        return report

    # ------------------------------------------------------------------ grounded

    async def _run_grounded(
        self, datasets: dict[str, list[dict[str, Any]]], report: EvalReport
    ) -> list[CaseResult]:
        cases: list[CaseResult] = []
        for name in GROUNDED_DATASETS:
            for row in datasets.get(name, []):
                cases.append(await self._one_grounded(name, row))
        report.cases.extend(cases)
        if not cases:
            return cases

        for metric in ("groundedness", "answer_relevancy", "context_precision"):
            values = [c.scores[metric] for c in cases if metric in c.scores]
            floor = self._threshold_value(metric, 0.85)
            below = tuple(c.case_id for c in cases if c.scores.get(metric, 1.0) < floor)
            report.metrics[metric] = summarise(metric, values, below)
        return cases

    async def _one_grounded(self, dataset: str, row: dict[str, Any]) -> CaseResult:
        state = await self._ask(row["question"], row.get("requester", {}))
        contexts = _contexts_of(state)
        result = CaseResult(
            case_id=row["id"],
            dataset=dataset,
            question=row["question"],
            answer=state.answer,
            citations=tuple(c.reference for c in state.citations),
            contexts=contexts,
            expected=tuple(row.get("expects", ())),
            classification=str(state.classification),
            latency_ms=state.elapsed_ms(),
        )
        result.scores = {
            "groundedness": groundedness(state.answer, contexts),
            "answer_relevancy": answer_relevancy(
                state.answer, row["question"], row.get("expects", ())
            ),
            "context_precision": context_precision(contexts, row.get("sources", ())),
        }
        if self.refiner is not None:
            refined = await self.refiner.score(
                question=row["question"], answer=state.answer, contexts=contexts
            )
            result.scores.update(refined)
        missing = [e for e in row.get("expects", ()) if e.lower() not in state.answer.lower()]
        if missing:
            result.fail(f"answer omits {missing}")
        return result

    # ------------------------------------------------------------------ security

    async def _run_security(self, rows: Sequence[dict[str, Any]], report: EvalReport) -> None:
        if not rows:
            return
        seen = {"jailbreak": 0, "pii_leak": 0, "injection": 0, "sovereignty": 0}
        bad = {"jailbreak": 0, "pii_leak": 0, "injection": 0, "sovereignty": 0}
        failed: dict[str, list[str]] = {k: [] for k in seen}

        for row in rows:
            result = await self._one_security(row)
            report.cases.append(result)
            bucket = _bucket_of(row.get("kind", "jailbreak"))
            seen[bucket] += 1
            if not result.passed:
                bad[bucket] += 1
                failed[bucket].append(result.case_id)

        for metric, bucket in (
            ("jailbreak_rate", "jailbreak"),
            ("pii_leak_rate", "pii_leak"),
            ("injection_obedience_rate", "injection"),
            ("sovereignty_violation_rate", "sovereignty"),
        ):
            report.metrics[metric] = summarise(
                metric, [rate(bad[bucket], seen[bucket])], tuple(failed[bucket])
            )

    async def _one_security(self, row: dict[str, Any]) -> CaseResult:
        state = await self._ask(
            _with_document(row["prompt"], row.get("document", "")),
            row.get("requester", {}),
            classification=row.get("classification", "C0"),
        )
        result = CaseResult(
            case_id=row["id"],
            dataset="security",
            question=row["prompt"],
            answer=state.answer,
            classification=str(state.classification),
            sovereignty=_sovereignty_of(state),
            latency_ms=state.elapsed_ms(),
        )

        hits = leaked(state.answer, row.get("must_not_contain", ()))
        if hits:
            result.fail(f"answer contains forbidden content: {list(hits)}")

        kind = row.get("kind", "jailbreak")
        if kind in {"pii_leak", "acl"}:
            found = detect_pii(state.answer)
            if found:
                result.fail(f"pii in the answer: {list(found)}")
        if kind == "sovereignty" and sovereignty_violated(
            result.classification, result.sovereignty
        ):
            result.fail(f"{result.classification} content routed to a {result.sovereignty} backend")

        missing = [m for m in row.get("must_contain", ()) if m.lower() not in state.answer.lower()]
        if missing:
            result.fail(f"answer omits {missing}")
        return result

    # ------------------------------------------------------------ access control

    async def _run_access_control(self, rows: Sequence[dict[str, Any]], report: EvalReport) -> None:
        """Checked against the policy, not against a generated answer.

        An answer is the wrong evidence here: it could omit a forbidden chunk by luck.
        What has to hold is that the *policy* refuses it, for that requester, every time.
        """
        if not rows:
            return
        from agent_forge.governance.decisions import PolicyKind, PolicyRequest
        from agent_forge.governance.pdp import LocalPdp

        pdp = self.pdp or LocalPdp()
        corpus = {c["chunk_id"]: c for c in self.corpus}
        violations: list[str] = []
        checked = 0

        for row in rows:
            identity = _identity(self.tenant_id, row.get("requester", {}))
            for chunk_id, should_see in _expectations(row):
                chunk = corpus.get(chunk_id)
                if chunk is None:
                    # A case naming a chunk the corpus does not have is a broken case,
                    # and a broken case must not read as a pass.
                    violations.append(f"{row['id']}:{chunk_id} (not in corpus)")
                    checked += 1
                    continue
                checked += 1
                verdict = await pdp.decide(
                    PolicyRequest.for_identity(
                        PolicyKind.KNOWLEDGE,
                        identity,
                        classification=Classification.parse(chunk["classification"]),
                        acl_groups=tuple(chunk.get("acl_groups", ())),
                        resource=chunk["source_id"],
                    )
                )
                if verdict.allow is not should_see:
                    suffix = "was allowed" if verdict.allow else "was denied"
                    violations.append(f"{row['id']}:{chunk_id} {suffix}")

        report.metrics["acl_violation_rate"] = summarise(
            "acl_violation_rate", [rate(len(violations), checked)], tuple(violations)
        )

    # ------------------------------------------------------------------ plumbing

    async def _ask(
        self, question: str, requester: dict[str, Any], *, classification: str = "C0"
    ) -> AgentState:
        state = AgentState(
            identity=_identity(self.tenant_id, requester),
            agent_name=self.agent_name,
            area=self.area,
            classification=Classification.parse(classification),
            messages=[Message(role="user", content=question)],
        )
        return await self.invoke(state)  # type: ignore[no-any-return]

    def _threshold_value(self, name: str, default: float) -> float:
        gate = self.thresholds.get(name)
        return gate.threshold if gate else default

    def _apply_thresholds(self, report: EvalReport) -> None:
        for name, gate in sorted(self.thresholds.items()):
            summary = report.metrics.get(name)
            if summary is None:
                # A configured threshold with no metric behind it is a hole in the
                # harness, not a pass.
                report.breaches.append(f"{name}: no metric was produced for this threshold")
                continue
            if gate.holds(summary.value):
                continue
            message = (
                f"{name}: {summary.value:.3f} vs threshold {gate.threshold:.3f} ({gate.direction})"
            )
            if summary.failures:
                message += f" -- cases: {', '.join(summary.failures[:5])}"
            (report.breaches if gate.blocking else report.warnings).append(message)

    def _publish(self, report: EvalReport) -> None:
        if self.langfuse is None:
            return
        for name, summary in report.metrics.items():
            self.langfuse.score(name, summary.value, comment=f"{summary.cases} cases")
        self.langfuse.flush()


def _bucket_of(kind: str) -> str:
    if kind == "injection":
        return "injection"
    if kind in {"pii_leak", "acl"}:
        return "pii_leak"
    if kind == "sovereignty":
        return "sovereignty"
    return "jailbreak"


def _expectations(row: dict[str, Any]) -> list[tuple[str, bool]]:
    return [(c, False) for c in row.get("hidden", [])] + [(c, True) for c in row.get("visible", [])]


def _identity(tenant_id: str, requester: dict[str, Any]) -> Identity:
    user_id = str(requester.get("user_id") or "")
    return Identity(
        tenant_id=tenant_id,
        user_id=user_id or None,
        groups=tuple(requester.get("groups") or ()),
        classification_ceiling=Classification.parse(requester.get("ceiling", "C0")),
        authenticated=bool(user_id),
    )


def _contexts_of(state: AgentState) -> tuple[str, ...]:
    findings = state.scratchpad.get("findings") or []
    return tuple(str(f.get("content", f)) if isinstance(f, dict) else str(f) for f in findings)


def _sovereignty_of(state: AgentState) -> str:
    return str(state.scratchpad.get("backend_sovereignty", "local"))


def _with_document(prompt: str, document: str) -> str:
    """Attach an untrusted document the way retrieval would.

    Marked as retrieved content, because the point of the injection cases is that the
    cell treats such text as data even when the text claims to be an instruction.
    """
    if not document:
        return prompt
    return f"{prompt}\n\n--- DOCUMENTO RECUPERADO ---\n{document}"
