# PROMPT MAESTRO — «Agent Forge»: Célula de Agente Empresarial Reutilizable, Multi‑tenant y Lista para Producción

> **Modelo ejecutor:** Claude Opus (modo max) en Claude Code o entorno agéntico equivalente, con acceso a sistema de archivos, terminal y Docker.
> **Origen:** Arquitectura PEAK v2.1 — Silent4Business · Plataforma de IA Soberana (Agosto 2026).
> **Adjunto opcional:** `Arquitectura_Orquestacion_Multiagente_PEAK.png`. Si el diagrama está disponible, úsalo como fuente de verdad visual; este documento ya codifica todos sus elementos relevantes.
> **Idiomas:** documentación en español; código, identificadores, commits y comentarios en inglés.

---

## 0. Rol y reglas de operación del ejecutor

Actúas como **arquitecto principal de plataformas de IA y staff engineer** de una consultora enterprise. No construyes un demo: construyes un **producto instalable N veces**, adaptable a cualquier giro de negocio, área o empresa, alineado a la arquitectura PEAK.

Reglas innegociables durante toda la ejecución:

1. **Lee este prompt completo antes de escribir la primera línea de código.** Luego trabaja por fases (sección 11) en orden estricto.
2. **Cero placeholders.** Nada de `TODO`, pseudocódigo ni funciones vacías. Todo archivo creado debe compilar, ejecutar y estar cubierto por al menos un test.
3. **Versiones fijadas.** Usa las últimas versiones estables disponibles al momento de construir y déjalas ancladas en lockfiles (`uv.lock`). Verifica compatibilidad real entre dependencias antes de fijarlas.
4. **Ante ambigüedad: decide, documenta en un ADR y continúa.** No te detengas a preguntar salvo bloqueo real de credenciales o infraestructura.
5. **Memoria del proyecto viva.** Al cierre de cada fase actualiza `docs/memory/DECISIONS_LOG.md`, crea una nota en `docs/memory/SESSION_NOTES/` y ajusta `CLAUDE.md` si cambió algo estructural.
6. **Conventional Commits + SemVer + Keep a Changelog.** Un commit por unidad lógica de trabajo.
7. **Seguridad primero.** Jamás incluyas secretos en el repositorio; todo va por `.env` (con `.env.example` completo y comentado). Contenedores non‑root.
8. **Reporta al cierre de cada fase:** qué se construyó, cómo se prueba (`comandos exactos`), qué quedó registrado en ADRs y qué sigue.

---

## 1. Contexto arquitectónico (síntesis del diagrama PEAK v2.1)

La plataforma PEAK organiza la IA soberana empresarial en capas:

- **Plataforma compartida de conocimiento y gobierno:** Data Warehouse, Telemetría, Knowledge Graph, RAG, Data Lake, AI Governance System.
- **Capa de experiencia:** Copilot / Canales de chat, con wrapper de gobernanza.
- **Orquestación central:** Router → Planner → **Agentic Graph Runtime** (orquestación, estado, checkpoints, retries, HITL) con **Redis** para estado, memoria por tenant, caché semántica y resultados.
- **Células de dominio:** por cada área (SOC, Ciber, Microsoft, Automatización, PM…) existe un **Agente + Subgrafo** propio. *Esta célula repetible es exactamente lo que este repositorio produce.*
- **MCP Gateway / Tool Fabric:** scopes mínimos, allowlist de herramientas por tenant, tool approvals; detrás viven SIEM, EDR, Graph API, Defender, Jira, APIs/SaaS.
- **Model Gateway:** policy engine, DLP, prompt firewall, budgets, **ruteo por clasificación de dato C0–C4**, versionado. Inferencia local (vLLM/SGLang, on‑prem para C2–C4) y modelos externos gobernados (solo C0–C2 desensibilizado, sin retención por contrato).
- **Cierre del ciclo:** Agregador/Síntesis → Judge/Verifier (con retry/replan) → Aprobación humana HITL (obligatoria en autonomía A2/A3; escala A0–A4) → **Evidence/Audit Ledger** (hash chain, WORM, export por tenant).
- **Broker / Event Fabric:** columna vertebral de eventos entre todas las capas.

**Qué construye este repositorio:** la **célula de agente** (agente + subgrafo de dominio + memoria + conocimiento + drivers) empaquetada como producto instalable. La gobernanza, el agregador, el judge central, el ledger y el MCP Gateway son **servicios externos de la plataforma**: la célula habla sus contratos (sección Apéndice A) y trae **fallbacks locales** para desarrollo y modo standalone.

---

## 2. Objetivo y producto final

Construir el monorepo **`agent-forge`** que, mediante configuración (no código), genera instancias llamadas **Agent Cells**. Una instancia:

1. Se despliega con `docker compose` y se adapta a cualquier área/empresa/giro editando **un solo archivo de perfil** (`agent.profile.yaml`) más su `.env`.
2. Atiende usuarios por chat (API compatible con OpenAI, OpenWebUI, Copilot Studio, Teams/Slack, WebSocket).
3. Ejecuta un **grafo agéntico con subgrafo de dominio intercambiable**, estado persistente, checkpoints, retries y pausas HITL.
4. **Aprende de las interacciones** de los usuarios de su área (memoria de corto y largo plazo, por tenant).
5. Construye su conocimiento con **RAG + CAG + GraphRAG** desde SharePoint o carpetas, filtrando cada respuesta según **identidad del solicitante y clasificación C0–C4** validada contra la gobernanza del orquestador.
6. Usa herramientas vía **MCP (a través del MCP Gateway de la plataforma), n8n, OpenConnector** y bases de datos (PostgreSQL, MySQL, MongoDB, Redis) mediante una arquitectura de **drivers/conectores enchufables**.
7. Es **orquestable desde un modelo superior** (Copilot Studio, AWS Bedrock, o un LLM grande en vLLM/Ollama) exponiéndose como servidor MCP, agente A2A y API OpenAPI.
8. Publica resultados al **Agregador** y acata veredictos del **Judge** vía event fabric, y emite **evidencia encadenada por hash**.
9. Cumple con observabilidad total, harness de evaluación continua y los pilares Well‑Architected de AWS y Azure.

