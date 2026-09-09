# Traspaso a otra máquina

> Última medición: 2026-09-09, rama `main`, árbol limpio. La célula se arrancó, se
> verificó en vivo y se corrigieron trece defectos de despliegue; las salidas crudas
> están en `docs/memory/evidence/`.
> Este documento existe para que la siguiente sesión **no dependa de la memoria de nadie**.
> Si algo aquí contradice al código, manda el código: corrige este fichero.

## 1. Dónde está el proyecto

**Terminado.** Las ocho fases (F0–F8) del `PROMPT_AGENT_FORGE.md` están cerradas y el
Definition of Done de su sección 12 tiene los 15 puntos con evidencia localizada. El
mensaje `FORGE_DONE` ya se emitió.

Lo que sigue —la PoC en la máquina de desarrollo y el paso a producción— está en
`docs/PRODUCTION_PLAN.md`, con el estado medido el 2026-09-07 y los hallazgos de esa
revisión.

| | |
|---|---|
| Commits | 26, uno por unidad lógica |
| Tests | 736 unit + policy · 28 de integración · 1 saltado (stdio MCP en win32) |
| Cobertura | global 82.4 % · `core` 85.7 % · `governance` 94.8 % · `knowledge` 80.2 % |
| ADRs | 9 (+ plantilla) |
| Decisiones | 71 en `docs/memory/DECISIONS_LOG.md`, una entrada por fase |
| Notas de sesión | 9, una por fase, en `docs/memory/SESSION_NOTES/` |
| SBOM | CycloneDX 1.7, 381 componentes · trivy CRITICAL = 0 |

Lo que **no** está hecho, y por qué —ninguno es un fallo del código:

* **Ingesta desde SharePoint**: el adaptador existe y está probado contra su contrato,
  pero verificarlo de verdad necesita credenciales de un tenant real. El propio DoD lo
  reconoce al escribir «(con credenciales)».
* **vLLM bajo Windows**: no arranca, y no es configuración. `RuntimeError: UVA is not
  available` — el motor V1 usa Unified Virtual Addressing y el passthrough de GPU de WSL2
  no lo expone. En Linux nativo con la misma tarjeta sí. Aquí la GPU la usa **Ollama**
  (`local/dev` para chat, `local/embeddings-dev` para embeddings), y funciona: 6.8 GB de
  VRAM, respuestas con citas en 9–18 s.
* **Restauración de respaldos**: el procedimiento está en el RUNBOOK; probarlo necesita un
  despliegue con datos.

## 2. Qué necesita la máquina nueva

| | Versión | Por qué esa |
|---|---|---|
| Python | **3.12 exacto** (`>=3.12,<3.13`) | `llm-guard` no soporta 3.13+. Ver ADR-006 |
| uv | 0.11+ | El lockfile es de uv; `pip install -r` no reproduce el conjunto |
| Docker | con Compose v2 (`include`, `extends`, `!reset`) | El generador de instancias usa las tres |
| Git | cualquiera | |

Opcional, sólo si se van a usar: **syft** y **trivy** (SBOM y escaneo), y el binario **opa**
o Docker para los tests de política —sin ellos, `tests/policies/test_rego.py` se salta con
motivo explícito y la tabla de casos sigue cubierta por el camino Python.

```bash
git clone https://github.com/LfJohnVo/aiforge.git
cd aiforge
make install          # crea el venv desde uv.lock e instala pre-commit
make check            # ruff + mypy strict + tests unitarios. Debe salir verde
```

`make check` verde en la máquina nueva es la única prueba de que el traspaso salió bien.

## 3. Arrancar y consumir

> Lo de abajo se ejecutó entero el 2026-09-09 y funciona. Si algo falla, es del entorno,
> no del procedimiento.

```bash
cp .env.example .env
```

Hay que rellenar **siete** valores; los cinco marcados `change-me` más dos que no lo están:

