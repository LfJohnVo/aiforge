# Despliegue

> Estado: contrato definido en F0; Compose completo en F1–F7, endurecimiento y Helm en F8.

## 1. Perfiles de Compose

| Perfil | Servicios | Para qué |
|---|---|---|
| `core` | agent-api, postgres, redis, litellm, opa | Mínimo para chatear |
| `serving` | vllm **o** ollama | Inferencia local |
| `knowledge` | qdrant, neo4j, ingestion-worker | RAG/GraphRAG |
| `events` | nats | Ciclo Agregador/Judge |
| `observability` | otel-collector, prometheus, grafana, loki, langfuse | Trazas y dashboards |
| `maintenance` | cron de repo-graph e ingesta | Tareas programadas |
| `full` | todo | Demostración y e2e |

```bash
make up PROFILE=core         # laptop, con Ollama aparte
make up PROFILE=full         # todo verde
make down                    # conserva volúmenes
make down-hard               # borra volúmenes (destructivo)
```

`make up` usa `--wait`: el comando no retorna hasta que todos los healthchecks pasan.

## 2. Reglas de los contenedores

* Usuario **non-root** en todas las imágenes propias (`UID 10001`).
* `read_only: true` en el sistema de archivos raíz donde el servicio lo tolera, con
  `tmpfs` para lo que necesite escribir.
* `cap_drop: [ALL]`; `no-new-privileges`.
* Límites de CPU y memoria declarados en todos los servicios.
* `restart: unless-stopped`.
* Healthchecks **reales** (que ejerciten la dependencia, no `exit 0`), con
  `depends_on: condition: service_healthy`.
* Volúmenes nombrados; ningún bind mount de datos en producción.

## 3. Redes

| Red | Quién |
|---|---|
| `frontend` | Sólo `agent-api` |
| `backend` | Almacenes, litellm, opa, nats, workers |
| `observability` | Collector y su stack |

`agent-api` es el único servicio en dos redes. Ningún almacén publica puerto al host
salvo en perfil `full` para desarrollo, y siempre a `127.0.0.1`.

## 4. GPU

`deploy/compose/gpu.override.yml` añade la reserva de GPU a vLLM:

```bash
docker compose -f deploy/compose/docker-compose.yml \
               -f deploy/compose/gpu.override.yml --profile serving up -d
```

Sin GPU, el perfil `serving` levanta Ollama.

**Ollama no puede descargar modelos.** La red `backend` es `internal: true` —sin salida a
internet, que es lo que impide que un almacén con datos del tenant tenga egress—, así que
`ollama pull` falla resolviendo DNS. El volumen de modelos se siembra una vez desde un
contenedor con salida; el procedimiento está en [`RUNBOOK.md`](RUNBOOK.md) §1.1.

Y en CPU, los alias `local/fast` y `local/quality` apuntan a vLLM: sin GPU hay que usar
`local/dev` o `local/tiny`. Ver RUNBOOK §1.2.

## 5. Varias células en el mismo host

Dos modelos de aislamiento, y la diferencia se mide en contenedores.

### Compartido — `SHARED=1`

```bash
make up PROFILE=core                                  # el stack base, una vez
make new-instance NAME=ventas TENANT=acme-mx SHARED=1
cd instances/acme-mx-ventas
docker compose --env-file ../../.env --env-file .env --profile core up -d
```

La instancia trae **sólo su célula** y se une a las redes del stack base. Postgres, Redis,
Qdrant, NATS y LiteLLM son los del base. Lo que separa a dos instancias es que **cada clave
lleva `tenant_id` + instancia**: para eso se construyó ese namespacing. Coste medido: **un
contenedor** por área adicional.

Para varias áreas de un mismo cliente.

### Independiente — por defecto

```bash
make new-instance NAME=ventas TENANT=acme-mx
```

La instancia clona el stack entero, almacenes incluidos. Radio de impacto más duro —una
base corrupta no toca a la vecina— a cambio de **cinco contenedores** por área. El
namespacing sigue ahí, pero como defensa en profundidad y no como la frontera.

Para tenants que exigen separación física, o cuando la carga de un área no debe poder
ahogar a otra.

### Lo que comparten los dos

El generador produce perfil, `.env`, override de Compose y README, con nombres de proyecto,
puertos y volúmenes de evidencia únicos. **Ninguno toca código**: hay un test que lo
comprueba comparando las marcas de tiempo de `src/`.

El override compartido usa `extends` y no `include`. `include` arrastra *todos* los
servicios, así que levantarlo bajo otro nombre de proyecto clona el stack —lo contrario de
compartir—; `extends` copia una sola definición de servicio, con su healthcheck, su
`cap_drop` y sus límites, de modo que sigue heredando los cambios del compose base sin
heredar sus almacenes.

## 6. Producción

Diferencias respecto a desarrollo:

1. TLS terminado en un reverse proxy; `agent-api` nunca expuesto directamente.
2. Secretos desde el gestor de secretos de la plataforma, no desde `.env` en disco.
3. `AGENT_FORGE_ENV=production` endurece el arranque. Lo aplica
   `runtime.enforce_production_settings`, y cada regla es un fallo de arranque con su
   motivo, no un aviso —los tres problemas se reportan juntos, para no gastar tres
   reinicios en descubrirlos—:

   | Regla | Por qué |
   |---|---|
   | Exige `OIDC_ISSUER` **y** `OIDC_JWKS_URL` | Una API key identifica al *tenant*, no a la persona: sin usuario no hay grupos y la recuperación por identidad no tiene por dónde filtrar (invariante 4) |
   | `fail_mode: permissive_c0c1` sólo con `GOVERNANCE_ACK_PERMISSIVE=1` | Legítimo durante una caída del PDP, nunca por defecto (invariante 3) |
   | Rechaza `DEV_SHARED_SECRET` | No lo lee ningún camino de código: puesto en un `.env` copiado, hace creer que hay una autenticación que no existe |

   Además desactiva `/docs` y `/openapi.json`, y convierte en fatal cualquier extra
   declarado en el perfil que no esté instalado (`check_capabilities`, ADR-005).
4. Backups: snapshots de Qdrant, `pg_dump` de Postgres, dump de Neo4j y copia del
   ledger. Frecuencia y verificación en [`RUNBOOK.md`](RUNBOOK.md).
5. Imágenes por digest, no por tag móvil.

## 7. Kubernetes

Esqueleto Helm en `deploy/helm/` (F8): Deployment de `agent-api` con HPA, Job de
ingesta, CronJob de mantenimiento, `ConfigMap` del perfil y `Secret` externo. No se
optimiza para Kubernetes a costa del objetivo Compose: el chart consume las mismas
imágenes y el mismo perfil.
