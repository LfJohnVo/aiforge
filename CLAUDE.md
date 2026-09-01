# agent-forge — misión de esta sesión

Objetivo único: ejecutar PROMPT_AGENT_FORGE.md hasta cumplir al 100% el
Definition of Done de su sección 12, y entonces escribir: FORGE_DONE.

Reglas permanentes:

- Trabaja fase por fase (sección 11); nunca saltes un criterio de salida.
- Un commit por unidad lógica; actualiza docs/memory/DECISIONS_LOG.md al cerrar cada fase.
- Ante ambigüedad: decide, registra un ADR y continúa. No esperes confirmación.
- En la Fase 0 expande este archivo según ordena el prompt, pero NUNCA borres este bloque de misión.
- Estado actual: Fase 5 — Upstream y canales (F0–F4 cerradas)

---

# Manual operativo para IAs

**Lee este archivo primero.** Contiene todo lo necesario para retomar el repositorio sin
contexto previo. Si algo aquí contradice al código, el código manda: corrige este archivo.

## 1. Qué es este repositorio

`agent-forge` produce **Agent Cells**: la célula de agente reutilizable de la
arquitectura PEAK v2.1. Una célula = un agente para un área de negocio, con su subgrafo
de dominio, memoria, conocimiento y conectores. Se instala N veces editando **sólo**
`agent.profile.yaml` y `.env`.

Lo que la célula **no** implementa (los consume de la plataforma, con fallbacks locales
para desarrollo): MCP Gateway, Agregador central, Judge central, AI Governance System,
Audit Ledger corporativo.

Idiomas: **documentación en español, código en inglés**.

## 2. Mapa del repositorio

```
src/agent_forge/
  core/          graph.py state.py planner.py router.py hitl.py autonomy.py errors.py
    subgraphs/   base.py generalist/ it_support/      <- plugins de dominio
  channels/      openai_api/ openwebui/ copilot_studio/ teams/ slack/ websocket/
  upstream/      mcp_server/ a2a/ openapi/            <- hacia el orquestador superior
  connectors/    base.py registry.py mcp_client/ n8n/ openconnector/ databases/
  memory/        short_term long_term episodic semantic_cache scrubbing
  knowledge/     ingestion/ rag/ graphrag/ cag/ sources/ access_control.py classifier.py
  governance/    pdp_client.py dlp.py tool_policy.py autonomy_map.py
  gateway/       litellm_client.py model_policy.py    <- ruteo C0-C4 (punto único)
  events/        bus.py nats_impl.py aggregator.py judge.py evidence.py schemas/
  observability/ otel.py logging.py metrics.py
  api/           app.py auth.py admin.py health.py
  profile/       modelo Pydantic del agent.profile.yaml
  runtime.py     composition root: perfil + entorno -> celula lista
configs/         agent.profile.example.yaml litellm.yaml policies/ prompts/
deploy/compose/  docker-compose.yml + overrides por profile
scripts/         repo_graph.py new_instance.py new_connector.py ingest.py verify_ledger.py
docs/            ARCHITECTURE GOVERNANCE KNOWLEDGE MEMORY ... adr/ memory/
tests/           unit/ integration/ e2e/ policies/
```

`docs/REPO_MAP.md` y `docs/graphs/` los **genera** `make repo-graph` desde el AST. No los
edites a mano.

## 3. Comandos

| Comando | Qué hace |
|---|---|
| `make install` | Crea el venv desde `uv.lock` e instala pre-commit |
| `make check` | **Gate obligatorio**: ruff + mypy strict + tests unitarios |
| `make test` | Suite completa |
| `make cov` | Cobertura con gate del 80 % en core/governance/knowledge |
| `make up PROFILE=core\|full` | Levanta el stack (`--wait` hasta healthy) |
| `make down` / `make down-hard` | Baja (conserva / borra volúmenes) |
| `make new-instance NAME=x TENANT=y` | Genera una célula nueva en `instances/` |
| `make new-connector NAME=x` | Esqueleto de conector + tests + doc |
| `make repo-graph` | Regenera `docs/graphs/` y `docs/REPO_MAP.md` |
| `make verify-ledger` | Valida la cadena de evidencia |
| `make evals` / `make evals-ci` | Harness de evaluación (con gate en CI) |
| `make docs-check` | Verifica que existe toda la documentación exigida |

## 4. Invariantes que no se negocian

Estas son las que rompen el producto si se violan. Cada una tiene un test que la protege.

1. **C3/C4 nunca llega a un modelo externo.** La decisión vive **sólo** en
   `gateway/model_policy.py`, sobre la clasificación *acumulada* (petición + chunks +
   resultados de tools). Ninguna otra capa elige backend.
2. **La recuperación filtra dentro de la consulta**, nunca después. Un usuario sin
   permiso obtiene una respuesta indistinguible de un corpus vacío.
3. **Fail-closed.** Sin decisión fresca del PDP: se deniega C2+ y A2+. Ni siquiera
   `permissive_c0c1` levanta esto para C3/C4 ni A2+.
4. **La identidad no viene del mensaje.** Sólo de JWT/OIDC verificado o header firmado.
   Sin identidad, el techo es C0.
