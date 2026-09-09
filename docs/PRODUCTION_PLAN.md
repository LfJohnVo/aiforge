# Plan de producción y PoC local

> Escrito el 2026-09-07, ejecutado la noche del 2026-09-09. La misión de
> `PROMPT_AGENT_FORGE.md` está cerrada (`FORGE_DONE`); esto es **lo que falta** para que
> una célula sirva usuarios reales, empezando por una PoC en la máquina de desarrollo.
>
> Precedencia: código medido > ADR > este plan. Si el código cambia, corrige el plan. Y
> lo que dice «medido» lleva su salida cruda en `docs/memory/evidence/`: sin eso, no está
> medido.

## 0. Estado medido

> Sesión del 2026-09-09, de noche. Todo lo de abajo se ejecutó; las salidas crudas están
> en `docs/memory/evidence/`.

| Qué | Resultado |
|---|---|
| Gate `make check` | **verde**: ruff, mypy strict, 736 passed · 1 skipped |
| Tests de política (OPA real) | 46, ejecutándose (antes se saltaban por falta de Docker) |
| Definition of Done | 15/15 con evidencia; los puntos 2 y 7 pasan de test a **medido en vivo** |
| Célula en vivo | responde con citas, filtra por identidad, deniega C3 a quien no toca |
| Cadena de evidencia | **28 registros verificados** en la célula desplegada |
| Segunda área | **un contenedor**, compartiendo Postgres con la primera |
| Rutas cerradas | B1, B2, B3, B5, B6, B7, B9 · A0, A1 (con salvedad), A2, A3, A4, A5 |

### La PoC, verificada

`scripts/demo/verify_live.py`, **7/7**:

| Comprobación | Resultado |
|---|---|
| Respuesta con cita a quien tiene el grupo | 1,500 MXN + `politica-viaticos.md#p0`, 8.8 s |
| El mismo dato preguntado con otras palabras | encontrado, 17.7 s — los embeddings son semánticos |
| Un usuario de otra área no obtiene el dato | nada |
| No se filtra ningún dato del documento reservado | ni cifras ni confirmación de que exista |
| Quien sí tiene el grupo obtiene el C3 | 6.2x + `plan-adquisicion.md#p0`, 15.6 s, modelo local |
| Sin grupos, indistinguible de corpus vacío | nada |
| Sin credencial | 401 |

Y además, a mano: con OPA parado la célula **deniega y explica** el motivo, y al volver OPA
responde sin reiniciar nada.

### Trece hallazgos, todos por arrancar el sistema

Ninguno lo habría encontrado la suite de tests, y ocho son de la clase peor: el sistema
sigue respondiendo, peor, sin que nada falle.

| # | Hallazgo | Estado |
|---|---|---|
| 1 | `make check` estaba **rojo** en `c8275ba` (formato) | corregido `9305458` |
| 2 | `DEPLOYMENT.md` §6 prometía un modo producción que el código no aplicaba | B1 |
| 3 | `EMBEDDING_MODEL`, `RERANK_MODEL`, `GOVERNANCE_FAIL_MODE`, `DEV_SHARED_SECRET`: cuatro variables muertas | B1 · B2 |
| 4 | `local/embeddings` sin backend en ningún perfil; el RAG caía a hashing léxico **en silencio** | B2 |
| 5 | `LexicalReranker` **descartaba** candidatos en vez de ordenarlos | B2 |
| 6 | El **worker de ingesta nunca pudo arrancar**: `python -m` sobre un paquete sin `__main__` | corregido |
| 7 | `make up` no leía el `.env` de la raíz: Compose lo busca junto al fichero compose | corregido |
| 8 | `LOCAL_CORPUS_PATH` significaba dos cosas; el valor del ejemplo rompía el montaje | corregido |
| 9 | La **imagen de la API no podía leer el corpus**: sin extras, almacén en memoria vacío | corregido |
| 10 | El segundo `uv sync` **desinstalaba** lo que ponía el primero | corregido |
| 11 | El **override manual de clasificación no existía**, y el modelo de amenazas se apoya en él | implementado |
| 12 | El clasificador **pisaba** la clasificación declarada, incluso a la baja | corregido |
| 13 | `verify_ledger` ignoraba `LEDGER_PATH`: reportaba éxito mirando donde no era | corregido |

