"""Embeddings.

Two implementations, both real:

* ``GatewayEmbeddings`` -- bge-m3 (or whatever the profile aliases) through the governed
  model gateway. Always a sovereign backend: the vector looks like noise, but the text
  that produced it is exactly the sensitive thing.
* ``HashingEmbeddings`` -- feature hashing over character n-grams and words. A real
  technique, not a stub: deterministic, dependency-free, and good enough for lexical
  similarity, which is what makes the retrieval tests runnable everywhere. It does not
  capture paraphrase, and the docs say so.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from agent_forge.core.classification import Classification
from agent_forge.core.errors import CapabilityUnavailableError, ModelGatewayError
from agent_forge.gateway.litellm_client import GovernedGateway
from agent_forge.observability.logging import get_logger
from agent_forge.observability.metrics import get_metrics

log = get_logger(__name__)

# bge-m3's dimensionality. The hashing embedder matches it so a corpus can be re-indexed
# from one to the other without recreating the collection.
DEFAULT_DIM = 1024
_WORD = re.compile(r"[a-z0-9ñ]+")


@runtime_checkable
class Embeddings(Protocol):
    """Batch text-to-vector."""

    dimension: int

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


class GatewayEmbeddings:
    """Embeddings via the model gateway, under the sovereignty policy."""

    def __init__(
        self,
        gateway: GovernedGateway,
        *,
        alias: str = "local/embeddings",
        dimension: int = DEFAULT_DIM,
        batch_size: int = 32,
    ) -> None:
        self._gateway = gateway
        self._alias = alias
        self.dimension = dimension
        self._batch = max(1, batch_size)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for start in range(0, len(texts), self._batch):
            batch = list(texts[start : start + self._batch])
            out.extend(
                await self._gateway.embed(
                    batch,
                    # Unknown-sensitivity content: only a sovereign backend may see it.
                    classification=Classification.C4,
                    preferred=[self._alias],
                )
            )
        return out

    async def embed_query(self, text: str) -> list[float]:
        vectors = await self.embed([text])
        return vectors[0] if vectors else [0.0] * self.dimension


class HashingEmbeddings:
    """Feature hashing over words and character trigrams.

    Deterministic and offline. Signed hashing keeps collisions from systematically
    inflating similarity; character trigrams give partial robustness to inflection and
    typos, which matters in Spanish. What it cannot do is match paraphrases -- that needs
    a trained model, and the profile should configure one for production.
    """

    def __init__(self, dimension: int = DEFAULT_DIM, *, char_ngrams: int = 3) -> None:
        self.dimension = dimension
        self._n = char_ngrams

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._vector(text)

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for feature, weight in self._features(text):
            index, sign = _bucket(feature, self.dimension)
            vector[index] += sign * weight
        return _normalise(vector)

    def _features(self, text: str) -> list[tuple[str, float]]:
        normalised = _fold(text)
        words = _WORD.findall(normalised)
        features: list[tuple[str, float]] = [(f"w:{w}", 1.0) for w in words]
        # Trigrams weigh less: they are noisier and would otherwise dominate.
        for word in words:
            padded = f" {word} "
            features.extend(
                (f"c:{padded[i : i + self._n]}", 0.35)
                for i in range(max(0, len(padded) - self._n + 1))
            )
        return features


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _bucket(feature: str, dimension: int) -> tuple[int, float]:
    digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    return value % dimension, 1.0 if (value >> 63) & 1 else -1.0


def _normalise(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0.0:
        return vector
    return [v / norm for v in vector]


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(x * y for x, y in zip(left, right, strict=True))
    norm = math.sqrt(sum(x * x for x in left)) * math.sqrt(sum(y * y for y in right))
    return dot / norm if norm else 0.0


def build_embeddings(
    gateway: GovernedGateway | None,
    *,
    alias: str = "local/embeddings",
    dimension: int = DEFAULT_DIM,
    strict: bool = False,
) -> Embeddings:
    """The gateway when there is one, hashing otherwise -- and say which, loudly.

    ``strict`` (production) makes the absence fatal. The hashing embedder is a genuine
    fallback -- retrieval still returns something -- and that is exactly what makes it
    dangerous here: a cell that silently answers from lexical overlap looks like a
    working RAG until someone asks a question in different words than the document used.
    A warning in a log nobody greps is not enough for that.
    """
    if gateway is None:
        if strict:
            raise CapabilityUnavailableError(
                "no model gateway, so retrieval would fall back to lexical hashing",
                hint="configure LITELLM_BASE_URL, or run with AGENT_FORGE_ENV=development",
            )
        log.warning(
            "embeddings.hashing_selected",
            detail="no model gateway; lexical similarity only, paraphrases will not match",
        )
        return HashingEmbeddings(dimension)
    return GatewayEmbeddings(gateway, alias=alias, dimension=dimension)


async def safe_embed(
    embeddings: Embeddings, texts: Sequence[str], *, fallback: Embeddings | None = None
) -> list[list[float]]:
    """Embed, degrading to the fallback if the gateway is unreachable.

    Ingestion that fails entirely because the embedding backend blinked is worse than
    ingestion that produces lexically-indexed chunks and logs it.
    """
    try:
        return await embeddings.embed(texts)
    except ModelGatewayError as exc:
        if fallback is None:
            raise
        log.error(
            "embeddings.degraded_to_fallback",
            detail=str(exc),
            impact="these chunks are indexed lexically and will not match paraphrases",
        )
        get_metrics().degraded.labels(component="embeddings").inc()
        return await fallback.embed(texts)
