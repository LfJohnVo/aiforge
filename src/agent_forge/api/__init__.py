"""HTTP surface: application assembly, auth, admin and health."""

from __future__ import annotations

__all__ = ["create_app"]


def __getattr__(name: str) -> object:
    # Lazy so that importing `agent_forge.api` does not drag FastAPI into tooling that
    # only needs the domain packages.
    if name == "create_app":
        from agent_forge.api.app import create_app

        return create_app
    raise AttributeError(name)
