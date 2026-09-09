---
status: accepted
date: 2026-09-09
deciders: arquitectura de plataforma, operaciones
---
# ADR-010 · Qué modelos locales sirven `local/fast` y `local/quality`, y con qué hardware

## Contexto y planteamiento del problema

Desde F0, `configs/litellm.yaml` nombra **alias** (`local/fast`, `local/quality`,
`local/embeddings`) y nunca modelos concretos. Fue deliberado —el perfil de una instancia
no debe nombrar un modelo real, porque entonces cambiarlo es un cambio de producto— y dejó
abierta la Q-01 desde el primer día: qué familia concreta, en qué tamaños, sobre qué
tarjeta.

La pregunta no se podía cerrar antes porque las tres cosas que la deciden —VRAM disponible,
latencia aceptable y licencia— sólo se saben midiendo. La noche del 2026-09-09 se midieron
las dos primeras.

## Lo que se midió

Máquina: RTX 5060 Ti 16 GB (Blackwell, sm_120), Ryzen 9 5900XT, Docker Desktop sobre WSL2.

**vLLM no arranca bajo WSL2.** `vllm/vllm-openai:v0.27.1-cu129` falla con
`RuntimeError: UVA is not available`. El motor V1 —el único desde 0.27, `VLLM_USE_V1=0` ya
no hace nada— reserva sus buffers con Unified Virtual Addressing, y el passthrough de GPU
de WSL2 no lo expone. No hay bandera que lo evite. En Linux nativo con la misma tarjeta sí
arranca, así que **es una restricción de la máquina de desarrollo en Windows, no del
producto**.

Con Ollama sobre la misma GPU, sirviendo `qwen3:8b` para chat y `bge-m3` para embeddings a
la vez:

| Peticiones simultáneas | p50 | p95 | máx | req/s | fallos |
|---|---|---|---|---|---|
| 1 | 19.1 s | 19.1 s | 19.1 s | 0.05 | 0 |
| 2 | 18.2 s | 20.7 s | 20.7 s | 0.10 | 0 |
| 4 | 31.2 s | 32.8 s | 36.0 s | 0.11 | 0 |
| 8 | 66.6 s | 79.7 s | 83.5 s | 0.10 | 0 |

Respuesta completa: recuperación híbrida, rerank, síntesis y citas. VRAM ocupada: 6.8 GB.
Cero fallos en todos los niveles.

**El dato que decide es el caudal, no la latencia.** Se estanca en ~0.10 req/s a partir de
dos peticiones simultáneas y a partir de ahí la latencia crece linealmente: la tarjeta está
saturada y lo que llega se encola. Una GPU sirve **una petición útil cada diez segundos**,
y eso no depende de cuántos usuarios haya conectados.

## Decisión

**`local/fast` es Qwen3-8B**, servido por vLLM en producción (Linux) y por Ollama en
desarrollo sobre Windows. En una tarjeta de 16 GB, en FP8; con 24 GB o más, en BF16.

**`local/quality` es Qwen3-32B en FP8**, y **no se despliega en el piloto**. Necesita
~35 GB de VRAM, que es una tarjeta distinta (L40S de 48 GB), y ninguna medición justifica
todavía ese coste: no hay una sola pregunta del corpus de evals que Qwen3-8B falle y un
32B resuelva. Hasta que la haya, el perfil apunta los dos alias al mismo modelo, que es una
configuración de una línea y no un cambio de código.

**Embeddings: bge-m3**, 1024 dimensiones, y esa cifra es un contrato: `DEFAULT_DIM` en
`rag/embeddings.py` la fija, y cambiar de modelo de embeddings a otra dimensionalidad
obliga a recrear la colección de Qdrant y a reindexar el corpus entero.

**Rerank: bge-reranker-v2-m3**, opcional, vía `RERANK_MODEL`. Vacío deja el reranker léxico
integrado, que es un default legítimo y no una carencia: no necesita un segundo modelo.

Las cuatro son Apache-2.0.

## Dimensionado que se sigue de la medición

| Escenario | GPU | Por qué |
|---|---|---|
| Desarrollo | cualquiera de 16 GB, o CPU con `local/tiny` | Una petición a la vez |
| Piloto (10–20 usuarios) | 1 × L4 24 GB (`g6.xlarge`) | A 0.1 req/s sostenidos, veinte personas preguntando cada pocos minutos caben con holgura |
| Producción por área | 2 × L4, detrás del router de LiteLLM | La segunda no es por caudal: es para que el mantenimiento de un nodo no sea una caída |
| `local/quality` | 1 × L40S 48 GB, sólo cuando una eval lo justifique | Ver arriba |

El caudal medido aquí es un **suelo**, no una estimación: es una tarjeta de consumo en WSL2
sirviendo también los embeddings, con prefix caching desactivado porque Ollama no lo tiene.
vLLM sobre L4, con la caché de prefijos y sin compartir la tarjeta con el embedder, debería
dar bastante más. Antes de comprar la tercera GPU, medir en el destino: **el número que
importa es el de la máquina que va a servir**, y esta no lo es.

## Consecuencias

* **Q-01 se cierra.** El perfil sigue sin nombrar modelos: sólo cambian los alias en
  `litellm.yaml`.
* **`local/quality` queda apuntando a `local/fast` en el perfil de la PoC**, y eso es
  visible en `configs/agent.profile.poc.yaml` con su motivo al lado.
* **El SLO de latencia (p95 < 30 s, `OBSERVABILITY.md` §7) se cumple hasta 4 peticiones
  simultáneas en esta máquina y se incumple en 8.** No es un problema del SLO: es la
  frontera de una sola GPU, y es exactamente lo que el SLO debe detectar.
* **`gpu.override.yml` deja vLLM en su propio perfil `vllm`.** No se borra —en Linux es lo
  correcto— pero no arranca por defecto, porque un servicio que crashea en bucle dentro del
  perfil normal hace que un stack sano parezca roto.

## Alternativas descartadas

**Llama 3.3 70B.** Mejor en razonamiento largo, pero 40 GB en FP8 y una licencia con
restricciones de uso que hay que revisar caso por caso. Qwen3 es Apache-2.0 y no obliga a
esa conversación.

**Un modelo por área.** Multiplicaría el coste de GPU por el número de células, que es
justo lo contrario de lo que compra compartir la infraestructura. El ajuste por área vive
en el perfil y en el prompt, no en los pesos.

**Servir los embeddings desde el mismo vLLM que el chat.** Un vLLM sirve un modelo. Serían
dos procesos compitiendo por la misma tarjeta, y la medición de arriba ya muestra qué pasa
cuando dos cosas comparten una GPU.
