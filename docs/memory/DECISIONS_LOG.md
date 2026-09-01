# Bitácora de decisiones

Cronológica. Una entrada por decisión significativa y una por cierre de fase. El
razonamiento largo vive en el ADR; aquí queda el rastro.

---

## 2026-09-01 · F0 · Fundación

### D-001 · Framework agéntico: LangGraph
**Alternativas:** Pydantic AI, orquestación propia sobre asyncio.
**Razón:** único candidato con checkpointer persistente, `interrupt()` para HITL y
subgrafos componibles ya construidos; las alternativas obligan a reimplementar la
persistencia de estado, que es donde un fallo cuesta tareas perdidas.
**Enlace:** [ADR-001](../adr/ADR-001-agentic-framework.md)

### D-002 · Memoria: Redis STM + Mem0/Qdrant LTM + Graphiti/Neo4j temporal
**Alternativas:** LangMem, Letta/MemGPT, implementación propia.
**Razón:** Mem0 aporta consolidación de hechos, Graphiti aporta bitemporalidad (ninguna
otra opción la da). Mitigación del lock-in: el core consume `Protocol`, ambos son
adapters, y el default en desarrollo es un adapter propio sobre Redis.
**Enlace:** [ADR-002](../adr/ADR-002-memory-architecture.md)

### D-003 · Broker: NATS JetStream tras la abstracción `EventBus`
**Alternativas:** Redpanda, Kafka, Redis Streams.
**Razón:** streams persistentes, consumers durables, DLQ y deduplicación por id en un
contenedor de decenas de MB, compatible con el objetivo "todo el stack en una laptop".
La abstracción es la mitad importante: swap a Kafka sin tocar `core/`.
**Enlace:** [ADR-003](../adr/ADR-003-event-broker.md)

### D-004 · Vector store: Qdrant
**Alternativas:** pgvector, Weaviate, Milvus.
**Razón:** el requisito "el usuario sin permiso no recibe ni la existencia del
documento" obliga a filtrar **dentro** de la búsqueda. Los filtros indexados sobre
payload de Qdrant lo resuelven; pgvector degrada el recall de forma difícil de acotar
al hacer filtrado exacto sobre HNSW.
**Enlace:** [ADR-004](../adr/ADR-004-vector-store.md)

### D-005 · Estratificación de dependencias: núcleo liviano + extras + fallbacks
**Contexto:** instalar docling, mem0, graphiti, presidio, llm-guard, deepeval y ragas
como obligatorias produce un entorno de varios GB, contradiciendo el objetivo de
quickstart en 15 minutos, imágenes slim y CI rápido.
**Decisión:** extras `knowledge`, `memory`, `guardrails`, `promptguard`, `databases`,
`sources`, `evals`, `analytics`, cada uno con un fallback de primera clase (no un error)
e importación perezosa confinada a su adapter.
**Enlace:** [ADR-005](../adr/ADR-005-dependency-tiering.md)

### D-006 · DLP: motor de reglas propio siempre activo
**Contexto:** al fijar `uv.lock` aparecieron dos incompatibilidades **reales**:
`llm-guard==0.3.16` fija `json-repair==0.44.1` mientras `lightrag-hku==1.5.6` exige
`>=0.59.9`; y `llm-guard` limita el intérprete a `<3.13`.
**Decisión:** el prompt firewall es un control de seguridad y debe funcionar sin extras
y sin red, así que se implementa como motor de reglas propio en `governance/dlp.py`,
con Presidio (extra `guardrails`) y LLM Guard (extra `promptguard`, declarado
conflictivo con `knowledge`) como refuerzos que sólo pueden **endurecer** el veredicto.
**Consecuencia colateral:** el proyecto queda anclado a Python 3.12 exacto.
**Enlace:** [ADR-006](../adr/ADR-006-dlp-stack.md)

### D-007 · Tres refinamientos sobre la estructura de la sección 7 del prompt
1. `connectors/sources/` pasa a `knowledge/sources/`: las fuentes de ingesta no son
   tools y no deben compartir registry con los conectores.
2. Se añade `src/agent_forge/profile/`: el modelo Pydantic del perfil es transversal a
   core, knowledge y governance; ponerlo dentro de cualquiera de ellos crea un ciclo.
3. `deploy/compose/` con un override por perfil en archivo separado, en lugar de un
   único fichero con todo.

### Cierre de F0

