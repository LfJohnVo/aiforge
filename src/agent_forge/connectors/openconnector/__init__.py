"""OpenConnector: turn an OpenAPI document into tools.

Point it at a spec and every operation becomes a tool, with its parameters as the input
schema. This is what makes "integrate the area's SaaS" a configuration task rather than a
code task.

Three rules that keep a generated connector from being a liability:

* **Only the operations the profile allows.** A spec with 300 endpoints does not become
  300 tools; an operation allowlist is required, and an empty one yields nothing.
* **Autonomy from the HTTP verb, upward only.** GET and HEAD are A0; anything that writes
  is at least A2. A spec cannot talk its way into a lower level.
* **No credentials in the spec.** Auth comes from the profile's environment reference,
  never from anything the document says.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from agent_forge.connectors.base import (
    CallContext,
    CircuitBreaker,
    ConnectorBase,
    ToolResult,
    ToolSpec,
)
from agent_forge.core.autonomy import AutonomyLevel
from agent_forge.core.classification import Classification
from agent_forge.core.errors import ToolError
from agent_forge.observability.logging import get_logger

log = get_logger(__name__)

__all__ = ["OpenConnectorDriver", "Operation", "operations_from_spec"]

READ_METHODS = frozenset({"get", "head", "options"})
_PATH_PARAM = re.compile(r"\{([^}]+)\}")
_SAFE_NAME = re.compile(r"[^a-zA-Z0-9_]+")


@dataclass(frozen=True, slots=True)
class Operation:
    """One OpenAPI operation, reduced to what a tool call needs."""

    operation_id: str
    method: str
    path: str
    summary: str = ""
    path_params: tuple[str, ...] = ()
    query_params: tuple[str, ...] = ()
    body_schema: dict[str, Any] = field(default_factory=dict)

    @property
    def readonly(self) -> bool:
        return self.method in READ_METHODS

    @property
    def autonomy(self) -> AutonomyLevel:
        """Derived from the verb, never from the document's own claims."""
        if self.readonly:
            return AutonomyLevel.A0
        if self.method == "delete":
            return AutonomyLevel.A3
        return AutonomyLevel.A2

    def input_schema(self) -> dict[str, Any]:
        properties: dict[str, Any] = {
            name: {"type": "string", "description": f"path parameter {name}"}
            for name in self.path_params
        }
        properties.update(
            {
                name: {"type": "string", "description": f"query parameter {name}"}
                for name in self.query_params
            }
        )
        if self.body_schema:
            properties["body"] = self.body_schema
        return {
            "type": "object",
            "properties": properties,
            "required": list(self.path_params),
            "additionalProperties": False,
        }


def operations_from_spec(spec: dict[str, Any]) -> list[Operation]:
    """Read an OpenAPI 3.x document into operations.

    Deliberately tolerant: a spec missing an ``operationId`` gets a derived one rather
    than being skipped, because most real specs are missing something.
    """
    out: list[Operation] = []
    paths = spec.get("paths")
    if not isinstance(paths, dict):
        return out

    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        shared = item.get("parameters") or []
        for method, operation in item.items():
            if method.lower() not in {
                "get",
                "post",
                "put",
                "patch",
                "delete",
                "head",
                "options",
            } or not isinstance(operation, dict):
                continue
            parameters = [*shared, *(operation.get("parameters") or [])]
            out.append(
                Operation(
                    operation_id=str(operation.get("operationId") or _derive_id(method, str(path))),
                    method=method.lower(),
                    path=str(path),
                    summary=str(operation.get("summary") or operation.get("description") or ""),
                    path_params=tuple(_names(parameters, "path"))
                    or tuple(_PATH_PARAM.findall(str(path))),
                    query_params=tuple(_names(parameters, "query")),
                    body_schema=_body_schema(operation),
                )
            )
    return out


def _names(parameters: Sequence[Any], location: str) -> list[str]:
    return [
        str(p.get("name"))
        for p in parameters
        if isinstance(p, dict) and p.get("in") == location and p.get("name")
    ]