Dos más, anotados sin tocar el código, porque cambiarlos de madrugada sin entenderlos del
todo sería peor que dejarlos escritos:

* **La caché semántica cachea los fallos.** Una respuesta «no hay información» producida
  por una recuperación rota se sirvió durante las horas siguientes con `similarity=1.0`.
  Con el TTL por defecto de 72 h, un fallo transitorio de 5 minutos contamina tres días.
  Esto fue lo que hizo que dos verificaciones seguidas dieran 3/7 con el sistema ya
  arreglado.
* **Con el PDP caído se deniega también C0.** El RUNBOOK §3.1 dice que C0/C1 sigue
  funcionando. Medido: no. Es el lado seguro del error, pero la documentación y el código
  no dicen lo mismo.

### Las rutas A y B, cerradas

Todo lo que no dependía de credenciales ajenas se ejecutó:

| | |
|---|---|
| **A7** carga | 0.10 req/s de techo; a partir de 2 peticiones simultáneas la GPU encola |
| **B4** ADR-010 | Q-01 cerrada con esos números: Qwen3-8B, bge-m3, `local/quality` aplazado |
| **B8** respaldos | Postgres + Qdrant + ledger verificado; sin planificador propio, lo llama el cron del host |
| **A6** restauración | **ejecutada**: 87 tablas y 410 checkpoints de LangGraph vueltos a la vida en un Postgres desechable |
| **B10** corpus hostil | 31 casos, y el gate ya no pasa por vacío |

### Lo que no está hecho

* **Ruta C** entera: necesita credenciales de nube y las siete decisiones de §7.
* **Ruta D**: necesita usuarios reales.
* **Repetir las evals contra una colección limpia.** La corrida del 2026-09-09 mezcló el
  corpus de evals con el de la PoC en el mismo Qdrant, así que `groundedness`,
  `answer_relevancy` y `context_precision` de esa corrida **no son válidos**. Las métricas
  de seguridad sí: cero en ACL, soberanía, PII y jailbreak.
* **Decidir si la inyección por documento funciona** (Q-11). El scorer compara subcadenas
  y no distingue obedecer de citar; con eso no se puede afirmar ni una cosa ni la otra.

## 1. Esta máquina (medido)

| | |
|---|---|
| CPU / RAM | Ryzen 9 5900XT 16c/32t · 32 GB |
| GPU | **RTX 5060 Ti 16 GB** (Blackwell), driver 610.62 · passthrough a Docker **funciona**, y Ollama la usa: 6.8 GB de VRAM y 37 % de uso con qwen3:8b cargado |
| Docker Desktop | 32 CPUs · **24 GB** tras poner `.wslconfig` (antes 15.6, el 50 % por defecto) |
| Disco E: | 571 GB libres |
| Imágenes ya en caché | todas las del stack, `agent-forge/api:0.1.0`, `vllm/vllm-openai:v0.27.1-cu129` (37.5 GB), `ollama/ollama:0.33.2`, `nvidia/cuda:12.8.0-base`, `axllent/mailpit` |
| Sobras de la demo | limpiadas |
| `.env` | creado, con contraseñas aleatorias, ignorado por git |

**vLLM no arranca en esta máquina, y no es configuración.** Medido:

```
RuntimeError: UVA is not available
```

El motor V1 de vLLM —el único desde 0.27; `VLLM_USE_V1=0` ya no hace nada— reserva sus
buffers con Unified Virtual Addressing, y el passthrough de GPU de WSL2 no lo expone. No
hay bandera que lo evite. En **Linux nativo con la misma tarjeta sí arranca**, así que esto
no afecta a producción sobre EKS ni a un host on-prem: sólo a la máquina de desarrollo.

Así que en Windows el chat lo sirve **Ollama sobre la misma GPU** (`local/dev`, qwen3:8b),
y los embeddings también (`local/embeddings-dev`, bge-m3, 1024 dimensiones). Dos líneas del
perfil vuelven a `local/fast` en Linux.

Latencias medidas con esa configuración, sin caché: **8.8 s a 17.7 s** por respuesta
completa con recuperación y citas. Es una sola tarjeta sirviendo chat y embeddings a la
vez; sirve para demostrar, no para dimensionar.

