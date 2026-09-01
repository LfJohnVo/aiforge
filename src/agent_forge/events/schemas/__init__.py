"""Versioned CloudEvents payloads (Appendix A of the specification).

The version is in the event **type**, not in a field: `com.peak.task.result.v1`. A
consumer subscribes to the version it understands, and publishing v2 alongside v1 is then
an additive change rather than a coordinated deployment.

Every model here is ``extra="allow"`` on the way in and strict on the way out. A consumer
that receives a field it does not know about must not fall over -- that is the whole point
of versioning the type -- but this cell never *emits* a field it did not mean to.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "CLOUDEVENTS_VERSION",
    "CloudEvent",
    "EvidenceRecordData",
    "JudgeVerdictData",
    "TaskResultData",
    "Verdict",
    "event_type",
]

CLOUDEVENTS_VERSION = "1.0"

TASK_RESULT = "com.peak.task.result.v1"
JUDGE_VERDICT = "com.peak.judge.verdict.v1"
EVIDENCE_RECORD = "com.peak.evidence.record.v1"

Verdict = Literal["approve", "retry", "replan", "escalate"]


class _Data(BaseModel):
    # Permissive inbound: a producer running a newer minor revision must not break this
    # consumer. The outbound shape is controlled by what this code sets, not by the model.
    model_config = ConfigDict(extra="allow")


class TaskResultData(_Data):
    """``com.peak.task.result.v1`` -- what the cell publishes when a task ends.

    Note ``answer_digest``, not the answer. The event fabric is shared infrastructure and
    the aggregator does not need the text: a digest is enough to correlate, and shipping
    the answer would put tenant content on a bus that other tenants' consumers touch.
    """

    task_id: str
    trace_id: str = ""
    tenant_id: str
    agent_id: str = ""
    area: str = ""
    autonomy_level: str = "A0"
    classification: str = "C0"
    output: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    status: Literal["completed", "failed", "awaiting_approval"] = "completed"


class JudgeVerdictData(_Data):
    """``com.peak.judge.verdict.v1`` -- what comes back from the judge."""

    task_id: str
    verdict: Verdict = "approve"
    score: float = 0.0
    reasons: list[str] = Field(default_factory=list)
    policy_refs: list[str] = Field(default_factory=list)


class EvidenceRecordData(_Data):
    """``com.peak.evidence.record.v1`` -- one link of the hash chain."""

    seq: int
    prev_hash: str
    hash: str
    ts: str
    tenant_id: str
    actor: Literal["agent", "human", "judge", "system"] = "agent"
    action: str = ""
    payload_digest: str = ""


class CloudEvent(BaseModel):
    """A CloudEvents 1.0 envelope in structured mode."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    specversion: str = CLOUDEVENTS_VERSION
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source: str
    type: str
    subject: str = ""
    time: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    datacontenttype: str = "application/json"
    # Not a CloudEvents core attribute; an extension the whole fabric agrees on, so a
    # consumer can shard and filter without parsing `data`.
    tenantid: str = ""
    data: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def wrap(
        cls,
        type_: str,
        data: _Data,
        *,
        source: str,
        tenant_id: str = "",
        subject: str = "",
        event_id: str = "",
    ) -> Self:
        payload = data.model_dump(mode="json")
        return cls(
            id=event_id or str(uuid.uuid4()),
            source=source,
            type=type_,
            subject=subject or str(payload.get("task_id", "")),
            tenantid=tenant_id or str(payload.get("tenant_id", "")),
            data=payload,
        )

    def subject_or_id(self) -> str:
        return self.subject or self.id


def event_type(kind: Literal["task_result", "judge_verdict", "evidence_record"]) -> str:
    return {
        "task_result": TASK_RESULT,
        "judge_verdict": JUDGE_VERDICT,
        "evidence_record": EVIDENCE_RECORD,
    }[kind]
