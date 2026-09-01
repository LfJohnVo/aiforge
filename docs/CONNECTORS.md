# Conectores

> Estado: contrato fijado en F0; implementación en F4.

Un conector expone capacidades externas como **tools** tipadas. El core nunca importa un
conector: los descubre por entry points y los consume por el `Protocol` `BaseConnector`.

## 1. El contrato

```python
class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict
    required_scope: str
    autonomy_min: str          # A0-A4

class CallContext(BaseModel):
    tenant_id: str
    user_id: str
    groups: list[str]
    classification_ceiling: str
    trace_id: str

class ToolResult(BaseModel):
    ok: bool
    data: dict | None = None
    error: str | None = None
    classification: str = "C0"
    evidence_digest: str | None = None

class BaseConnector(Protocol):
    name: str
    version: str
    async def health(self) -> bool: ...
    def capabilities(self) -> list[ToolSpec]: ...
    async def invoke(self, tool: str, args: dict, ctx: CallContext) -> ToolResult: ...
```

Tres obligaciones de todo conector:

1. **`autonomy_min` honesto.** Si la tool escribe en un sistema de registro, es A3. El
   registry aplica el máximo entre este valor, el perfil y el PDP.
2. **`classification` del resultado.** Un resultado de tool puede subir la clasificación
   acumulada de la tarea y, con ella, forzar backend local. Marcar de menos es una fuga.
3. **`health()` real.** Sin health verde el conector no se registra y sus tools no
   existen para el modelo.

## 2. Registro

Entry points en `pyproject.toml`:

```toml
[project.entry-points."agent_forge.connectors"]
servicenow = "agent_forge.connectors.servicenow:ServiceNowConnector"
```

`connectors/registry.py` descubre, verifica `health()`, filtra por la allowlist del
tenant y por el PDP, y sólo entonces publica el catálogo al modelo. La allowlist se
reevalúa justo antes de cada invocación.

## 3. Conectores incluidos

| Conector | Qué hace |
|---|---|
| `mcp_gateway` | Cliente MCP multi-transporte (stdio, SSE, streamable HTTP) contra el MCP Gateway de la plataforma. Las tools remotas se reexponen con su `autonomy_min` y scope. |
| `n8n` | Dispara workflows por webhook/API y espera el callback por `correlation_id`, que reactiva el grafo desde su checkpoint. Exposición inversa incluida. |
| `openconnector` | Driver genérico dirigido por spec OpenAPI: genera tools desde el documento. |
| `postgres` `mysql` `mongodb` `redis` | Consultas **parametrizadas** desde plantillas allowlisted. El modelo elige plantilla y argumentos; nunca escribe SQL. |
| `repo_graph` | `repo_graph.query`: el agente responde sobre su propio repositorio (RF-12). |

## 4. Añadir uno

```bash
make new-connector NAME=servicenow
```

Genera el módulo con el esqueleto del Protocol, el registro del entry point, un test de
contrato y la entrada de documentación. Luego:

1. Implementa `capabilities()` con `autonomy_min` y `required_scope` reales.
2. Implementa `invoke()` con timeouts y circuit breaker (helpers en `connectors/base.py`).
3. Declara la clasificación de los resultados.
4. `uv sync` para que el entry point se registre; `make check`.

El test de contrato generado corre la misma batería que todos los demás conectores:
health, esquema de capacidades válido, rechazo cuando el contexto no alcanza el
`autonomy_min`, y propagación correcta de la clasificación.

## 5. Seguridad

* Scopes mínimos: un conector pide el permiso más pequeño que le permita funcionar.
* Sin credenciales en el código: todo por variable de entorno referenciada desde el
  perfil (`dsn_env`).
* Conexiones de base de datos en modo `readonly` salvo declaración explícita.
* Todo `invoke()` deja registro en el ledger con el digest de argumentos y resultado.
