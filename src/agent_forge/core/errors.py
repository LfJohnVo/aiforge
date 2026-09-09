"""Domain errors.

Every error carries a stable ``code``: it is surfaced in API responses, written to the
evidence ledger and used by operators in the runbook, so renaming one is a breaking
change (see docs/VERSIONING.md).
"""

from __future__ import annotations

from typing import Any


class AgentForgeError(Exception):
    """Base class for every error this system raises on purpose."""

    code: str = "agent_forge_error"
    http_status: int = 500

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context = context

    def to_dict(self) -> dict[str, Any]:
        """Serialisable form, safe to return to a caller: no stack, no secrets."""
        return {"code": self.code, "message": self.message, **self.context}

    def __str__(self) -> str:
        if not self.context:
            return self.message
        details = ", ".join(f"{k}={v!r}" for k, v in sorted(self.context.items()))
        return f"{self.message} ({details})"


# --------------------------------------------------------------------- config


class ProfileError(AgentForgeError):
    """The instance profile is missing, malformed or internally inconsistent."""

    code = "profile_invalid"
    http_status = 500


class InsecureConfigurationError(AgentForgeError):
    """Production was asked to start with a configuration only development may use.

    Raised at startup and never recovered from. The alternative -- warn and continue --
    is how a cell ends up serving real users with development authentication, which is
    exactly the failure the check exists to prevent.
    """

    code = "insecure_configuration"
    http_status = 500


class CapabilityUnavailableError(AgentForgeError):
    """The profile asks for a capability whose optional extra is not installed.

    Raised at startup, never mid-request: a missing extra must be a boot failure with a
    clear message, not a surprise three hours into production (ADR-005).
    """

    code = "capability_unavailable"
    http_status = 500


# ------------------------------------------------------------------- identity


class AuthenticationError(AgentForgeError):
    """No verifiable identity was presented."""

    code = "unauthenticated"
    http_status = 401


class AuthorizationError(AgentForgeError):
    """Identity is known but not permitted to do this."""

    code = "forbidden"
    http_status = 403


# ----------------------------------------------------------------- governance


class PolicyDeniedError(AgentForgeError):
    """A policy decision point denied the operation."""

    code = "policy_denied"
    http_status = 403


class SovereigntyError(AgentForgeError):
    """An attempt to route classified content to a non-sovereign backend.

    This is the single most important error in the system. It means a control worked;
    it is also a signal that something upstream tried to do the wrong thing, so it is
    always recorded and always surfaces in metrics.
    """

    code = "data_sovereignty_violation"
    http_status = 403


class AutonomyError(AgentForgeError):
    """An action requires a higher autonomy level than this context grants."""

    code = "autonomy_insufficient"
    http_status = 403


class ApprovalError(AgentForgeError):
    """An approval decision is invalid: wrong approver, wrong state, or duplicated."""

    code = "approval_invalid"
    http_status = 409


# ------------------------------------------------------------------- runtime


class ToolError(AgentForgeError):
    """A connector could not complete a tool call."""

    code = "tool_failed"
    http_status = 502


class ModelGatewayError(AgentForgeError):
    """The model gateway is unreachable or refused the request."""

    code = "model_gateway_error"
    http_status = 502


class TaskNotFoundError(AgentForgeError):
    """No task with that identifier for this tenant."""

    code = "task_not_found"
    http_status = 404


class BudgetExceededError(AgentForgeError):
    """The tenant has spent its configured budget."""

    code = "budget_exceeded"
    http_status = 429
