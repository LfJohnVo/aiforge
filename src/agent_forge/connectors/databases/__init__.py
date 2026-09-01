"""Database connectors: parameterised templates only, never model-written SQL.

The rule (anti-goal 4, RF-06) is absolute: the model chooses a **template** by name and
supplies **arguments**. It never composes a query. That is enforced structurally -- there
is no code path that accepts a query string from a caller -- rather than by asking a
prompt nicely.

Each template declares:

* its parameters, which become the tool's input schema,
* whether it writes, which sets the autonomy floor,
* the classification of what it returns, which flows into the task maximum.

Four drivers: PostgreSQL and Redis on the core dependencies, MySQL and MongoDB behind the
``databases`` extra. A driver whose extra is missing reports unhealthy and its tools are
never offered (ADR-005).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

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

__all__ = [
    "MongoDBConnector",
    "MySQLConnector",
    "PostgresConnector",
    "QueryTemplate",
    "RedisConnector",
    "build_database_connectors",
]

MAX_ROWS = 200
_PARAM = re.compile(r"[:$]\{?(\w+)\}?")


@dataclass(frozen=True, slots=True)
class QueryTemplate:
    """One allowlisted query. The only thing a database connector will execute."""

    name: str
    description: str
    statement: str
    parameters: tuple[str, ...] = ()
    writes: bool = False
    classification: Classification = Classification.C2
    max_rows: int = MAX_ROWS

    @property
    def autonomy_min(self) -> AutonomyLevel:
        """A write against a system of record is A3; a read is A0."""
        return AutonomyLevel.A3 if self.writes else AutonomyLevel.A0

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                name: {"type": "string", "description": f"valor de {name}"}
                for name in self.parameters
            },
            "required": list(self.parameters),
            "additionalProperties": False,
        }

    def bind(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """Take exactly the declared parameters. Anything else is dropped.

        Dropping rather than erroring on extras is deliberate: a model that hallucinates
        an extra argument should not fail the call, and an undeclared argument can never
        reach the driver anyway.
        """
        missing = [name for name in self.parameters if name not in args]
        if missing:
            raise ToolError("missing template parameters", template=self.name, missing=missing)
        return {name: args[name] for name in self.parameters}


def _validate(template: QueryTemplate, *, readonly: bool) -> None:
    """Reject a template that contradicts the connection's own configuration."""
    if template.writes and readonly:
        raise ToolError(
            "a write template cannot be registered on a read-only connection",
            template=template.name,
        )
    declared = set(template.parameters)
    used = set(_PARAM.findall(template.statement))
    undeclared = used - declared
    if undeclared:
        raise ToolError(
            "template uses placeholders it does not declare",
            template=template.name,
            undeclared=sorted(undeclared),
        )


class _DatabaseConnector(ConnectorBase):
    """Shared behaviour: template registry, tool specs, row capping."""

    def __init__(
        self,
        *,
        alias: str,
        dsn: str,
        templates: Sequence[QueryTemplate],
        readonly: bool = True,
        timeout: float = 20.0,
    ) -> None:
        super().__init__(name=alias, version="1.0.0", timeout=timeout, breaker=CircuitBreaker())
        self._dsn = dsn
        self._readonly = readonly
        for template in templates:
            _validate(template, readonly=readonly)
        self._templates = {t.name: t for t in templates}

    @property
    def readonly(self) -> bool:
        return self._readonly

    def capabilities(self) -> Sequence[ToolSpec]:
        return tuple(
            ToolSpec(
                name=f"{self.name}.{template.name}",
                description=template.description,
                input_schema=template.input_schema(),
                required_scope=f"db:{self.name}:{template.name}",
                autonomy_min=template.autonomy_min,
                max_classification=template.classification,
                readonly=not template.writes,
            )
            for template in self._templates.values()
        )

    def template_for(self, tool: str) -> QueryTemplate | None:
        return self._templates.get(tool.rsplit(".", 1)[-1])

    async def invoke(self, tool: str, args: dict[str, Any], ctx: CallContext) -> ToolResult:
        template = self.template_for(tool)
        if template is None:
            return ToolResult.failure(f"unknown query template {tool}")
        if template.classification > ctx.classification_ceiling:
            # Refuse before running: the requester could not be shown the result anyway,
            # and running it would put the data in a log and a trace for nothing.
            return ToolResult.failure("this query returns material above the requester's clearance")
        bound = template.bind(args)

        async def call() -> dict[str, Any]:
            return await self.execute(template, bound)

        return await self.run(tool, call, classification=template.classification)

    async def execute(
        self, template: QueryTemplate, params: dict[str, Any]
    ) -> dict[str, Any]:  # pragma: no cover - overridden
        raise NotImplementedError


