# Well-Architected: AWS y Azure

> Estado: checklist completado en F8. Cada casilla marcada nombra **dónde se comprueba**,
> porque un checklist sin evidencia es una lista de buenas intenciones.

Leyenda: `[x]` implementado y verificable · `[~]` implementado, verificable sólo en un
despliegue real · `[-]` brecha conocida y aceptada, con su compensación.

---

## AWS Well-Architected

### Excelencia operativa

- [x] **Infraestructura como código**: todo el stack en `deploy/compose/`, con imágenes
      fijadas por tag verificado contra el registro. Esqueleto Helm en `deploy/helm/`.
- [x] **Documentación viva** versionada con el código; `REPO_MAP.md` y `docs/graphs/` se
      generan del AST (`make repo-graph`) y CI falla si están desactualizados.
- [x] **Runbook con procedimientos**: [`RUNBOOK.md`](RUNBOOK.md), una entrada por alerta
      de `OBSERVABILITY.md`.
- [x] **Observabilidad de extremo a extremo**: span por nodo, por llamada a modelo y por
      tool call (`test_a_full_request_produces_a_span_per_graph_node`); cinco dashboards
      provisionados en `deploy/observability/grafana/dashboards/`.
- [x] **Cambios por PR con gate automatizado**: `make check` (lint + tipos + tests) en
      `.github/workflows/ci.yml`.
- [x] **Evals como gate**: `make evals-ci` sale distinto de cero si un umbral bloqueante
      no se cumple, y CI comprueba que el gate **puede** fallar
      (`evals.yml` → *verify the gate can actually fail*).
- [x] **Memoria de proyecto**: `docs/memory/` con una entrada de decisiones por fase y una
      nota de sesión por fase.

### Seguridad

- [x] **Sin secretos en el repositorio**: `gitleaks` en pre-commit y en CI sobre el
      historial completo; `.env.example` documenta cada variable sin traer ninguna.
- [x] **Identidad verificada en todo canal**: JWT OIDC o API key por tenant; sin identidad
      de usuario el techo es **C0** (`test_api_key_alone_yields_an_anonymous_c0_identity`).
      Teams valida contra el JWKS de Microsoft con la audiencia fijada; Slack, HMAC v0 con
      ventana de cinco minutos.
- [x] **Mínimo privilegio**: allowlist de tools por tenant evaluada dos veces —al construir
      el catálogo y antes de invocar—, plantillas SQL allowlisted, conexiones de BD de sólo
      lectura por defecto, y `autonomy_min` que ninguna capa puede bajar.
- [x] **Fail-closed para C3/C4 y A2+**: no configurable. `PolicyRequest.needs_fresh_decision`
      se consulta antes que la caché (`test_an_unreachable_pdp_denies_a_restricted_request`).
- [x] **Defensa en profundidad**: DLP de entrada y salida, PDP local + overlay remoto que
      sólo puede estrechar, juez con comprobaciones deterministas, y ledger hash-chain.
- [x] **Contenedores non-root**: UID 10001, `cap_drop: ALL`, `no-new-privileges`, rootfs de
      sólo lectura con `tmpfs` acotado. Igual en Compose y en el chart.
- [x] **SBOM y escaneo**: `make sbom` (CycloneDX vía syft) y `make scan` (trivy);
      `security.yml` sube el SARIF completo y **bloquea sólo en CRITICAL**.
- [x] **Modelo de amenazas STRIDE**: [`THREAT_MODEL.md`](THREAT_MODEL.md).
- [x] **Soberanía del dato**: C3/C4 nunca alcanza un backend externo, con un único punto de
      decisión y test de extremo a extremo
      (`test_c4_content_never_reaches_an_external_backend`).

### Fiabilidad

- [x] **Estado fuera del proceso**: checkpointer en Postgres, memoria de corto plazo en
      Redis. Ninguna réplica guarda estado de conversación en RAM.
- [x] **Reanudación exacta tras caída**: `tests/integration/test_checkpoint_resume.py`
      reanuda una aprobación pendiente contra un Postgres real, en otro objeto de grafo.
- [x] **Timeouts y circuit breakers** en todo I/O externo (`connectors/base.py::guard`).
- [x] **DLQ e idempotencia por `event_id`**: verificado contra un NATS real
      (`tests/integration/test_events_nats.py`), incluida la redelivery de un mensaje no
      confirmado.
- [x] **Modo degradado**: sin PDP decide la base local; sin broker, bus en proceso; sin
      Langfuse, no se traza; sin Postgres, checkpointer en memoria **y aviso en el log**.
      Cada degradación se anuncia; ninguna es silenciosa.
- [~] **Backups con restauración verificada**: procedimiento en
      [`RUNBOOK.md`](RUNBOOK.md) §4. La verificación trimestral es una tarea de operación,
      no algo que este repositorio pueda demostrar por sí mismo.

### Eficiencia de rendimiento

- [x] **Streaming SSE de extremo a extremo**: `test_streaming_emits_sse_chunks_and_done`.
- [x] **Caché semántica** con umbral de similitud, respetando clasificación **y grupos**;
      CAG con presupuesto de tokens.
- [x] **Retrieval híbrido** BM25 + vectorial + grafo con fusión RRF, re-ranking y umbral de
      relevancia mínimo.
- [x] **Trabajo pesado desacoplado**: la ingesta corre en `ingestion-worker`, imagen
      distinta de la de la API.