La RAM del VM subió de 15.6 GB a 24 GB (`.wslconfig`), y con el stack entero más la
segunda célula el consumo se quedó en **7 GB**: no era el cuello.

## 2. Qué es «producción v1»

La célula consume cinco servicios de la plataforma PEAK que **no existen todavía**: MCP
Gateway, Agregador, Judge, AI Governance System y Audit Ledger. Todos tienen fallback local
de primera clase (ADR-005, ADR-008). Producción v1 = **célula standalone**: OPA propio con
los Rego del repo, NATS propio, `LocalJudge` + `Aggregator` en proceso, ledger local firmado
y exportado por tenant. Cuando exista la plataforma, cada pieza se cambia por configuración
(`GOVERNANCE_PDP_URL`, `MCP_GATEWAY_URL`, `NATS_URL`): es lo que las fronteras por
`Protocol` compran.

Registrado como provisional en D-072; ver §7, decisión 3.

## 3. Ruta A — PoC en esta PC (3–5 días)

Objetivo: demostrar, con este hardware y sin credenciales externas, las siete cosas que un
comprador de la célula quiere ver, y cerrar todo lo «nunca verificado en vivo».

### A0 · Preparación (medio día)

**A0.1 Memoria del VM.** `%UserProfile%\.wslconfig`:

```ini
[wsl2]
memory=24GB
processors=16
```

y `wsl --shutdown`; Docker Desktop se reinicia solo.

**A0.2 `deploy/compose/gpu.override.yml`.** *Ejecutado, y el resultado cambió el plan:
vLLM no arranca bajo WSL2 (§1). El override quedó parametrizado igual —sirve tal cual en
Linux— y además da la GPU a Ollama, que es lo que acabó sirviendo el chat aquí.*

* `image: ${VLLM_IMAGE:-vllm/vllm-openai:v0.28.0}` → en `.env`,
  `VLLM_IMAGE=vllm/vllm-openai:v0.27.1-cu129` (ya en caché; CUDA 12.9 soporta Blackwell).
* `--served-model-name=${VLLM_SERVED_NAME:-Qwen/Qwen3-8B}` separado de
  `--model=${VLLM_MODEL}` → `VLLM_MODEL=Qwen/Qwen3-8B-FP8`, servido como `Qwen/Qwen3-8B`,
  que es lo que `local/fast` espera.
* `VLLM_GPU_UTIL=0.75`, `VLLM_MAX_LEN=16384`: deja ~4 GB de VRAM para el embedder.

**A0.3 Embedder real.** Recomendado, por ser lo más barato en RAM: **Ollama sirve `bge-m3`**
(1.2 GB, 1024 dimensiones = `DEFAULT_DIM`) y vLLM sirve el chat.

* `gpu.override.yml`: quitar `profiles: !override ["never"]` de `ollama`.
* `configs/litellm.yaml`: el alias `local/embeddings` apunta a `ollama/bge-m3` en
  `OLLAMA_BASE_URL` para desarrollo (o un alias `local/embeddings-ollama` seleccionado por
  `EMBEDDING_MODEL`, que es B2).
* Sembrar el volumen (`RUNBOOK` §1.1) con `ollama pull bge-m3`.

Alternativa: segundo contenedor vLLM con `--task embed`. Más fiel a producción, ~4 GB más
de RAM.

**A0.4 `.env`.** `cp .env.example .env` y los siete valores del HANDOFF §3, más las
`VLLM_*` de arriba y `LOCAL_CORPUS_PATH`. `HUGGING_FACE_HUB_TOKEN` no hace falta:
`Qwen/Qwen3-8B-FP8` es público. **Poner en `ANTHROPIC_API_KEY` una clave inválida a
propósito**: la demo de soberanía es que, con un externo configurado, C3 sigue sin salir.

**A0.5 Perfil de la PoC.** Copiar `configs/agent.profile.example.yaml` a la ruta de
`AGENT_FORGE_PROFILE` y ajustar: `knowledge.sources` sólo con la fuente `folder`, con `path`
en el directorio donde `ingestion-worker` monta `LOCAL_CORPUS_PATH`; `graphrag.enabled:
false` (Neo4j no aporta nada al guion y cuesta 1.5 GB); `models.external_allowed` como está.