class PostgresConnector(_DatabaseConnector):
    """PostgreSQL over asyncpg. Core dependency, so always available."""

    def __init__(self, *, alias: str, dsn: str, **kwargs: Any) -> None:
        super().__init__(alias=alias, dsn=dsn, **kwargs)
        self._pool: Any = None

    async def _ensure_pool(self) -> Any:
        if self._pool is None:
            import asyncpg

            self._pool = await asyncpg.create_pool(
                self._dsn, min_size=1, max_size=4, command_timeout=self.timeout
            )
        return self._pool

    async def health(self) -> bool:
        if not self._dsn:
            return False
        try:
            pool = await self._ensure_pool()
            async with pool.acquire() as connection:
                await connection.fetchval("SELECT 1")
        except Exception as exc:
            log.warning("db.health_failed", alias=self.name, detail=type(exc).__name__)
            return False
        return True

    async def execute(self, template: QueryTemplate, params: dict[str, Any]) -> dict[str, Any]:
        pool = await self._ensure_pool()
        statement, values = _to_positional(template, params)
        async with pool.acquire() as connection:
            if template.writes:
                status = await connection.execute(statement, *values)
                return {"status": status, "rows": []}
            records = await connection.fetch(statement, *values)
        rows = [dict(record) for record in records[: template.max_rows]]
        return {"rows": rows, "row_count": len(rows), "truncated": len(records) > len(rows)}

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None


def _to_positional(template: QueryTemplate, params: dict[str, Any]) -> tuple[str, list[Any]]:
    """Rewrite ``:name`` placeholders as asyncpg's ``$1``, preserving order."""
    values: list[Any] = []
    order: dict[str, int] = {}

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in order:
            values.append(params[name])
            order[name] = len(values)
        return f"${order[name]}"

    return _PARAM.sub(replace, template.statement), values


class MySQLConnector(_DatabaseConnector):
    """MySQL over asyncmy. Needs the ``databases`` extra."""

    def __init__(self, *, alias: str, dsn: str, **kwargs: Any) -> None:
        super().__init__(alias=alias, dsn=dsn, **kwargs)
        self._pool: Any = None

    async def _ensure_pool(self) -> Any:
        if self._pool is None:
            try:
                import asyncmy
            except ImportError as exc:
                raise ToolError(
                    "the `databases` extra is required for MySQL",
                    hint="uv sync --extra databases",
                ) from exc
            self._pool = await asyncmy.create_pool(**_mysql_dsn(self._dsn))
        return self._pool

    async def health(self) -> bool:
        if not self._dsn:
            return False
        try:
            pool = await self._ensure_pool()
            async with pool.acquire() as connection, connection.cursor() as cursor:
                await cursor.execute("SELECT 1")
        except Exception as exc:
            log.warning("db.health_failed", alias=self.name, detail=type(exc).__name__)
            return False
        return True

    async def execute(self, template: QueryTemplate, params: dict[str, Any]) -> dict[str, Any]:
        pool = await self._ensure_pool()
        # asyncmy takes %(name)s placeholders; the rewrite is mechanical and the values
        # still travel out of band.
        statement = _PARAM.sub(lambda m: f"%({m.group(1)})s", template.statement)
        async with pool.acquire() as connection, connection.cursor() as cursor:
            await cursor.execute(statement, params)
            if template.writes:
                await connection.commit()
                return {"status": "ok", "row_count": cursor.rowcount, "rows": []}
            columns = [c[0] for c in (cursor.description or [])]
            records = await cursor.fetchmany(template.max_rows)
        rows = [dict(zip(columns, record, strict=False)) for record in records]
        return {"rows": rows, "row_count": len(rows)}

    async def aclose(self) -> None:
        if self._pool is not None:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None