**Construido:** árbol completo de la sección 7, `pyproject.toml` con versiones fijadas y
`uv.lock` resuelto y verificado, ruff + mypy strict + pre-commit, Makefile con la
interfaz completa, `.env.example` exhaustivo con defaults seguros, CI base
(lint/type/test, security, repo-graph), esqueleto de las 9 skills y documentos exigidos
por la sección 8 con contenido real, ADR-001 a ADR-006, memoria de proyecto.

**Verificado:** `make check` en verde; `uv lock` resuelve sin conflictos; `make
docs-check` confirma que existe cada documento exigido y que ninguno es un esqueleto
vacío.

**Siguiente:** F1 · núcleo agéntico (grafo LangGraph, estado tipado, checkpointer,
autonomía A0–A4, HITL, subgrafos `generalist` e `it_support`) y canal
OpenAI-compatible con streaming.

---

## 2026-09-01 · F1 · Núcleo agéntico + canal base

### D-008 · Autonomía: separar el nivel *requerido* del nivel *concedido*
**Contexto:** `resolve()` combina por máximo, que es correcto para el requisito de una
acción (la tool, el perfil y el PDP sólo pueden endurecerlo). Aplicado también al techo
del solicitante, hacía imposible que un usuario anónimo (A0) quedara por debajo del
`autonomy.default` del perfil: podía ejecutar acciones A1.
**Decisión:** son dos cantidades distintas. Requerido combina por **máximo**; concedido
combina por **mínimo**. Una acción corre sin humano sólo si
`requerido < A2 Y requerido <= concedido`.
**Consecuencia:** `GateOutcome.autonomy_granted` sustituye a `autonomy_ceiling`.
Documentado en `docs/GOVERNANCE.md` §4, con test de regresión.

### D-009 · El transporte de modelos habla HTTP con el proxy, no importa el SDK
**Alternativas:** importar `litellm` en proceso.
**Razón:** pasar por el proxy da claves virtuales por tenant, budgets y registro de coste
sin código propio, y mantiene la imagen de `agent-api` libre de SDKs de proveedores
(coherente con ADR-005). Además hace que `GovernedGateway` sea la única puerta: ninguna
capa puede alcanzar un backend sin pasar por la comprobación de soberanía.

### D-010 · Los subgrafos no importan LangGraph
**Razón:** materializa la mitigación de lock-in que promete el ADR-001. El contrato
`DomainSubgraph` es una fachada (`DomainContext` / `DomainOutcome`); cambiar de runtime
reescribiría `core/graph.py` y dejaría intacto cada plugin de dominio.

### D-011 · `psycopg[binary,pool]` como dependencia del núcleo
**Contexto:** `langgraph-checkpoint-postgres` declara `psycopg` pero no la rueda binaria,
así que el checkpointer fallaba en tiempo de ejecución con `no pq wrapper available`.
**Decisión:** fijarla explícitamente. La rueda binaria trae libpq, de modo que la imagen
no necesita cliente de PostgreSQL del sistema.

### D-012 · Nodos del grafo con `functools.partial`, no `lambda`
**Razón:** LangGraph decide si esperar un nodo con `iscoroutinefunction`. Una lambda que
devuelve una corrutina no lo es, y el grafo falla con `InvalidUpdateError`. `partial`
sobre una función asíncrona sí se detecta correctamente.

### Cierre de F1

**Construido:** kernel de dominio (errores, clasificación, autonomía, estado, prompts),
grafo LangGraph de ocho nodos con checkpointer y HITL, subgrafos `generalist` e
`it_support`, model gateway con la invariante de soberanía, perfil Pydantic validado,
canal OpenAI-compatible con SSE, `/admin/*`, `/health/*`, Dockerfile multi-stage y
Compose con siete perfiles.

**Verificado:** chat e2e con streaming real (`agent-api` → LiteLLM → Ollama `qwen3:0.6b`);
HITL que pausa y reanuda por HTTP; reanudación tras descartar el proceso, contra Postgres
real vía testcontainers. `make check` verde; cobertura `core` 84 %, `gateway` 98 %.

**Siete bugs reales corregidos** durante la fase, detallados en la nota de sesión. Dos
eran de seguridad: la autonomía concedida que no podía reducirse, y la pausa HITL que no
llegaba a la cola (una acción A2 quedaba esperando a un humano que nunca la veía).

**Siguiente:** F2 · memoria.

---

## 2026-09-01 · F2 · Memoria

