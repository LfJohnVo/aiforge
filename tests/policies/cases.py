"""One table of policy cases, evaluated twice.

``LocalPdp`` is a hand translation of ``configs/policies/*.rego``, which means two things
that can disagree. This table is what stops them: every case runs through the Python
implementation (``test_local_pdp.py``) and through the real Rego under OPA
(``test_rego.py``). A change to one and not the other fails here rather than in
production, where the two would quietly decide differently depending on whether the
remote PDP happened to be reachable.

Each case names the invariant it protects. A case whose name does not say what would go
wrong if it failed is a case nobody will maintain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_forge.core.autonomy import AutonomyLevel
from agent_forge.core.classification import Classification
from agent_forge.governance.decisions import PolicyKind, PolicyRequest

__all__ = ["CASES", "Case", "case_ids"]


@dataclass(frozen=True, slots=True)
class Case:
    """One policy question and what both evaluators must answer."""

    name: str
    request: PolicyRequest
    allow: bool
    # Substring that must appear in at least one reason. Checked rather than the exact
    # text so wording can improve without breaking the table -- but checked at all,
    # because a denial the operator cannot explain is not much better than no denial.
    reason_contains: str = ""
    obligations: tuple[str, ...] = ()
    autonomy: str = ""


def _req(kind: PolicyKind, **kwargs: Any) -> PolicyRequest:
    base: dict[str, Any] = {
        "kind": kind,
        "tenant_id": "acme-mx",
        "user_id": "u1",
        "groups": ("finanzas",),
        "authenticated": True,
        "ceiling": Classification.C2,
        "classification": Classification.C0,
    }
    base.update(kwargs)
    return PolicyRequest(**base)


CASES: tuple[Case, ...] = (
    # ------------------------------------------------------------------ knowledge
    Case(
        name="knowledge/public-material-is-readable-by-anyone",
        request=_req(PolicyKind.KNOWLEDGE, authenticated=False, ceiling=Classification.C0),
        allow=True,
    ),
    Case(
        name="knowledge/anonymous-requester-cannot-read-internal-material",
        request=_req(
            PolicyKind.KNOWLEDGE,
            authenticated=False,
            user_id="",
            groups=(),
            ceiling=Classification.C0,
            classification=Classification.C1,
        ),
        allow=False,
        reason_contains="anonymous",
    ),
    Case(
        name="knowledge/content-above-the-ceiling-is-refused",
        request=_req(
            PolicyKind.KNOWLEDGE, ceiling=Classification.C2, classification=Classification.C3
        ),
        allow=False,
        reason_contains="ceiling",
    ),
    Case(
        name="knowledge/an-acl-the-requester-does-not-hold-denies",
        request=_req(
            PolicyKind.KNOWLEDGE,
            classification=Classification.C2,
            acl_groups=("direccion",),
        ),
        allow=False,
        reason_contains="ACL",
    ),
    Case(
        name="knowledge/one-matching-group-is-enough",
        request=_req(
            PolicyKind.KNOWLEDGE,
            groups=("finanzas", "ventas"),
            classification=Classification.C2,
            acl_groups=("direccion", "finanzas"),
        ),
        allow=True,
    ),
    Case(
        name="knowledge/restricted-material-must-be-cited",
        request=_req(
            PolicyKind.KNOWLEDGE,
            ceiling=Classification.C4,
            classification=Classification.C3,
        ),
        allow=True,
        obligations=("cite_source",),
    ),
    Case(
        name="knowledge/a-request-without-a-tenant-cannot-be-scoped",
        request=_req(PolicyKind.KNOWLEDGE, tenant_id=""),
        allow=False,
        reason_contains="tenant",
    ),
    # ---------------------------------------------------------------------- tools
    Case(
        name="tools/an-identified-requester-may-run-a-reversible-action",
        request=_req(
            PolicyKind.TOOLS, resource="jira.create_ticket", autonomy_declared=AutonomyLevel.A1
        ),
        allow=True,
        autonomy="A1",
    ),
    Case(
        name="tools/an-anonymous-requester-may-not-invoke-tools-at-all",
        request=_req(
            PolicyKind.TOOLS,
            authenticated=False,
            user_id="",
            groups=(),
            resource="jira.create_ticket",
            autonomy_declared=AutonomyLevel.A1,
        ),
        allow=False,
        reason_contains="anonymous",
    ),
    Case(
        name="tools/an-externally-visible-action-carries-a-human-approval-obligation",
        request=_req(PolicyKind.TOOLS, resource="email.send", autonomy_declared=AutonomyLevel.A2),
        allow=True,
        obligations=("human_approval", "ledger_record"),
    ),
    Case(
        name="tools/an-irreversible-action-needs-two-approvers",
        request=_req(PolicyKind.TOOLS, resource="erp.delete", autonomy_declared=AutonomyLevel.A4),
        allow=True,
        obligations=("human_approval", "elevated_role", "two_approvers"),
    ),
    Case(
        name="tools/acting-on-restricted-data-is-never-an-unattended-action",
        request=_req(
            PolicyKind.TOOLS,
            resource="erp.update",
            ceiling=Classification.C4,
            classification=Classification.C3,
            autonomy_declared=AutonomyLevel.A1,
        ),
        allow=False,
        reason_contains="restricted",
    ),
    # --------------------------------------------------------------------- models
    Case(
        name="models/confidential-content-may-go-to-a-governed-external-backend",
        request=_req(
            PolicyKind.MODELS,
            resource="anthropic/claude",
            sovereignty="external",
            classification=Classification.C2,
        ),
        allow=True,
        obligations=("ledger_record", "redact_pii"),
    ),
    Case(
        name="models/restricted-content-never-reaches-an-external-backend",
        request=_req(
            PolicyKind.MODELS,
            resource="anthropic/claude",
            sovereignty="external",
            ceiling=Classification.C4,
            classification=Classification.C3,
        ),
        allow=False,
        reason_contains="never reach an external backend",
    ),
    Case(
        name="models/secret-content-never-reaches-an-external-backend",
        request=_req(
            PolicyKind.MODELS,
            resource="azure/gpt",
            sovereignty="external",
            ceiling=Classification.C4,
            classification=Classification.C4,
        ),
        allow=False,
        reason_contains="never reach an external backend",
    ),
    Case(
        name="models/secret-content-is-fine-on-a-local-backend",
        request=_req(
            PolicyKind.MODELS,
            resource="local/quality",
            sovereignty="local",
            ceiling=Classification.C4,
            classification=Classification.C4,
        ),
        allow=True,
    ),
    Case(
        name="models/a-backend-that-does-not-declare-its-sovereignty-is-refused",
        request=_req(
            PolicyKind.MODELS,
            resource="mystery/model",
            sovereignty="",
            classification=Classification.C0,
        ),
        allow=False,
        reason_contains="sovereignty",
    ),
    # ------------------------------------------------------------------- autonomy
    Case(
        name="autonomy/an-identified-requester-is-granted-a1",
        request=_req(PolicyKind.AUTONOMY, autonomy_declared=AutonomyLevel.A1),
        allow=True,
        autonomy="A1",
    ),
    Case(
        name="autonomy/an-anonymous-requester-is-granted-a0-not-a1",
        request=_req(
            PolicyKind.AUTONOMY,
            authenticated=False,
            user_id="",
            groups=(),
            ceiling=Classification.C0,
            autonomy_declared=AutonomyLevel.A1,
        ),
        allow=False,
        reason_contains="granted A0",
    ),
    Case(
        name="autonomy/restricted-data-drops-the-grant-to-a0",
        request=_req(
            PolicyKind.AUTONOMY,
            ceiling=Classification.C4,
            classification=Classification.C3,
            autonomy_declared=AutonomyLevel.A0,
        ),
        allow=True,
        autonomy="A0",
    ),
    Case(
        name="autonomy/an-a2-action-is-escalated-rather-than-denied",
        request=_req(PolicyKind.AUTONOMY, autonomy_declared=AutonomyLevel.A2),
        allow=True,
        obligations=("human_approval",),
    ),
)


def case_ids() -> list[str]:
    return [case.name for case in CASES]


# Present so a reader sees the count without running anything; a table that silently
# shrinks is a test suite that silently weakens.
EXPECTED_CASE_COUNT = 21
assert len(CASES) == EXPECTED_CASE_COUNT, (
    f"expected {EXPECTED_CASE_COUNT} cases, found {len(CASES)}"
)
