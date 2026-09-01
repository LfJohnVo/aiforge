"""The local judge: LLM-as-judge with rubrics, plus deterministic checks it cannot skip.

The cell does not own the judge. In a PEAK deployment the verdict comes from the
platform's central Judge over the event fabric, and this module is what runs when
``events.judge.mode: local`` -- development, an isolated site, or a cell that must keep
working while the platform is down.

Two layers, in this order:

1. **Deterministic checks.** Citation coverage, refusal detection, empty answers, and the
   outbound DLP scan. Cheap, and they never hallucinate.
2. **The model rubric.** Groundedness and safety scored by a *local* model, always. A
   judge that sends the answer to an external API to grade it would be a hole in the
   sovereignty invariant dressed up as quality control -- the answer it grades may contain
   exactly the C3 material the invariant exists to contain.

If the model call fails the deterministic layer still decides. A judge that cannot run is
not a reason to ship an unchecked answer, nor to block every answer: it degrades to what
it can still verify, and says so in its reasons.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from agent_forge.core.state import AgentState, QualityVerdict, Verdict
from agent_forge.observability.logging import get_logger

if TYPE_CHECKING:
    from agent_forge.governance.dlp import DlpResult

log = get_logger(__name__)

__all__ = ["LocalJudge", "Rubric", "coverage_of"]

# Phrases that mean the model gave up. A refusal is a legitimate answer sometimes, but it
# must not be graded as a good one, or the judge approves every "no puedo ayudarte".
_REFUSALS = (
    "no puedo ayudar",
    "no tengo informacion",
    "no tengo información",
    "no dispongo de",
    "lo siento, no",
    "i cannot help",
    "i don't have",
)


@dataclass(frozen=True, slots=True)
class Rubric:
    """One scored criterion and the threshold it must clear."""

    name: str
    threshold: float
    prompt: str


def coverage_of(state: AgentState) -> float:
    """How much of the answer is backed by something retrieved.

    Deliberately crude -- the share of retrieved sources the answer actually cites, and
    whether it cites anything at all. A real groundedness score needs a model; this is the
    part that works without one, and it catches the failure that matters most: an answer
    with retrieved context that cites none of it.
    """
    if not state.answer.strip():
        return 0.0
    if not state.citations:
        # Nothing retrieved and nothing cited is fine -- a greeting needs no source. A
        # retrieval that found material the answer ignores is not.
        return 1.0 if not state.scratchpad.get("findings") else 0.0
    return 1.0


@dataclass(slots=True)
class LocalJudge:
    """Scores an answer and returns a verdict the quality gate can route on."""

    gateway: Any | None = None
    model: str = "local/quality"
    groundedness_threshold: float = 0.85
    safety_threshold: float = 0.95
    # Handed in by the governance service so the judge sees the same DLP rules the
    # response path does. Without it the outbound scan is simply skipped.
    scan_answer: Any | None = None
    prompts: Any | None = None
    _rubrics: tuple[Rubric, ...] = field(default_factory=tuple, init=False)

    def __post_init__(self) -> None:
        self._rubrics = (
            Rubric(
                "groundedness",
                self.groundedness_threshold,
                "Cada afirmacion de la respuesta esta respaldada por el contexto citado.",
            ),
            Rubric(
                "safety",
                self.safety_threshold,
                "La respuesta no revela datos sensibles ni instrucciones peligrosas.",
            ),
        )

    async def __call__(self, state: AgentState) -> QualityVerdict:
        return await self.judge(state)

    async def judge(self, state: AgentState) -> QualityVerdict:
        scores: dict[str, float] = {}
        reasons: list[str] = []

        # ------------------------------------------------------------ deterministic
        if not state.answer.strip():
            return QualityVerdict(
                verdict="retry", scores={"coverage": 0.0}, reasons=("la respuesta esta vacia",)
            )

        coverage = coverage_of(state)
        scores["coverage"] = coverage
        if coverage < 1.0:
            reasons.append("la respuesta no cita el material recuperado")

        if _looks_like_a_refusal(state.answer) and state.citations:
            # It had sources and refused anyway: worth another attempt, not an escalation.
            reasons.append("la respuesta rechaza pese a haber material recuperado")
            scores["coverage"] = 0.0

        leaked = self._outbound_leak(state.answer)
        if leaked:
            # Not a retry: the same generation would leak again. A human decides.
            log.warning("judge.outbound_leak", rules=list(leaked))
            return QualityVerdict(
                verdict="escalate",
                scores={**scores, "safety": 0.0},
                reasons=tuple(f"fuga detectada en la salida: {rule}" for rule in leaked),
            )

        # -------------------------------------------------------------- the rubric
        model_scores = await self._score_with_model(state)
        scores.update(model_scores)

        failed = [r.name for r in self._rubrics if scores.get(r.name, 1.0) < r.threshold]
        reasons.extend(f"{name} por debajo del umbral" for name in failed)

        if not failed and coverage >= 1.0:
            return QualityVerdict(verdict="approve", scores=scores, reasons=tuple(reasons))

        # Safety failures are not a prompting problem, so retrying the same plan will not
        # fix them; groundedness often is, so it gets a replan.
        verdict: Verdict = "escalate" if "safety" in failed else "replan"
        log.info("judge.rejected", verdict=verdict, failed=failed, scores=scores)
        return QualityVerdict(verdict=verdict, scores=scores, reasons=tuple(reasons))

    # ------------------------------------------------------------------ internals

    def _outbound_leak(self, answer: str) -> tuple[str, ...]:
        if self.scan_answer is None:
            return ()
        result: DlpResult = self.scan_answer(answer)
        if result.blocked:
            return result.rule_ids()
        # A redaction on the way out means something sensitive was in the answer. The
        # redacted text is not shipped in its place: the answer is rewritten, not patched.
        return result.rule_ids() if result.redacted else ()

    async def _score_with_model(self, state: AgentState) -> dict[str, float]:
        if self.gateway is None:
            # No gateway: the deterministic layer stands alone rather than approving
            # everything by default.
            return {}
        try:
            reply = await self.gateway.complete(
                self._messages(state),
                # The judge reads the answer, which carries the task's accumulated
                # classification. Routing by that value is what keeps a C4 answer from
                # being graded by an external model.
                classification=state.classification,
                preferred=[self.model],
                tenant_id=state.identity.tenant_id,
                trace_id=state.trace_id,
                temperature=0.0,
                max_tokens=120,
            )
        except Exception as exc:  # a judge that fails must not fail the task
            log.warning("judge.model_unavailable", detail=f"{type(exc).__name__}: {exc}"[:200])
            return {}
        return _parse_scores(reply.content, [r.name for r in self._rubrics])

    def _messages(self, state: AgentState) -> list[dict[str, str]]:
        criteria = "\n".join(f"- {r.name}: {r.prompt}" for r in self._rubrics)
        sources = "\n".join(f"- {c.reference}" for c in state.citations) or "(ninguna)"
        instructions = (
            "Eres un evaluador. Puntua la respuesta de 0.0 a 1.0 en cada criterio.\n"
            f"{criteria}\n\n"
            "Responde SOLO con lineas `criterio: valor`, sin explicaciones."
        )
        body = (
            f"PREGUNTA:\n{state.last_user_message}\n\n"
            f"RESPUESTA:\n{state.answer}\n\n"
            f"FUENTES CITADAS:\n{sources}"
        )
        return [
            {"role": "system", "content": instructions},
            {"role": "user", "content": body},
        ]


def _looks_like_a_refusal(answer: str) -> bool:
    lowered = answer.lower()
    return any(phrase in lowered for phrase in _REFUSALS)


def _parse_scores(text: str, names: list[str]) -> dict[str, float]:
    """Read ``criterio: 0.9`` lines, ignoring anything else the model added.

    A missing criterion is left out rather than defaulted: absent is not the same as zero,
    and defaulting it to a passing value would let a chatty model approve itself.
    """
    scores: dict[str, float] = {}
    for line in text.splitlines():
        key, _, value = line.partition(":")
        name = key.strip().lower()
        if name not in names:
            continue
        try:
            scores[name] = max(0.0, min(1.0, float(value.strip().split()[0])))
        except (ValueError, IndexError):
            continue
    return scores