### D-013 · Una entrada de caché con PII no se guarda, no se guarda redactada
**Contexto:** la caché semántica persistía pregunta y respuesta en claro mientras las
otras tres capas scrubbeaban. Lo detectó un test de invariante.
**Alternativas:** (a) redactar la respuesta antes de cachearla — serviría una réplica
degradada, distinta de lo que devolvería una llamada fresca; (b) usar la pregunta
redactada como clave — haría que la pregunta de otra persona con otro correo coincidiera
con la misma entrada, una fuga peor que la original.
**Decisión:** si la pregunta o la respuesta contienen PII, la entrada se omite y se
registra (`cache.skipped_pii`). Una respuesta con PII es específica de una persona y por
tanto mal candidato a caché de todos modos.

### D-014 · La caché comprueba alcance, no sólo clasificación
**Razón:** dos personas con el mismo techo C2 pueden tener reach distinto. Una respuesta
sintetizada para `finanzas-lideres` puede contener material que un miembro de `finanzas`
no debe ver. La entrada guarda el conjunto de grupos con el que se generó y sólo sirve
peticiones cuyo conjunto lo contenga.

### D-015 · Las preferencias se recuperan siempre, no por consulta
**Contexto:** `recall()` puntúa por solapamiento con la pregunta. "Responde breve" no
comparte vocabulario con ninguna pregunta, así que nunca se recuperaba.
**Decisión:** `recall()` acepta filtro por tipo de hecho, y `context_for` trae los
`preference` de forma incondicional además de los `fact` que coinciden con la consulta.

### D-016 · El almacén de memoria es un `Protocol`, no Redis directamente
**Razón:** hace que las pruebas de aislamiento entre tenants y de borrado —las dos que
protegen requisitos de cumplimiento— corran en cada commit sin infraestructura. El
`InMemoryStore` implementa TTL real, así que no es un doble degradado. Las partes que
pueden divergir (TTL, SCAN, conteos de borrado) se prueban además contra Redis real.

### D-017 · Alcance `user` no lee el bucket del área
**Razón:** un hecho aprendido de un colega es memoria del **área**, no de esta persona.
Mezclarlos haría que un alcance restrictivo devolviera exactamente lo que restringe.

### Cierre de F2

**Construido:** las cuatro capas de memoria tras `Protocol`, con dos implementaciones
cada una donde importa; scrubbing determinista de nueve tipos de PII ES/EN con checksum;
`forget` que barre todas las capas y devuelve un informe por capa; cableado al grafo y a
`/admin/memory`; embeddings soberanos en el gateway.

**Verificado:** el agente recuerda entre sesiones; la caché ahorra la llamada al modelo y
el ahorro es medible; `forget` no deja una sola clave, comprobado también contra Redis
real vía testcontainers. 202 tests unitarios y 11 de integración en verde; cobertura
`memory` 85.7 %.

**Tres defectos de diseño corregidos**, detallados en la nota de sesión. El primero era
una fuga real de PII a un almacén persistente.

**Siguiente:** F3 · conocimiento.

---

## 2026-09-01 · F3 · Conocimiento

### D-018 · El filtro de acceso se traduce en un solo sitio
**Razón:** el requisito "el usuario sin permiso no recibe ni la existencia del documento"
obliga a que el filtro entre en la consulta. Que sea auditable exige además que exista una
única traducción de (identidad x grupos x techo x política) a filtro de almacén:
`build_filter`. Un segundo punto de traducción es, por definición, el bug.
**Consecuencia:** cada rama de recuperación —vectorial, BM25, grafo, CAG— filtra con el
mismo predicado **antes** de fusionar. Fusionar y filtrar después reintroduce el canal
lateral: la longitud de la lista fusionada dependería de lo que el solicitante no puede
ver.

### D-019 · Umbral de relevancia sobre la puntuación del re-ranker
**Contexto:** la búsqueda por vecino más cercano siempre devuelve algo. Una pregunta ajena
al corpus devolvía el documento menos lejano, con cita, y el modelo podía citarlo.
**Decisión:** `MIN_RELEVANCE = 0.15` sobre la cobertura normalizada del re-ranker. No
sobre RRF: sus valores no son comparables entre corpus, y por eso el re-ranking queda
activo por defecto.

### D-020 · `point_id` como UUID5, no como digest
**Contexto:** Qdrant sólo acepta enteros sin signo o UUIDs como identificador de punto. El
digest hexadecimal hacía fallar **todos** los upserts con 400, y el almacén en memoria de
los tests unitarios lo aceptaba encantado.
**Decisión:** UUID5 con espacio de nombres constante. Sigue siendo determinista —re-ingerir
reemplaza en vez de duplicar— y ahora es válido.
**Lección:** el adaptador real necesita su propio test de integración; el doble in-memory
sólo prueba el contrato que uno mismo escribió.

