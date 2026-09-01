# Quickstart — de cero a chat en menos de 15 minutos

Objetivo: una Agent Cell respondiendo por una API compatible con OpenAI, en tu laptop,
sin GPU.

## Requisitos

| Necesitas | Comprobar |
|---|---|
| Docker con Compose v2 | `docker compose version` |
| GNU Make | `make --version` |
| `uv` | `uv --version` (si no: `pipx install uv`) |
| ~8 GB de RAM libres | Para el modelo pequeño en Ollama |

Python **no** hace falta instalarlo: `uv` gestiona el 3.12 que el proyecto exige.

---

## 1 · Entorno (2 min)

```bash
git clone https://github.com/silent4business/agent-forge.git
cd agent-forge
uv sync --extra dev --extra databases
```

## 2 · Configuración (3 min)

```bash
cp .env.example .env
```

Edita `.env` y cambia como mínimo estas cuatro, que vienen marcadas `change-me`:

```bash
POSTGRES_PASSWORD=...        # y actualiza POSTGRES_DSN con el mismo valor
LITELLM_MASTER_KEY=sk-...
LITELLM_SALT_KEY=...
AGENT_API_KEYS=acme-mx:...   # formato tenant:clave
```

Genera valores decentes con:

```bash
uv run python -c "import secrets; print(secrets.token_urlsafe(32))"
```

## 3 · Arrancar (5 min, casi todo descarga)

```bash
make up PROFILE=core
```

`--wait` hace que el comando no retorne hasta que todos los healthchecks estén verdes.
Verifica:

```bash
make ps
curl -fsS localhost:8080/health/ready | jq
```

## 4 · Un modelo local (3 min)

```bash
docker compose -f deploy/compose/docker-compose.yml --profile serving up -d ollama
docker compose -f deploy/compose/docker-compose.yml exec ollama ollama pull qwen3:8b
```

## 5 · Hablar con la célula (1 min)

```bash
curl -N localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer $TU_API_KEY" \
  -H 'content-type: application/json' \
  -d '{
        "model": "agent-forge",
        "stream": true,
        "messages": [{"role":"user","content":"¿Qué eres y qué puedes hacer?"}]
      }'
```

Deberías ver tokens llegando en SSE.

## 6 · Conectar OpenWebUI (opcional, 1 min)

En OpenWebUI: *Settings → Connections → OpenAI API*, y añade
`http://host.docker.internal:8080/v1` con tu API key. No hace falta nada más: la célula
ya habla el protocolo.

---

## Siguiente paso: darle conocimiento

```bash
mkdir -p data/corpus && cp ~/mis-documentos/*.pdf data/corpus/
make up PROFILE=knowledge
make ingest
```

Ahora pregunta por algo que esté en tus documentos: la respuesta llega **con citas**.
Si no las trae, la ingesta falló; mira los logs del `ingestion-worker`.

## Siguiente paso: una segunda célula

```bash
make new-instance NAME=ventas TENANT=acme-mx
cd instances/acme-mx-ventas
docker compose up -d
```

Corre **a la vez** que la primera, con sus propios puertos, volúmenes y namespace. No se
ha tocado una línea de código.

---

## Problemas frecuentes

| Síntoma | Causa | Solución |
|---|---|---|
| `make up` se queda esperando | Un healthcheck no pasa | `make logs` y busca el servicio en `starting` |
| 401 en `/v1/chat/completions` | `AGENT_API_KEYS` mal formado | Debe ser `tenant:clave`, sin espacios |
| Respuestas sin citas | Corpus vacío | `make ingest` y revisa el worker |
| "connection refused" a Ollama | Falta el perfil `serving` | Paso 4 |
| Todo devuelve denegado | OPA sin arrancar y fail-closed | `make up PROFILE=core` incluye `opa` |

Más detalle en [`docs/RUNBOOK.md`](docs/RUNBOOK.md).