---

## 3. Principios de diseño (innegociables)

1. **Configuración sobre código:** todo lo específico del dominio vive en `agent.profile.yaml` + prompts versionados. El core no conoce ningún giro de negocio.
2. **Arquitectura de plugins:** canales, conectores y subgrafos se registran vía entry points; añadir uno nuevo jamás toca el core.
3. **Multi‑tenant y multi‑instancia por diseño:** namespacing estricto por `tenant_id` en Redis, Qdrant, Neo4j, Postgres y colas. Dos instancias deben poder correr lado a lado en el mismo host.
4. **Interoperar por estándares, no por integraciones ad‑hoc:** OpenAI‑compatible hacia abajo (canales), MCP + A2A + OpenAPI hacia arriba (orquestadores), CloudEvents en el event fabric.
5. **Gobernanza externa con fail‑closed:** el Policy Decision Point vive en la nube; la célula cachea decisiones con TTL y, ante pérdida de conectividad, **niega por defecto** operaciones sobre datos C3/C4 y acciones A2+.
6. **Datos soberanos:** ningún contenido clasificado C3/C4 sale a modelos externos, bajo ninguna ruta de código. Esto se prueba con tests automatizados.
7. **Observabilidad primero:** OpenTelemetry instrumenta cada nodo del grafo, cada llamada a modelo y cada tool call desde el día uno.
8. **12‑Factor:** config por entorno, imágenes inmutables, logs a stdout, procesos sin estado (el estado vive en Redis/Postgres).
9. **Documentación viva:** el repo se auto‑describe (skill de grafo, `REPO_MAP.md`, memoria de proyecto) para que cualquier IA o humano lo retome sin contexto previo.

---

## 4. Requisitos funcionales

### RF‑01 · Núcleo agéntico (Agentic Graph Runtime local)
- Grafo principal en **LangGraph**: `intake → governance_gate → planner → domain_subgraph → tools → synthesis → quality_gate → respond`.
- **Subgrafo de dominio intercambiable:** clase base `DomainSubgraph` + un subgrafo de ejemplo genérico (`generalist`) y uno de muestra (`it_support`) que demuestre la especialización. El perfil decide cuál carga.
- Estado tipado con Pydantic (`AgentState`): mensajes, tenant, usuario, clasificación acumulada, plan, resultados de tools, citas, costos, trace_id.
- **Checkpointer** persistente (Postgres como principal, Redis para estado efímero) → reanudación exacta tras caída.
- Retries con backoff (tenacity) por nodo; timeouts configurables.
- **HITL nativo** con `interrupt()`: acciones marcadas A2/A3 pausan el grafo y esperan aprobación (endpoint + evento); niveles de autonomía **A0–A4** definidos en el perfil por categoría de acción.

### RF‑02 · Canales de chat (downstream)
- **API OpenAI‑compatible** (`/v1/chat/completions`, `/v1/models`) con streaming SSE — esto habilita OpenWebUI, LibreChat y cualquier cliente estándar sin adaptador.
- Adaptador **OpenWebUI** (pipe/function empaquetada) y manifiesto **OpenAPI 3.1** limpio para registrar la célula como acción en **Copilot Studio**.
- Webhooks entrantes para **Teams (Bot Framework) y Slack** (firma verificada), y canal **WebSocket** para UIs propias.
- Identidad del usuario final propagada en cada request (`user_id`, `groups`, `tenant_id`) vía JWT/OIDC o headers firmados; sin identidad ⇒ solo contenido C0.

### RF‑03 · Integración con orquestador superior (upstream)
- La célula se publica simultáneamente como:
  a) **Servidor MCP** (streamable HTTP + stdio) con tools tipadas: `ask`, `run_task`, `get_status`, `search_knowledge`, `repo_graph.query`.
  b) **Agente A2A** (Agent Card en `/.well-known/agent.json`, task lifecycle completo) — interoperable con Copilot Studio, Bedrock AgentCore y cualquier runtime A2A.
  c) **OpenAPI** para plataformas que consumen REST plano.
- Contrato de tarea asíncrona: `task.submitted → task.progress → task.result` (Apéndice A), correlacionado por `task_id` y `trace_id`.

### RF‑04 · Memoria del agente (STM/LTM)
> **Nota de interpretación:** el requerimiento original menciona «LTSM (long short term memory)». En contexto de agentes 2026 esto se implementa como **memoria de corto y largo plazo del agente**, no como redes neuronales LSTM. (Si un dominio requiere modelos secuenciales para series de tiempo, eso se cubre aparte en RF‑14.)

- **Corto plazo (STM):** buffer de sesión en Redis con TTL, más **resumen incremental** cuando la ventana crece; scratchpad del grafo.
- **Largo plazo (LTM):** capa **Mem0 (OSS)** sobre Qdrant para hechos/preferencias por usuario y por área, complementada con **Graphiti** (grafo temporal de conocimiento sobre Neo4j) para entidades, relaciones y evolución en el tiempo. Alternativa aceptable si simplifica: LangMem. Decide y registra ADR.
- **Memoria episódica de área:** cada interacción relevante se destila (qué se preguntó, qué funcionó, feedback) y alimenta el subgrafo — así el agente «aprende de lo que interactúan los usuarios del área».
- **Memoria procedimental:** few‑shots y reglas aprendidas, versionadas en `configs/prompts/` con promoción manual (nunca auto‑deploy de prompts).
- **Caché semántica (CAG operativo):** Redis con embeddings para respuestas repetidas; umbral de similitud y TTL por perfil.
- Todo namespaced por `tenant_id`; **PII scrubbing** (Presidio, ES/EN) antes de persistir; políticas de retención y comando de olvido (`forget user/tenant`) para cumplimiento.