### D-021 · La ACL va dentro de `min_should`, no en un `should`
**Contexto:** el filtro de Qdrant colocaba las condiciones de ACL en un `should` con
`min_should: {"conditions": 1}`, que es una forma inválida.
**Decisión:** `min_should: {"conditions": [...], "min_count": 1}`.
**Por qué importa más de lo que parece:** la variante *silenciosa* de este error —dejar las
condiciones en un `should` a secas— no falla, simplemente convierte la ACL en una pista de
ranking. Habría devuelto documentos prohibidos, ordenados un poco más abajo.

### D-022 · Embeddings por *feature hashing* como implementación por defecto
**Alternativas:** exigir el extra `knowledge` para cualquier recuperación.
**Razón:** el hashing con signo sobre palabras y trigramas es una técnica real,
determinista y sin dependencias. Permite que los tests de aislamiento y de ACL corran en
cualquier entorno. Su límite es honesto y está documentado: es **léxico**, no semántico.

### D-023 · ACL de usuario y ACL de grupo conceden de forma independiente
**Razón:** un documento dirigido nominalmente a una persona no lo ve su jefe por ser su
jefe. Las visibilidades se solapan, no se anidan. Lo hizo explícito un test de integración
cuya aserción original (`analista ⊂ líder`) era falsa por una razón correcta.

### Cierre de F3

**Construido:** modelo de documento con ACL y clasificación embebidas, clasificador C0–C4
de cuatro niveles de autoridad, control de acceso con una sola traducción, recuperación
híbrida (vectorial + BM25 + grafo) con RRF, re-ranking y umbral, GraphRAG con Neo4j y
memoria, CAG con presupuesto, e ingesta incremental desde carpeta, SharePoint (delta) y S3.

**Verificado:** una pregunta sobre un documento ingerido responde con su cita; el mismo
query desde un usuario sin permiso no revela contenido, ni nombre, ni existencia —probado
también contra **Qdrant real**. Cobertura `knowledge` 80.9 %.

**Tres bugs reales**, dos de ellos invisibles para los tests unitarios porque sólo existían
en la traducción al almacén real.

**Siguiente:** F4 · conectores.

---

## 2026-09-01 · F4 · Conectores

### D-024 · Los defaults del contrato van a la dirección segura
**Razón:** el registry puede comprobar la allowlist y la salud, pero no puede comprobar
*honestidad*. Un conector que declara `autonomy_min=A0` para una acción destructiva pasa
todos los controles. Los defaults compensan: `A2` (quien no piensa en autonomía obtiene un
humano), `C4` (quien no clasifica su salida no la publica) y un `health()` que debe
sondear.

### D-025 · La allowlist vacía no permite nada
**Alternativas:** interpretar "sin allowlist" como "todo permitido", que es lo cómodo.
**Razón:** un perfil que olvida declarar sus tools debe producir un agente que no puede
actuar, no uno que puede actuar sobre todo.

### D-026 · El catálogo de tools es por petición
**Razón:** depende del techo de clasificación del solicitante y de qué conectores están
sanos en ese instante. Un catálogo por célula ofrecería al modelo herramientas que van a
ser rechazadas, lo que gasta una llamada y le enseña un mal hábito.

### D-027 · Una tool no disponible se rechaza, no se pausa para aprobación
**Contexto:** si el descriptor no estaba en el catálogo, el nivel requerido salía A4 y el
grafo llamaba a `interrupt()`.
**Por qué está mal:** pedirle a una persona que autorice una herramienta inexistente le
hace perder el tiempo, y si la aprueba la acción falla igual.
**Decisión:** rechazo inmediato, registrado, y el modelo se entera —necesita saber que la
acción no ocurrió, o la dará por hecha.

### D-028 · El MCP client comprueba los dos nombres de `is_error`
**Contexto:** MCP 2.x renombró `isError` a `is_error` (y `inputSchema` a `input_schema`).
Con el nombre viejo, **un fallo remoto se leía como éxito**.
**Decisión:** comprobar ambos, porque un gateway puede estar corriendo cualquiera de las
dos generaciones del SDK y tratar un fallo como éxito es el peor modo de fallo posible
para un agente.

### D-029 · La identidad del usuario viaja en una clave propia hacia MCP
**Razón:** enviada dentro de los argumentos de la tool, un servidor remoto podría declarar
un parámetro `user_id` y recibir —y devolver— algo suplantable. Va bajo `_agentforge`,
separada de lo que la tool declara.

