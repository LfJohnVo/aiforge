"""Identity-aware retrieval: the one place that turns identity into a store filter.

The requirement -- "a user without permission does not learn that the document exists" --
is not a product nicety, it is what forces the filter *into the query*. Filtering after
the search would let a forbidden document consume a `top_k` slot, and the absence is
observable: the same question returns fewer results for one person than another.

So there is exactly one function that builds the filter, exactly one type that carries
it, and every store implements the same predicate. No caller composes filters by hand.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from agent_forge.core.classification import Classification
from agent_forge.core.state import Identity
from agent_forge.knowledge.documents import Chunk
from agent_forge.observability.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class AccessFilter:
    """The conditions a chunk must satisfy to be visible to one requester.

    Every field is a hard condition; there is no "soft" mode. ``allow_nothing`` exists so
    an unresolvable identity produces an explicit empty result rather than an accidentally
    permissive filter.
    """

    tenant_id: str
    groups: frozenset[str] = frozenset()
    user_id: str = ""
    ceiling: Classification = Classification.C0
    denied_sources: frozenset[str] = frozenset()
    allow_nothing: bool = False

    def permits(self, chunk: Chunk) -> bool:
        """The predicate. Stores translate it; this is the reference implementation."""
        if self.allow_nothing:
            return False
        if chunk.tenant_id != self.tenant_id:
            return False
        if chunk.classification > self.ceiling:
            return False
        if chunk.source_id in self.denied_sources:
            return False
        return chunk.acl.permits(user_id=self.user_id, groups=tuple(self.groups))

    def describe(self) -> dict[str, Any]:
        """Non-sensitive summary for traces. Never logs the group list itself."""
        return {
            "tenant_id": self.tenant_id,
            "ceiling": str(self.ceiling),
            "group_count": len(self.groups),
            "identified": bool(self.user_id),
            "denied_sources": len(self.denied_sources),
            "allow_nothing": self.allow_nothing,
        }


@dataclass(frozen=True, slots=True)
class PolicyView:
    """What the governance layer contributes to the filter.

    In F1/F2 this is empty. In F6 the PDP fills it, and the filter tightens without any
    caller changing.
    """

    denied_sources: frozenset[str] = frozenset()
    ceiling_override: Classification | None = None
    deny_all: bool = False
    reasons: tuple[str, ...] = field(default=())


def build_filter(
    identity: Identity,
    *,
    policy: PolicyView | None = None,
) -> AccessFilter:
    """Translate (identity x groups x ceiling x policy) into a store filter.

    Called from one place in the retriever. If a second caller ever appears, that is the
    bug: the guarantee depends on this being the only translation.
    """
    view = policy or PolicyView()

    if view.deny_all:
        log.info("access.denied_by_policy", reasons=list(view.reasons))
        return AccessFilter(tenant_id=identity.tenant_id, allow_nothing=True)

    ceiling = identity.classification_ceiling
    if view.ceiling_override is not None:
        # Policy may only tighten. A PDP that tried to raise a ceiling would be
        # overriding the identity's own entitlement, which is not its job.
        ceiling = min(ceiling, view.ceiling_override)

    if identity.is_anonymous:
        # No verified identity: C0 only, and no group membership to match ACLs with.
        # In practice this returns nothing, which is the correct answer.
        return AccessFilter(
            tenant_id=identity.tenant_id,
            groups=frozenset(),
            user_id="",
            ceiling=Classification.C0,
            denied_sources=view.denied_sources,
        )

    return AccessFilter(
        tenant_id=identity.tenant_id,
        groups=frozenset(identity.groups),
        user_id=identity.user_id or "",
        ceiling=ceiling,
        denied_sources=view.denied_sources,
    )


def apply(chunks: Sequence[Chunk], access: AccessFilter) -> list[Chunk]:
    """Reference filtering, used by stores that cannot push the predicate down.

    Applied *before* ranking and truncation, never after: that ordering is the whole
    point.
    """
    return [chunk for chunk in chunks if access.permits(chunk)]


def to_qdrant_filter(access: AccessFilter) -> dict[str, Any]:
    """The same predicate as a Qdrant filter, evaluated inside the HNSW search.

    Returned as a plain dict so this module never imports ``qdrant_client`` (ADR-005);
    the store adapter converts it.
    """
    if access.allow_nothing:
        # A condition that cannot match. Cheaper and safer than special-casing the
        # caller, which would eventually forget.
        return {"must": [{"key": "tenant_id", "match": {"value": "\x00never"}}]}

    must: list[dict[str, Any]] = [
        {"key": "tenant_id", "match": {"value": access.tenant_id}},
        {"key": "classification", "range": {"lte": int(access.ceiling)}},
    ]
    should: list[dict[str, Any]] = []
    if access.groups:
        should.append({"key": "acl_groups", "match": {"any": sorted(access.groups)}})
    if access.user_id:
        should.append({"key": "acl_users", "match": {"any": [access.user_id]}})

    payload: dict[str, Any] = {"must": must}
    if should:
        # At least one ACL condition must hold. Qdrant's `min_should` carries the
        # conditions *inside* it together with a count; leaving them in a plain `should`
        # makes them optional, which is the difference between an ACL and a ranking hint.
        payload["min_should"] = {"conditions": should, "min_count": 1}
    else:
        # No groups and no user id: the requester matches no ACL, and the correct answer
        # is nothing.
        payload["must"].append({"key": "tenant_id", "match": {"value": "\x00never"}})

    if access.denied_sources:
        payload["must_not"] = [
            {"key": "source_id", "match": {"any": sorted(access.denied_sources)}}
        ]
    return payload


# Payload fields that must be indexed for the filter to run inside the search rather
# than as a post-scan. Created by the store on first use.
INDEXED_PAYLOAD_FIELDS: dict[str, str] = {
    "tenant_id": "keyword",
    "acl_groups": "keyword",
    "acl_users": "keyword",
    "source_id": "keyword",
    "classification": "integer",
}