### RF‑05 · Conocimiento: RAG + CAG + GraphRAG con control de acceso
- **Fuentes:** SharePoint/OneDrive vía Microsoft Graph (delta queries para sync incremental), carpetas locales/SMB/S3‑compatible. Programables por perfil (cron).
- **Pipeline de ingesta:** parseo con **Docling** (PDF/Office/HTML con tablas) → chunking semántico → embeddings multilingües (**bge‑m3**) → **Qdrant** (payload: tenant, fuente, ACL, clasificación C0–C4, fecha).
- **Clasificación C0–C4 automática** en ingesta (reglas + LLM local) con override manual; la etiqueta viaja con cada chunk.
- **GraphRAG:** extracción de entidades/relaciones → **Neo4j**; usa **LightRAG** (o `neo4j-graphrag`; decide por madurez al construir y registra ADR) para consultas globales/locales sobre el grafo.
- **CAG (cache‑augmented generation):** corpus estable y pequeño del perfil se precarga en contexto con prefix caching de vLLM + caché semántica, evitando retrieval innecesario.
- **Retrieval híbrido:** BM25 + vectorial + grafo, fusión RRF, **re‑ranking** (bge‑reranker‑v2‑m3). **Citas obligatorias** en toda respuesta basada en conocimiento.
- **Control de acceso en recuperación (identity‑aware):** antes de sintetizar, cada chunk se filtra por (identidad + grupos del solicitante) × (ACL + clasificación) × (decisión del PDP de gobernanza). El usuario sin permiso **no recibe ni la existencia** del documento.

### RF‑06 · Conectores y drivers (arquitectura enchufable)
- Interfaz común `BaseConnector` (Apéndice B): `health()`, `capabilities()`, `invoke()`, auth declarativa, límites de tasa. Registro por entry points + descubrimiento en arranque.
- **Cliente MCP multi‑servidor** (stdio, SSE y streamable HTTP) apuntando al **MCP Gateway** de la plataforma (compatible con Docker MCP Gateway, IBM ContextForge, MCPX/Lunar o gateway propio); allowlist de tools por tenant sincronizada con gobernanza.
- **n8n:** disparo de workflows por webhook/API, espera de callback con `correlation_id`, y exposición inversa (n8n puede invocar la célula).
- **OpenConnector:** driver genérico REST/OpenAPI‑driven que genera tools desde un spec.
- **Bases de datos:** drivers async para **PostgreSQL** (asyncpg/SQLAlchemy), **MySQL** (asyncmy), **MongoDB** (motor) y **Redis** — expuestos como tools con consultas parametrizadas (nunca SQL crudo desde el LLM sin allowlist de plantillas).
- Generador: `make new-connector NAME=x` produce esqueleto + tests + doc.

### RF‑07 · Model Gateway local + Router/Planner
- **LiteLLM Proxy** como gateway local de modelos: keys virtuales por tenant, **budgets**, rate limits, fallbacks, logging de costos; réplica funcional del Model Gateway central del diagrama.
- **Ruteo por clasificación de dato:** C3/C4 → exclusivamente backends locales (vLLM); C0–C2 → permitidos externos gobernados (Anthropic/Azure OpenAI/Bedrock) según perfil. Implementado como política, con test que lo garantiza.
- **Router de intents** (semantic‑router o clasificador ligero local) para elegir subgrafo/herramientas; **Planner** = nodo supervisor del grafo que descompone tareas.
- Backends soportados desde el día uno: **vLLM** (prod, prefix caching), **Ollama** (dev), Bedrock, Azure OpenAI, Anthropic API.
- Modelos: elige al construir la mejor familia abierta vigente para 2 tamaños (≈7–14B razonamiento rápido; ≈70B calidad) según VRAM disponible y licencia; embeddings bge‑m3; registra la elección en ADR con criterios.

### RF‑08 · Agregador y Judge (cierre del ciclo)
- Al finalizar una tarea, la célula emite `task.result` (CloudEvents, Apéndice A) al **event fabric** para el Agregador/Síntesis central.
- Se suscribe a `judge.verdict`: `approve` libera; `retry`/`replan` reactivan el grafo desde el checkpoint correspondiente; `escalate` dispara HITL.
- **Modo standalone:** judge local (LLM‑as‑judge con rúbricas: groundedness, seguridad, política) + agregador in‑process, activables por perfil para desarrollo o despliegues aislados.

### RF‑09 · Gobernanza distribuida
- **Cliente PDP:** consulta OPA remoto (o API del AI Governance System) por decisión de: acceso a conocimiento, uso de tool, envío a modelo externo, nivel de autonomía. Cache con TTL corto; **fail‑closed** para C3/C4 y A2+.
- **Políticas locales en Rego** (`configs/policies/`) como base + overlay remoto; mismas políticas evaluables offline en tests.
- **DLP / prompt firewall** en entrada y salida: **LLM Guard** (inyección, jailbreak, secretos) + **Presidio** (PII ES/EN) — espejo local del wrapper de gobernanza del diagrama.
- **Tool allowlist por tenant** aplicada antes de exponer tools al modelo (scopes mínimos).
- **HITL obligatorio** en acciones A2/A3 según el mapa de autonomía del perfil; A4 requiere doble aprobación.

### RF‑10 · Evidencia y auditoría
- **Ledger local append‑only** con hash‑chain (cada registro referencia el hash del anterior): prompts (digest), decisiones de política, tool calls, aprobaciones humanas, veredictos.
- Export por tenant (JSONL firmado) y emisión asíncrona de `evidence.record` al Audit Ledger central. Script `verify-ledger` que valida la cadena.

### RF‑11 · Broker de eventos
- **NATS JetStream** en Compose (streams persistentes, consumers durables, **DLQ**, idempotencia por `event_id`); abstracción `EventBus` que permita swap a Kafka/Redpanda sin tocar lógica.
- Todos los mensajes en **CloudEvents 1.0** con esquemas versionados en `src/agent_forge/events/schemas/`.