- [~] **Objetivo p95 < 4 s sin tools**: el histograma
      `agentforge_task_duration_seconds` y el dashboard de latencia por nodo están puestos.
      El número depende del hardware y del modelo, así que se mide en el despliegue, no
      aquí.

### Optimización de costos

- [x] **Contadores de tokens y coste por tarea**: en el estado, en `task.result` y en
      `agentforge_llm_cost_usd_total{tenant,model}`.
- [x] **Ruteo a modelo pequeño por defecto**: `models.fast` para intake, router y planner;
      `models.quality` sólo para síntesis y juez.
- [x] **Caché semántica como primera línea de ahorro**, con hit rate en el dashboard.
- [x] **Dashboard de coste por tenant**: `03-cost-per-tenant.json`.
- [~] **Budgets por tenant**: LiteLLM los soporta con claves virtuales; configurarlos es
      una tarea de despliegue y su valor depende del contrato de cada tenant.

### Sostenibilidad

- [x] **Modelos pequeños por defecto**: ver ruteo, arriba.
- [x] **Caché que evita inferencia repetida**.
- [x] **Imágenes slim y dependencias estratificadas** (ADR-005): la imagen de API no lleva
      torch ni modelos.
- [x] **Perfiles de Compose**: se levanta sólo lo necesario; `core` no arranca Neo4j, ni
      Grafana, ni el colector.

---

## Azure Well-Architected

### Fiabilidad

- [x] **Procesos sin estado**: `agent-api` escala horizontalmente; el estado vive en el
      checkpointer. El servidor MCP se monta `stateless_http`, sin sesiones pegajosas.
- [x] **Healthchecks reales** con `depends_on: service_healthy`; `/health/live` sin
      dependencias y `/health/ready` por dependencia.
- [x] **Reintentos con backoff** por nodo y presupuesto de reintentos
      (`events.judge.max_retries`), que al agotarse escala a un humano en vez de reintentar.
- [x] **Degradación elegante**: ver *Modo degradado*.

### Seguridad

- [x] **Confianza cero hacia el contenido**: documentos y resultados de tools son datos.
      Comprobado por los casos de inyección del dataset de seguridad, con umbral cero.
- [x] **Segmentación de red** frontend/backend/observability; `agent-api` es el único
      servicio en dos redes y ningún almacén publica puerto salvo en perfil `full`, siempre
      a `127.0.0.1`.
- [x] **Auditoría inmutable con hash-chain**: `make verify-ledger`; editar o borrar un
      registro rompe la cadena y el script sale con 1.
- [x] **Gestión de secretos por entorno**, nunca en imagen ni repositorio. El chart de Helm
      **espera** un Secret y no lo renderiza desde values, porque acabaría en el historial
      de Helm.
- [~] **TLS en el borde**: la célula nunca se expone directamente; la terminación es del
      proxy. Documentado en `DEPLOYMENT.md` §6.

### Optimización de costos

- [x] **Dimensionado por perfil**: `core` corre en un portátil.
- [x] **Sin dependencias pesadas en la imagen de API** (ADR-005).
- [~] **Límites de tasa por tenant**: LiteLLM los aplica por clave virtual; el valor es del
      contrato.

### Excelencia operativa

- [x] **Convenciones de commit, versionado y changelog** definidas y aplicadas
      ([`VERSIONING.md`](VERSIONING.md)).
- [x] **Generadores**: `make new-instance` crea una segunda célula sin tocar código y
      `make new-connector` genera módulo, entry point, test de contrato y documentación.
- [x] **Memoria de proyecto** para continuidad entre sesiones y equipos.
- [~] **Despliegue reproducible por digest**: el chart prefiere `image.digest` y avisa en
      las NOTES cuando se usa un tag. Fijar el digest es del pipeline de release.

### Eficiencia de rendimiento

- [x] **Async en todo I/O**: sin drivers síncronos en el camino caliente; la escritura del
      ledger va a un hilo para no bloquear el bucle.
- [x] **Índices de payload en Qdrant** para que el filtro de identidad no degrade la
      búsqueda.
- [x] **Medición antes de optimizar**: OTel y las métricas están antes que cualquier tuning.

---

## Brechas conocidas y aceptadas

| Brecha | Pilar | Por qué se acepta | Compensación |
|---|---|---|---|
| Sin multi-región | Fiabilidad | La célula es un componente de área, no un servicio global; la topología la gobierna PEAK | Estado externalizado: recrear la célula en otra región es levantar el Compose |
| Sin autoscaling en Compose | Eficiencia | El objetivo declarado es Compose | HPA definido en el chart |
| Cobertura de jailbreaks limitada por reglas | Seguridad | Ver ADR-006 | Juez, evals con gate y umbral cero, extra `promptguard` |
| Clasificación automática C0–C4 imperfecta | Seguridad | Depende del contenido | El default nunca es C0, hay override manual y muestreo mensual (RUNBOOK §8) |
| Scorers de calidad propios más toscos que Ragas | Excelencia operativa | Ver ADR-009: el gate debe correr sin red ni modelo | Ragas opcional en CI, apuntado al modelo local |
| Servidor MCP sólo por HTTP | Excelencia operativa | Ver ADR-007: un servidor stdio no puede autenticar al solicitante | Puente genérico para clientes que sólo hablan stdio |
| La base de políticas existe dos veces | Excelencia operativa | Ver ADR-008: la célula debe decidir con OPA caído | Una tabla de casos ejecutada por ambos motores |
