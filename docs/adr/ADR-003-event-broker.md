---
status: accepted
date: 2026-09-01
deciders: arquitectura de plataforma
---
# ADR-003 · Broker de eventos: NATS JetStream con abstracción `EventBus`

## Contexto y planteamiento del problema

El diagrama PEAK sitúa un **Broker / Event Fabric** como columna vertebral entre todas
las capas. La célula lo usa para tres flujos: publicar `task.result` al Agregador,
consumir `judge.verdict` (que puede reactivar el grafo desde un checkpoint), y emitir
`evidence.record` al Audit Ledger central. Restricción operativa dominante: la célula
tiene que arrancar completa en un `docker compose` sobre una laptop, pero el mismo
código debe hablar con el fabric corporativo (que muy probablemente sea Kafka) sin
reescribir la lógica del grafo.

## Motores de la decisión

* Huella mínima en Compose: el desarrollador no debe necesitar ZooKeeper/KRaft ni 4 GB
  de RAM para probar un cambio en un nodo del grafo.
* Streams persistentes, consumers durables y **DLQ**: un `judge.verdict` no se pierde
  porque la célula estuviera reiniciándose.
* Idempotencia por `event_id`: el mismo veredicto entregado dos veces no debe replanear
  dos veces.
* Swap a Kafka/Redpanda sin tocar `core/`.

## Opciones consideradas

* **NATS JetStream**
* **Redpanda** (compatible Kafka, binario único)
* **Apache Kafka**
* Redis Streams (reutilizar la dependencia que ya existe)

## Resultado de la decisión

Opción elegida: **NATS JetStream**, detrás de la abstracción `EventBus`
(`events/bus.py`). NATS da streams persistentes, consumers durables, DLQ y
deduplicación por `Nats-Msg-Id` en un contenedor de decenas de MB. La abstracción es la
mitad importante de la decisión: `EventBus` expone `publish(CloudEvent)` y
`subscribe(type, handler)` y nada más; el core no conoce sujetos, particiones ni offsets.

Todos los mensajes viajan como **CloudEvents 1.0** con esquemas versionados en
`events/schemas/` (`com.peak.task.result.v1`, `com.peak.judge.verdict.v1`,
`com.peak.evidence.record.v1`), de modo que el versionado del contrato es independiente
del broker.

### Consecuencias

* Bueno: `make up PROFILE=events` añade ~40 MB de RAM, no un cluster.
* Bueno: la deduplicación por `event_id` es nativa del broker (ventana configurable), no
  código nuestro.
* Bueno: `InMemoryEventBus` implementa el mismo `Protocol`, así que los tests unitarios
  del ciclo Agregador/Judge corren sin infraestructura.
* Malo: el ecosistema de conectores gestionados de Kafka es mayor. Irrelevante aquí: la
  célula es *productora y consumidora*, no un pipeline de datos.
* Neutro: si la plataforma impone Kafka, se implementa `KafkaEventBus` y se cambia una
  línea de configuración del perfil.

## Validación

`tests/unit/test_event_bus_contract.py` corre la misma batería contra `InMemoryEventBus`
y `NatsEventBus` (este último marcado `integration`): orden, durabilidad,
idempotencia por `event_id` y enrutado a DLQ tras N fallos del handler.

## Pros y contras de las opciones

### NATS JetStream
* Bueno: huella mínima, DLQ y dedupe nativos, operación trivial.
* Malo: menos ubicuo que Kafka en arquitecturas corporativas heredadas.

### Redpanda
* Bueno: API Kafka sin ZooKeeper; buen término medio.
* Malo: consumo de recursos notablemente superior en Compose; sin dedupe por id nativo.

### Kafka
* Bueno: estándar de facto corporativo.
* Malo: incompatible con el objetivo "laptop"; operación pesada para una célula.

### Redis Streams
* Bueno: cero infraestructura nueva (Redis ya es dependencia).
* Malo: sin DLQ real ni semántica de entrega comparable; mezclaría el plano de estado
  con el plano de eventos, que es exactamente lo que el diagrama separa.
