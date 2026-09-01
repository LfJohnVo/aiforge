"""The same case table, evaluated by the real Rego under OPA.

RF-09 requires the policies to be evaluable offline. Offline here means "without a running
PDP service", not "without OPA": the ``.rego`` files are the artefact a platform team
reviews and deploys, and a test that only exercised the Python translation would leave
them unverified -- which is how a policy that looks right and evaluates wrong reaches
production.

OPA runs from its pinned container image, the same one Compose uses. Where Docker is not
available the module skips with an explicit reason; ``test_local_pdp.py`` still holds the
table, so the invariants are never simply untested.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

import pytest

from tests.policies.cases import CASES, Case, case_ids

REPO_ROOT = Path(__file__).resolve().parents[2]
POLICY_DIR = REPO_ROOT / "configs" / "policies"
# Pinned to the tag Compose deploys, so a policy that passes here is the policy that will
# run in the cluster.
OPA_IMAGE = "openpolicyagent/opa:1.20.1-static"
TIMEOUT_SECONDS = 120


@lru_cache(maxsize=1)
def _runner() -> list[str] | None:
    """How to run OPA here: a local binary, a container, or not at all."""
    binary = shutil.which("opa")
    if binary:
        return [binary]
    docker = shutil.which("docker")
    if not docker:
        return None
    try:
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            [docker, "info"], capture_output=True, timeout=30, check=True
        )
    except (subprocess.SubprocessError, OSError):
        return None
    return [
        docker,
        "run",
        "--rm",
        "-i",
        "-v",
        f"{POLICY_DIR.as_posix()}:/policies:ro",
        OPA_IMAGE,
    ]


pytestmark = pytest.mark.skipif(
    _runner() is None,
    reason="neither an `opa` binary nor a usable Docker daemon is available",
)


def _policy_path(runner: list[str]) -> str:
    return "/policies" if "docker" in runner[0] else str(POLICY_DIR)


def _evaluate(query: str, document: dict[str, object]) -> dict[str, object]:
    """Run ``opa eval`` with the document on stdin and return the decision."""
    runner = _runner()
    assert runner is not None
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [*runner, "eval", "-d", _policy_path(runner), "-I", "-f", "json", query],
        input=json.dumps(document),
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        pytest.fail(f"opa eval failed: {completed.stderr[:800]}")
    payload = json.loads(completed.stdout)
    results = payload.get("result") or []
    if not results:
        pytest.fail(f"{query} produced no result: {completed.stdout[:400]}")
    value = results[0]["expressions"][0]["value"]
    assert isinstance(value, dict)
    return value


def test_the_policy_bundle_compiles() -> None:
    """``opa check`` before anything else: a syntax error makes every other test a lie."""
    runner = _runner()
    assert runner is not None
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [*runner, "check", _policy_path(runner)],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[:1000]


@pytest.mark.parametrize("case", CASES, ids=case_ids())
def test_rego_matches_the_case_table(case: Case) -> None:
    """The Rego base and ``LocalPdp`` must answer identically. This is that check."""
    decision = _evaluate(f"data.{case.request.kind.package}.decision", case.request.to_input())

    assert decision["allow"] is case.allow, f"{case.name}: reasons were {decision['reasons']}"

    reasons = [str(r) for r in decision.get("reasons", [])]
    assert reasons, f"{case.name}: the policy denied or allowed without a reason"
    if case.reason_contains:
        joined = " | ".join(reasons)
        assert case.reason_contains.lower() in joined.lower(), (
            f"{case.name}: no reason mentions {case.reason_contains!r}; got {joined!r}"
        )

    obligations = [str(o) for o in decision.get("obligations", [])]
    for obligation in case.obligations:
        assert obligation in obligations, f"{case.name}: missing {obligation}"
    if case.autonomy:
        assert str(decision.get("autonomy")) == case.autonomy


def test_an_empty_input_document_is_denied_by_every_package() -> None:
    """Default deny, checked rather than assumed.

    A package whose ``default`` line is missing evaluates to *undefined* on an input it
    does not match, and an undefined decision read carelessly is an allow.
    """
    for package in ("peak.knowledge", "peak.tools", "peak.models", "peak.autonomy"):
        decision = _evaluate(f"data.{package}.decision", {})
        assert decision["allow"] is False, f"{package} allowed an empty input document"


def test_an_unknown_classification_is_treated_as_the_most_restrictive() -> None:
    """A typo in a classification must fail safe, not read as C0.

    Rego has no enums, so ``level()`` maps unknown values to 4. Without that, ``"C5"`` --
    or ``"c2"``, or a null -- would compare as 0 and sail past every ceiling check.
    """
    document = {
        "kind": "models",
        "tenant_id": "acme-mx",
        "user_id": "u1",
        "groups": [],
        "authenticated": True,
        "ceiling": "C4",
        "classification": "NOT-A-LEVEL",
        "resource": "anthropic/claude",
        "sovereignty": "external",
        "acl_groups": [],
        "autonomy_declared": "A0",
        "action_category": "",
        "attributes": {},
    }

    decision = _evaluate("data.peak.models.decision", document)

    assert decision["allow"] is False
