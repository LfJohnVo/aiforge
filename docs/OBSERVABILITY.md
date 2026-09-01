# Observabilidad

> Estado: implementado en F7.

Principio: **cada nodo del grafo, cada llamada a modelo y cada tool call producen una
traza desde el día uno**. Si algo no está instrumentado, no está terminado.

## 1. Topología

```
agent-api ─┐
           ├─ OTLP ─> otel-collector ─┬─> Prometheus (métricas)
ingestion ─┘                          ├─> Loki (logs)
                                      └─> Tempo/Jaeger opcional (trazas)
agent-api ──── SDK ────────────────────> Langfuse (trazas LLM, evals, costos)
                    Prometheus + Loki ──> Grafana (dashboards)
```

Perfil de Compose: `observability`.

## 2. Trazas

Un `trace_id` W3C nace en `intake` y viaja por: nodos del grafo, llamadas al Model
Gateway, tool calls, consultas al PDP, recuperación de conocimiento, eventos CloudEvents
(`traceparent` como extensión) y entradas del ledger.

Atributos obligatorios en cada span: `tenant_id`, `agent_id`, `task_id`, `thread_id`,
`autonomy_level`, `classification`. Prohibido: contenido de mensajes, PII, secretos. Se
registran **digests**, no textos.

La prohibición está en el código, no sólo en esta página: `observability/tracing.py`
mantiene una **allowlist** (`CORRELATION_KEYS`) y descarta —registrándolo— cualquier
atributo fuera de ella. Es una allowlist y no una lista de prohibidos porque un atributo
nuevo debe empezar rechazado hasta que alguien lo revise.

Dos detalles del SDK que hubo que apagar, y que valen para cualquiera que instrumente algo
parecido:

* `start_as_current_span` trae `record_exception=True` y `set_status_on_exception=True`.
  Ambos escriben el **mensaje** de la excepción en el span, y un mensaje cita de rutina lo
  que lo provocó: un documento rechazado, un fragmento recuperado. Se desactivan, y el
  span guarda sólo el tipo.
* El colector borra además los atributos de contenido
  (`deploy/observability/otel-collector.yaml`). Defensa en profundidad: la aplicación no
  los envía y el colector no los reenviaría aunque llegaran.

Spans nombrados: `graph.node.<nombre>`, `llm.<alias>`, `tool.<connector>.<tool>`,
`pdp.<decision>`, `knowledge.retrieve`, `memory.<capa>`.

## 3. Métricas (Prometheus, `/metrics`)

| Métrica | Tipo | Etiquetas |
|---|---|---|
| `agentforge_task_duration_seconds` | histogram | tenant, area, status |
| `agentforge_node_duration_seconds` | histogram | tenant, node |
| `agentforge_llm_tokens_total` | counter | tenant, model, direction |
| `agentforge_llm_cost_usd_total` | counter | tenant, model |
| `agentforge_semantic_cache_hits_total` | counter | tenant |
| `agentforge_tool_calls_total` | counter | tenant, connector, tool, outcome |
| `agentforge_policy_decisions_total` | counter | tenant, decision, effect |
| `agentforge_hitl_pending` | gauge | tenant |
| `agentforge_judge_verdicts_total` | counter | tenant, verdict |
| `agentforge_external_model_blocked_total` | counter | tenant, classification |
| `agentforge_ledger_entries_total` | counter | tenant |

`agentforge_external_model_blocked_total` es una métrica de seguridad: si sube, alguien
está intentando enviar datos clasificados fuera.

## 4. Logs

`structlog` a stdout en JSON. Campos fijos: `ts`, `level`, `event`, `service`,
`instance`, `tenant_id`, `trace_id`, `span_id`, `task_id`. Nunca contenido de usuario.

## 5. Langfuse

Traza LLM completa por tarea: prompt, respuesta, modelo, tokens, coste, latencia y
scores del judge. Es también donde aterrizan los resultados de las evals, lo que permite
comparar una corrida de CI con producción.

**Langfuse es alojado por defecto, y su valor está en mostrar prompts y respuestas.** Por
eso el filtro es de clasificación y vive en `observability/langfuse_client.py`, no en cada
punto de llamada:

| Clasificación | Qué se envía |
|---|---|
| C0 · C1 | Prompt y respuesta completos |
| C2 y superior | Sólo digests, contadores de tokens y scores |

Un Langfuse autoalojado dentro del perímetro puede subir ese límite con
`max_content_class`, y esa es una decisión de quien opera. Una clasificación que no se
puede interpretar cuenta como C4: la duda se resuelve sin enviar nada.

## 6. Dashboards Grafana

Cinco, en `deploy/observability/grafana/dashboards/`, provisionados automáticamente al
levantar el perfil `observability`. Todos filtran por `$tenant`:

1. **Overview de la célula** — RPS, p50/p95/p99, tasa de error, tareas en HITL.
2. **Latencia por nodo del grafo** — heatmap por nodo, para ver dónde se va el tiempo.
3. **Coste por tenant** — tokens y USD acumulados, consumo frente a budget.
4. **Caché y conocimiento** — hit rate semántico, latencia de recuperación, citas por
   respuesta.
5. **Gobernanza y calidad** — decisiones de política, veredictos del judge, bloqueos de
   modelo externo, entradas del ledger.

## 7. Alertas mínimas

| Alerta | Condición |
|---|---|
| Célula caída | `/health` readiness falla 3 veces seguidas |
| Fail-closed activo | Errores de PDP > 0 durante 5 min |
| Intento de fuga | `agentforge_external_model_blocked_total` crece |
| Budget agotado | Coste del tenant supera el 90 % del mensual |
| HITL estancado | `agentforge_hitl_pending` > 0 durante más de 24 h |
| Cadena de evidencia rota | `verify-ledger` falla en el cron de mantenimiento |

Procedimiento de respuesta para cada una en [`RUNBOOK.md`](RUNBOOK.md).
