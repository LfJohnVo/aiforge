"""The shared case table, evaluated by the in-process policy engine.

``test_rego.py`` runs the same table through the real Rego under OPA. Between them they
are what keeps the two implementations from drifting.
"""

from __future__ import annotations

import pytest

from agent_forge.governance.decisions import PolicyVerdict
from agent_forge.governance.pdp import LocalPdp
from tests.policies.cases import CASES, Case, case_ids


@pytest.mark.parametrize("case", CASES, ids=case_ids())
async def test_local_policy_matches_the_case_table(case: Case) -> None:
    verdict = await LocalPdp().decide(case.request)

    assert verdict.allow is case.allow, f"{case.name}: reasons were {verdict.reasons}"
    _assert_reason(case, verdict)
    for obligation in case.obligations:
        assert obligation in verdict.obligations, f"{case.name}: missing {obligation}"
    if case.autonomy:
        assert str(verdict.autonomy) == case.autonomy


def _assert_reason(case: Case, verdict: PolicyVerdict) -> None:
    if not case.reason_contains:
        return
    joined = " | ".join(verdict.reasons)
    assert case.reason_contains.lower() in joined.lower(), (
        f"{case.name}: no reason mentions {case.reason_contains!r}; got {joined!r}"
    )


async def test_every_decision_carries_at_least_one_reason() -> None:
    """A verdict with no reason cannot be explained to a user or defended in a review."""
    pdp = LocalPdp()

    for case in CASES:
        verdict = await pdp.decide(case.request)
        assert verdict.reasons, f"{case.name} produced a verdict with no reason"