### D-030 · El test de MCP usa un servidor real, no un doble
**Razón:** los tres bugs de esta fase vivían en la traducción al protocolo real. Un
`ClientSession` mockeado los habría aprobado todos. El servidor de prueba se lanza como
subproceso y habla el protocolo de verdad.

### Cierre de F4

**Construido:** contrato `BaseConnector` con defaults seguros, registry con allowlist y
doble comprobación, cliente MCP multi-transporte, n8n con callbacks, OpenConnector,
cuatro drivers de BD con plantillas allowlisted, `repo_graph.query`, la ruta de callback
y el generador `make new-connector`.

**Verificado:** una tool servida por un servidor MCP real se ejecuta e2e; un workflow n8n
disparado desde el grafo lo deja esperando y su callback lo reanuda hasta la respuesta.
Cobertura `connectors` 83.6 %.

**Cuatro bugs reales**, tres de ellos invisibles para un test con dobles. Uno —el fallo
remoto leído como éxito— habría hecho que el agente afirmara haber ejecutado acciones que
fallaron.

**Siguiente:** F5 · upstream y canales.


## 2026-09-01 · F5 · Upstream y canales

### D-031 · El servidor MCP se publica sólo por streamable HTTP
**Contexto:** `ORCHESTRATORS.md` prometía en F0 stdio *y* HTTP.
**Decisión:** sólo HTTP. Un servidor stdio es un proceso hijo cuyo control de acceso es el
permiso de ejecución del binario, lo que rompe la invariante de que toda superficie
autentica al solicitante y aplica su techo. El stdio sigue en el *cliente* MCP, donde la
célula es quien lanza el proceso. Registrado como
[ADR-007](../adr/ADR-007-mcp-server-transport.md).

### D-032 · El `lifespan` padre arranca el de la sub-app de MCP
**Contexto:** Starlette no ejecuta el `lifespan` de una aplicación montada. El servidor
MCP quedaba montado pero sin gestor de sesiones, y **toda llamada fallaba en caliente**
—no en el arranque— con `Task group is not initialized`.
**Decisión:** el `lifespan` de la aplicación entra en el de la sub-app a través del
`AsyncExitStack`. Un fallo que sólo aparece en la primera petición de un orquestador es
peor que uno que impide arrancar.

### D-033 · La protección contra DNS rebinding se configura, no se desactiva
**Contexto:** el servidor MCP valida la cabecera `Host` y sin configurar sólo acepta
loopback; detrás de un proxy rechazaba todo. Desactivarla era una línea.
**Decisión:** derivar la lista de `AGENT_PUBLIC_URL` y ampliarla con `MCP_ALLOWED_HOSTS`.
Un despliegue detrás de proxy que no fije la URL pública falla cerrado y con mensaje
claro, que es como debe fallar un control de este tipo.

### D-034 · El Agent Card anuncia skills, nunca el catálogo de tools
**Razón:** es el único endpoint no autenticado además de liveness —la discovery precede a
la autenticación—, así que su contenido es público para cualquiera que alcance la célula.
Enumerar tools o fuentes de conocimiento diría qué sistemas corre el tenant sin aportar
nada a quien la consume legítimamente. Hay un test que lo comprueba.

### D-035 · El spec de Copilot Studio se genera y se recorta, no se escribe
**Razón:** un documento OpenAPI escrito a mano se desincroniza del código con total
seguridad. Se post-procesa el que genera FastAPI y se limita a tres endpoints;
administración y salud quedan fuera porque publicarlas como acciones invita a un
orquestador a usarlas.

### D-036 · Un usuario de Teams o Slack fuera del mapa de grupos no ve nada con ACL
**Razón:** el canal no adivina entitlements. Sin entrada en `TEAMS_GROUP_MAP` /
`SLACK_GROUP_MAP` el usuario queda autenticado pero sin grupos, y bajo recuperación
identity-aware eso significa sólo material sin ACL. Es el resultado correcto para quien el
directorio no sitúa.

### D-037 · El pipe de OpenWebUI es opcional y vive fuera de la célula
**Razón:** el criterio de salida es que OpenWebUI conecte **sólo con la URL base**, y eso
ya funciona porque el canal es OpenAI-compatible. El pipe existe únicamente para lo que
esa conexión no puede hacer —propagar identidad y renderizar citas y `awaiting_approval`
como estados propios— y se ejecuta dentro de OpenWebUI, no dentro de la célula.