def _mysql_dsn(dsn: str) -> dict[str, Any]:
    """Parse ``mysql://user:pass@host:port/db`` into asyncmy kwargs."""
    from urllib.parse import unquote, urlparse

    parsed = urlparse(dsn)
    return {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 3306,
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "db": (parsed.path or "/").lstrip("/"),
        "minsize": 1,
        "maxsize": 4,
    }


class MongoDBConnector(_DatabaseConnector):
    """MongoDB over motor. Needs the ``databases`` extra.

    A template's ``statement`` is a JSON filter document with ``:name`` placeholders,
    prefixed by the collection: ``collection|{"field": ":name"}``. The filter is built by
    substituting values into the parsed document, so no operator can arrive from the
    model's arguments.
    """

    def __init__(self, *, alias: str, dsn: str, database: str = "", **kwargs: Any) -> None:
        super().__init__(alias=alias, dsn=dsn, **kwargs)
        self._database_name = database
        self._client: Any = None

    def _ensure_client(self) -> Any:
        if self._client is None:
            try:
                from motor.motor_asyncio import AsyncIOMotorClient
            except ImportError as exc:
                raise ToolError(
                    "the `databases` extra is required for MongoDB",
                    hint="uv sync --extra databases",
                ) from exc
            self._client = AsyncIOMotorClient(self._dsn, serverSelectionTimeoutMS=5000)
        return self._client

    async def health(self) -> bool:
        if not self._dsn:
            return False
        try:
            await self._ensure_client().admin.command("ping")
        except Exception as exc:
            log.warning("db.health_failed", alias=self.name, detail=type(exc).__name__)
            return False
        return True

    async def execute(self, template: QueryTemplate, params: dict[str, Any]) -> dict[str, Any]:
        import json

        collection_name, _, raw_filter = template.statement.partition("|")
        if not raw_filter:
            raise ToolError(
                "mongo template must be 'collection|<json filter>'", template=template.name
            )
        query = _substitute(json.loads(raw_filter), params)
        client = self._ensure_client()
        database = client[self._database_name] if self._database_name else client.get_database()
        cursor = database[collection_name.strip()].find(query).limit(template.max_rows)
        documents = [_stringify_ids(doc) async for doc in cursor]
        return {"rows": documents, "row_count": len(documents)}

    async def aclose(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


def _substitute(node: Any, params: Mapping[str, Any]) -> Any:
    """Replace ``:name`` leaves with values. Structure comes from the template only."""
    if isinstance(node, dict):
        return {k: _substitute(v, params) for k, v in node.items()}
    if isinstance(node, list):
        return [_substitute(v, params) for v in node]
    if isinstance(node, str) and node.startswith(":"):
        return params.get(node[1:], node)
    return node


def _stringify_ids(document: Mapping[str, Any]) -> dict[str, Any]:
    return {k: (str(v) if k == "_id" else v) for k, v in document.items()}


class RedisConnector(_DatabaseConnector):
    """Redis as a business data source, distinct from the cell's own memory Redis.

    Templates are ``operation|key-pattern``; only reads are supported, because a model
    writing to a shared cache is a class of bug nobody wants to debug.
    """

    SUPPORTED = frozenset({"get", "hgetall", "smembers", "lrange", "exists"})

    def __init__(self, *, alias: str, dsn: str, **kwargs: Any) -> None:
        kwargs.setdefault("readonly", True)
        super().__init__(alias=alias, dsn=dsn, **kwargs)
        self._client: Any = None

    def _ensure_client(self) -> Any:
        if self._client is None:
            from redis.asyncio import Redis

            self._client = Redis.from_url(
                self._dsn, decode_responses=True, socket_connect_timeout=5
            )
        return self._client

    async def health(self) -> bool:
        if not self._dsn:
            return False
        try:
            return bool(await self._ensure_client().ping())
        except Exception:
            return False

    async def execute(self, template: QueryTemplate, params: dict[str, Any]) -> dict[str, Any]:
        operation, _, key_pattern = template.statement.partition("|")
        operation = operation.strip().lower()
        if operation not in self.SUPPORTED:
            raise ToolError("unsupported redis operation", operation=operation)
        key = _PARAM.sub(lambda m: str(params.get(m.group(1), "")), key_pattern.strip())
        client = self._ensure_client()

        if operation == "lrange":
            value = await client.lrange(key, 0, template.max_rows - 1)
        elif operation == "smembers":
            value = sorted(await client.smembers(key))[: template.max_rows]
        else:
            value = await getattr(client, operation)(key)
        return {"key": key, "operation": operation, "value": value}

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


# ---------------------------------------------------------------------- build

# Templates a cell gets with no configuration. Deliberately generic and read-only: a real
# deployment adds its own in the profile, and these exist so a driver is testable and a
# newly configured database is immediately useful.
DEFAULT_TEMPLATES: dict[str, tuple[QueryTemplate, ...]] = {
    "postgres": (
        QueryTemplate(
            name="count_rows",
            description="Cuenta las filas de una tabla permitida",
            statement="SELECT count(*) AS total FROM information_schema.tables "
            "WHERE table_schema = :schema",
            parameters=("schema",),
            classification=Classification.C1,
        ),
    ),
    "mysql": (
        QueryTemplate(
            name="count_rows",
            description="Cuenta las tablas de un esquema",
            statement="SELECT count(*) AS total FROM information_schema.tables "
            "WHERE table_schema = :schema",
            parameters=("schema",),
            classification=Classification.C1,
        ),
    ),
    "mongodb": (),
    "redis": (
        QueryTemplate(
            name="get",
            description="Lee una clave por su identificador",
            statement="get|:key",
            parameters=("key",),
            classification=Classification.C2,
        ),
    ),
}

_DRIVERS: dict[str, type[_DatabaseConnector]] = {
    "postgres": PostgresConnector,
    "mysql": MySQLConnector,
    "mongodb": MongoDBConnector,
    "redis": RedisConnector,
}


def build_database_connectors(
    entries: Sequence[Any],
    *,
    env: Mapping[str, str] | None = None,
    templates: Mapping[str, Sequence[QueryTemplate]] | None = None,
) -> list[_DatabaseConnector]:
    """Build the drivers a profile's ``connectors.databases`` block declares."""
    source = dict(env if env is not None else os.environ)
    overrides = dict(templates or {})
    built: list[_DatabaseConnector] = []

    for entry in entries:
        dsn = source.get(entry.dsn_env, "")
        if not dsn:
            log.warning(
                "db.dsn_missing",
                alias=entry.alias,
                variable=entry.dsn_env,
                detail="the connector is registered but will report unhealthy",
            )
        driver = _DRIVERS.get(entry.type)
        if driver is None:  # pragma: no cover - profile validation prevents this
            raise ToolError("unknown database type", type=entry.type)
        built.append(
            driver(
                alias=entry.alias,
                dsn=dsn,
                templates=tuple(
                    overrides.get(entry.alias) or DEFAULT_TEMPLATES.get(entry.type, ())
                ),
                readonly=entry.readonly,
            )
        )
    return built


# Entry-point targets. Each builds from the environment so `uv sync` alone is enough to
# register them; a cell without the corresponding DSN simply reports unhealthy.
def _from_env(kind: str) -> Any:
    def factory() -> _DatabaseConnector:
        driver = _DRIVERS[kind]
        return driver(
            alias=kind,
            dsn=os.environ.get(f"{kind.upper()}_DSN", ""),
            templates=DEFAULT_TEMPLATES.get(kind, ()),
            readonly=True,
        )

    return factory


postgres_connector = _from_env("postgres")
mysql_connector = _from_env("mysql")
mongodb_connector = _from_env("mongodb")
redis_connector = _from_env("redis")