**A0.6 Una tool A2 de verdad.** El perfil declara `enviar_correo: A2` pero ningún conector
la sirve. Para el guion: un servidor MCP de demostración de ~30 líneas (`FastMCP`) con la
tool `enviar_correo` que entrega a **Mailpit** (imagen ya en caché, UI en el navegador), y
`MCP_GATEWAY_URL` apuntando a él con `enviar_correo` en la allowlist. Es código de demo, vive
en `scripts/demo/`, no en `src/`.

**A0.7 Limpiar la demo anterior**: los tres `down -v` del HANDOFF §8.

*Criterio de salida*: `docker compose -f deploy/compose/docker-compose.yml -f
deploy/compose/gpu.override.yml --profile full config` sin errores.

### A1 · Arrancar `full` con GPU (medio día; la primera descarga del modelo manda)

```bash
docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/gpu.override.yml --profile full up -d --wait
curl -s http://127.0.0.1:8080/health/ready
curl -sN http://127.0.0.1:8080/v1/chat/completions \
  -H "Authorization: Bearer <clave de AGENT_API_KEYS>" -H "Content-Type: application/json" \
  -d '{"model":"agent-forge","stream":true,"messages":[{"role":"user","content":"Hola"}]}'
```

El campo `model` se ignora: el backend lo elige `gateway/model_policy.py` a partir del
perfil y la clasificación (invariante 1); la respuesta siempre dice `agent-forge`.

*Criterio*: todos los servicios `healthy`; primer token < 2 s en la segunda petición;
`nvidia-smi` muestra el proceso de vLLM. **Anotar VRAM y tokens/s**: cierra Q-01 para el
tamaño pequeño.

Trampa: `start_period` de vLLM es 300 s y cargar 8.5 GB desde disco tarda. Si `--wait`
expira, `docker compose logs -f vllm`; no reiniciar.

### A2 · Conocimiento con citas (1 día)

1. Corpus: 20–50 documentos reales de un área **C0–C2** (políticas internas, manuales) en
   `LOCAL_CORPUS_PATH`.
2. `make ingest`. En el log del worker debe verse `GatewayEmbeddings`, **nunca**
   `embeddings.hashing_selected`; si aparece, A0.3 está mal.
3. Pregunta cuya respuesta está en un documento → respuesta con cita. Misma pregunta con un
   usuario de otro grupo → «no hay información», sin nombrar el documento (invariante 2).
4. Repetir la pregunta → hit de caché semántica en el dashboard 04.

*Criterio*: DoD-2 y DoD-7 (carpeta) **en vivo**, con capturas.

### A3 · Gobernanza en vivo (medio día)

1. Documento marcado C3 en el corpus; pregunta sobre él con el externo configurado → la
   respuesta viene de `local/fast`, el ledger registra el backend, el dashboard 05 muestra
   la decisión. Invariante 1 delante del público.
2. «Envía por correo el resumen a …» → la tarea queda pendiente; `GET /admin/approvals` la
   lista; aprobar → el correo aparece en Mailpit. DoD-6.
3. `make verify-ledger` → íntegro; alterar un byte del fichero → falla. DoD-10.
4. `docker stop` del contenedor de OPA → petición C2 denegada con motivo, C0 responde
   (fail-closed, invariante 3). Levantarlo de nuevo.

### A4 · Observabilidad (2 horas)

Grafana en `http://127.0.0.1:3000` (contraseña de `.env`): los cinco dashboards con datos
tras A2–A3. Las trazas OTel salen por el colector; no hay backend de trazas en el compose
(Langfuse tampoco). Para verlas en la demo: añadir Tempo al perfil `observability` o
mostrar los spans en el log del colector. Decisión de la PoC, no del producto.

### A5 · Segunda célula (1 hora)

`make new-instance NAME=soc TENANT=acme-mx SHARED=1` y levantarla. La pregunta de A2 desde
`soc` no ve el corpus de la primera célula. DoD-3 en vivo con infraestructura compartida.

### A6 · Simulacro de restauración (medio día)

`pg_dump` + snapshot de Qdrant + copia del ledger según el RUNBOOK; `make down-hard`;
restaurar; la pregunta de A2 responde con la misma cita y `verify-ledger` pasa. Cierra el
«no verificado» de respaldos y da el RTO real de esta máquina.

### A7 · Carga (medio día)