def _body_schema(operation: dict[str, Any]) -> dict[str, Any]:
    body = operation.get("requestBody")
    if not isinstance(body, dict):
        return {}
    content = body.get("content")
    if not isinstance(content, dict):
        return {}
    json_body = content.get("application/json")
    if not isinstance(json_body, dict):
        return {}
    schema = json_body.get("schema")
    return dict(schema) if isinstance(schema, dict) else {}


def _derive_id(method: str, path: str) -> str:
    return _SAFE_NAME.sub("_", f"{method}_{path}").strip("_").lower()


class OpenConnectorDriver(ConnectorBase):
    """A REST API described by an OpenAPI document, exposed as tools."""

    def __init__(
        self,
        *,
        service: str,
        base_url: str,
        spec: dict[str, Any] | None = None,
        operations: Sequence[Operation] = (),
        allow_operations: Sequence[str] = (),
        headers: dict[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
        result_classification: Classification = Classification.C4,
    ) -> None:
        super().__init__(
            name=f"api.{service}", version="1.0.0", timeout=timeout, breaker=CircuitBreaker()
        )
        self._service = service
        self._base_url = base_url.rstrip("/")
        self._allow = tuple(allow_operations)
        self._classification = result_classification
        discovered = list(operations) or (operations_from_spec(spec or {}))
        self._operations = {
            op.operation_id: op for op in discovered if self._permitted(op.operation_id)
        }
        if discovered and not self._operations:
            log.warning(
                "openconnector.no_operations_allowed",
                service=service,
                discovered=len(discovered),
                detail="the profile's allow_operations matched nothing",
            )
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout, connect=10.0),
            headers=headers or {},
        )

    def _permitted(self, operation_id: str) -> bool:
        # An empty allowlist exposes nothing. A spec is not an invitation.
        return any(fnmatch.fnmatch(operation_id, pattern) for pattern in self._allow)

    def capabilities(self) -> Sequence[ToolSpec]:
        return tuple(
            ToolSpec(
                name=f"{self.name}.{op.operation_id}",
                description=op.summary or f"{op.method.upper()} {op.path}",
                input_schema=op.input_schema(),
                required_scope=f"{self._service}:{op.method}:{op.path}",
                autonomy_min=op.autonomy,
                max_classification=self._classification,
                readonly=op.readonly,
            )
            for op in self._operations.values()
        )

    async def health(self) -> bool:
        if not self._operations:
            return False
        try:
            response = await self._client.request("GET", "/", timeout=5.0)
        except httpx.HTTPError:
            return False
        # Any answer at all means the host is up; 404 on "/" is normal for an API.
        return response.status_code < 500

    async def invoke(self, tool: str, args: dict[str, Any], ctx: CallContext) -> ToolResult:
        operation = self._operations.get(tool.rsplit(".", 1)[-1])
        if operation is None:
            return ToolResult.failure(f"unknown operation {tool}")

        async def call() -> dict[str, Any]:
            return await self._request(operation, args, ctx)

        return await self.run(tool, call, classification=self._classification)

    async def _request(
        self, operation: Operation, args: dict[str, Any], ctx: CallContext
    ) -> dict[str, Any]:
        path = operation.path
        for name in operation.path_params:
            value = args.get(name)
            if value is None:
                raise ToolError("missing path parameter", parameter=name)
            path = path.replace(f"{{{name}}}", str(value))

        query = {k: args[k] for k in operation.query_params if k in args}
        body = args.get("body") if not operation.readonly else None

        try:
            response = await self._client.request(
                operation.method.upper(),
                path,
                params=query or None,
                json=body,
                headers={"x-request-id": ctx.trace_id} if ctx.trace_id else None,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ToolError(
                "the API rejected the request",
                operation=operation.operation_id,
                status=exc.response.status_code,
            ) from exc
        except httpx.HTTPError as exc:
            raise ToolError(
                "the API is unreachable", operation=operation.operation_id, detail=str(exc)
            ) from exc

        return {"status": response.status_code, "body": _decode(response)}

    async def aclose(self) -> None:
        await self._client.aclose()


def _decode(response: httpx.Response) -> Any:
    content_type = response.headers.get("content-type", "")
    if "json" in content_type:
        try:
            return response.json()
        except ValueError:
            pass
    # Bounded: an API returning a megabyte of HTML must not end up in a prompt.
    return response.text[:8000]