### RF‑12 · Skill de grafo del repositorio (auto‑conocimiento)
- Script `scripts/repo_graph.py`: parsea el repo con **tree‑sitter** → grafo de módulos/clases/funciones/imports/dependencias (networkx) → exporta `docs/graphs/repo-graph.{json,graphml}` + resumen **Mermaid** + `docs/REPO_MAP.md` regenerado + embeddings a la colección `repo_knowledge` de Qdrant.
- Ejecutable de tres formas: comando `make repo-graph`, cron en Compose (perfil `maintenance`, frecuencia configurable) y GitHub Action semanal.
- Doble consumo: (a) el **agente** lo consulta vía tool `repo_graph.query` para responder sobre sí mismo; (b) **Claude Code** lo usa como skill de desarrollo en `.claude/skills/repo-graph/SKILL.md`. Crea también las skills `.claude/skills/new-connector/` y `.claude/skills/release/`.

### RF‑13 · Harness de evaluación continua («harness de IA»)
- **Promptfoo** para suites de regresión de prompts; **DeepEval + Ragas** para métricas RAG (groundedness, context precision/recall, answer relevancy) y seguridad (jailbreak, PII leak).
- Datasets semilla por dominio en `evals/datasets/` (mínimo 25 casos el genérico) + generador de casos sintéticos desde el corpus ingerido.
- **Gates en CI:** el pipeline falla si groundedness, safety o el test «C4 nunca sale a externo» caen bajo umbral. Trazas y scores visibles en **Langfuse**.

### RF‑14 · Analytics opcional (series de tiempo)
- Módulo `analytics/` desactivado por defecto: forecasting/detección de anomalías sobre telemetría del área usando modelos secuenciales clásicos (LSTM/TFT con PyTorch, o statsforecast como alternativa ligera) cuando el dominio lo amerite (p. ej. SOC). Expuesto como tool. Documentar en ADR cuándo activarlo.

### RF‑15 · API de administración y operación
- Endpoints autenticados: `/health` (liveness/readiness por dependencia), `/admin/config` (perfil efectivo, sin secretos), `/admin/approvals` (cola HITL: listar/aprobar/rechazar), `/admin/memory` (inspección/olvido), `/admin/ingest` (disparar sync), `/metrics` (Prometheus).

---

## 5. Requisitos no funcionales

- **RNF‑01 Escalabilidad:** core stateless (estado en Redis/Postgres); escalar = subir réplicas del servicio `agent-api`; trabajo pesado (ingesta, analytics) en workers desacoplados por colas.
- **RNF‑02 Resiliencia:** circuit breakers y timeouts en todo I/O externo; backpressure en colas; **modo degradado** documentado (sin nube: fail‑closed C3/C4 y A2+, respuestas C0–C1 con políticas cacheadas si el perfil lo permite).
- **RNF‑03 Rendimiento:** streaming SSE extremo a extremo; caché semántica; prefix/KV caching en vLLM; p95 objetivo < 4 s en respuestas sin tools (documentar mediciones).
- **RNF‑04 Seguridad:** OIDC/JWT (Keycloak opcional en Compose), API keys por tenant, mTLS opcional entre servicios; imágenes slim non‑root; **SBOM (syft) + escaneo (trivy/grype)** en CI sin CRITICAL; principio de mínimo privilegio en todos los scopes.
- **RNF‑05 Observabilidad:** OTel traces/metrics/logs → collector → **Prometheus + Grafana + Loki**; trazas LLM y evals en **Langfuse**; dashboards incluidos (latencia por nodo, costo por tenant, cache hit rate, verdicts).
- **RNF‑06 Portabilidad:** misma imagen dev/prod; Compose con profiles; preparado para Kubernetes (esqueleto Helm en fase 8, sin bloquear el objetivo Compose).
- **RNF‑07 Well‑Architected:** entregar `docs/WELL_ARCHITECTED.md` mapeando cada decisión a los pilares de **AWS** (Excelencia Operativa, Seguridad, Fiabilidad, Eficiencia de Rendimiento, Optimización de Costos, Sostenibilidad) y **Azure** (Fiabilidad, Seguridad, Optimización de Costos, Excelencia Operativa, Eficiencia de Rendimiento), con checklist verificable y brechas conocidas.
- **RNF‑08 Costos:** budgets por tenant en LiteLLM, contadores de tokens/costo por tarea en el estado del grafo y en `task.result`.

---

## 6. Stack tecnológico (elección justificada)

Usa exactamente este stack salvo incompatibilidad real detectada al construir; cualquier sustitución requiere ADR con comparativa.

| Capa | Selección | Alternativa aceptable | Por qué |
|---|---|---|---|
| Lenguaje núcleo | Python 3.12+ (gestor **uv**) | — | Ecosistema agéntico dominante; uv = builds reproducibles rápidos |
| Framework agéntico | **LangGraph** | Pydantic AI | Subgrafos nativos, checkpointers, `interrupt()` HITL: mapa 1:1 con el «Agentic Graph Runtime» del diagrama |
| API | **FastAPI** + Uvicorn | Litestar | Async, OpenAPI automático, SSE maduro |
| Gateway de modelos | **LiteLLM Proxy** | agentgateway | Budgets, keys virtuales, fallbacks, ruteo multi‑backend; espejo local del Model Gateway central |
| Serving local (prod) | **vLLM** | SGLang | Prefix/KV caching y throughput; nombrado en el propio diagrama |
| Serving dev | **Ollama** | — | Fricción cero en laptop |
| Modelos abiertos | Mejor familia vigente al construir (Llama/Qwen/Mistral u otra), 2 tamaños (~7–14B y ~70B) | — | Soberanía C3/C4; decisión final en ADR por VRAM/licencia/benchmarks |
| Embeddings / rerank | **bge‑m3** + **bge‑reranker‑v2‑m3** | nomic‑embed | Multilingüe (español), ejecutable local |
| Vector store | **Qdrant** | pgvector | Filtros por payload (tenant/ACL/C0–C4), snapshots, rendimiento |
| Grafo de conocimiento | **Neo4j Community** | FalkorDB / Memgraph | Cypher + ecosistema GraphRAG |
| GraphRAG | **LightRAG** | neo4j‑graphrag, MS GraphRAG | Incremental y ligero; valida madurez al construir (ADR) |
| Memoria LTM | **Mem0 (OSS)** + **Graphiti** | LangMem, Letta | Drop‑in de hechos/preferencias + grafo temporal por tenant |
| STM / caché | **Redis 7** | Valkey | Sesiones, caché semántica, TTL; fijado por el diagrama |
| Broker de eventos | **NATS JetStream** | Redpanda / Kafka | Ligero para Compose, streams persistentes, DLQ, consumers durables |
| Ingesta documental | **Docling** | Unstructured | PDF/Office fieles, tablas, layout |
| SharePoint / M365 | **Microsoft Graph SDK (Python)** | Office365‑REST | Delta queries para sync incremental |
| Policy engine | **OPA (Rego)** | Cedar | Policy‑as‑code, PDP remoto/local, testeable offline |
| DLP / guardrails | **LLM Guard** + **Presidio** | NeMo Guardrails | Prompt firewall + PII multilingüe (ES/EN) |
| Observabilidad | **OpenTelemetry + Langfuse + Prometheus/Grafana/Loki** | Arize Phoenix | Trazas LLM + métricas de plataforma unificadas |
| Evals | **Promptfoo + DeepEval + Ragas** | — | Regresión de prompts + métricas RAG/seguridad con gates CI |
| Protocolo tools | **MCP SDK oficial (Python)** cliente y servidor | — | Estándar de facto; el gateway MCP es de la plataforma, la célula habla el protocolo |
| Interop agentes | **a2a‑sdk** (Agent2Agent, Linux Foundation/AAIF) | ACP | Estándar abierto adoptado por Google/Microsoft/AWS; agent card + task lifecycle |
| Tests | **pytest + testcontainers + respx** | — | Integración real efímera (Redis/Qdrant/NATS reales en tests) |
| Calidad | **ruff + mypy (strict) + pre‑commit** | — | Estándar de industria |
| Contenedores | **Docker + Docker Compose (profiles)** | — | Requerimiento del proyecto |
| CI | **GitHub Actions** | GitLab CI | Pipelines de lint, tests, evals, SBOM, scan, repo‑graph |

