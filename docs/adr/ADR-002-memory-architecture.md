---
status: accepted
date: 2026-09-01
deciders: arquitectura de plataforma
---
# ADR-002 · Arquitectura de memoria: Redis (STM) + Mem0/Qdrant (LTM) + Graphiti/Neo4j (temporal)

## Contexto y planteamiento del problema

RF-04 pide que el agente "aprenda de lo que interactúan los usuarios del área". El
requerimiento original decía «LTSM»; en el contexto de agentes esto es **memoria de
corto y largo plazo**, no redes LSTM (los modelos secuenciales quedan en RF-14,
`analytics/`, desactivado por defecto). Hay que cubrir cuatro tipos de memoria con
semánticas distintas: sesión (volátil), hechos y preferencias (perdurable, por usuario
y por área), evolución de entidades en el tiempo (qué era verdad en marzo), y
procedimientos aprendidos (few-shots, reglas). Todo con aislamiento estricto por
`tenant_id` y con derecho al olvido.

## Motores de la decisión

* Aislamiento multi-tenant demostrable con un test, no por convención de nombres.
* Derecho al olvido: `forget user|tenant` debe borrar en **todas** las capas.
* Ninguna capa puede persistir PII sin pasar por el scrubber.
* La memoria temporal (qué cambió y cuándo) es requisito de auditoría, no un lujo:
  "el proveedor X era crítico hasta abril" es una respuesta distinta a "es crítico".

## Opciones consideradas

* **Mem0 (OSS) + Graphiti**
* **LangMem** (memoria integrada al ecosistema LangGraph)
* **Letta / MemGPT**
* Implementación propia sobre Qdrant + tablas de Postgres

## Resultado de la decisión

Opción elegida: **Mem0 sobre Qdrant para hechos y preferencias, Graphiti sobre Neo4j
para el grafo temporal**, con Redis para STM y caché semántica. Mem0 aporta la
extracción y consolidación de hechos (deduplicación, actualización, contradicción), que
es la parte laboriosa. Graphiti aporta bitemporalidad, que ninguna otra opción da.

**Restricción de diseño que anula el riesgo de lock-in:** el core **no importa Mem0 ni
Graphiti**. Consume los `Protocol` definidos en `memory/long_term.py`; ambos son
*adapters* registrados. El adapter por defecto en desarrollo es una implementación
propia sobre Redis que satisface el mismo contrato, para que el repositorio arranque sin
las dependencias pesadas.

### Consecuencias

* Bueno: sustituir Mem0 por LangMem es escribir un adapter, no tocar el grafo.
* Bueno: `forget()` es una operación del `Protocol`; un test parametrizado la exige a
  todos los adapters registrados.
* Malo: dos almacenes que mantener (Qdrant + Neo4j). Aceptado: ambos ya son requisitos
  de RF-05 (vectores y GraphRAG), así que la memoria no añade infraestructura nueva.
* Malo: Mem0 y Graphiti son dependencias pesadas, por eso van en el extra opcional
  `memory` (ver ADR-005).

## Validación

`tests/unit/test_memory_contract.py` ejecuta la misma batería contra **todos** los
adapters registrados: aislamiento entre dos tenants, persistencia entre sesiones,
scrubbing antes de escribir, y borrado total tras `forget`.

## Pros y contras de las opciones

### Mem0 + Graphiti
* Bueno: consolidación de hechos madura más bitemporalidad real.
* Malo: peso de dependencias; dos sistemas que operar.

### LangMem
* Bueno: menos piezas, mismo ecosistema que LangGraph.
* Malo: sin grafo temporal; la pregunta "qué era verdad entonces" queda sin respuesta.

### Letta / MemGPT
* Bueno: gestión de memoria muy elaborada.
* Malo: asume ser el runtime del agente; choca frontalmente con ADR-001.