### D-038 · La construcción de una célula de prueba se comparte en `tests/cell.py`
**Contexto:** `Runtime` ganó dos campos obligatorios y el `Runtime` hecho a mano de
`test_api.py` dejó de compilar.
**Decisión:** un solo constructor compartido en vez de parchear la copia. Tres módulos
ensamblando la célula de tres maneras distintas es tener tres tests de una célula que
nadie despliega.

### Cierre de F5

**Construido:** ciclo de vida de tarea compartido por MCP y A2A, servidor MCP con las
cinco tools de RF-03, Agent Card y JSON-RPC A2A, spec saneado para Copilot Studio, canal
WebSocket, webhooks de Teams y Slack con verificación de firma obligatoria, y pipe
opcional de OpenWebUI.

**Verificado:** un cliente real del SDK de MCP lista las tools e invoca `run_task` contra
la app montada; el Agent Card sirve sin credencial y trae los campos que A2A exige sin
filtrar el catálogo interno; OpenWebUI conecta con sólo la URL base y su API key.

**Dos bugs reales**, ambos de integración y ambos invisibles para un test con dobles: el
gestor de sesiones de MCP que nunca arrancaba, y el rechazo por cabecera `Host`. El
primero habría dejado pasar un despliegue aparentemente sano hasta la primera llamada de
un orquestador.

Cobertura global 82.7 %; 440 tests unit + policy, 1 saltado por plataforma.

**Siguiente:** F6 · gobernanza, ciclo Agregador/Judge y evidencia.

## 2026-09-01 · F6 · Gobernanza, ciclo Agregador/Judge y evidencia

### D-039 · La base de politicas existe dos veces, y una tabla de casos las iguala
**Contexto:** RF-09 pide Rego revisable por la plataforma *y* que la celula siga decidiendo
con el PDP caido, por cada chunk y antes de cada tool.
**Decision:** escribir la base dos veces —`configs/policies/*.rego` y `LocalPdp`— y
ejecutar una sola tabla (`tests/policies/cases.py`) por ambos caminos. No hay interprete
de Rego maduro para Python, y un sidecar OPA obligatorio contradice el requisito de
funcionar aislada. Registrado como
[ADR-008](../adr/ADR-008-policy-two-implementations.md).

### D-040 · El overlay remoto solo puede estrechar
**Razon:** `decisions.combine` hace `allow` por conjuncion, el techo por minimo y la
autonomia requerida por maximo. Si un overlay pudiera permitir lo que la base prohibe, una
mala configuracion remota bastaria para abrir un tenant entero. La direccion del fallo
importa mas que su probabilidad.

### D-041 · C3/C4 y A2+ nunca corren sin decision fresca, y eso no es configurable
**Razon:** `fail_mode` distingue que hacer con C0/C1 cuando el PDP no responde; para lo
demas no hay modo. Lo impone `PolicyRequest.needs_fresh_decision`, consultado **antes** de
mirar la cache, de modo que ninguna entrada cacheada por fresca que sea sustituye a una
decision viva sobre dato restringido o accion con efecto.

### D-042 · El DLP se ejecuta antes que el PDP
**Razon:** el DLP inspecciona texto y puede terminar la peticion; el PDP decide sobre
hechos. Preguntar al PDP por algo que se va a bloquear gasta una llamada y, peor, acerca el
texto sin depurar a un servicio remoto.

### D-043 · La deteccion de PII no se reimplementa en el DLP
**Razon:** `memory/scrubbing.py` ya tiene regex con checksum (Luhn, mod-97, RFC/CURP/NIF) y
Presidio opcional encima. Dos detectores serian dos juegos de reglas que se desincronizan,
y el dia que discrepan uno de los dos esta mal.

### D-044 · Un hallazgo del DLP nunca lleva el texto que lo disparo
**Razon:** un registro sobre un secreto no puede contener ese secreto. `DlpMatch` guarda
id de regla, severidad, accion y cuenta.

### D-045 · El ledger guarda digests, jamas contenido
**Razon:** una cadena de evidencia se conserva años y la leen personas sin derecho al
contenido del tenant. Guardar prompts o respuestas ahi convertiria la pista de auditoria en
la mayor copia sin clasificar de todo lo que la celula ha visto. El hash cubre **todos** los
campos, metadatos incluidos: lo que quede fuera se puede editar libremente, y el nombre de
la tool es justo el campo que alguien querria cambiar.

