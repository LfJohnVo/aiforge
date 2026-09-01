---
name: new-connector
description: Añade un conector nuevo a agent-forge (ServiceNow, Jira, un SaaS, una base de datos). Genera el esqueleto con make new-connector y guía la implementación del contrato BaseConnector con autonomía, scopes y clasificación correctos. Úsala siempre que haya que integrar un sistema externo como tool del agente.
---

# Skill · Conector nuevo

## 1. Generar

```bash
make new-connector NAME=servicenow
```

Produce: `src/agent_forge/connectors/servicenow/`, el entry point en `pyproject.toml`,
`tests/unit/connectors/test_servicenow.py` con el test de contrato, y la entrada en
`docs/CONNECTORS.md`.

## 2. Implementar el contrato

```python
class BaseConnector(Protocol):
    name: str
    version: str
    async def health(self) -> bool: ...
    def capabilities(self) -> list[ToolSpec]: ...
    async def invoke(self, tool: str, args: dict, ctx: CallContext) -> ToolResult: ...
```

### Las tres cosas que se hacen mal

1. **`autonomy_min` demasiado bajo.** Si la tool escribe en un sistema de registro es
   **A3**, no A1. El registry toma el máximo entre este valor, el perfil y el PDP, así
   que declararlo bajo es la única forma de saltarse el HITL. Regla: ¿lo puedes
   deshacer sin que nadie se entere? A1. ¿Alguien externo lo verá? A2. ¿Toca un sistema
   de registro? A3. ¿Es irreversible? A4.

2. **`ToolResult.classification` por defecto.** Si la tool devuelve datos de un sistema
   con información sensible, **no** es C0. La clasificación del resultado sube la
   clasificación acumulada de la tarea y con ella fuerza backend local. Marcar de menos
   es una fuga de datos, no un detalle.

3. **`health()` que devuelve `True` sin comprobar nada.** Sin health verde el conector
   no se registra; un health falso positivo hace que el modelo vea tools que fallarán.
   Comprueba credencial y conectividad, con timeout.

## 3. Seguridad

* Scope mínimo real, no el cómodo.
* Credenciales sólo por variable de entorno referenciada desde el perfil (`dsn_env`).
* Bases de datos en `readonly` salvo declaración explícita.
* **Nunca** SQL ni comandos construidos por el LLM: plantillas parametrizadas
  allowlisted, el modelo elige plantilla y argumentos.
* Timeout y circuit breaker en todo I/O (helpers en `connectors/base.py`).

## 4. Verificar

```bash
uv sync            # registra el entry point
make check
uv run pytest tests/unit/connectors/test_servicenow.py -v
```

El test de contrato generado comprueba: esquema de capacidades válido, rechazo cuando el
contexto no alcanza `autonomy_min`, propagación de la clasificación, y `health()` con el
servicio caído.

## 5. Documentar

Añade la fila a la tabla de `docs/CONNECTORS.md` y la línea en `CHANGELOG.md`. Si el
conector introduce un patrón nuevo (un transporte, un modelo de auth), es un ADR.