| Variable | Nota |
|---|---|
| `POSTGRES_PASSWORD` · `NEO4J_PASSWORD` · `GRAFANA_ADMIN_PASSWORD` | marcadas `change-me` |
| `LITELLM_MASTER_KEY` · `LITELLM_SALT_KEY` | marcadas `change-me` |
| `AGENT_API_KEYS` | vacía. Formato `tenant:clave`, p. ej. `acme-mx:una-clave-larga` |
| `POSTGRES_DSN` | **duplica la contraseña**. Cambiar `POSTGRES_PASSWORD` y olvidar ésta es la trampa |

```bash
make up PROFILE=core
curl -s http://127.0.0.1:8080/health/ready
```

`make up` ya pasa `--env-file .env` cuando ese fichero existe. Hace falta: Compose resuelve
`.env` **junto al fichero compose**, no desde donde corres el comando, así que sin eso el
`.env` de la raíz no lo lee nadie y el error es «falta LITELLM_MASTER_KEY» sobre una
variable que está puesta.

Para la PoC completa —con GPU, corpus, OIDC de juguete y Mailpit— hay tres ficheros de
override y un guion que lo comprueba:

```bash
uv run python scripts/demo/make_corpus.py      # corpus sintetico, todo inventado
uv run python scripts/demo/mint_tokens.py      # emisor OIDC local y cuatro usuarios
docker compose -f deploy/compose/docker-compose.yml \
               -f deploy/compose/gpu.override.yml \
               -f deploy/compose/demo.override.yml --env-file .env \
               --profile core --profile serving --profile knowledge up -d --wait
docker exec <proyecto>-ingestion-worker-1 python -m agent_forge.knowledge.ingestion --once
uv run python scripts/demo/verify_live.py      # 7/7
```

El filtrado por identidad **no se puede demostrar con una API key**: una key identifica al
tenant, así que `groups` viene vacío y no hay nada que filtrar. De ahí el emisor de juguete.

Para obtener una respuesta de un modelo hacen falta **dos pasos más** que no son obvios;
están en `docs/RUNBOOK.md` §1.1 y §1.2 y se resumen así:

1. **Ollama no puede descargar modelos.** La red `backend` es `internal: true` —sin salida
   a internet, que es lo que impide que un almacén con datos del tenant tenga egress—, así
   que `ollama pull` falla resolviendo DNS y parece un problema del host. El volumen se
   siembra una vez desde un contenedor que sí tiene salida.
2. **Los alias por defecto piden GPU.** `local/fast` y `local/quality` apuntan a vLLM. En
   CPU hay que apuntar el perfil de la instancia a `local/dev` (qwen3:8b) o `local/tiny`
   (qwen3:0.6b, 522 MB, sólo para comprobar que el stack responde).

## 4. Trampas del entorno que ya costaron tiempo

Windows, y varias de éstas se descubrieron a base de perderlas:

* **Los heredoc de bash colapsan `\\` a `\`.** Un `"\\n"` dentro de un heredoc llega al
  fichero como un salto de línea real y parte el código. Para parchear ficheros con
  secuencias de escape: escribe un script `.py` con la herramienta Write y ejecútalo.
* **`MSYS_NO_PATHCONV=1`** delante de cualquier `docker run -v` en Git Bash, o Git Bash
  convierte `/policies` en `C:/Program Files/Git/policies`.
* **`ruff format` junta líneas**, así que un reemplazo de texto multilínea que funcionaba
  antes de formatear deja de encajar después. Verifica el texto real antes de parchear.
* **`uv tool install`, no `pip install`**, para herramientas externas: este repo tiene
  pines deliberados (`mcp` 2.x, `json-repair`) que una instalación compartida rompería.
* **El plugin de pytest de `deepeval`** lee `.env` al arrancar y mataba la sesión entera
  antes de recolectar un solo test. Ya está desactivado con `-p no:deepeval` en
  `pyproject.toml`; si aparece algo parecido con otra extra, el patrón es el mismo.

## 5. Bugs de despliegue ya corregidos

No los vuelvas a buscar; están arreglados y con test. Se listan porque explican por qué el
compose y los perfiles tienen la forma que tienen:

1. `cap_drop: ALL` tumbaba Postgres, Redis y Neo4j: hacen `chown` como root antes de bajar
   de privilegios. Cada uno lleva el `cap_add` mínimo **que su log pidió**.
2. `postgres:18` se niega a arrancar si encuentra el volumen en `/var/lib/postgresql/data`:
   lo lee como un clúster sin migrar. Va en `/var/lib/postgresql`.
3. `otel-collector` y `loki` son distroless: sus sondas `CMD-SHELL` no podían ejecutarse y
   los dejaban `unhealthy` para siempre. No llevan healthcheck de contenedor.
4. Con NATS configurado y ausente, `nats.connect` reintentaba **para siempre** y colgaba la
   petición. Ahora está acotado y degrada.
5. `check_capabilities` avisaba en desarrollo mientras los constructores de Qdrant y Neo4j
   lanzaban igualmente, así que el perfil `core` **no podía arrancar**. Ahora coinciden.

Y ocho más de la noche del 2026-09-09, con el detalle en `docs/PRODUCTION_PLAN.md` §0. Los
tres que más tiempo cuestan si se vuelven a encontrar a ciegas:

6. **El worker de ingesta nunca arrancó.** Su `CMD` era `python -m
   agent_forge.knowledge.ingestion` y ese paquete no tenía `__main__`. Ahora existe, con
   una planificación por fuente leída de `sync_cron`.
7. **La imagen de la API no podía leer el corpus.** Se construía sin extras, así que caía a
   un almacén vectorial en memoria —vacío— mientras el worker escribía en Qdrant. Y el
   arreglo tenía trampa: el segundo `uv sync` de la etapa *desinstala* lo que instaló el
   primero si no repite los extras.
8. **La caché semántica cachea los fallos.** Dos verificaciones seguidas dieron 3/7 con el
   sistema ya arreglado porque servía respuestas «no hay información» de antes, con
   `similarity=1.0`. Si algo va mal y acabas de arreglarlo, vacía Redis antes de concluir
   nada.

## 6. Dos modelos de instancia

```bash
make new-instance NAME=ventas TENANT=acme-mx SHARED=1   # 1 contenedor
make new-instance NAME=ventas TENANT=acme-mx            # 5 contenedores
```

Compartido usa `extends` y se une a las redes del stack base; el aislamiento es el
namespacing por `tenant_id` + instancia, que es su propósito. Independiente clona el stack.
Medido con tres células a la vez. Ver `docs/DEPLOYMENT.md` §5 y la decisión D-071.

## 7. Herramientas de sesión que hay que reinstalar

`code-review-graph` está registrado en `.mcp.json` (sin ruta absoluta, portable), pero el
binario y el grafo son locales:

```bash
uv tool install code-review-graph
code-review-graph build
```

El grafo vive en `.code-review-graph/`, que está en `.gitignore`. Sus hooks en
`.claude/settings.json` y el `pre-commit` de git ya vienen en el repo.

**Nota**: `.claude/settings.json` instala un hook `PostToolUse` que corre una actualización
del grafo tras cada edición. Si molesta, se quita de ahí.

## 8. Antes de irte de esta máquina

Quedan **12 contenedores de demostración** de la verificación del 2026-09-02, parados
(`Exited`) pero no eliminados, con sus volúmenes. No hacen falta para nada:

```bash
docker compose -p demo -f deploy/compose/docker-compose.yml down -v
docker compose -p demo-soc -f instances/acme-mx-soc/docker-compose.yml down -v
docker compose -p demo-ventas -f instances/acme-mx-ventas/docker-compose.yml down -v
```

`instances/` está en `.gitignore`: las instancias generadas son artefactos de despliegue
local, no fuente. En la máquina nueva se regeneran con un comando.

## 9. Orden de lectura para la siguiente sesión

1. `CLAUDE.md` — el bloque de misión y las once invariantes. **Nunca borres ese bloque.**
2. Este fichero.
3. `docs/memory/DECISIONS_LOG.md` — 71 decisiones, del final hacia atrás.
4. `docs/ARCHITECTURE.md` y `docs/REPO_MAP.md` (generado del AST con `make repo-graph`).
5. El ADR concreto cuando preguntes «por qué X y no Y».

Precedencia si dos documentos discrepan: **el código medido > los ADR > las notas de sesión
> el resto**. Y un `DESCONOCIDO` escrito es mejor que un aprobado supuesto.