### D-046 · El id de un evento se registra despues de manejarlo, no al recibirlo
**Contexto:** `SeenEvents` marcaba al recibir. Si el handler fallaba, la redelivery de
JetStream se leia como duplicado y se descartaba: **cualquier fallo perdia el evento**, y la
entrega at-least-once se volvia at-most-once en silencio.
**Decision:** registrar tras el exito del handler. Lo encontro el test de integracion
contra NATS real; el bus en memoria no podia verlo.

### D-047 · El DLQ vive fuera del arbol `peak.`
**Contexto:** `PEAK` cubria `peak.>` y `PEAK_DLQ` cubria `peak.dlq.>`, un subconjunto.
JetStream rechaza asuntos solapados, asi que `connect()` fallaba contra cualquier broker
limpio.
**Decision:** `peak-dlq.>`. Otro fallo que solo aparece contra el broker real.

### D-048 · El juez califica con la clasificacion acumulada de la tarea
**Razon:** el juez lee la respuesta, que arrastra el maximo acumulado. Enrutar su llamada
por ese valor es lo que impide que una respuesta C4 se mande a un modelo externo para
calificarla —un agujero en la invariante de soberania disfrazado de control de calidad.

### D-049 · Una fuga en la salida escala, no reintenta
**Razon:** la misma generacion volveria a filtrar. Un fallo de `safety` tampoco es un
problema de prompting; `groundedness` a menudo si, y por eso ese va a `replan`.

### D-050 · `QualityHook` devuelve un veredicto, no un diccionario de puntuaciones
**Contexto:** F1 dejo "valor negativo = falla", y `replan` no lo producia nadie pese a estar
en el tipo `Verdict`.
**Decision:** `QualityVerdict` con veredicto, puntuaciones y razones. Los umbrales viven con
el juez, donde el perfil los configura; la puerta solo aplica el presupuesto de reintentos y
el enrutado. Un `retry` conserva el plan; un `replan` lo descarta, porque conservarlo
reproduciria la respuesta rechazada.

### D-051 · Un veredicto reanuda desde el checkpoint, no re-ejecuta la tarea
**Razon:** el veredicto llega segundos o minutos despues, por un bus, posiblemente en otro
proceso. Re-ejecutar repetiria cada tool call que la tarea ya hizo. El mecanismo: escribir
el veredicto en el estado checkpointado como si lo hubiera producido la puerta de calidad, y
continuar. La arista condicional del grafo hace el resto, asi que no hay una segunda copia
del enrutado de reintentos.

### Cierre de F6

**Construido:** contrato de decisiones con razones obligatorias, `LocalPdp` + `OpaPdp` +
`CachingPdp` con fail-closed no configurable para C3/C4 y A2+, cinco paquetes Rego con
`default deny`, motor DLP bidireccional apoyado en el scrubber existente, `EventBus` con
NATS JetStream y bus en proceso, CloudEvents versionados, juez local con checks
deterministas y rubrica, agregador que reanuda desde checkpoint, y ledger hash-chain con
`verify-ledger` y export por tenant.

**Verificado:** los cuatro criterios de salida, cada uno sobre el grafo ensamblado —C4 no
sale, A2 espera, `retry` reanuda desde el checkpoint (tambien con el veredicto llegando por
el bus), y la cadena verifica y detecta tanto una edicion como un borrado.

**Tres bugs reales**, ninguno visible desde una prueba unitaria: un paquete Rego que
permitia un input vacio, dos streams de JetStream solapados que rompian `connect()`, y una
deduplicacion que anulaba los reintentos y perdia el evento para siempre.

578 tests unit + policy, 28 de integracion. Cobertura global 83.0 % sin Docker, 85.3 % con
la suite de integracion.

**Siguiente:** F7 · observabilidad y evals.

## 2026-09-01 · F7 · Observabilidad y evals

### D-052 · Los atributos de span son una allowlist, no una lista de prohibidos
**Razon:** las trazas salen del perimetro por diseño y un atributo de span es el camino mas
corto entre un documento clasificado y un colector que nadie clasifico. Con una lista de
prohibidos, un atributo nuevo entra por defecto; con una allowlist empieza rechazado hasta
que alguien lo revisa. El guardia esta en `span()` y no en cada punto de llamada porque hay
decenas y basta un `answer=...` distraido.

### D-053 · El SDK de OTel no puede registrar la excepcion
**Contexto:** `start_as_current_json` —`start_as_current_span`— trae `record_exception` y
`set_status_on_exception` en True, y ambos escriben el **mensaje** de la excepcion. Un
mensaje cita de rutina lo que lo provoco.
**Decision:** desactivar los dos y registrar solo el tipo. Lo encontro el test que afirma
que ningun span cita la entrada; el codigo ya intentaba hacerlo bien y el SDK lo
sobrescribia por detras.

