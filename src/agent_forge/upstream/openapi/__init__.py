"""A sanitised OpenAPI 3.1 document for platforms that consume plain REST.

FastAPI already generates a spec. Copilot Studio and the Power Platform reject several
constructs it emits freely -- unresolved ``$ref`` chains, ``anyOf`` around nullable types,
``additionalProperties: true``, missing ``operationId`` -- so the document is post-processed
rather than hand-written. Hand-writing it would guarantee it drifts from the code.

What the sanitiser does is narrow and mechanical: inline the components the operations
actually use, collapse nullable unions, close open objects, and drop everything that is
not part of the surface an orchestrator should call.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable
from typing import Any

from agent_forge.observability.logging import get_logger

log = get_logger(__name__)

__all__ = ["OPERATION_PATHS", "sanitise_for_copilot_studio"]

# The endpoints an external platform is meant to call. Admin and health are excluded:
# they exist for operators, and exposing them as actions invites an orchestrator to use
# them.
OPERATION_PATHS: frozenset[str] = frozenset({"/v1/chat/completions", "/v1/models", "/a2a"})

MAX_DEPTH = 12


def sanitise_for_copilot_studio(
    spec: dict[str, Any],
    *,
    paths: Iterable[str] = OPERATION_PATHS,
    title: str = "Agent Forge",
    server_url: str = "",
) -> dict[str, Any]:
    """Return a spec Copilot Studio will accept.

    Deliberately lossy: the result describes fewer endpoints and simpler schemas than the
    cell really has. That is the point -- a connector definition is an interface for a
    platform, not a mirror of the implementation.
    """
    wanted = set(paths)
    components = spec.get("components", {}).get("schemas", {})
    out: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {
            "title": title,
            "version": str(spec.get("info", {}).get("version", "0.1.0")),
            "description": (
                "Celula de agente PEAK. Cada peticion requiere la identidad del usuario "
                "final; sin ella la respuesta se limita a contenido publico."
            ),
        },
        "servers": [{"url": server_url or "/"}],
        "paths": {},
        "components": {
            "securitySchemes": {
                "bearer": {"type": "http", "scheme": "bearer"},
            }
        },
        "security": [{"bearer": []}],
    }

    for path, item in (spec.get("paths") or {}).items():
        if path not in wanted or not isinstance(item, dict):
            continue
        cleaned: dict[str, Any] = {}
        for method, operation in item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            cleaned[method] = _operation(operation, components, path, method)
        if cleaned:
            out["paths"][path] = cleaned

    log.info("openapi.sanitised", operations=len(out["paths"]))
    return out


def _operation(
    operation: Any, components: dict[str, Any], path: str, method: str
) -> dict[str, Any]:
    source = operation if isinstance(operation, dict) else {}
    return {
        # A stable operationId is required, and Copilot Studio shows it to the user.
        "operationId": str(source.get("operationId") or _derive_id(path, method)),
        "summary": str(source.get("summary") or source.get("description") or path)[:200],
        "requestBody": _body(source.get("requestBody"), components),
        "responses": {
            "200": {
                "description": "Respuesta correcta",
                "content": {
                    "application/json": {"schema": {"type": "object"}},
                },
            },
            "401": {"description": "Credencial ausente o invalida"},
            "403": {"description": "La politica de gobernanza lo deniega"},
        },
    } | ({} if source.get("requestBody") else {"requestBody": None})


def _body(raw: Any, components: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    content = raw.get("content")
    if not isinstance(content, dict):
        return None
    json_body = content.get("application/json")
    schema = json_body.get("schema") if isinstance(json_body, dict) else None
    return {
        "required": bool(raw.get("required", True)),
        "content": {"application/json": {"schema": simplify(schema or {}, components)}},
    }


def simplify(schema: Any, components: dict[str, Any], *, depth: int = 0) -> dict[str, Any]:
    """Inline references, collapse nullable unions and close open objects."""
    if depth > MAX_DEPTH or not isinstance(schema, dict):
        return {"type": "object"}

    node = copy.deepcopy(schema)

    ref = node.pop("$ref", None)
    if isinstance(ref, str):
        name = ref.rsplit("/", 1)[-1]
        target = components.get(name)
        if target is None:
            return {"type": "object"}
        return simplify(target, components, depth=depth + 1)

    for key in ("anyOf", "oneOf", "allOf"):
        variants = node.pop(key, None)
        if not isinstance(variants, list):
            continue
        # `anyOf: [X, null]` is how FastAPI spells optional. Copilot Studio reads it as a
        # polymorphic type it cannot render, so it collapses to X.
        concrete = [v for v in variants if isinstance(v, dict) and v.get("type") != "null"]
        if concrete:
            merged = simplify(concrete[0], components, depth=depth + 1)
            node = {**merged, **node}
        else:
            node.setdefault("type", "string")

    if "properties" in node and isinstance(node["properties"], dict):
        node["properties"] = {
            name: simplify(value, components, depth=depth + 1)
            for name, value in node["properties"].items()
        }
        # An open object is rejected; closing it also stops a platform from inventing
        # fields the cell would then ignore.
        node["additionalProperties"] = False

    if "items" in node:
        node["items"] = simplify(node["items"], components, depth=depth + 1)

    # Metadata that adds nothing for a connector definition and trips strict validators.
    for noise in ("discriminator", "xml", "externalDocs", "example", "const"):
        node.pop(noise, None)

    node.setdefault("type", "object" if "properties" in node else "string")
    return node


def _derive_id(path: str, method: str) -> str:
    cleaned = path.strip("/").replace("/", "_").replace("{", "").replace("}", "")
    return f"{method}_{cleaned}" if cleaned else method