---

## 7. Estructura del repositorio (crear exactamente)

```text
agent-forge/
├── CLAUDE.md                    # Manual operativo para IAs (≤300 líneas, siempre actualizado)
├── README.md                    # Qué es, arquitectura en 1 diagrama Mermaid, quickstart
├── QUICKSTART.md                # De cero a chat funcionando en <15 min
├── CHANGELOG.md                 # Keep a Changelog + SemVer
├── CONTRIBUTING.md              # Flujo de trabajo, Conventional Commits, DoD por PR
├── SECURITY.md                  # Reporte de vulnerabilidades, supply chain
├── LICENSE
├── Makefile                     # up/down/check/test/evals/new-instance/new-connector/repo-graph/verify-ledger
├── pyproject.toml / uv.lock
├── .env.example                 # TODAS las variables, comentadas, con defaults seguros
├── .pre-commit-config.yaml
├── .github/workflows/           # ci.yml · evals.yml · security.yml · repo-graph.yml
├── .claude/
│   ├── settings.json
│   └── skills/                  # repo-graph/ · new-connector/ · release/  (cada una con SKILL.md)
├── src/agent_forge/
│   ├── core/                    # graph.py · state.py · planner.py · router.py · hitl.py · autonomy.py
│   │   └── subgraphs/           # base.py · generalist/ · it_support/ (ejemplo de especialización)
│   ├── channels/                # openai_api/ · openwebui/ · copilot_studio/ · teams/ · slack/ · websocket/
│   ├── upstream/                # mcp_server/ · a2a/ · openapi/
│   ├── connectors/              # base.py · registry.py · mcp_client/ · n8n/ · openconnector/
│   │   └── databases/           # postgres/ · mysql/ · mongodb/ · redis/
│   │   └── sources/             # sharepoint/ · filesystem/ · s3/
│   ├── memory/                  # short_term.py · long_term.py · episodic.py · semantic_cache.py · scrubbing.py
│   ├── knowledge/               # ingestion/ · rag/ · graphrag/ · cag/ · access_control.py · classifier.py
│   ├── governance/              # pdp_client.py · dlp.py · tool_policy.py · autonomy_map.py
│   ├── gateway/                 # litellm_client.py · model_policy.py (ruteo C0–C4)
│   ├── events/                  # bus.py · nats_impl.py · aggregator.py · judge.py · evidence.py · schemas/
│   ├── analytics/               # (opcional) forecasting/anomalías series de tiempo
│   ├── observability/           # otel.py · logging.py · metrics.py
│   └── api/                     # app.py · auth.py · admin.py · health.py
├── configs/
│   ├── agent.profile.example.yaml
│   ├── litellm.yaml
│   ├── policies/                # *.rego + tests de política
│   └── prompts/                 # registry de prompts versionado (system, judge, classifier…)
├── instances/                   # generadas por `make new-instance` (overrides por tenant/área)
├── evals/                       # promptfoo.yaml · datasets/ · judges/ · synthetic/
├── deploy/
│   ├── compose/                 # docker-compose.yml + overrides por profile
│   ├── observability/           # otel-collector.yaml · prometheus.yml · dashboards Grafana · loki
│   └── helm/                    # esqueleto (fase 8)
├── scripts/                     # repo_graph.py · new_instance.py · new_connector.py · ingest.py · verify_ledger.py · seed.py
├── docs/                        # ver sección 8
└── tests/                       # unit/ · integration/ (testcontainers) · e2e/ · policies/
```

---

## 8. Documentación y memoria del proyecto (crear TODOS estos archivos)

**Raíz:** `README.md`, `QUICKSTART.md`, `CLAUDE.md`, `CHANGELOG.md`, `CONTRIBUTING.md`, `SECURITY.md`.