### D-054 · Langfuse recibe texto solo hasta C1
**Razon:** es un producto alojado por defecto y su valor esta en enseñar prompts y
respuestas. De C2 en adelante recibe digests, contadores y scores. El filtro vive en
`langfuse_client.py`, no en cada punto de llamada, y una clasificacion ilegible cuenta como
C4: la duda se resuelve sin enviar nada. Un Langfuse autoalojado puede subir el limite con
`max_content_class`, y esa decision es de quien opera.

### D-055 · Cada nodo se instrumenta una vez, al registrarlo
**Razon:** envolver el nodo en `build_graph` significa que un nodo añadido mañana queda
trazado y medido por haberlo registrado, no por haberse acordado. Ocho funciones
instrumentadas a mano son ocho oportunidades de olvidar la novena.

### D-056 · Ninguna etiqueta de metrica la controla quien llama
**Razon:** una etiqueta de cardinalidad libre es una bomba de relojeria: la primera vez que
alguien pega un UUID en una, Prometheus empieza a comerse el host. Todas salen de
configuracion o de un catalogo fijo, y hay un test que recorre el registro y lo comprueba.

### D-057 · El gate lo llevan scorers propios; Ragas refina
**Contexto:** `ragas==0.4.3` no importaba (ver D-058) y su factory por defecto va a OpenAI.
**Decision:** scorers deterministas propios como gate —corren sin red, sin modelo y sin
extra— y Ragas como refinamiento opcional apuntado al proxy LiteLLM de la propia celula.
Las metricas de seguridad no se delegan nunca: la opinion de un evaluador externo sobre si
hubo fuga no es evidencia de lo que la celula hizo. Registrado como
[ADR-009](../adr/ADR-009-eval-scorers-and-the-ragas-pin.md).

### D-058 · `langchain-community` fijado a 0.3.31 en la extra `evals`
**Contexto:** Ragas importa `langchain_community.chat_models.vertexai`, que 0.4.x elimino,
y no declara cota superior. La extra entera fallaba con `ModuleNotFoundError`, en silencio,
porque nadie la importa hasta que corre el gate.
**Decision:** fijarla, con el motivo escrito junto al pin. Es una dependencia de una
dependencia y es incomodo; la alternativa es una extra que no importa.

### D-059 · El pase de control de acceso se juzga contra la politica, no contra la respuesta
**Razon:** una respuesta podria omitir un fragmento prohibido por casualidad. Lo que tiene
que ser cierto es que el PDP lo niegue, para ese solicitante, siempre. Un caso que nombra
un fragmento ausente del corpus cuenta como violacion: un caso roto no puede leerse como
aprobado.

### D-060 · Un umbral sin metrica detras es un incumplimiento
**Razon:** un gate que mide nada en silencio es peor que no tener gate. Si `thresholds.yaml`
declara una metrica que el harness no produce, la corrida falla y lo dice.

### D-061 · `QualityVerdict` vive en `core/state.py`
**Razon:** el juez que lo produce no tiene por que importar el grafo que lo consume. Esa
arista cerraba un ciclo `core.graph → observability → events → governance → core.graph` en
cuanto se instrumento el grafo. Las otras dos aristas malas —`langfuse_client` importando
`events.evidence` y `judge` importando `governance.dlp`— eran del mismo tipo: capas bajas
importando capas altas.

### Cierre de F7

**Construido:** trazas OTel con allowlist de atributos, quince metricas Prometheus con
`/metrics`, Langfuse con filtro por clasificacion, instrumentacion de nodos, gateway y tool
calls, cinco dashboards de Grafana provisionados, harness de evals con scorers propios y
Ragas opcional, datasets semilla, umbrales con gate, generador de datasets sinteticos y
config de Promptfoo.

**Verificado:** una traza completa request→respuesta, asertada como arbol de spans; y el
gate fallando cuando se endurece el umbral de groundedness, tanto en un test como en un
paso de CI que espera salida distinta de cero.

**Dos fugas y una dependencia rota**: el SDK de OTel escribiendo mensajes de excepcion en
los spans, Ragas que habria evaluado contra OpenAI, y la extra `evals` que no importaba.

630 tests unit + policy; cobertura global 82.7 %.

**Siguiente:** F8 · endurecimiento y empaque.
