# Changelog

Formato [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/); versionado
[SemVer](https://semver.org/lang/es/).

## [Unreleased]

### Added
- **F8 · Endurecimiento y empaque**: `make new-instance` (perfil, `.env`, override de
  Compose que **incluye** el base en vez de copiarlo, y README por instancia); chart de
  Helm completo (Deployment con HPA, ConfigMap del perfil, PVC del ledger con
  `resource-policy: keep`, Job de ingesta, CronJob que verifica la cadena, ServiceMonitor,
  Ingress) que espera un Secret y no empaqueta los almacenes; `cap_drop: ALL` en los
  catorce servicios con `cap_add` minimo **verificado arrancando cada imagen**; targets
  `sbom`, `sbom-image`, `scan` y `scan-image` que terminan; alias `local/tiny` para que el
  arranque sea comprobable en CPU; `RUNBOOK.md` y `WELL_ARCHITECTED.md` completados con
  evidencia por casilla.

### Fixed
- **Una segunda instancia clonaba el stack entero.** El override generado hacia `include`
  del compose base, que define todos los servicios, asi que cada area costaba cinco
  contenedores donde tres paginas de documentacion prometian infraestructura compartida
  —y el namespacing por `tenant_id`, que existe precisamente para que las celulas
  compartan almacenes, no estaba haciendo nada. `make new-instance ... SHARED=1` genera
  ahora un override con `extends` que trae solo la celula: **un** contenedor por area. Sin
  la bandera se conserva el stack completo, que es el modelo correcto cuando un tenant
  exige separacion fisica.
- **El perfil `core` no arrancaba**: `check_capabilities` avisaba en desarrollo mientras
  `build_vector_store` y `build_graph_store` lanzaban igualmente, y la imagen de API no
  lleva el extra `knowledge` por diseno (ADR-005). Ahora degradan en desarrollo y siguen
  siendo fatales en produccion.
- **Postgres 18 se negaba a arrancar**: el volumen se montaba en `/var/lib/postgresql/data`
  y la imagen lo lee como un cluster sin migrar. Movido a `/var/lib/postgresql`.
- **Una peticion se colgaba sin broker**: `nats.connect` reintenta indefinidamente por
  defecto y el primer publish ocurre dentro de la peticion. Connect acotado, fallo
  recordado, y `BrokerUnavailableError` para distinguir degradacion de defecto.
- **Dos healthchecks que no podian ejecutarse**: `otel-collector` y `loki` son distroless y
  sus sondas `CMD-SHELL` fallaban en cada intento, dejandolas `unhealthy` para siempre.
- **`MCP_GATEWAY_URL` y `N8N_URL` sin valor por defecto** impedian arrancar una celula que
  no usa ninguno de los dos.
- **El plugin de pytest de `deepeval`** leia `.env` al arrancar y mataba la sesion entera
  antes de recolectar un solo test. Desactivado con `-p no:deepeval`.
- **`make sbom` y `make scan` no terminaban**: el primero recorria 1.6 GB de `.venv`, el
  segundo moria en el timeout por defecto de trivy al recorrer `.git`.

### Changed
- `QualityHook` devuelve `QualityVerdict` en vez de un diccionario de puntuaciones, y el
  grafo enruta tambien `replan`. Los umbrales viven con el juez, donde el perfil los
  configura.
- `QualityVerdict` se movio de `core/graph.py` a `core/state.py`, junto a `Verdict`: el
  juez que lo produce no tiene por que importar el grafo que lo consume.
- El asunto del DLQ paso de `peak.dlq.>` a `peak-dlq.>`; JetStream rechaza asuntos
  solapados y `connect()` fallaba contra cualquier broker limpio.

### Added
- **F7 · Observabilidad y evals**: trazas OTel con **allowlist** de atributos (un span
  nunca lleva contenido) y el registro de excepciones del SDK desactivado para que no
  escriba el mensaje; quince metricas Prometheus servidas en `/metrics`, sin ninguna
  etiqueta que controle quien llama; Langfuse con filtro por clasificacion (texto solo
  hasta C1); instrumentacion de cada nodo del grafo, del gateway y de cada tool call;
  cinco dashboards de Grafana provisionados; harness de evaluacion con scorers propios y
  deterministas, Ragas opcional apuntado al proxy de la propia celula, datasets semilla
  (corpus + generalist + it_support + security + access_control), umbrales en
  `evals/thresholds.yaml` con gate en CI, generador de datasets sinteticos y config de
  Promptfoo.
- ADR-009 (scorers propios como gate y el pin de `langchain-community`).
- `LEDGER_PATH`, `MCP_ALLOWED_HOSTS` y las notas de clasificacion de Langfuse en
  `.env.example`.
- **F6 · Gobernanza, ciclo Agregador/Judge y evidencia**: contrato `PolicyRequest`/
  `PolicyVerdict` con razones obligatorias; `LocalPdp` (base Rego en proceso), `OpaPdp`
  (PDP remoto) y `CachingPdp` con fail-closed **no configurable** para C3/C4 y A2+;
  paquetes Rego `peak.{knowledge,tools,models,autonomy,common}` con `default deny`,
  evaluados por OPA real y por su traduccion Python contra una sola tabla de casos;
  motor DLP bidireccional (`configs/policies/dlp_rules.yaml`) apoyado en el scrubber de
  PII existente; `EventBus` con NATS JetStream (consumidores durables, ack explicito,
  DLQ, idempotencia por `event_id`) y bus en proceso; CloudEvents 1.0 versionados en el
  *type*; juez local con checks deterministas y rubrica; agregador que publica
  `task.result` y reanuda desde el checkpoint ante `retry`/`replan`; ledger hash-chain
  append-only con `make verify-ledger` y export JSONL por tenant.
- ADR-008 (la base de politicas existe dos veces y una tabla de casos las iguala).
- **F5 · Upstream y canales**: ciclo de vida de tarea compartido por MCP y A2A
  (`submitted → working → input-required → completed | failed | canceled`), servidor MCP
  por streamable HTTP en `/mcp/` con `ask`, `run_task`, `get_status`, `search_knowledge`
  y `repo_graph_query`; Agent Card A2A en `/.well-known/agent.json` y
  `/.well-known/agent-card.json` con JSON-RPC en `POST /a2a`; spec OpenAPI 3.1 saneado
  para Copilot Studio en `/openapi/copilot-studio.json`; canal WebSocket `/ws/chat` con
  eventos tipados; webhooks de Teams y Slack con verificación de firma obligatoria; y
  pipe opcional de OpenWebUI.
- ADR-007 (transporte del servidor MCP).
- `MCP_ALLOWED_HOSTS`, `TEAMS_APP_ID`, `TEAMS_GROUP_MAP`, `SLACK_SIGNING_SECRET`,
  `SLACK_BOT_TOKEN` y `SLACK_GROUP_MAP` en `.env.example`.
- **F4 · Conectores**: `BaseConnector` con defaults seguros (A2, C4, health real),
  registry con allowlist por tenant y doble comprobación, cliente MCP multi-transporte,
  n8n con callbacks que reanudan el grafo, OpenConnector dirigido por spec OpenAPI,
  cuatro drivers de BD con plantillas allowlisted, `repo_graph.query`,
  `POST /channels/n8n/callback` y `make new-connector`.
- **F3 · Conocimiento**: ingesta incremental desde carpeta, SharePoint (delta queries) y
  S3; pipeline parseo → chunking semántico → clasificación C0–C4 → embeddings → Qdrant;
  GraphRAG sobre Neo4j; CAG con presupuesto; retrieval híbrido BM25 + vectorial + grafo
  con fusión RRF, re-ranking y umbral de relevancia; y control de acceso identity-aware
  aplicado **dentro** de la consulta. `POST /admin/ingest`, `GET /admin/knowledge`,
  `scripts/ingest.py` y `scripts/seed.py`.
- **F2 · Memoria**: `KeyValueStore` tras `Protocol` (Redis y en memoria), STM con TTL y
  resumen incremental, LTM con adapter propio por defecto y Mem0 para producción,
  memoria episódica de área, caché semántica que respeta clasificación **y grupos**,
  scrubbing de PII ES/EN con checksum, y `forget` que barre las cuatro capas expuesto en
  `/admin/memory/forget`. Embeddings soberanos en el gateway (`/v1/embeddings`).
- **F1 · Núcleo agéntico + canal base**: grafo LangGraph completo
  (`intake → governance_gate → planner → domain_subgraph → tools → synthesis →
  quality_gate → respond`), `AgentState` tipado, checkpointer Postgres/Redis/memoria,
  autonomía A0–A4 con HITL por `interrupt()`, subgrafos `generalist` e `it_support`,
  registry de prompts versionado, router de intents, planner, model gateway con la
  invariante de soberanía C0–C4, API OpenAI-compatible con streaming SSE,
  `/admin/approvals`, `/admin/config`, `/health/{live,ready}`, y Compose con perfiles.
- **F0 · Fundación**: estructura completa del monorepo, tooling (uv + ruff + mypy
  strict + pre-commit), CI base, esqueleto documental y memoria de proyecto.
- ADR-001 (LangGraph), ADR-002 (memoria), ADR-003 (NATS JetStream), ADR-004 (Qdrant).
- ADR-005 (estratificación de dependencias) y ADR-006 (stack DLP), derivados de dos
  incompatibilidades reales detectadas al fijar `uv.lock`.

[Unreleased]: https://github.com/silent4business/agent-forge/compare/v0.1.0...HEAD
