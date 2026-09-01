# Runbook operativo

> Estado: completado en F8. Los procedimientos que se pudieron ejecutar aquí se
> ejecutaron, y donde el número depende del despliegue se dice en vez de inventarlo.

Documento para quien está de guardia. Cada procedimiento asume acceso al host y a
`make`.

## 1. Arranque y verificación

```bash
make up PROFILE=full
make ps                        # todos healthy
curl -fsS localhost:8080/health/ready | jq
make verify-ledger
```

Dos contenedores **no** tienen healthcheck y eso es correcto: `otel-collector` y `loki`
son imágenes distroless, sin shell, así que una sonda dentro del contenedor no puede
ejecutarse y los dejaría en `unhealthy` para siempre. Su salud se ve en Prometheus, que
raspa `/ready` y el endpoint del health_check extension. Nada depende de ellos con
`service_healthy`.

`/health/ready` reporta **por dependencia** (postgres, redis, litellm, opa, qdrant,
neo4j, nats) y además las **capacidades instaladas** (qué extras están presentes). Un
`ready: false` con `postgres: down` es un problema distinto de `knowledge: unavailable`.

## 2. Diagnóstico rápido

| Síntoma | Primer comando | Causa habitual |
|---|---|---|
| Respuestas lentas | Dashboard "Latencia por nodo" | Retrieval o modelo; mirar p95 por nodo |
| Todo denegado | `docker compose logs opa` | PDP caído: fail-closed activo |
| Sin citas | `make ingest` y revisar el worker | Corpus vacío o ingesta fallida |
| Tarea colgada | `GET /admin/approvals` | Espera aprobación humana |
| Coste disparado | Dashboard "Coste por tenant" | Caché semántica desactivada o budget mal puesto |
| `verify-ledger` falla | Ver sección 6 | Incidente de integridad: escalar |
| Un contenedor en `Restarting` | `docker compose logs <servicio>` | Casi siempre permisos: ver 3.6 |

## 3. Incidentes

### 3.1 PDP inalcanzable (fail-closed activo)

Impacto: se deniega C2+ y A2+. C0/C1 sigue funcionando.

1. Confirmar: `agentforge_policy_decisions_total{effect="fail_closed"}` creciendo.
   Es una etiqueta propia, distinta de `deny`: "la política dijo que no" y "el PDP no
   respondía y por eso rechazamos" son incidentes distintos y no deben confundirse en un
   gráfico.
2. Verificar red y credenciales hacia `GOVERNANCE_PDP_URL`.
3. Mientras dure, **no** cambiar a `permissive_c0c1` sin autorización del responsable de
   seguridad: es una decisión de riesgo, y queda registrada en el ledger en cada uso.
4. Restablecido el PDP, la caché se repuebla sola; no hace falta reiniciar.

### 3.2 Intento de fuga de datos clasificados

Señal: `agentforge_external_model_blocked_total` crece.

1. **No es un fallo del sistema**: el control funcionó. Es una señal de investigación.
2. Localizar las tareas por `tenant_id` y `trace_id` en Langfuse.
3. Determinar si el origen es una clasificación mal aplicada en la ingesta o un intento
   deliberado. Registrar el hallazgo.
4. Si es clasificación errónea: corregir el override del documento y re-ingerir.

### 3.3 Bucle de replan

Señal: `agentforge_judge_verdicts_total{verdict="retry"}` alto para un mismo `task_id`.

1. El límite de reintentos escala a HITL automáticamente; verificar que ocurrió.
2. Revisar el prompt del judge y la disponibilidad de conocimiento: casi siempre es
   groundedness bajo por corpus incompleto.

### 3.4 Cola HITL estancada

1. `GET /admin/approvals?status=pending` ordenada por antigüedad.
2. Verificar que el grupo `governance.hitl_approvers_group` tiene miembros activos.
3. Las tareas no expiran solas por diseño: una acción A2 pendiente es una acción que
   **no** se ha ejecutado. Cancelar explícitamente si procede.

### 3.6 Un contenedor de infraestructura en bucle de reinicio

Casi siempre son permisos, y hay dos causas conocidas porque las dos se produjeron al
endurecer el stack.

