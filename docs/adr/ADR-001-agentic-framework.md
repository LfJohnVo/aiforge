---
status: accepted
date: 2026-09-01
deciders: arquitectura de plataforma
---
# ADR-001 · Framework agéntico: LangGraph

## Contexto y planteamiento del problema

El diagrama PEAK v2.1 define un **Agentic Graph Runtime** con responsabilidades
explícitas: orquestación, estado, *checkpoints*, *retries* y **HITL**. La célula debe
implementar ese runtime localmente y, además, exponer un **subgrafo de dominio
intercambiable** por área (SOC, Ciber, Microsoft, Automatización, PM...). El requisito
duro es que una tarea interrumpida a mitad de ejecución (caída del proceso, aprobación
humana pendiente durante horas) se reanude **exactamente** donde estaba, y que ese punto
de reanudación sea el mismo que usa el veredicto `retry`/`replan` del Judge central.

## Motores de la decisión

* Reanudación exacta desde checkpoint persistente, no "reintentar la tarea entera".
* Pausa indefinida por aprobación humana como primitiva del framework, no como máquina
  de estados ad-hoc en la aplicación.
* Composición de subgrafos como unidad de plugin: añadir un área no toca el core.
* Correspondencia 1:1 con el vocabulario del diagrama (nodo, estado, checkpoint) para
  que documentación y código digan lo mismo.

## Opciones consideradas

* **LangGraph** — grafo explícito, checkpointers pluggables, `interrupt()`
* **Pydantic AI** — agentes tipados, composición por código
* Orquestación propia sobre `asyncio` + máquina de estados en Postgres

## Resultado de la decisión

Opción elegida: **LangGraph**, porque es el único candidato que aporta las tres
primitivas críticas (checkpointer persistente pluggable, `interrupt()` con reanudación
por `Command(resume=...)`, y subgrafos componibles) sin que tengamos que construirlas.
Las otras dos obligan a reimplementar la persistencia de estado, que es precisamente
la parte donde un error se paga con tareas perdidas o duplicadas.

### Consecuencias

* Bueno: `AgentState` tipado con Pydantic se serializa al checkpointer sin capa extra.
* Bueno: el HITL A2/A3 es `interrupt()` + `Command(resume=...)`; el endpoint
  `/admin/approvals` sólo transporta la decisión.
* Bueno: `retry`/`replan` del Judge se implementan reanudando desde el checkpoint del
  nodo correspondiente, no re-ejecutando la tarea completa.
* Malo: acopla la evolución del core a la API de LangGraph. Se mitiga confinando el uso
  a `core/graph.py` y `core/state.py`; los subgrafos ven una fachada propia
  (`DomainSubgraph`) y no importan LangGraph directamente.
* Neutro: el checkpointer de Postgres exige una migración de esquema en el arranque.

## Validación

`tests/integration/test_checkpoint_resume.py` interrumpe el runtime a mitad de grafo y
comprueba que la reanudación continúa en el nodo siguiente con el mismo `thread_id`.
`tests/unit/test_hitl.py` comprueba que una acción A2 queda `awaiting_approval` y que
sólo un aprobador del grupo declarado en el perfil puede liberarla.

## Pros y contras de las opciones

### LangGraph
* Bueno: checkpointers Postgres/Redis oficiales; `interrupt()` nativo; subgrafos.
* Bueno: el streaming de eventos alimenta directamente el SSE del canal OpenAI-compatible.
* Malo: superficie de API amplia y en evolución rápida.

### Pydantic AI
* Bueno: ergonomía de tipos superior, dependencias mínimas.
* Malo: sin checkpointer persistente ni pausa/reanudación de primera clase; habría que
  construir el runtime que el diagrama ya exige.

### Orquestación propia
* Bueno: cero acoplamiento externo.
* Malo: reimplementar persistencia de estado, reanudación y HITL es trabajo de meses y
  el lugar más caro donde tener bugs.
