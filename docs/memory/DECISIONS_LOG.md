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

