# Orquestadores (upstream)

> Estado: implementado en F5.

La célula se publica **simultáneamente** por tres protocolos, sobre el mismo grafo y el
mismo estado. Un orquestador superior (Copilot Studio, Bedrock AgentCore, un LLM grande
en vLLM/Ollama, o el Router/Planner central de PEAK) elige el que le convenga.

## 1. Servidor MCP

`upstream/mcp_server/`. Transporte: **streamable HTTP**, montado en el mismo proceso y
puerto que el resto de la API. El endpoint canónico es `/mcp/` (`/mcp` responde 307 hacia
él, y todo SDK de MCP sigue esa redirección).

No hay transporte stdio hacia arriba: un servidor stdio es un proceso hijo cuyo control de
acceso es el permiso de ejecución, lo que rompe la invariante de que toda superficie
autentica al solicitante. Ver [ADR-007](adr/ADR-007-mcp-server-transport.md). El stdio sí
existe en el *cliente* MCP (`connectors/mcp_client/`), donde la célula es quien lanza el
proceso.

El servidor valida la cabecera `Host` para impedir *DNS rebinding*. Sin configuración sólo
acepta loopback, así que un despliegue detrás de proxy **debe** fijar `AGENT_PUBLIC_URL`;
`MCP_ALLOWED_HOSTS` añade nombres adicionales. El control no se desactiva, se configura.

| Tool | Qué hace | Autonomía |
|---|---|---|
| `ask` | Pregunta síncrona con respuesta y citas | A0 |
| `run_task` | Tarea asíncrona; devuelve `task_id` | Según la acción |
| `get_status` | Estado de una tarea, incluido `awaiting_approval` | A0 |
| `search_knowledge` | Recuperación identity-aware, sin síntesis | A0 |
| `repo_graph_query` | Consulta sobre el propio repositorio | A0 |

(El nombre lleva guión bajo porque MCP no admite puntos en el identificador de una
tool; el catálogo interno lo sigue llamando `repo_graph.query`.)

La identidad del solicitante viaja en el contexto de la llamada MCP y se aplica igual que
en cualquier canal: **un orquestador no hereda privilegios**. Si el orquestador no
propaga identidad de usuario final, el techo es C0.

## 2. Agente A2A

`upstream/a2a/`. Agent Card en `/.well-known/agent.json` —y en `/.well-known/agent-card.json`,
la grafía nueva del mismo documento— describiendo skills, capacidades y esquemas de
autenticación. Ciclo de vida completo de tarea:
`submitted → working → input-required → completed | failed | canceled`.

El transporte es JSON-RPC 2.0 sobre `POST /a2a`, con tres métodos: `message/send`
(`blocking: false` para la variante asíncrona), `tasks/get` y `tasks/cancel`. Un método
no soportado devuelve un error JSON-RPC, no un 500.

El estado `input-required` es el que expone el HITL hacia arriba: cuando una acción A2
espera aprobación, la tarea A2A queda en `input-required` y el orquestador puede
mostrarlo a un humano en su propia interfaz.

## 3. OpenAPI

`upstream/openapi/`, servido en `GET /openapi/copilot-studio.json`. Documento 3.1
generado por FastAPI y saneado para plataformas estrictas (Copilot Studio, Power
Platform): `$ref` resueltos en línea, `anyOf` de nulabilidad colapsados, objetos cerrados
con `additionalProperties: false`, `operationId` estable por endpoint.

Es deliberadamente **incompleto**: sólo describe `/v1/chat/completions`, `/v1/models` y
`/a2a`. Administración y salud quedan fuera porque publicarlas como acciones invita a un
orquestador a usarlas. El `servers[0].url` sale de `AGENT_PUBLIC_URL`.

## 4. Contrato de tarea asíncrona

Correlación por `task_id` y `trace_id` (W3C traceparent), idéntico en los tres
protocolos y en el event fabric:

```
task.submitted → task.progress* → task.result
```

`task.result` se publica como CloudEvent `com.peak.task.result.v1`. El Agregador central
lo consume; el Judge responde con `com.peak.judge.verdict.v1`.

## 5. Modo de orquestación

`upstream.orchestrator` en el perfil:

| Valor | Comportamiento |
|---|---|
| `standalone` | Judge y Agregador locales; la célula cierra su propio ciclo |
| `copilot_studio` | Se anuncia por A2A y OpenAPI; judge de plataforma |
| `bedrock` | A2A + eventos; judge de plataforma |
| `vllm_supervisor` | MCP contra un supervisor local; judge de plataforma |

El modo cambia **de dónde viene el veredicto**, no el grafo. Los nodos son los mismos.

## 6. Seguridad hacia arriba

* Toda superficie upstream exige autenticación; no hay endpoints anónimos salvo
  `/.well-known/agent.json` y `/health` (liveness).
* El Agent Card no revela tools ni fuentes de conocimiento: describe *skills*, que son
  capacidades declaradas, no el catálogo interno.
* Un orquestador no puede fijar `classification_ceiling` por sí mismo; lo decide el PDP
  a partir de la identidad propagada.