5. **El core no conoce dominios.** Ningún término de negocio en `core/`, `governance/`,
   `gateway/` o `api/`. Todo dominio vive en subgrafos y perfil.
6. **El LLM no escribe SQL ni comandos.** Sólo elige plantilla allowlisted y
   argumentos; no existe ruta de código que acepte una consulta de quien llama.
7. **`tenant_id` es parte de la clave** en Redis, Postgres, Qdrant, Neo4j, NATS y ledger.
8. **Los prompts no se auto-despliegan.** Cambio de prompt = PR + evals.
9. **El ledger es append-only** con hash-chain; se verifica antes de exportar.
10. **Nada persiste sin scrubbear.** Las cuatro capas de memoria pasan por
    `memory/scrubbing.py`; una entrada de caché con PII no se guarda redactada, no
    se guarda.
11. **La caché semántica respeta alcance**, no sólo clasificación: sirve una entrada
    sólo si el solicitante tiene *todos* los grupos con los que se generó.

## 5. Convenciones

* Commits **Conventional Commits**, un commit por unidad lógica. Ámbitos: `core`,
  `channels`, `upstream`, `connectors`, `memory`, `knowledge`, `governance`, `gateway`,
  `events`, `observability`, `api`, `deploy`, `docs`, `evals`, `ci`.
* SemVer + Keep a Changelog. Ver `docs/VERSIONING.md`.
* Estándares de código en `docs/CODING_STANDARDS.md`. Resumen: sin placeholders, mypy
  strict, async en todo I/O, fronteras por `Protocol` con dos implementaciones,
  dependencias pesadas en extras con importación perezosa (ADR-005).
* Toda decisión significativa ⇒ ADR en `docs/adr/` (MADR, plantilla en ADR-000).
* Todo cierre de fase ⇒ `docs/memory/DECISIONS_LOG.md` + nota en
  `docs/memory/SESSION_NOTES/` + línea en `CHANGELOG.md`.

## 6. Decisiones ya tomadas (no reabrir sin ADR nuevo)

| ADR | Decisión |
|---|---|
| [001](docs/adr/ADR-001-agentic-framework.md) | LangGraph como runtime agéntico |
| [002](docs/adr/ADR-002-memory-architecture.md) | Redis STM + Mem0/Qdrant LTM + Graphiti/Neo4j temporal, todo tras `Protocol` |
| [003](docs/adr/ADR-003-event-broker.md) | NATS JetStream tras la abstracción `EventBus` |
| [004](docs/adr/ADR-004-vector-store.md) | Qdrant, por filtrado indexado dentro de la búsqueda |
| [005](docs/adr/ADR-005-dependency-tiering.md) | Núcleo liviano + extras + fallbacks de primera clase |
| [006](docs/adr/ADR-006-dlp-stack.md) | DLP propio siempre activo; Presidio y LLM Guard opcionales |

Dos restricciones que vienen de resolver el lockfile, no de preferencia:

* **Python 3.12 exacto** (`>=3.12,<3.13`): `llm-guard` no soporta 3.13+.
* **`promptguard` y `knowledge` son mutuamente excluyentes**: `llm-guard` fija
  `json-repair==0.44.1` y `lightrag-hku` exige `>=0.59.9`. Declarado en
  `[tool.uv].conflicts`.

## 7. Plan de fases y estado

| Fase | Contenido | Estado |
|---|---|---|
| F0 | Fundación: scaffold, tooling, CI, docs, ADR-001..006 | **cerrada** |
| F1 | Núcleo agéntico + canal OpenAI-compatible | **cerrada** |
| F2 | Memoria STM/LTM/episódica, caché semántica, olvido | **cerrada** |
| F3 | Conocimiento: ingesta, RAG/CAG/GraphRAG, ACL | **cerrada** |
| F4 | Conectores: registry, MCP client, n8n, OpenConnector, BDs | **cerrada** |
| F5 | Upstream MCP/A2A/OpenAPI + canales restantes | **en curso** |
| F6 | Gobernanza, Agregador/Judge, evidencia | pendiente |
| F7 | Observabilidad y evals con gates | pendiente |
| F8 | Endurecimiento, empaque, Helm, Well-Architected | pendiente |

Criterios de salida de cada fase: sección 11 de `PROMPT_AGENT_FORGE.md`.
Definition of Done global: sección 12 del mismo documento.

## 8. Dónde mirar cuando

| Pregunta | Documento |
|---|---|
| Cómo funciona el sistema | `docs/ARCHITECTURE.md` |
| Por qué se decidió X | `docs/adr/` y `docs/memory/DECISIONS_LOG.md` |
| Qué se hizo en cada sesión | `docs/memory/SESSION_NOTES/` |
| Qué está sin resolver | `docs/memory/OPEN_QUESTIONS.md` |
| Cómo se opera esto | `docs/RUNBOOK.md` |
| Cómo añadir un conector / canal / subgrafo | `docs/CONNECTORS.md` · `docs/CHANNELS.md` · `CONTRIBUTING.md` |
| Qué se está midiendo | `docs/OBSERVABILITY.md` · `docs/EVALS.md` |
