"""Append-only evidence ledger with a hash chain.

Each record carries the hash of the one before it, so altering or removing a record
invalidates every record after it. That is the whole mechanism: it does not stop a
determined operator with disk access from rewriting the file, it makes the rewrite
*detectable* -- which is what an audit needs.

What is recorded: prompt digests, policy decisions, tool calls, human approvals, judge
verdicts. What is **not** recorded: the prompt text, the answer text, the tool arguments.
Only digests. A ledger is retained for years and read by people who are not entitled to
the tenant's content; storing the content there would turn the audit trail into the
largest unclassified copy of everything the cell ever saw.

One chain per tenant, one file per day. Per tenant because export is per tenant and a
shared chain cannot be exported without leaking the neighbours' sequence; per day because
a single unbounded file is the thing nobody can rotate.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from agent_forge.events.schemas import EVIDENCE_RECORD, CloudEvent, EvidenceRecordData
from agent_forge.observability.logging import get_logger
from agent_forge.observability.metrics import get_metrics

log = get_logger(__name__)

__all__ = [
    "GENESIS_HASH",
    "ChainError",
    "EvidenceLedger",
    "JsonlSink",
    "LedgerRecord",
    "LedgerSink",
    "digest",
    "verify_chain",
    "verify_path",
]

# The predecessor of the first record. All zeros rather than an empty string so a
# truncated file cannot be mistaken for a chain that legitimately starts here.
GENESIS_HASH = "0" * 64

Actor = Literal["agent", "human", "judge", "system"]


class ChainError(Exception):
    """The chain does not verify. Carries where it broke."""

    def __init__(self, message: str, *, seq: int = -1, path: str = "") -> None:
        super().__init__(message)
        self.seq = seq
        self.path = path


def digest(payload: Any) -> str:
    """SHA-256 of a canonical JSON rendering.

    Canonical because the digest has to be reproducible: sorted keys and no incidental
    whitespace, or the same payload hashes differently depending on who serialised it.
    """
    if isinstance(payload, str):
        raw = payload.encode()
    else:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class LedgerRecord:
    """One link. Immutable once written."""

    seq: int
    prev_hash: str
    hash: str
    ts: str
    tenant_id: str
    actor: Actor
    action: str
    payload_digest: str
    # Small, non-sensitive facts worth having without opening another system: the tool
    # name, the verdict, the policy id. Never content.
    metadata: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def compute_hash(
        *,
        seq: int,
        prev_hash: str,
        ts: str,
        tenant_id: str,
        actor: str,
        action: str,
        payload_digest: str,
        metadata: dict[str, Any],
    ) -> str:
        """Hash over every field except the hash itself.

        ``metadata`` is included: leaving it out would let it be edited freely, and the
        tool name in a tool-call record is exactly the field somebody would want to edit.
        """
        return digest(
            {
                "seq": seq,
                "prev_hash": prev_hash,
                "ts": ts,
                "tenant_id": tenant_id,
                "actor": actor,
                "action": action,
                "payload_digest": payload_digest,
                "metadata": metadata,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
            "ts": self.ts,
            "tenant_id": self.tenant_id,
            "actor": self.actor,
            "action": self.action,
            "payload_digest": self.payload_digest,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> LedgerRecord:
        return cls(
            seq=int(raw["seq"]),
            prev_hash=str(raw["prev_hash"]),
            hash=str(raw["hash"]),
            ts=str(raw["ts"]),
            tenant_id=str(raw["tenant_id"]),
            actor=raw.get("actor", "agent"),
            action=str(raw.get("action", "")),
            payload_digest=str(raw.get("payload_digest", "")),
            metadata=dict(raw.get("metadata") or {}),
        )

    def to_event(self, source: str) -> CloudEvent:
        return CloudEvent.wrap(
            EVIDENCE_RECORD,
            EvidenceRecordData(
                seq=self.seq,
                prev_hash=self.prev_hash,
                hash=self.hash,
                ts=self.ts,
                tenant_id=self.tenant_id,
                actor=self.actor,
                action=self.action,
                payload_digest=self.payload_digest,
            ),
            source=source,
            tenant_id=self.tenant_id,
        )


@runtime_checkable
class LedgerSink(Protocol):
    """Where records are appended."""

    async def append(self, record: LedgerRecord) -> None: ...

    def read(self, tenant_id: str) -> Iterator[LedgerRecord]: ...


class JsonlSink:
    """One JSONL file per tenant per day, opened in append mode.

    Append mode and one line per record so a crash mid-write costs the last line and not
    the file, and so an operator can make the directory WORM at the filesystem level --
    which is where write-once actually belongs.
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def _path(self, tenant_id: str, day: str = "") -> Path:
        stamp = day or datetime.now(UTC).strftime("%Y-%m-%d")
        return self._root / _safe_name(tenant_id) / f"{stamp}.jsonl"

    async def append(self, record: LedgerRecord) -> None:
        path = self._path(record.tenant_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"
        # Off the event loop: a sync write to a local file is fast, but "fast" under a
        # full disk is not fast at all, and blocking the loop there stalls every request.
        await asyncio.to_thread(_append_line, path, line)

    def read(self, tenant_id: str) -> Iterator[LedgerRecord]:
        folder = self._root / _safe_name(tenant_id)
        if not folder.is_dir():
            return
        for path in sorted(folder.glob("*.jsonl")):
            yield from _read_file(path)


def _append_line(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)


def _read_file(path: Path) -> Iterator[LedgerRecord]:
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                yield LedgerRecord.from_dict(json.loads(text))
            except (ValueError, KeyError) as exc:
                raise ChainError(
                    f"line {number} is not a ledger record: {exc}", path=str(path)
                ) from exc


class EvidenceLedger:
    """The append-only chain, one sequence per tenant.

    Serialised with a lock per tenant: two concurrent appends that both read the same
    ``prev_hash`` would produce a fork, and a forked chain fails verification in a way
    that looks exactly like tampering.
    """

    def __init__(
        self,
        sink: LedgerSink,
        *,
        bus: Any | None = None,
        source: str = "agent-forge",
    ) -> None:
        self._sink = sink
        self._bus = bus
        self._source = source
        self._heads: dict[str, tuple[int, str]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def record(
        self,
        action: str,
        actor: Actor,
        payload: Any,
        *,
        tenant_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> LedgerRecord:
        """Append one record and return it.

        ``payload`` is digested, never stored.
        """
        lock = self._locks.setdefault(tenant_id, asyncio.Lock())
        async with lock:
            seq, prev_hash = self._head(tenant_id)
            meta = dict(metadata or {})
            ts = datetime.now(UTC).isoformat()
            record = LedgerRecord(
                seq=seq + 1,
                prev_hash=prev_hash,
                hash=LedgerRecord.compute_hash(
                    seq=seq + 1,
                    prev_hash=prev_hash,
                    ts=ts,
                    tenant_id=tenant_id,
                    actor=actor,
                    action=action,
                    payload_digest=digest(payload),
                    metadata=meta,
                ),
                ts=ts,
                tenant_id=tenant_id,
                actor=actor,
                action=action,
                payload_digest=digest(payload),
                metadata=meta,
            )
            await self._sink.append(record)
            self._heads[tenant_id] = (record.seq, record.hash)
        get_metrics().ledger_entries.labels(tenant=tenant_id, action=action).inc()

        if self._bus is not None:
            # Asynchronous by contract: the central Audit Ledger being unavailable must
            # not stop the cell from answering. The local chain is the source of truth.
            try:
                await self._bus.publish(record.to_event(self._source))
            except Exception as exc:
                log.warning("ledger.publish_failed", seq=record.seq, detail=str(exc)[:200])
        return record

    def _head(self, tenant_id: str) -> tuple[int, str]:
        cached = self._heads.get(tenant_id)
        if cached is not None:
            return cached
        # Cold start: resume the existing chain rather than beginning a new one, or a
        # restart would produce two records with seq 1 and break verification.
        last: LedgerRecord | None = None
        for record in self._sink.read(tenant_id):
            last = record
        head = (last.seq, last.hash) if last is not None else (0, GENESIS_HASH)
        self._heads[tenant_id] = head
        return head

    def verify(self, tenant_id: str) -> int:
        """Walk the tenant's chain. Returns how many records verified."""
        return verify_chain(self._sink.read(tenant_id))

    def export(self, tenant_id: str) -> Iterator[str]:
        """JSONL export for one tenant, verified as it goes.

        Verified during export rather than after, so a broken chain is reported instead
        of handed to an auditor as if it were sound.
        """
        expected_prev = GENESIS_HASH
        expected_seq = 1
        for record in self._sink.read(tenant_id):
            _check(record, expected_prev, expected_seq)
            expected_prev, expected_seq = record.hash, record.seq + 1
            yield json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":"))


def verify_chain(records: Iterator[LedgerRecord]) -> int:
    """Verify a sequence of records. Raises ``ChainError`` at the first break."""
    expected_prev = GENESIS_HASH
    expected_seq = 1
    count = 0
    for record in records:
        _check(record, expected_prev, expected_seq)
        expected_prev, expected_seq = record.hash, record.seq + 1
        count += 1
    return count


def _check(record: LedgerRecord, expected_prev: str, expected_seq: int) -> None:
    if record.seq != expected_seq:
        raise ChainError(
            f"sequence jumps from {expected_seq - 1} to {record.seq}: a record is missing",
            seq=record.seq,
        )
    if record.prev_hash != expected_prev:
        raise ChainError(
            f"record {record.seq} points at {record.prev_hash[:12]}... but the previous "
            f"record hashes to {expected_prev[:12]}...",
            seq=record.seq,
        )
    recomputed = LedgerRecord.compute_hash(
        seq=record.seq,
        prev_hash=record.prev_hash,
        ts=record.ts,
        tenant_id=record.tenant_id,
        actor=record.actor,
        action=record.action,
        payload_digest=record.payload_digest,
        metadata=record.metadata,
    )
    if recomputed != record.hash:
        raise ChainError(f"record {record.seq} was modified after it was written", seq=record.seq)


def verify_path(root: str | Path) -> dict[str, int]:
    """Verify every tenant chain under a ledger directory."""
    base = Path(root)
    if not base.is_dir():
        raise ChainError(f"{base} is not a ledger directory", path=str(base))
    results: dict[str, int] = {}
    sink = JsonlSink(base)
    for folder in sorted(p for p in base.iterdir() if p.is_dir()):
        results[folder.name] = verify_chain(sink.read(folder.name))
    return results


def _safe_name(tenant_id: str) -> str:
    """A tenant id becomes a directory name, so it must not be able to escape one."""
    cleaned = "".join(c if c.isalnum() or c in "-_" else "_" for c in tenant_id)
    return cleaned or "_"