`k6` contra `/v1/chat/completions` con 1, 4, 8 y 16 usuarios concurrentes y prompts de
~500 tokens: p95 del primer token, tokens/s agregados, punto en que vLLM empieza a encolar.
Ese número dimensiona la GPU de producción (Ruta C).

### Guion de demo (20 minutos)

| min | Paso | Qué demuestra |
|---|---|---|
| 0–3 | Pregunta con cita (A2.3) | RAG con acceso por identidad |
| 3–5 | Misma pregunta sin permiso | Invariante 2: ni la existencia del documento |
| 5–9 | Pregunta sobre C3 con externo configurado; ledger (A3.1) | Invariante 1: soberanía |
| 9–13 | Correo A2 pausado, aprobado, entregado en Mailpit (A3.2) | HITL |
| 13–16 | Grafana: coste por tenant, decisiones de gobernanza | Observabilidad |
| 16–18 | Segunda área en un comando (A5) | Producto, no framework |
| 18–20 | `make verify-ledger` | Evidencia verificable |

### Criterios de aceptación de la PoC

* Los siete pasos del guion, de una sentada, sobre un stack arrancado desde cero.
* Cero `embeddings.hashing_selected` en los logs.
* A6 ejecutado una vez, RTO anotado.
* Números de A1 y A7 en la nota de sesión y en `OPEN_QUESTIONS` (Q-01 para el tamaño
  pequeño; Q-06 con la tasa de aciertos de caché observada).

## 4. Ruta B — Endurecer el repo (2–3 semanas, solapada con A)

| # | Trabajo | Criterio de salida | Tamaño |
|---|---|---|---|
| B1 | **Modo producción real.** Con `AGENT_FORGE_ENV=production`: sin OIDC → el arranque falla con motivo; `GOVERNANCE_FAIL_MODE=permissive_c0c1` → falla salvo `GOVERNANCE_ACK_PERMISSIVE=1`; `DEV_SHARED_SECRET` ignorado. O corregir `DEPLOYMENT.md` §6 para que diga lo que hay. **Recomendación: implementar** (≈15 líneas en `runtime.py` + 3 tests). | Un test por regla; `DEPLOYMENT.md` §6 exacto | S |
| B2 | **Embeddings y rerank configurables.** `EMBEDDING_MODEL` elige el alias que recibe `build_embeddings`; `RERANK_MODEL` vacío = `LexicalReranker`, con valor = cross-encoder vía gateway (`bge-reranker-v2-m3` por vLLM `--task score` o TEI). `.env.example` sólo con variables que algo lee. | Test: alias inyectado; test: el reranker con modelo ordena distinto al léxico | M |
| B3 | **Límite de tasa por tenant** en la API: middleware sobre Redis (ya está), 429 con `Retry-After`, métrica Prometheus. Defensa en profundidad tras el proxy. | Test de 429; visible en el dashboard 01 | S |
| B4 | **ADR-010, cierra Q-01**: `local/fast` = Qwen3-8B-FP8 (≥16 GB VRAM), `local/quality` = Qwen3-32B-FP8 (≥48 GB), embeddings bge-m3, reranker bge-reranker-v2-m3; licencias Apache-2.0 verificadas; con los números de A1/A7. | ADR aceptado; `litellm.yaml` y `.env.example` coherentes | S |
| B5 | **Pipeline de release**: `release.yml` en tag `v*`: build, push a GHCR **por digest**, `cosign sign` keyless con el OIDC de GitHub, SBOM como attestation, trivy con gate. La skill `release` deja de terminar en el tag. | `cosign verify` pasa; `DEPLOYMENT.md` §6.5 cumplido | M |
| B6 | **`deploy/compose/prod.override.yml`** para el camino VM/on-prem: ningún puerto publicado salvo 443 de un reverse proxy (Caddy con TLS automático; ya lo usáis), `AGENT_FORGE_ENV=production`, imágenes por digest, `read_only: true` donde se pueda, logs a Loki. | `make up PROFILE=full ENV=prod` levanta y sólo 443 escucha | M |
| B7 | **Alertas y SLOs**: reglas Prometheus (5xx > 1 %, p95 primer token > 5 s, denegaciones PDP anómalas, ledger sin escrituras 10 min, vLLM sin GPU, ingesta atascada). SLOs: disponibilidad 99.5 %, p95 primer token, groundedness ≥ `evals/thresholds.yaml`. | `deploy/observability/alerts.yml` cargado; test de que cada regla nombra una métrica emitida (como el de dashboards) | M |
| B8 | **Respaldos automáticos** en el perfil `maintenance`: `pg_dump`, snapshot Qdrant, dump Neo4j, ledger a S3/MinIO con retención; **restauración probada** mensualmente (RUNBOOK). | Job programado; restore drill con fecha | M |
| B9 | **Helm listo**: `NetworkPolicy` (egress cero para almacenes y modelos, como `backend: internal`), `ingress.tls` con cert-manager, `PodDisruptionBudget`, `values-prod.yaml`, `ExternalSecret`. | `helm template` pasa `kubeconform`; `helm test` contra kind | M |
| B10 | **Corpus hostil**: 30 documentos con inyección de prompt, exfiltración por cita y escalada de autonomía en `evals/datasets/` con umbral cero; revisión del THREAT_MODEL §4 con lo que salga. | Evals verdes con el corpus; THREAT_MODEL actualizado | M |

