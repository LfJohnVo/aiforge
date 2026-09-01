# Well-Architected: AWS y Azure

> Estado: mapeo inicial en F0. El checklist se completa con evidencia verificable al
> cerrar F8; las casillas marcadas aquí son las que ya tienen implementación y test.

Leyenda: `[x]` implementado y verificable · `[ ]` planificado, con fase indicada ·
`[-]` brecha conocida y aceptada.

---

## AWS Well-Architected

### Excelencia operativa
- [x] Infraestructura como código: todo el stack en `deploy/compose/`.
- [x] Documentación viva versionada con el código; `REPO_MAP.md` generado del AST.
- [ ] Runbook con procedimientos probados (F8).
- [ ] Observabilidad de extremo a extremo con dashboards provisionados (F7).
- [x] Cambios por PR con gate automatizado (`make check` en CI).
- [ ] Evals como gate de despliegue (F7).

### Seguridad
- [x] Sin secretos en el repositorio; `gitleaks` en pre-commit y CI.
- [ ] Identidad verificada (OIDC/JWT) en todo canal; sin identidad, C0 (F1).
- [ ] Mínimo privilegio: scopes mínimos, allowlist por tenant, BD readonly (F4/F6).
- [ ] Fail-closed por defecto para C3/C4 y A2+ (F6).
- [ ] Defensa en profundidad: DLP entrada/salida, PDP, judge, ledger (F6).
- [ ] Contenedores non-root, `cap_drop: ALL`, rootfs de sólo lectura (F8).
- [ ] SBOM y escaneo sin CRITICAL en CI (F8).
- [x] Modelo de amenazas STRIDE documentado.

### Fiabilidad
- [ ] Estado fuera del proceso: checkpointer Postgres, STM Redis (F1).
- [ ] Reanudación exacta tras caída, con test (F1).
- [ ] Timeouts y circuit breakers en todo I/O externo (F4).
- [ ] DLQ e idempotencia por `event_id` en el bus (F6).
- [ ] Modo degradado documentado y probado (F6).
- [ ] Backups con restauración verificada trimestralmente (F8).

### Eficiencia de rendimiento
- [ ] Streaming SSE de extremo a extremo (F1).
- [ ] Caché semántica y CAG con prefix caching (F2/F3).
- [ ] Retrieval híbrido con re-ranking acotado por `top_k` (F3).
- [ ] Objetivo p95 < 4 s sin tools, medido y publicado (F7).
- [ ] Trabajo pesado desacoplado en workers (F3).

### Optimización de costos
- [ ] Budgets por tenant en LiteLLM (F6).
- [ ] Contadores de tokens y coste por tarea, en estado y en `task.result` (F1/F6).
- [ ] Ruteo a modelo pequeño por defecto; el grande sólo cuando el planner lo pide (F7).
- [ ] Caché semántica como primera línea de ahorro (F2).
- [ ] Dashboard de coste por tenant con alerta al 90 % del budget (F7).

### Sostenibilidad
- [ ] Modelos pequeños por defecto (F7).
- [ ] Caché que evita inferencia repetida (F2).
- [x] Imágenes slim y dependencias estratificadas (ADR-005) para reducir cómputo de
      build y transferencia.
- [ ] Perfiles de Compose: se levanta sólo lo necesario (F1).

---

## Azure Well-Architected

### Fiabilidad
- [ ] Procesos sin estado, réplicas horizontales de `agent-api` (F1).
- [ ] Healthchecks reales con `depends_on: service_healthy` (F1).
- [ ] Reintentos con backoff por nodo (tenacity) y presupuesto de reintentos (F1).
- [ ] Degradación elegante ante caída de dependencias no críticas (F6).

### Seguridad
- [ ] Confianza cero hacia el contenido: documentos y resultados de tools son datos (F3).
- [ ] Segmentación de red frontend/backend/observability (F1).
- [ ] Cifrado en tránsito hacia servicios externos; TLS en el borde (F8).
- [ ] Auditoría inmutable con hash-chain (F6).
- [x] Gestión de secretos por entorno, nunca en imagen ni repositorio.

### Optimización de costos
- [ ] Dimensionado por perfil: `core` corre en una laptop (F1).
- [ ] Budgets y límites de tasa por tenant (F6).
- [x] Sin dependencias pesadas en la imagen de API (ADR-005).

### Excelencia operativa
- [x] Convenciones de commit, versionado y changelog definidos y aplicados.
- [ ] Despliegue reproducible por digest de imagen (F8).
- [ ] Generadores (`new-instance`, `new-connector`) que hacen repetible lo repetitivo (F8/F4).
- [x] Memoria de proyecto (`docs/memory/`) para continuidad entre sesiones y equipos.

### Eficiencia de rendimiento
- [ ] Async en todo I/O; sin drivers síncronos (F1).
- [ ] Índices de payload en Qdrant para que el filtro no degrade la búsqueda (F3).
- [ ] Medición antes de optimizar: OTel primero, tuning después (F7).

---

## Brechas conocidas y aceptadas

| Brecha | Pilar | Por qué se acepta | Compensación |
|---|---|---|---|
| Sin multi-región | Fiabilidad (AWS/Azure) | La célula es un componente de área, no un servicio global; la plataforma PEAK gobierna la topología | Estado externalizado: recrear la célula en otra región es levantar el Compose |
| Sin autoscaling automático en Compose | Eficiencia | El objetivo declarado es Compose; Kubernetes queda en el esqueleto Helm | HPA definido en el chart (F8) |
| Cobertura de jailbreaks limitada por reglas | Seguridad | Ver ADR-006 | Judge, evals con gate, extra `promptguard` |
| Clasificación automática C0–C4 imperfecta | Seguridad | Depende del contenido | Default nunca C0, override manual, muestreo mensual |
