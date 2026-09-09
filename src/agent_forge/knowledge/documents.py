"""The units the knowledge pipeline moves: documents and chunks.

A chunk carries its own access control. That is the whole design: the ACL and the
classification travel *with the text*, into the vector store's payload, so the filter can
run inside the search instead of after it (ADR-004). Nothing downstream has to remember
to apply them.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agent_forge.core.classification import Classification
from agent_forge.core.state import Citation

# Stable namespace for chunk point ids. Changing it re-keys every corpus, so it is a
# constant, not configuration.
POINT_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class SourceRef:
    """Where a document came from, addressable and stable across syncs."""

    kind: str  # folder | sharepoint | s3
    locator: str  # path, drive item id, object key

    @property
    def source_id(self) -> str:
        return f"{self.kind}://{self.locator}"

    def __str__(self) -> str:
        return self.source_id


@dataclass(frozen=True, slots=True)
class AccessControl:
    """Who may see a piece of content.

    Empty ``groups`` and empty ``users`` means "nobody but the tenant's admins" -- not
    "everybody". Access defaults closed here for the same reason it does everywhere else:
    a document whose ACL failed to resolve must not become public.
    """

    groups: frozenset[str] = frozenset()
    users: frozenset[str] = frozenset()

    def permits(self, *, user_id: str, groups: Sequence[str]) -> bool:
        return bool(self.groups & frozenset(groups)) or user_id in self.users

    def to_payload(self) -> dict[str, list[str]]:
        return {"acl_groups": sorted(self.groups), "acl_users": sorted(self.users)}

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> AccessControl:
        return cls(
            groups=frozenset(payload.get("acl_groups") or []),
            users=frozenset(payload.get("acl_users") or []),
        )


@dataclass(slots=True)
class Document:
    """A parsed source document, before chunking."""

    source: SourceRef
    tenant_id: str
    title: str
    text: str
    acl: AccessControl = field(default_factory=AccessControl)
    classification: Classification = Classification.C2
    updated_at: datetime = field(default_factory=_now)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Normalise a classification that arrived as a string.

        The field is typed `Classification`, and anything built from JSON -- an eval
        corpus, a payload -- hands it `"C2"` instead. That does not fail loudly: `"C2" ==
        Classification.C2` is simply False, so the document compares wrong everywhere
        until something tries to order it and raises. mypy calls the branch unreachable
        because from the annotation it is; the guard exists for the callers the annotation
        does not reach.
        """
        current: object = self.classification
        if not isinstance(current, Classification):
            self.classification = Classification.parse(current)  # type: ignore[arg-type]

    @property
    def source_id(self) -> str:
        return self.source.source_id

    @property
    def content_hash(self) -> str:
        """Content-addressed, so an unchanged document is not re-embedded."""
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:32]