**`docs/` (estándares y arquitectura):**
- `ARCHITECTURE.md` — vistas C4 (contexto/contenedor/componente) en Mermaid + mapeo explícito al diagrama PEAK (Apéndice C).
- `adr/ADR-000-template.md` (formato MADR) + un ADR por decisión significativa (framework, memoria, broker, vector, GraphRAG, modelos…).
- `CONNECTORS.md` · `CHANNELS.md` · `ORCHESTRATORS.md` — contratos, cómo añadir uno nuevo paso a paso.
- `MEMORY.md` — diseño STM/LTM/episódica/procedimental, retención, olvido.
- `KNOWLEDGE.md` — pipeline RAG/CAG/GraphRAG, clasificación C0–C4, control de acceso.
- `GOVERNANCE.md` — PDP, autonomía A0–A4, HITL, fail‑closed, allowlists.
- `THREAT_MODEL.md` — STRIDE aplicado a la célula (incluye prompt injection y exfiltración vía tools).
- `OBSERVABILITY.md` · `EVALS.md` · `DEPLOYMENT.md` · `RUNBOOK.md` (operación: arranque, incidentes, backups, rotación de claves).
- `WELL_ARCHITECTED.md` — mapeo a pilares AWS/Azure con checklist.
- `REPO_MAP.md` — **generado** por el skill de grafo; nunca editar a mano.
- `GLOSSARY.md` · `CODING_STANDARDS.md` · `VERSIONING.md`.

**`docs/memory/` (memoria de construcción — obligatoria):**
- `DECISIONS_LOG.md` — bitácora cronológica: fecha, decisión, alternativas, razón, enlace a ADR/commit.
- `SESSION_NOTES/AAAA-MM-DD-fase-N.md` — una por sesión/fase: qué se hizo, qué falló, pendientes.
- `OPEN_QUESTIONS.md` y `LESSONS_LEARNED.md`.

**Reglas de mantenimiento:** toda decisión significativa ⇒ ADR; todo cierre de fase ⇒ entrada en `DECISIONS_LOG` + nota de sesión + `CHANGELOG`; `CLAUDE.md` contiene siempre: propósito, mapa del repo, comandos (`make …`), convenciones, estado actual y siguiente fase — es lo primero que cualquier IA debe leer.

---

## 9. Perfil de instancia (el corazón de la reutilización)

`configs/agent.profile.example.yaml` — crea este archivo completo y funcional; instalar la célula en otra área/empresa = copiarlo y editarlo:

```yaml
schema_version: 1
identity:
  agent_name: "asistente-finanzas"        # único por instancia
  tenant_id: "acme-mx"
  area: "finanzas"                         # libre: soc, ciber, rrhh, ventas…
  language: "es"
  persona: "Asistente experto del área de Finanzas de ACME. Formal, preciso, cita siempre sus fuentes."
domain:
  subgraph: "generalist"                   # o uno especializado registrado por plugin
  intents_extra: ["conciliacion", "presupuesto"]
autonomy:                                  # A0 solo-lectura … A4 autónomo total
  default: A1
  overrides: { "enviar_correo": A2, "modificar_erp": A3 }
channels:
  openai_api: { enabled: true }
  openwebui:  { enabled: true }
  copilot_studio: { enabled: false }
  teams: { enabled: false }
  websocket: { enabled: true }
upstream:
  mcp_server: { enabled: true }
  a2a: { enabled: true }
  orchestrator: "standalone"               # standalone | copilot_studio | bedrock | vllm_supervisor
knowledge:
  sources:
    - type: sharepoint
      site: "https://acme.sharepoint.com/sites/finanzas"
      drives: ["Documentos"]
      sync_cron: "0 */6 * * *"
    - type: folder
      path: "/data/finanzas"
  rag: { enabled: true, top_k: 8, rerank: true }
  graphrag: { enabled: true }
  cag: { enabled: true, stable_corpus: ["politicas/**.md"] }
  default_classification: C2
models:
  fast: "local/qwen-14b"                   # alias resueltos en litellm.yaml
  quality: "local/llama-70b"
  external_allowed: ["anthropic/claude"]   # solo se usa con datos C0–C2
  budgets: { monthly_usd: 300 }
memory:
  stm_ttl_minutes: 240
  ltm: { enabled: true, scope: "area" }    # user | area | tenant
  semantic_cache: { enabled: true, similarity: 0.92, ttl_hours: 72 }
  retention_days: 365
connectors:
  mcp_gateway: { url: "${MCP_GATEWAY_URL}", allowlist: ["jira.*", "graph_api.read_*"] }
  n8n: { url: "${N8N_URL}", workflows: ["facturas-ocr"] }
  databases:
    - { type: postgres, alias: "erp_ro", dsn_env: "ERP_PG_DSN", readonly: true }
governance:
  pdp_url: "${GOVERNANCE_PDP_URL}"
  fail_mode: closed                        # closed | permissive_c0c1
  hitl_approvers_group: "finanzas-lideres"
events:
  fabric_url: "${NATS_URL}"
  aggregator: { enabled: true }
  judge: { mode: "platform" }              # platform | local
observability:
  otel_endpoint: "${OTEL_EXPORTER_OTLP_ENDPOINT}"
  langfuse: { enabled: true }
```

- `make new-instance NAME=ventas TENANT=acme-mx` genera `instances/acme-mx-ventas/` (perfil + `.env` + override de Compose con puertos/nombres únicos) — **sin tocar código**.
- Validación del perfil con Pydantic al arranque; errores claros y accionables.

---

## 10. Docker y Docker Compose

- **Profiles:** `core` (agent-api + redis + litellm + postgres) · `serving` (vllm | ollama) · `knowledge` (qdrant + neo4j + ingestion-worker) · `events` (nats) · `observability` (otel-collector + prometheus + grafana + loki + langfuse) · `maintenance` (cron repo-graph e ingesta) · `full` (todo).
- Requisitos: healthchecks reales en todos los servicios; `depends_on: condition: service_healthy`; usuarios non‑root; límites de CPU/RAM; redes segmentadas (`frontend`/`backend`/`observability`); volúmenes nombrados; reinicio `unless-stopped`.
- `make up PROFILE=full` debe dejar el sistema completo verde; `make up PROFILE=core` + Ollama debe bastar para desarrollar en laptop.
- GPU opcional vía override (`deploy/compose/gpu.override.yml`) para vLLM.

---

## 11. Plan de ejecución por fases (orden estricto, con criterio de salida)

