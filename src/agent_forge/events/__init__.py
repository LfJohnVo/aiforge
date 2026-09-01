"""Event fabric: CloudEvents over a swappable bus, plus the evidence chain.

``build_bus`` picks the implementation. A cell with no ``fabric_url`` runs the in-process
bus, which is a supported deployment rather than a degraded one -- standalone mode closes
its own loop with the local judge, and the same publish/subscribe code runs either way.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_forge.events.aggregator import Aggregator, result_event
from agent_forge.events.bus import DEAD_LETTER_SUBJECT, EventBus, InMemoryBus, subject_for
from agent_forge.events.evidence import (
    ChainError,
    EvidenceLedger,
    JsonlSink,
    LedgerRecord,
    digest,
    verify_chain,
    verify_path,
)
from agent_forge.events.judge import LocalJudge
from agent_forge.events.schemas import (
    EVIDENCE_RECORD,
    JUDGE_VERDICT,
    TASK_RESULT,
    CloudEvent,
    EvidenceRecordData,
    JudgeVerdictData,
    TaskResultData,
)
from agent_forge.observability.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "DEAD_LETTER_SUBJECT",
    "EVIDENCE_RECORD",
    "JUDGE_VERDICT",
    "TASK_RESULT",
    "Aggregator",
    "ChainError",
    "CloudEvent",
    "EventBus",
    "EvidenceLedger",
    "EvidenceRecordData",
    "InMemoryBus",
    "JsonlSink",
    "JudgeVerdictData",
    "LedgerRecord",
    "LocalJudge",
    "TaskResultData",
    "build_bus",
    "build_ledger",
    "digest",
    "result_event",
    "subject_for",
    "verify_chain",
    "verify_path",
]

DEFAULT_LEDGER_PATH = "./var/ledger"


def build_bus(fabric_url: str, *, source: str = "agent-forge") -> EventBus:
    """NATS when configured, in-process otherwise."""
    if not fabric_url:
        log.info("events.in_process", detail="no fabric_url; running the in-process bus")
        return InMemoryBus()
    from agent_forge.events.nats_impl import NatsBus

    return NatsBus(fabric_url, source=source)


def build_ledger(
    path: str | Path = DEFAULT_LEDGER_PATH,
    *,
    bus: Any | None = None,
    source: str = "agent-forge",
) -> EvidenceLedger:
    """The local hash chain, optionally mirroring records onto the fabric."""
    return EvidenceLedger(JsonlSink(path), bus=bus, source=source)
