# Memoria del agente

Cuatro memorias con semánticas distintas. Todas namespaced por `tenant_id`, todas sujetas
a scrubbing de PII antes de persistir, todas alcanzadas por `forget`. Decisión en
[ADR-002](adr/ADR-002-memory-architecture.md).

---

## 1. Almacenamiento

Todo pasa por el `Protocol` `KeyValueStore` (`memory/store.py`) con dos implementaciones:
`RedisStore` (producción) e `InMemoryStore` (desarrollo y tests, con TTL real). Ambas
pasan la misma batería, la unitaria contra la de memoria y `tests/integration/
test_memory_redis.py` contra Redis real.

**La clave lleva el tenant dentro**, no es un filtro posterior:

```
af:{tenant_id}:{instance}:{capa}:{...}
```

Sin `tenant_id` la construcción de la clave lanza excepción. Un acceso cruzado entre
tenants no es "algo que no hacemos": es algo que no se puede expresar.

Sin `REDIS_URL` la célula arranca con el almacén en memoria y **lo advierte en el log**:
la memoria no sobrevive a un reinicio ni se comparte entre réplicas.

---

## 2. Corto plazo (STM)

`memory/short_term.py`. Buffer de sesión con TTL (`memory.stm_ttl_minutes`).

**Condensa, no trunca.** Al superar la ventana (20 turnos por defecto), los más antiguos
se pliegan en un resumen incremental y se descartan. Cortar la conversación por la mitad
pierde exactamente el contexto que el usuario da por supuesto que el agente conserva.

El resumen lo genera el modelo rápido; si el gateway no responde, cae a un digest
determinista. Perder el modelo degrada el resumen, nunca pierde la conversación.

El scratchpad del grafo (razonamiento intermedio, resultados de tools del turno) vive
aquí y caduca con la sesión. Promocionarlo a largo plazo es un acto deliberado y separado.

---

## 3. Largo plazo (LTM)

`memory/long_term.py` define el `Protocol`; los adapters son intercambiables:

| Adapter | Backend | Cuándo |
|---|---|---|
| `KeyValueLongTermMemory` | El propio `KeyValueStore` | **Default.** Recuperación léxica; sin extras |
| `Mem0LongTermMemory` | Mem0 sobre Qdrant | Producción: consolidación y contradicción de hechos |

El adapter por defecto **no es un stub**: cumple el contrato completo y es el que
ejercitan los tests, lo que hace que las pruebas de aislamiento y de borrado corran en
cualquier entorno. Su límite es honesto: recupera por solapamiento de términos, no por
semántica, y acota cada bucket a 500 hechos.

### Alcance (`memory.ltm.scope`)

| Alcance | Buckets que puede leer |
|---|---|
| `user` | Sólo el del propio usuario |
| `area` | El del área **y** el del usuario |
| `tenant` | Los tres |

El alcance `area` es lo que materializa "el agente aprende de lo que interactúan los
usuarios del área". Un alcance `user` **no** lee el bucket compartido: un hecho aprendido
de un colega es memoria del área, no de esta persona.

### Preferencias

Un hecho de tipo `preference` ("responde breve") se recupera **siempre**, no por
coincidencia con la pregunta. Es relevante en todos los turnos y no comparte vocabulario
con ninguno, así que una recuperación dirigida por consulta jamás lo devolvería.

---

## 4. Episódica de área

`memory/episodic.py`. Destila interacciones terminadas en patrones reutilizables como
few-shot.

Dos reglas que impiden que se convierta en una fuga:

* **Sólo tareas completadas.** Registrar una respuesta rechazada o escalada enseña al
  agente a repetirla.
* **Guarda el patrón, no el caso.** El texto se scrubbea y los identificadores concretos
  (`INC-2024-0093`, UUIDs, números largos) se sustituyen por `<id>`. Lo que merece
  recordarse es "las preguntas de facturas se responden desde la política", no el número
  de factura.

La recuperación respeta el techo de clasificación del solicitante: qué herramienta
funcionó para cierto tipo de pregunta también puede ser sensible.

---

## 5. Caché semántica