**F0 · Fundación.** Scaffold completo del repo (sección 7), tooling (uv, ruff, mypy, pre‑commit), CI básico, esqueleto de todos los docs, `CLAUDE.md` v1, ADR‑001…004 (framework agéntico, memoria, broker, vector store).
✔ *Done cuando:* `make check` pasa, el árbol coincide con la sección 7 y los docs existen con contenido inicial real (no lorem ipsum).

**F1 · Núcleo agéntico + canal base.** Grafo LangGraph con estado tipado, checkpointer Postgres/Redis, autonomía A0–A4, `interrupt()` HITL, subgrafos `generalist` e `it_support`, API OpenAI‑compatible con streaming.
✔ *Done cuando:* chat e2e contra Ollama funciona con streaming; test de HITL pausa y reanuda; caída del proceso a mitad de tarea se reanuda desde checkpoint.

**F2 · Memoria.** STM Redis + resúmenes, caché semántica, LTM Mem0 + Graphiti namespaced por tenant, memoria episódica de área, PII scrubbing, comando de olvido.
✔ *Done cuando:* el agente recuerda entre sesiones un hecho del usuario; cache hit medible; `forget` elimina y lo prueba un test.

**F3 · Conocimiento.** Ingesta carpeta + SharePoint (delta), pipeline Docling→Qdrant, clasificador C0–C4, GraphRAG en Neo4j, CAG, retrieval híbrido + rerank + citas, control de acceso identity‑aware.
✔ *Done cuando:* pregunta sobre un doc ingerido responde con cita correcta; el mismo query con usuario sin permiso no revela ni la existencia del documento (test).

**F4 · Conectores.** `BaseConnector` + registry, cliente MCP multi‑transporte contra gateway, driver n8n con callbacks, OpenConnector, 4 drivers de BD, `make new-connector`.
✔ *Done cuando:* una tool servida por un MCP server de prueba se ejecuta e2e; un workflow n8n se dispara y su callback reactiva el grafo.

**F5 · Upstream y canales restantes.** Servidor MCP de la célula, Agent Card A2A + task lifecycle, OpenAPI para Copilot Studio, pipe OpenWebUI, webhooks Teams/Slack.
✔ *Done cuando:* un cliente MCP externo lista e invoca `run_task`; `/.well-known/agent.json` valida contra el spec A2A; OpenWebUI conecta usando solo la URL base.

**F6 · Gobernanza, ciclo Agregador/Judge y evidencia.** PDP OPA + políticas Rego + fail‑closed, DLP in/out, tool allowlist, ruteo de modelos por C0–C4, eventos CloudEvents a NATS, suscripción a verdicts con retry/replan, judge/agregador locales para standalone, ledger hash‑chain + `verify-ledger`.
✔ *Done cuando:* tests demuestran: dato C4 jamás llega a backend externo; acción A2 espera aprobación; `verdict=retry` re‑ejecuta desde checkpoint; la cadena de evidencia verifica.

**F7 · Observabilidad y evals.** OTel end‑to‑end (cada nodo, cada tool, cada llamada LLM), Langfuse, dashboards Grafana, harness Promptfoo/DeepEval/Ragas con datasets semilla y gates en CI.
✔ *Done cuando:* una traza completa request→respuesta es visible; CI falla artificialmente al bajar el umbral de groundedness (probar y revertir).

**F8 · Endurecimiento y empaque.** SBOM + escaneo, imágenes slim non‑root, `make new-instance`, esqueleto Helm, `RUNBOOK.md`, `WELL_ARCHITECTED.md` con checklist completado, revisión final de docs y memoria.
✔ *Done cuando:* el DoD global (sección 12) se cumple al 100 % y queda demostrado en la nota de sesión final.

---

## 12. Definition of Done global (verificar y evidenciar cada punto)

1. `make up PROFILE=full` levanta todos los servicios con healthchecks verdes.
2. `curl` a `/v1/chat/completions` responde en streaming; con conocimiento ingerido, responde **con citas**.
3. `make new-instance` crea una segunda instancia que corre **simultáneamente** con la primera sin tocar código.
4. La célula, registrada como servidor MCP, responde `tools/list` y ejecuta una tool e2e desde un cliente externo.
5. El Agent Card A2A es válido y una tarea A2A completa su ciclo de vida.
6. Flujo HITL: acción A2 queda pausada hasta aprobación por `/admin/approvals`.
7. Ingesta desde carpeta (dev) y SharePoint (con credenciales) funciona; el filtrado por identidad/C0–C4 tiene tests.
8. Test automatizado garantiza que contenido C3/C4 **nunca** alcanza un modelo externo.
9. `task.result` llega a NATS; un `verdict=retry` provoca replan (test de integración).
10. `make verify-ledger` valida la cadena de evidencia.
11. Trazas OTel + Langfuse visibles; dashboards Grafana incluidos y con datos.
12. Suite `pytest` verde; cobertura ≥ 80 % en `core/`, `governance/`, `knowledge/`.
13. Evals en CI con umbrales activos; SBOM generado; escaneo sin vulnerabilidades CRITICAL.
14. `make repo-graph` regenera `docs/graphs/` y `REPO_MAP.md`; la tool `repo_graph.query` responde sobre el propio repo.
15. Todos los `.md` de la sección 8 existen, completos, y `DECISIONS_LOG.md` tiene una entrada por fase.

---

## 13. Mejoras incorporadas sobre la visión original (implementar todas)

1. **A2A además de MCP** hacia el orquestador: estándar abierto (Linux Foundation/AAIF) ya integrado en Copilot Studio, Bedrock y ecosistema Google — evita lock‑in del canal de orquestación.
2. **CloudEvents + esquemas versionados** en el event fabric (evolución sin romper consumidores).
3. **Policy‑as‑code (OPA/Rego)** testeable offline, en lugar de reglas embebidas en prompts.
4. **Recuperación identity‑aware**: el ACL se aplica en el retrieval, no solo en la respuesta final.
5. **Caché semántica + CAG** con prefix caching: menor costo/latencia en preguntas recurrentes de área.
6. **Gates de evaluación en CI**: la calidad y la seguridad bloquean el merge, no solo se observan.
7. **SBOM + escaneo de imágenes** (cadena de suministro) desde el primer pipeline.
8. **Generador de instancias y de conectores** (`make new-instance` / `new-connector`): la reutilización es un comando, no un tutorial.
9. **Modo degradado explícito** con fail‑closed por clasificación y autonomía.
10. **Registry de prompts versionado** con promoción manual (nada de prompts editados en caliente).
11. **Idempotencia + DLQ** en el bus de eventos.
12. **Skill de grafo de repositorio** de doble uso (agente en runtime + Claude Code en desarrollo) — el sistema se conoce a sí mismo.

