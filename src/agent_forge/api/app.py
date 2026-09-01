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
from agent_forge.api import admin, health
from agent_forge.channels import openai_api
from agent_forge.core.errors import AgentForgeError, ProfileError
from agent_forge.observability.logging import clear_request_context, get_logger
from agent_forge.profile import format_profile_error
from agent_forge.runtime import Runtime, Settings, build_runtime

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
            _mount_channels(app, runtime)
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
    app.include_router(admin.router)
    return app


def _mount_channels(app: FastAPI, runtime: Runtime) -> None:
    """Mount only the channels the profile enables."""
    mounted: list[str] = []
    if runtime.profile.channels.openai_api.enabled or runtime.profile.channels.openwebui.enabled:
        # OpenWebUI speaks the OpenAI protocol; one router serves both.
        app.include_router(openai_api.router)
        mounted.append("openai_api")
    log.info("api.channels_mounted", channels=mounted)


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
