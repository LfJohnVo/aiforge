"""The data-sovereignty invariant: C3/C4 content never reaches an external backend.

DoD item 8. These tests are not metrics with a threshold; a single failure here is a
blocking security defect, which is why `evals.yml` runs them before anything else.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agent_forge.core.classification import Classification, accumulate, coerce_payload
from agent_forge.core.errors import ModelGatewayError, SovereigntyError
from agent_forge.gateway.model_policy import (
    ModelBackend,
    ModelPolicy,
    Sovereignty,
    _parse_ceiling,
)

LOCAL_FAST = ModelBackend("local/fast", Sovereignty.LOCAL, Classification.C4)
LOCAL_QUALITY = ModelBackend("local/quality", Sovereignty.LOCAL, Classification.C4)
EXTERNAL = ModelBackend("anthropic/claude", Sovereignty.EXTERNAL, Classification.C2)
LOCAL_EMBED = ModelBackend(
    "local/embeddings", Sovereignty.LOCAL, Classification.C4, purpose="embedding"
)


@pytest.fixture
def policy() -> ModelPolicy:
    return ModelPolicy([LOCAL_FAST, LOCAL_QUALITY, EXTERNAL, LOCAL_EMBED])


# ------------------------------------------------------------------ the invariant


@pytest.mark.parametrize("classification", [Classification.C3, Classification.C4])
def test_classified_content_never_routes_externally(
    policy: ModelPolicy, classification: Classification
) -> None:
    decision = policy.route(
        classification,
        preferred=["anthropic/claude", "local/fast"],
        external_allowed=["anthropic/claude"],
    )

    assert decision.backend.is_local
    assert any("cannot serve" in reason for reason in decision.rejected)


@pytest.mark.parametrize("classification", [Classification.C3, Classification.C4])
def test_classified_content_with_only_external_backends_raises(
    classification: Classification,
) -> None:
    """Refusing to answer is the correct outcome; falling back externally is not."""
    external_only = ModelPolicy([EXTERNAL])

    with pytest.raises(SovereigntyError):
        external_only.route(
            classification,
            preferred=["anthropic/claude"],
            external_allowed=["anthropic/claude"],
        )


def test_assert_allowed_catches_classification_raised_after_routing(
    policy: ModelPolicy,
) -> None:
    """A tool result can raise the cumulative classification after route() decided."""
    decision = policy.route(
        Classification.C1, preferred=["anthropic/claude"], external_allowed=["anthropic/claude"]
    )
    assert decision.alias == "anthropic/claude"

    with pytest.raises(SovereigntyError):
        policy.assert_allowed(decision.alias, Classification.C3)


def test_external_backend_cannot_declare_a_ceiling_above_c2() -> None:
    """A misconfiguration that would break the guarantee must stop construction."""
    lying = ModelBackend("rogue/model", Sovereignty.EXTERNAL, Classification.C4)

    with pytest.raises(SovereigntyError):
        ModelPolicy([lying, LOCAL_FAST])


def test_config_without_sovereignty_metadata_is_treated_as_external() -> None:
    """Forgetting the metadata must not silently create a channel for classified data."""
    config = {"model_list": [{"model_name": "mystery", "model_info": {"mode": "chat"}}]}

    policy = ModelPolicy.from_litellm_config(config)
    backend = policy.get("mystery")

    assert backend.sovereignty is Sovereignty.EXTERNAL
    assert backend.max_classification == Classification.C2
    assert not backend.accepts(Classification.C3)


def test_external_claim_of_c4_in_config_is_clamped_not_honoured() -> None:
    config = {
        "model_list": [
            {
                "model_name": "rogue",
                "model_info": {
                    "mode": "chat",
                    "metadata": {"sovereignty": "external", "max_classification": "C4"},
                },
            }
        ]
    }

    policy = ModelPolicy.from_litellm_config(config)

    assert policy.get("rogue").max_classification == Classification.C2


def test_shipped_litellm_config_declares_sovereignty_correctly(repo_root: Path) -> None:
    """The config we ship must itself satisfy the invariant."""
    config = yaml.safe_load((repo_root / "configs" / "litellm.yaml").read_text(encoding="utf-8"))

    policy = ModelPolicy.from_litellm_config(config)

    for alias in policy.aliases():
        backend = policy.get(alias)
        if not backend.is_local:
            assert backend.max_classification <= Classification.C2, alias
    # And the sovereign path actually exists for classified work.
    assert policy.local_aliases("chat")
    assert policy.local_aliases("embedding")


# ------------------------------------------------------------------- preferences


def test_preferred_alias_wins_when_it_is_allowed(policy: ModelPolicy) -> None:
    decision = policy.route(
        Classification.C1, preferred=["anthropic/claude"], external_allowed=["anthropic/claude"]
    )

    assert decision.alias == "anthropic/claude"


def test_external_not_in_profile_allowlist_is_skipped(policy: ModelPolicy) -> None:
    decision = policy.route(
        Classification.C0, preferred=["anthropic/claude", "local/fast"], external_allowed=[]
    )

    assert decision.alias == "local/fast"
    assert any("allowlist" in reason for reason in decision.rejected)


def test_falls_back_to_a_local_backend_rather_than_failing(policy: ModelPolicy) -> None:
    decision = policy.route(Classification.C2, preferred=["does/not-exist"])

    assert decision.backend.is_local
    assert "fallback" in decision.reason


def test_tool_support_is_honoured() -> None:
    no_tools = ModelBackend(
        "local/plain", Sovereignty.LOCAL, Classification.C4, supports_tools=False
    )
    policy = ModelPolicy([no_tools, LOCAL_FAST])

    decision = policy.route(Classification.C2, preferred=["local/plain"], require_tools=True)

    assert decision.alias == "local/fast"


def test_purpose_is_honoured(policy: ModelPolicy) -> None:
    decision = policy.route(Classification.C4, preferred=["local/fast"], purpose="embedding")

    assert decision.alias == "local/embeddings"


def test_unknown_alias_lookup_raises(policy: ModelPolicy) -> None:
    with pytest.raises(ModelGatewayError):
        policy.get("nope")


def test_empty_policy_is_rejected() -> None:
    with pytest.raises(ModelGatewayError):
        ModelPolicy([])


def test_config_without_model_list_is_rejected() -> None:
    with pytest.raises(ModelGatewayError):
        ModelPolicy.from_litellm_config({})


# -------------------------------------------------------------- classification


def test_accumulate_takes_the_maximum() -> None:
    assert accumulate("C0", None, "C3", Classification.C1) == Classification.C3
    assert accumulate() == Classification.C0


def test_unparseable_payload_classification_defaults_to_c4_not_c0() -> None:
    """Unknown sensitivity must never become public by accident."""
    assert coerce_payload("banana") == Classification.C4
    assert coerce_payload(None) == Classification.C4
    assert coerce_payload("c2") == Classification.C2


def test_classification_parse_rejects_none() -> None:
    with pytest.raises(ValueError, match="required"):
        Classification.parse(None)


def test_parse_ceiling_falls_back_safely() -> None:
    assert _parse_ceiling("nonsense", Sovereignty.LOCAL) == Classification.C4
    assert _parse_ceiling("nonsense", Sovereignty.EXTERNAL) == Classification.C2