**`Operation not permitted` / `Permission denied` al arrancar.** El stack corre con
`cap_drop: ALL`. Postgres, Redis y Neo4j hacen `chown` de su directorio de datos como root
antes de bajar a su propio usuario, así que llevan un `cap_add` mínimo y explícito. Si se
añade un servicio nuevo que falle así:

```bash
docker compose logs <servicio> | head -20    # la línea dirá qué operación se le negó
```

Añadir **sólo** la capacidad que pide, con un comentario que diga por qué. `cap_add: ALL`
para salir del paso convierte el endurecimiento en decoración.

**Postgres se queja de `pg_ctlcluster` y datos sin migrar.** La imagen 18+ guarda los datos
en directorios por versión mayor bajo `/var/lib/postgresql` y **se niega a arrancar** si
encuentra un montaje en el antiguo `/var/lib/postgresql/data`: lo lee como un clúster sin
actualizar. El compose ya monta en la ruta correcta; si aparece tras una migración, es un
volumen viejo y hay que hacer `pg_upgrade`, no mover el montaje.

## 4. Backups

| Almacén | Método | Frecuencia |
|---|---|---|
| Postgres (checkpoints, HITL) | `pg_dump` | Diaria, retención 30 días |
| Qdrant | Snapshot por colección | Diaria |
| Neo4j | `neo4j-admin database dump` | Diaria |
| Ledger | Copia del directorio + `verify-ledger` antes y después | Diaria, retención según política de auditoría |
| Redis | Sin backup: es estado efímero por diseño | — |

**Restauración probada trimestralmente.** Un backup no verificado no es un backup.

## 5. Rotación de claves

Trimestral, en este orden:

1. `LITELLM_MASTER_KEY` → regenerar claves virtuales por tenant → `make restart`.
2. `AGENT_API_KEYS` → publicar nuevas a los clientes con solape de 7 días → retirar.
3. `LEDGER_SIGNING_KEY` → **no** re-firma lo anterior: se anota el cambio de clave como
   entrada del ledger para que la verificación sepa qué clave aplica a qué rango.
4. Credenciales de Graph/S3 y contraseñas de almacenes.

Cada rotación se registra en el ledger.

## 6. Cadena de evidencia rota

`make verify-ledger` reporta el número de secuencia donde falla.

1. **Tratar como incidente de seguridad.** No reparar el fichero.
2. Aislar el fichero afectado y conservarlo tal cual.
3. Comparar con lo emitido al Audit Ledger central: los eventos
   `com.peak.evidence.record.v1` permiten reconstruir el rango.
4. Escalar al responsable de cumplimiento antes de reanudar operaciones.

## 7. Desarrollo nativo en Windows

El producto corre en contenedores Linux; el único punto donde Windows difiere es el
driver async de Postgres. `psycopg` rechaza el `ProactorEventLoop` que Python usa por
defecto en Windows.

* La suite de tests lo resuelve sola (`tests/conftest.py` fuerza el selector loop).
* Ejecutar `agent-api` nativamente en Windows **con checkpointer Postgres** requiere
  arrancar uvicorn con `--loop asyncio` y una política de bucle selector, o simplemente
  usar `make up`, que es la vía soportada.
* Con `POSTGRES_DSN` sin definir la célula arranca con checkpointer en memoria y lo
  advierte en el log: sirve para desarrollo, y una aprobación pendiente **no** sobrevive
  a un reinicio.

## 8. Segunda célula en el mismo host

```bash
make new-instance NAME=ventas TENANT=acme-mx
cd instances/acme-mx-ventas
docker compose --env-file ../../.env --env-file .env --profile core up -d
curl -s http://127.0.0.1:8180/health/ready | jq
```

Los dos `--env-file` y en ese orden: el del repositorio trae la infraestructura compartida
y sus secretos, el de la instancia sólo lo que la hace distinta. El generador no toca
código —hay un test que lo comprueba comparando las marcas de tiempo de `src/`—, y cada
instancia tiene su propio proyecto de Compose, su puerto y su cadena de evidencia.

## 9. Revisiones periódicas

| Qué | Cada |
|---|---|
| Reglas DLP (`configs/policies/dlp_rules.yaml`) | Trimestre |
| Allowlists de tools por tenant | Trimestre |
| Muestreo de clasificación C0–C4 en la ingesta | Mes |
| Restauración de backups | Trimestre |
| Umbrales de evals frente a la realidad de producción | Mes |
