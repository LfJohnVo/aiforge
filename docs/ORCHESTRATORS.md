# Orquestadores (upstream)

> Estado: contrato definido en F0; implementación en F5.

La célula se publica **simultáneamente** por tres protocolos, sobre el mismo grafo y el
mismo estado. Un orquestador superior (Copilot Studio, Bedrock AgentCore, un LLM grande
en vLLM/Ollama, o el Router/Planner central de PEAK) elige el que le convenga.

## 1. Servidor MCP

`upstream/mcp_server/`. Transportes: **stdio** y **streamable HTTP**.

| Tool | Qué hace | Autonomía |
|---|---|---|
| `ask` | Pregunta síncrona con respuesta y citas | A0 |
| `run_task` | Tarea asíncrona; devuelve `task_id` | Según la acción |
| `get_status` | Estado de una tarea, incluido `awaiting_approval` | A0 |
| `search_knowledge` | Recuperación identity-aware, sin síntesis | A0 |
| `repo_graph.query` | Consulta sobre el propio repositorio | A0 |

La identidad del solicitante viaja en el contexto de la llamada MCP y se aplica igual que
en cualquier canal: **un orquestador no hereda privilegios**. Si el orquestador no
propaga identidad de usuario final, el techo es C0.

## 2. Agente A2A

`upstream/a2a/`. Agent Card en `/.well-known/agent.json` describiendo skills,
capacidades de streaming y esquemas de autenticación. Ciclo de vida completo de tarea:
`submitted → working → input-required → completed | failed | canceled`.

El estado `input-required` es el que expone el HITL hacia arriba: cuando una acción A2
espera aprobación, la tarea A2A queda en `input-required` y el orquestador puede
mostrarlo a un humano en su propia interfaz.

## 3. OpenAPI

`upstream/openapi/`. Documento 3.1 generado por FastAPI y saneado para plataformas
estrictas (Copilot Studio, Power Platform): sin `oneOf` anidados, sin
`additionalProperties: true`, `operationId` estable por endpoint.

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
