"""FastAPI application: assembly and lifecycle.

Routers are mounted according to the profile. A channel that is disabled is not mounted
at all -- not mounted-and-403 -- so a disabled surface has no attack surface.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from agent_forge import __version__
from agent_forge.api import admin, health, metrics
from agent_forge.channels import n8n_callback, openai_api
from agent_forge.core.errors import AgentForgeError, ProfileError
from agent_forge.observability.logging import clear_request_context, get_logger
from agent_forge.profile import format_profile_error
from agent_forge.runtime import Runtime, Settings, build_runtime
from agent_forge.upstream import a2a
from agent_forge.upstream.openapi import sanitise_for_copilot_studio

log = get_logger(__name__)


def create_app(
    *, settings: Settings | None = None, env: Mapping[str, str] | None = None
) -> FastAPI:
    """Build the application. The runtime is created in the lifespan, not here."""
    resolved = settings or Settings.from_env(env)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            try:
                runtime = await build_runtime(resolved, stack, env=env)
            except ProfileError as exc:
                # A misconfigured profile must fail visibly at boot with a message an
                # operator can act on, not with a stack trace.
                log.error("startup.profile_invalid", detail=format_profile_error(exc))
                raise
            app.state.runtime = runtime
            await _mount_channels(app, runtime, stack)
            yield

    app = FastAPI(
        title="Agent Forge",
        version=__version__,
        summary="Celula de agente empresarial PEAK: chat, conocimiento y herramientas.",
        lifespan=lifespan,
        # Interactive docs are a discovery surface; production turns them off.
        docs_url=None if resolved.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if resolved.is_production else "/openapi.json",
        root_path=os.environ.get("AGENT_API_ROOT_PATH", ""),
    )

    _install_cors(app, env)
    _install_error_handlers(app)

    app.include_router(health.router)
    app.include_router(metrics.router)
    app.include_router(admin.router)
    return app


async def _mount_channels(app: FastAPI, runtime: Runtime, stack: AsyncExitStack) -> None:
    """Mount only the surfaces the profile enables.

    Takes the lifespan's exit stack because one of them -- the MCP server -- is a
    sub-application with a lifespan of its own that the parent has to start.
    """
    mounted: list[str] = []
    if runtime.profile.channels.openai_api.enabled or runtime.profile.channels.openwebui.enabled:
        # OpenWebUI speaks the OpenAI protocol; one router serves both.
        app.include_router(openai_api.router)
        mounted.append("openai_api")
    if runtime.profile.channels.websocket.enabled:
        from agent_forge.channels import websocket

        app.include_router(websocket.router)
        mounted.append("websocket")
    if runtime.profile.channels.teams.enabled:
        from agent_forge.channels import teams

        app.include_router(teams.router)
        mounted.append("teams")
    if runtime.profile.channels.slack.enabled:
        from agent_forge.channels import slack

        app.include_router(slack.router)
        mounted.append("slack")
    if "n8n" in runtime.connectors.names():
        # Mounted only when an n8n connector exists: an inbound route with nothing behind
        # it is attack surface for no benefit.
        app.include_router(n8n_callback.router)
        mounted.append("n8n_callback")

    upstream: list[str] = []
    if runtime.profile.upstream.a2a.enabled:
        app.include_router(a2a.router)
        upstream.append("a2a")
    if runtime.profile.upstream.mcp_server.enabled:
        await _mount_mcp(app, runtime, stack)
        upstream.append("mcp_server")
    if runtime.profile.upstream.openapi.enabled:
        _mount_openapi(app, runtime)
        upstream.append("openapi")

    log.info("api.surfaces_mounted", channels=mounted, upstream=upstream)


async def _mount_mcp(app: FastAPI, runtime: Runtime, stack: AsyncExitStack) -> None:
    """Publish the cell as an MCP server on the same process and port."""
    from agent_forge.upstream.mcp_server import build_mcp_server

    async def retrieve(query: str, identity: Any, limit: int) -> Any:
        return await runtime.knowledge.retrieve(query, identity, top_k=limit)

    async def repo_graph(kind: str, target: str) -> dict[str, Any]:
        from agent_forge.connectors.base import CallContext

        connector = runtime.connectors.get("repo_graph")
        result = await connector.invoke(
            "repo_graph.query",
            {"kind": kind, "target": target},
            CallContext(tenant_id=runtime.tenant_id),
        )
        return result.data or {"error": result.error}

    server = build_mcp_server(
        runner=runtime.tasks,
        agent_name=runtime.profile.identity.agent_name,
        area=runtime.profile.identity.area,
        tenant_id=runtime.tenant_id,
        retrieve=retrieve if runtime.profile.knowledge.rag.enabled else None,
        repo_graph=repo_graph if "repo_graph" in runtime.connectors.names() else None,
    )
    # Stateless, and mounted at the root of its own sub-application: each call is
    # independent, which is what lets an orchestrator scale the cell horizontally without
    # sticky sessions.
    sub_app = server.streamable_http_app(
        stateless_http=True,
        streamable_http_path="/",
        transport_security=_mcp_security(runtime),
    )
    app.mount("/mcp", sub_app)
    # Starlette does not run a mounted sub-application's lifespan, so the MCP session
    # manager has to be started from here. Without this every call to /mcp fails with
    # "Task group is not initialized" -- at runtime, not at boot.
    await stack.enter_async_context(sub_app.router.lifespan_context(sub_app))
    # Note for operators: the canonical endpoint is `/mcp/`. A client that asks for
    # `/mcp` gets a 307 to it, which every MCP SDK follows.


def _mcp_security(runtime: Runtime) -> Any:
    """Host and origin allowlists for the MCP surface.

    The MCP server checks the Host header to stop DNS rebinding, so the allowlist has to
    name every hostname the cell answers on. Left unconfigured it accepts only loopback,
    which is why a cell behind a proxy needs `AGENT_PUBLIC_URL` -- or `MCP_ALLOWED_HOSTS`
    when it answers on several names. Protection stays on; only the list is configured.
    """
    from urllib.parse import urlparse

    from mcp.server.transport_security import TransportSecuritySettings

    hosts: list[str] = ["127.0.0.1", "localhost", "127.0.0.1:*", "localhost:*"]
    origins: list[str] = []
    public = urlparse(runtime.settings.public_url) if runtime.settings.public_url else None
    if public and public.netloc:
        hosts.append(public.netloc)
        origins.append(f"{public.scheme}://{public.netloc}")
    for extra in runtime.settings.mcp_allowed_hosts:
        hosts.append(extra)
    return TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=origins)


def _mount_openapi(app: FastAPI, runtime: Runtime) -> None:
    """Serve the sanitised connector definition for Copilot Studio."""

    @app.get("/openapi/copilot-studio.json", tags=["openapi"])
    async def copilot_studio_spec() -> dict[str, Any]:
        return sanitise_for_copilot_studio(
            app.openapi(),
            title=f"Agent Forge - {runtime.profile.identity.agent_name}",
            server_url=runtime.settings.public_url,
        )


def _install_cors(app: FastAPI, env: Mapping[str, str] | None) -> None:
    source = env if env is not None else os.environ
    raw = source.get("AGENT_API_CORS_ORIGINS", "").strip()
    if not raw:
        # No CORS by default: the cell is meant to sit behind a proxy, and a permissive
        # default is how an internal API ends up callable from any page a user visits.
        return
    origins = [o.strip() for o in raw.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["authorization", "content-type", "x-api-key"],
    )
    log.info("api.cors_enabled", origins=origins)


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AgentForgeError)
    async def domain_error(request: Request, exc: AgentForgeError) -> JSONResponse:
        del request
        log.warning("api.domain_error", code=exc.code, detail=exc.message)
        clear_request_context()
        return JSONResponse(
            {"error": exc.to_dict()},
            status_code=exc.http_status,
            headers={"www-authenticate": "Bearer"} if exc.http_status == 401 else None,
        )

    @app.middleware("http")
    async def clear_context(request: Request, call_next: Any) -> Any:
        try:
            return await call_next(request)
        finally:
            clear_request_context()


app_factory = create_app