S ≈ ≤1 día · M ≈ 2–4 días.

## 5. Ruta C — Infraestructura de producción (3–4 semanas, desde la semana 2)

**Supuesto**: AWS, porque es donde vive el resto de vuestra infraestructura (imágenes ECR
de `us-east-1` en esta máquina; Terragrunt en 4Utf). Si es Azure u on-prem cambia la
columna, no las filas: `WELL_ARCHITECTED.md` cubre ambas nubes.

| Pieza | Producción v1 | Por qué así |
|---|---|---|
| Cómputo | **EKS**; el chart existe. Nodegroup CPU (m7i) para célula y almacenes; nodegroup GPU **g6.xlarge (L4 24 GB)** para `local/fast`; **g6e.xlarge (L40S 48 GB)** para `local/quality` cuando haga falta | 8B-FP8 sobra en 24 GB con contexto largo; 32B-FP8 necesita 48 GB. A7 dice cuántos |
| Postgres | RDS PostgreSQL 17, Multi-AZ, cifrado | checkpointer LangGraph + LiteLLM; sin extensiones |
| Redis | ElastiCache (Valkey), TLS, AUTH | STM y caché semántica |
| Qdrant | **StatefulSet en EKS** sobre gp3; **no** Qdrant Cloud | los vectores de C3/C4 no pueden salir: es la fuga que la sección 14 nombra |
| Neo4j | StatefulSet Community, o **GraphRAG al piloto 2** | mismo motivo; Aura queda fuera |
| NATS | Helm oficial, 3 réplicas, JetStream en EBS | ADR-003 |
| OPA | Deployment con bundle desde S3/OCI, versionado con el repo | ADR-008: el Rego del repo es la política |
| Modelos | vLLM en el nodegroup GPU, un Deployment por modelo; embedder bge-m3 (vLLM `--task embed` o TEI) | mismas imágenes y aliases que la PoC |
| Externos | Anthropic / Azure OpenAI vía LiteLLM, sólo C0–C2 | **Decidir por escrito** si un endpoint privado de Bedrock/Azure es «externo»: `model_policy.py` hoy dice que sí. ADR |
| Identidad | **Entra ID** como OIDC (`OIDC_ISSUER`, `OIDC_JWKS_URL`); grupos → `TEAMS_GROUP_MAP`. Cognito si no hay Entra | ya usáis Teams/SharePoint/Copilot Studio; `auth.py` valida RS256/ES256 con JWKS |
| Entrada | ALB + ACM + WAF → Ingress; nada más con IP pública | `DEPLOYMENT.md` §6.1 |
| Secretos | Secrets Manager + External Secrets Operator → el `Secret` que el chart espera | §6.2 |
| Red | subredes privadas; **egress cero** por `NetworkPolicy` para almacenes y vLLM; NAT sólo para LiteLLM hacia externos y para un Job de siembra de modelos | espejo de `backend: internal: true` |
| Observabilidad | OTel Collector → Amazon Managed Prometheus + Grafana (o el stack propio en EKS); Loki en S3; Tempo para trazas | dashboards y alertas de B7 sin cambios |
| Respaldos | RDS snapshots; Qdrant snapshots y ledger a S3 con **Object Lock** | el ledger es evidencia: append-only también en reposo |
| IaC | Terragrunt (vpc, eks, rds, elasticache, s3, iam/irsa, secrets); imágenes en **GHCR** (ya lo usáis) o ECR | vuestra convención |
| Entornos | dev = esta PC · staging = 1 nodo GPU, datos sintéticos + un corpus C0/C1 real · prod | promoción por tag firmado (B5) |

