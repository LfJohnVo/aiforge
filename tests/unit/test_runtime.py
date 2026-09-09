"""Startup gates: what production refuses to boot with.

These are the checks nobody sees working. Their whole value is that a cell configured
the development way cannot quietly end up serving real users -- so every case here is
asserted as a *boot failure*, not as a warning, and the message is asserted too: an
operator woken at 3am reads the reason, not the stack.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_forge.core.errors import InsecureConfigurationError
from agent_forge.profile import load_profile
from agent_forge.runtime import Settings, enforce_production_settings

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PROFILE = REPO_ROOT / "configs" / "agent.profile.example.yaml"

# The smallest environment that production accepts. Each test breaks exactly one thing.
PRODUCTION_ENV = {
    "AGENT_FORGE_ENV": "production",
    "OIDC_ISSUER": "https://login.example.test/tenant",
    "OIDC_JWKS_URL": "https://login.example.test/tenant/keys",
}


def _profile(**env: str):
    merged = {**PRODUCTION_ENV, **env}
    return load_profile(EXAMPLE_PROFILE, env=merged)


def _settings(**env: str) -> Settings:
    return Settings.from_env({**PRODUCTION_ENV, **env})


def _enforce(**env: str) -> None:
    merged = {**PRODUCTION_ENV, **env}
    enforce_production_settings(_settings(**env), _profile(**env), merged)


def test_a_correctly_configured_production_cell_starts() -> None:
    """The baseline. Without this the other tests could pass for the wrong reason."""
    _enforce()


def test_production_refuses_to_start_without_an_identity_provider() -> None:
    """Invariant 4: an API key names a tenant, not a person.

    With only API keys every user of a tenant carries the same groups, so
    identity-aware retrieval has nothing to filter on and the whole corpus is visible
    to whoever holds the key. Fine on a laptop, a data leak in production.
    """
    with pytest.raises(InsecureConfigurationError) as raised:
        _enforce(OIDC_ISSUER="", OIDC_JWKS_URL="")

    assert "OIDC" in str(raised.value)


def test_half_configured_oidc_is_treated_as_no_oidc() -> None:
    """An issuer without a JWKS URL cannot verify a signature, so it verifies nothing."""
    with pytest.raises(InsecureConfigurationError):
        _enforce(OIDC_JWKS_URL="")


def test_production_refuses_a_permissive_fail_mode_unless_it_is_acknowledged() -> None:
    """Invariant 3. Legitimate during an OPA outage, never a default."""
    with pytest.raises(InsecureConfigurationError) as raised:
        _enforce(GOVERNANCE_FAIL_MODE="permissive_c0c1")

    assert "GOVERNANCE_ACK_PERMISSIVE" in str(raised.value)


def test_an_acknowledged_permissive_fail_mode_is_allowed() -> None:
    """The escape hatch has to work, or an outage becomes an outage plus a hostage."""
    _enforce(GOVERNANCE_FAIL_MODE="permissive_c0c1", GOVERNANCE_ACK_PERMISSIVE="1")


def test_production_refuses_a_security_variable_that_configures_nothing() -> None:
    """`DEV_SHARED_SECRET` was never read by any code path.

    Left in a copied `.env`, it reads like authentication is configured. Refusing is
    cheaper than an operator believing in a control that does not exist.
    """
    with pytest.raises(InsecureConfigurationError) as raised:
        _enforce(DEV_SHARED_SECRET="hunter2")

    assert "DEV_SHARED_SECRET" in str(raised.value)


def test_the_reasons_are_reported_together_not_one_per_restart() -> None:
    """Three restarts to learn three problems is how a deploy window gets burned."""
    with pytest.raises(InsecureConfigurationError) as raised:
        _enforce(
            OIDC_ISSUER="",
            OIDC_JWKS_URL="",
            GOVERNANCE_FAIL_MODE="permissive_c0c1",
            DEV_SHARED_SECRET="hunter2",
        )

    reasons = raised.value.context["reasons"]
    assert len(reasons) == 3


def test_the_failure_never_repeats_the_secret_it_is_complaining_about() -> None:
    """The message reaches logs and an error report; the value must not travel with it."""
    with pytest.raises(InsecureConfigurationError) as raised:
        _enforce(DEV_SHARED_SECRET="hunter2")

    assert "hunter2" not in str(raised.value)
    assert "hunter2" not in repr(raised.value.to_dict())


def test_development_accepts_everything_production_rejects() -> None:
    """The quickstart must keep working with an API key and no identity provider."""
    env = {
        "AGENT_FORGE_ENV": "development",
        "GOVERNANCE_FAIL_MODE": "permissive_c0c1",
        "DEV_SHARED_SECRET": "hunter2",
    }
    enforce_production_settings(Settings.from_env(env), load_profile(EXAMPLE_PROFILE, env=env), env)