---

## 14. Qué NO hacer (anti‑objetivos)

- No acoplar lógica de negocio de ningún giro al core (todo dominio vive en subgrafos + perfil).
- No implementar tu propio MCP Gateway ni tu propio judge central: la célula **consume** los de la plataforma (con fallbacks locales mínimos para dev).
- No usar frameworks abandonados o sin releases en los últimos 6 meses.
- No permitir SQL/comandos arbitrarios generados por el LLM sin plantillas allowlisted.
- No enviar PII ni contenido C3/C4 a servicios externos, ni siquiera para embeddings o evals.
- No dejar defaults inseguros en `.env.example` (puertos expuestos, contraseñas triviales sin advertencia).
- No crear documentación aspiracional: cada doc describe lo que el código realmente hace.
- No optimizar prematuramente para Kubernetes a costa del objetivo Compose.

---

## 15. Arranque (primer output esperado del ejecutor)

Al recibir este prompt, produce en este orden y **luego ejecuta sin esperar confirmación**:

1. **Resumen de entendimiento** (≤ 15 líneas): qué vas a construir y los 5 riesgos principales.
2. **Árbol del repositorio final** (puede refinar la sección 7; justifica cambios).
3. **ADR‑001…004 propuestos** (framework agéntico, arquitectura de memoria, broker, vector store) en formato MADR.
4. **Plan detallado de F0** y comienzo inmediato de su implementación, continuando fase por fase con reporte al cierre de cada una.

---

## Apéndice A · Contratos de eventos (CloudEvents `data`, versionados)

```jsonc
// type: com.peak.task.result.v1
{
  "task_id": "uuid", "trace_id": "w3c-traceparent",
  "tenant_id": "acme-mx", "agent_id": "asistente-finanzas", "area": "finanzas",
  "autonomy_level": "A1", "classification": "C2",
  "output": { "answer_digest": "sha256", "citations": ["source_id#chunk"], "artifacts": [] },
  "metrics": { "latency_ms": 2140, "tokens_in": 1830, "tokens_out": 412, "cost_usd": 0.011 },
  "status": "completed"                      // completed | failed | awaiting_approval
}

// type: com.peak.judge.verdict.v1
{
  "task_id": "uuid", "verdict": "approve",   // approve | retry | replan | escalate
  "score": 0.93, "reasons": ["grounded", "policy_ok"], "policy_refs": ["gov/rag-c2"]
}

// type: com.peak.evidence.record.v1
{
  "seq": 1042, "prev_hash": "sha256", "hash": "sha256", "ts": "RFC3339",
  "tenant_id": "acme-mx", "actor": "agent|human|judge",
  "action": "tool_call|approval|verdict|policy_decision",
  "payload_digest": "sha256"
}
```

## Apéndice B · Contrato `BaseConnector` (fijar esta interfaz)

```python
from typing import Protocol
from pydantic import BaseModel

class ToolSpec(BaseModel):
    name: str; description: str; input_schema: dict
    required_scope: str; autonomy_min: str  # A0–A4

class CallContext(BaseModel):
    tenant_id: str; user_id: str; groups: list[str]
    classification_ceiling: str  # máx. C permitido en esta llamada
    trace_id: str

class ToolResult(BaseModel):
    ok: bool; data: dict | None = None; error: str | None = None
    classification: str = "C0"; evidence_digest: str | None = None

class BaseConnector(Protocol):
    name: str
    version: str
    async def health(self) -> bool: ...
    def capabilities(self) -> list[ToolSpec]: ...
    async def invoke(self, tool: str, args: dict, ctx: CallContext) -> ToolResult: ...
```

Registro vía entry points (`[project.entry-points."agent_forge.connectors"]`); el registry valida capacidades contra la allowlist del tenant **antes** de exponer tools al modelo.

## Apéndice C · Mapa diagrama PEAK → módulos del repo

| Elemento del diagrama | En este repo |
|---|---|
| Agente + Subgrafo por área | `core/graph.py` + `core/subgraphs/` (instanciado N veces vía perfiles) |
| Agentic Graph Runtime (estado, checkpoints, retries, HITL) | LangGraph + checkpointer + `hitl.py` |
| Redis / Estado y Memoria | `memory/` (STM, caché semántica) + checkpointer |
| Router / Planner | `core/router.py` · `core/planner.py` |
| MCP Gateway / Tool Fabric | Consumido por `connectors/mcp_client/` (allowlist por tenant) |
| Model Gateway (DLP, budgets, ruteo C0–C4) | LiteLLM Proxy + `gateway/model_policy.py` + `governance/dlp.py` |
| Inferencia local / externos gobernados | Profiles `serving` (vLLM/Ollama) + backends LiteLLM |
| Agregador / Judge / retry‑replan | `events/aggregator.py` · `events/judge.py` + hooks del grafo |
| Aprobación humana HITL (A0–A4) | `core/autonomy.py` + `/admin/approvals` |
| Evidence / Audit Ledger (hash chain) | `events/evidence.py` + `verify_ledger.py` |
| Broker / Event Fabric | NATS JetStream + `events/bus.py` (CloudEvents) |
| Plataforma de conocimiento (RAG/KG) | `knowledge/` (Qdrant, Neo4j, LightRAG, Docling) |
| Wrapper / gobernanza en canales | `governance/dlp.py` aplicado en `channels/` y `api/` |
| SIEM/EDR/Telemetría | Tools vía MCP Gateway + OTel hacia observabilidad |

---

**Fin del prompt. Comienza con la sección 15.**