Coste de referencia (lista, `us-east`, **a verificar**): g6.xlarge ≈ 0.8 USD/h;
g6e.xlarge ≈ 1.9 USD/h. La GPU es el 60–70 % de la factura: empezar sólo con `local/fast`
y apagar el nodegroup GPU de staging fuera de horario.

## 6. Ruta D — Piloto y operación (4 semanas tras C)

1. Un área, 10–20 usuarios, corpus real ≤ C2 el primer mes.
2. Métricas fijadas antes de empezar: groundedness ≥ umbral en muestra semanal; p95 primer
   token < 3 s; ≤ 5 % de tareas a HITL; cero incidentes de clasificación (muestreo mensual,
   RUNBOOK §8).
3. Guardia y runbook: alertas de B7 a un canal; `RUNBOOK.md` como fuente; simulacro de
   restauración mensual.
4. Retención y exportación del ledger acordadas con el cliente antes del primer dato real
   (cierra Q-05).
5. Salida a GA: dos ciclos de evals con datos reales anonimizados en verde, restore drill
   hecho en producción, THREAT_MODEL revisado y firmado.

## 7. Decisiones que necesito de ti

| # | Decisión | Bloquea | Recomendación |
|---|---|---|---|
| 1 | Nube u on-prem | toda la Ruta C | AWS/EKS, donde ya estáis |
| 2 | Proveedor de identidad | B1, C-Identidad | Entra ID |
| 3 | Producción v1 standalone o con plataforma PEAK | §2, C-OPA/NATS | Standalone; la plataforma se enchufa por config |
| 4 | ¿Bedrock/Azure por endpoint privado cuenta como «externo»? | C-Externos, `model_policy.py` | Sí, hasta que Legal diga lo contrario; ADR |
| 5 | ¿`local/quality` (32B, 48 GB) desde el día uno? | C-Cómputo, coste | No; 8B-FP8 y medir con A7 |
| 6 | Registro de imágenes | B5 | GHCR |
| 7 | Quién aporta el corpus y los usuarios del piloto | Ruta D | — |

## 8. Riesgos principales

| Riesgo | Prob. | Impacto | Mitigación |
|---|---|---|---|
| Blackwell + vLLM v0.27.1: kernel no soportado o lento | media | A1 se atasca | probar con `--dtype auto` y sin prefix caching; plan B: Ollama con `qwen3:8b` en GPU (misma API, sin prefix caching) |
| RAM del VM insuficiente con `full` + vLLM | alta sin A0.1 | contenedores OOM | A0.1 (24 GB); si no, `core` + `serving` + `knowledge` sin observabilidad |
| Clasificación C0–C4 errónea en el corpus real | media | fuga interna | default nunca C0; override manual; muestreo (RUNBOOK §8) |
| GraphRAG/Neo4j retrasa el piloto | media | alcance | posponerlo: el RAG híbrido sin grafo ya cita |
| La plataforma PEAK cambia contratos (PDP, ledger) | media | retrabajo | `Protocol` con dos implementaciones cada uno (ADR-005): adapter, no reescritura |

## 9. Calendario (1 dev + 1 infra)

| Semana | A · PoC | B · Repo | C · Infra | D · Piloto |
|---|---|---|---|---|
| 1 | A0–A4 | B1, B2 | decisiones de §7 | |
| 2 | A5–A7, demo | B3, B4, B5 | Terragrunt base: vpc, eks, rds, redis | |
| 3 | | B6, B7 | Qdrant, NATS, OPA, vLLM en EKS; staging | |
| 4 | | B8, B9, B10 | secretos, red, observabilidad, respaldos | |
| 5 | | | promoción firmada a prod | arranca |
| 6–9 | | evals con datos reales | restore drill en prod | operación y métricas |
| 10 | | | | GA o extensión |
