# Memoria del agente

> Estado: contrato definido en F0; implementación en F2. Decisión en ADR-002.

Cuatro memorias con semánticas distintas. Todas namespaced por `tenant_id`, todas
sujetas a scrubbing de PII antes de persistir, todas alcanzadas por `forget`.

## 1. Corto plazo (STM)

`memory/short_term.py`. Buffer de sesión en Redis con TTL (`memory.stm_ttl_minutes`).
Cuando la ventana crece por encima del umbral, se genera un **resumen incremental** y
se descartan los turnos ya resumidos: la conversación no se trunca por el medio, se
condensa. El scratchpad del grafo (razonamiento intermedio, resultados de tools del
turno) vive aquí y **no** se promociona a largo plazo salvo destilación explícita.

Clave: `af:{tenant}:{instance}:stm:{thread_id}`.

## 2. Largo plazo (LTM)

`memory/long_term.py` define el `Protocol`; los adapters son intercambiables:

| Adapter | Backend | Cuándo |
|---|---|---|
| `RedisLtmAdapter` | Redis | Default en desarrollo; sin extras |
| `Mem0Adapter` | Mem0 sobre Qdrant | Producción, hechos y preferencias |
| `GraphitiAdapter` | Graphiti sobre Neo4j | Grafo temporal de entidades |

Alcance configurable (`memory.ltm.scope`): `user`, `area` o `tenant`. El alcance `area`
es el que materializa "el agente aprende de lo que interactúan los usuarios del área":
un hecho aprendido de un usuario queda disponible para el área, previo scrubbing.

## 3. Episódica de área

`memory/episodic.py`. Cada interacción relevante se destila en un episodio: qué se
preguntó, qué camino funcionó, qué herramientas se usaron, qué feedback dio el usuario.
Alimenta al subgrafo como contexto de few-shot recuperado por similitud. Un episodio
guarda **el patrón**, no el contenido: los identificadores concretos se sustituyen por
marcadores durante el scrubbing.

## 4. Procedimental

`configs/prompts/`. Few-shots y reglas aprendidas, versionados en el repositorio con
**promoción manual**. Nunca hay auto-deploy de prompts: un cambio de prompt es un PR con
su corrida de evals, igual que un cambio de código.

## 5. Caché semántica (CAG operativo)

`memory/semantic_cache.py`. Embedding de la pregunta contra las preguntas recientes del
mismo tenant y mismo techo de clasificación; por encima de `memory.semantic_cache.similarity`
se devuelve la respuesta cacheada. Dos condiciones no negociables: la entrada de caché
guarda el techo de clasificación y el conjunto de grupos con el que se generó, y sólo
sirve a peticiones con **el mismo o menor** alcance. Si no, la caché se convierte en un
canal de fuga entre usuarios.

## 6. PII y derecho al olvido

`memory/scrubbing.py` corre **antes** de cualquier escritura persistente, en las cuatro
capas. Motor propio siempre activo, Presidio como refuerzo si el extra `guardrails`
está instalado (ADR-006).

`forget`:

| Alcance | Qué borra |
|---|---|
| `user` | STM del usuario, hechos LTM con ese sujeto, episodios donde es actor, entradas de caché suyas |
| `tenant` | Todo lo anterior para el tenant completo, incluidos checkpoints y colección Qdrant |

Expuesto en `/admin/memory` (autenticado) y como comando. Cada ejecución entra en el
ledger. Retención por defecto: `memory.retention_days` (365).

## 7. Test de contrato

`tests/unit/test_memory_contract.py` corre la misma batería contra **todos** los
adapters registrados: aislamiento entre tenants, persistencia entre sesiones, scrubbing
antes de escribir, y borrado completo tras `forget`. Un adapter nuevo no se acepta sin
pasarla.