@dataclass(slots=True)
class Chunk:
    """A retrievable span of a document, carrying its own access control."""

    chunk_id: str
    source_id: str
    tenant_id: str
    text: str
    title: str = ""
    acl: AccessControl = field(default_factory=AccessControl)
    classification: Classification = Classification.C2
    updated_at: datetime = field(default_factory=_now)
    position: int = 0
    vector: tuple[float, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def reference(self) -> str:
        return f"{self.source_id}#{self.chunk_id}"

    @property
    def point_id(self) -> str:
        """Deterministic id, so re-ingesting a document replaces rather than duplicates.

        A UUID5 rather than a raw digest: Qdrant only accepts unsigned integers or UUIDs
        as point ids, and a hex string is neither.
        """
        return str(
            uuid.uuid5(POINT_NAMESPACE, f"{self.tenant_id}|{self.source_id}|{self.chunk_id}")
        )

    def to_citation(self, score: float = 0.0) -> Citation:
        return Citation(
            source_id=self.source_id,
            chunk_id=self.chunk_id,
            title=self.title,
            score=score,
            classification=self.classification,
        )

    def to_payload(self) -> dict[str, Any]:
        """The vector-store payload. Every filter field is here and indexed."""
        return {
            "tenant_id": self.tenant_id,
            "source_id": self.source_id,
            "chunk_id": self.chunk_id,
            "title": self.title,
            "text": self.text,
            "classification": int(self.classification),
            "updated_at": self.updated_at.isoformat(),
            "position": self.position,
            **self.acl.to_payload(),
            **self.metadata,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Chunk:
        known = {
            "tenant_id",
            "source_id",
            "chunk_id",
            "title",
            "text",
            "classification",
            "updated_at",
            "position",
            "acl_groups",
            "acl_users",
        }
        raw_date = payload.get("updated_at")
        return cls(
            chunk_id=str(payload.get("chunk_id", "")),
            source_id=str(payload.get("source_id", "")),
            tenant_id=str(payload.get("tenant_id", "")),
            text=str(payload.get("text", "")),
            title=str(payload.get("title", "")),
            acl=AccessControl.from_payload(payload),
            classification=Classification(int(payload.get("classification", 2))),
            updated_at=datetime.fromisoformat(raw_date) if raw_date else _now(),
            position=int(payload.get("position", 0)),
            metadata={k: v for k, v in payload.items() if k not in known},
        )


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """A chunk that survived the filter, with its score."""

    chunk: Chunk
    score: float
    retriever: str = ""  # vector | bm25 | graph | fused

    @property
    def text(self) -> str:
        return self.chunk.text

    @property
    def citation(self) -> Citation:
        return self.chunk.to_citation(self.score)


# ---------------------------------------------------------------------- chunking

# Split on blank lines first, then sentences. Splitting mid-sentence produces chunks that
# retrieve well and read badly, and the reader is a language model that will quote them.
_PARAGRAPH = re.compile(r"\n\s*\n")
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-ZÁÉÍÓÚÑ¿¡])")

DEFAULT_CHUNK_CHARS = 1200
DEFAULT_OVERLAP_CHARS = 150


def chunk_text(
    text: str,
    *,
    max_chars: int = DEFAULT_CHUNK_CHARS,
    overlap: int = DEFAULT_OVERLAP_CHARS,
) -> list[str]:
    """Semantic chunking: paragraphs, then sentences, never mid-word.

    Overlap exists so a fact that straddles a boundary is retrievable from either side.
    """
    cleaned = text.strip()
    if not cleaned:
        return []
    if len(cleaned) <= max_chars:
        return [cleaned]

    units = [u.strip() for u in _PARAGRAPH.split(cleaned) if u.strip()]
    pieces: list[str] = []
    for unit in units:
        if len(unit) <= max_chars:
            pieces.append(unit)
            continue
        pieces.extend(_split_long(unit, max_chars))

    return _merge(pieces, max_chars=max_chars, overlap=overlap)


def _split_long(unit: str, max_chars: int) -> list[str]:
    """Break an oversized paragraph on sentence boundaries, then on words."""
    out: list[str] = []
    current = ""
    for sentence in _SENTENCE.split(unit):
        if len(sentence) > max_chars:
            if current:
                out.append(current.strip())
                current = ""
            out.extend(_split_words(sentence, max_chars))
            continue
        if len(current) + len(sentence) + 1 > max_chars:
            out.append(current.strip())
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        out.append(current.strip())
    return out


def _split_words(sentence: str, max_chars: int) -> list[str]:
    out: list[str] = []
    current = ""
    for word in sentence.split():
        if len(current) + len(word) + 1 > max_chars:
            out.append(current.strip())
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        out.append(current.strip())
    return out


def _merge(pieces: Sequence[str], *, max_chars: int, overlap: int) -> list[str]:
    """Pack small pieces together and carry an overlap tail into the next chunk."""
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        candidate = f"{current}\n\n{piece}".strip() if current else piece
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            chunks.append(current)
            tail = current[-overlap:] if overlap else ""
            current = f"{tail}\n\n{piece}".strip() if tail else piece
        else:
            current = piece
    if current:
        chunks.append(current)
    return chunks


def build_chunks(document: Document, **kwargs: int) -> list[Chunk]:
    """Chunk a document, propagating its ACL and classification to every piece."""
    return [
        Chunk(
            chunk_id=f"p{index}",
            source_id=document.source_id,
            tenant_id=document.tenant_id,
            text=text,
            title=document.title,
            acl=document.acl,
            classification=document.classification,
            updated_at=document.updated_at,
            position=index,
            metadata=dict(document.metadata),
        )
        for index, text in enumerate(chunk_text(document.text, **kwargs))
    ]
