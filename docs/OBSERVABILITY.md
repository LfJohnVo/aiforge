# Observabilidad

> Estado: contrato definido en F0; implementación en F7.

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

Traza LLM completa por tarea: prompt (con variables, sin PII), respuesta, modelo,
tokens, coste, latencia y scores del judge. Es también donde aterrizan los resultados de
las evals, lo que permite comparar una corrida de CI con producción.

## 6. Dashboards Grafana

Incluidos en `deploy/observability/grafana/dashboards/`, provisionados
automáticamente:

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
