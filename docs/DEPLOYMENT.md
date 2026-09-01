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

## 5. Varias células en el mismo host

```bash
make new-instance NAME=ventas TENANT=acme-mx
cd instances/acme-mx-ventas && docker compose up -d
```

El generador produce perfil, `.env` y override de Compose con nombres de proyecto,
puertos y volúmenes únicos. El aislamiento lógico ya lo da el namespacing por
`tenant_id` + `AGENT_FORGE_INSTANCE`; el override sólo evita colisiones de host.

## 6. Producción

Diferencias respecto a desarrollo:

1. TLS terminado en un reverse proxy; `agent-api` nunca expuesto directamente.
2. Secretos desde el gestor de secretos de la plataforma, no desde `.env` en disco.
3. `AGENT_FORGE_ENV=production`: desactiva la documentación interactiva, exige OIDC y
   rechaza `GOVERNANCE_FAIL_MODE=permissive_c0c1` sin confirmación explícita.
4. Backups: snapshots de Qdrant, `pg_dump` de Postgres, dump de Neo4j y copia del
   ledger. Frecuencia y verificación en [`RUNBOOK.md`](RUNBOOK.md).
5. Imágenes por digest, no por tag móvil.

## 7. Kubernetes

Esqueleto Helm en `deploy/helm/` (F8): Deployment de `agent-api` con HPA, Job de
ingesta, CronJob de mantenimiento, `ConfigMap` del perfil y `Secret` externo. No se
optimiza para Kubernetes a costa del objetivo Compose: el chart consume las mismas
imágenes y el mismo perfil.