`memory/semantic_cache.py`. La mitad operativa de CAG.

### Lo que no es una optimización

Cada entrada guarda **la clasificación y el conjunto de grupos con los que se produjo**, y
sólo sirve a una petición de alcance igual o menor:

```
entrada.clasificacion <= techo_del_solicitante   Y   entrada.grupos ⊆ grupos_del_solicitante
```

La segunda condición es la sutil: una respuesta sintetizada para alguien de
`finanzas-lideres` puede contener material que un miembro de `finanzas` no debe ver,
aunque ambos tengan el mismo nivel de clasificación. Sin esa comprobación, la caché deja
de ser una optimización y pasa a ser un canal que entrega a un usuario la respuesta de
otro.

### PII: no se cachea, no se redacta

Si la pregunta **o** la respuesta contienen PII, la entrada **no se guarda**. Las dos
alternativas son peores:

* redactar la respuesta serviría una réplica degradada, distinta de lo que devolvería una
  llamada fresca;
* usar la pregunta redactada como clave haría que la pregunta de otra persona con otro
  correo coincidiera con esta entrada.

Además, una respuesta con PII es específica de una persona y por tanto mal candidato a
caché. Cada omisión se registra (`cache.skipped_pii`).

### Similitud

Con embeddings disponibles: coseno, y las paráfrasis aciertan. Sin ellos: solapamiento
léxico normalizado (sin acentos, sin mayúsculas), que con el umbral por defecto de 0.92
acierta las reformulaciones casi idénticas —la mayor parte del tráfico real de un área— y
falla las paráfrasis. Los embeddings se calculan **siempre en backend soberano**: el
vector parece ruido pero el texto que lo produjo no lo era.

---

## 6. Scrubbing de PII

`memory/scrubbing.py`, activo antes de cualquier escritura persistente en las cuatro
capas. Motor determinista siempre activo, con checksum donde el formato lo tiene (Luhn
para tarjetas, mod-97 para IBAN, letra de control para NIF/NIE). Presidio (extra
`guardrails`) se ejecuta **después** y sólo puede añadir hallazgos.

Detecta: `EMAIL`, `CREDIT_CARD`, `IBAN`, `CURP`, `RFC`, `NIF`, `SSN`, `PHONE`, `IP`.

Los validadores existen para evitar falsos positivos, que no son inocuos: scrubbear un
número de pedido de 16 dígitos destruye la respuesta. Un pedido sin checksum de Luhn
válido, una fecha o un `INC-2024-0093` se dejan intactos.

`allow_kinds` permite conservar tipos que un área necesita legítimamente (un SOC guarda
direcciones IP).

---

## 7. Derecho al olvido

`MemoryManager.forget()` es el único punto que barre **las cuatro capas**. Existe
precisamente por eso: un borrado que limpia tres de cuatro almacenes es un fallo de
cumplimiento, y la única forma de que siga siendo cierto al añadir capas es que haya un
solo sitio responsable del barrido.

| Alcance | Qué borra |
|---|---|
| `user` | Hechos con ese sujeto en todos los buckets visibles, sus sesiones, e invalida la caché del área (una respuesta cacheada pudo sintetizarse a partir de lo que se borra) |
| `tenant` | Todo: STM, LTM, episodios y caché del tenant completo |

Expuesto en `POST /admin/memory/forget`. **Borrar un tenant completo exige pertenecer al
grupo de aprobadores**, no basta con estar autenticado: es una acción irreversible de
alcance total. Cada ejecución devuelve un informe por capa y queda registrada.

`GET /admin/memory` reporta salud, hit rate de caché y política de retención.

---

## 8. Tests de contrato

`tests/unit/test_memory.py` corre la misma batería contra **todos** los adapters LTM
registrados: recuperación, idempotencia, aislamiento entre tenants, scrubbing antes de
escribir, borrado por usuario y borrado por tenant. Un adapter nuevo no se acepta sin
pasarla.

`tests/unit/test_memory_in_graph.py` cubre el comportamiento visible: el agente recuerda
entre sesiones, la caché evita la llamada al modelo, y nada de lo que el grafo persiste
contiene PII en claro.
