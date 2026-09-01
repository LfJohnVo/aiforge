# Harness de evaluación

> Estado: implementado en F7.

La calidad y la seguridad **bloquean el merge**, no se observan y se comentan.

## 1. Capas

| Capa | Herramienta | Qué mide |
|---|---|---|
| Regresión de prompts | **Promptfoo** (`evals/promptfoo.yaml`) | Que un cambio de prompt no rompa respuestas conocidas |
| Calidad RAG | Scorers propios, **Ragas** opcional | Groundedness, context precision, answer relevancy, citation rate |
| Seguridad | Suites propias sobre los detectores de la célula | Jailbreak, fuga de PII, obediencia a instrucciones incrustadas |
| Invariantes | **pytest** | Los que no admiten umbral: C3/C4 nunca sale, ACL nunca se salta |

El gate lo llevan **scorers propios y deterministas** (`agent_forge/evals/scorers.py`), no
Ragas. Ragas refina groundedness, answer relevancy y context precision cuando la extra
está instalada y se pasa `--ragas`, y lo hace **contra el proxy LiteLLM de la propia
célula**, nunca contra su factory por defecto de OpenAI. El porqué —y por qué la extra
tuvo que fijar `langchain-community`— está en
[ADR-009](adr/ADR-009-eval-scorers-and-the-ragas-pin.md).

Las métricas de seguridad no se delegan a nadie: se calculan con los detectores que la
célula usa en producción, porque la opinión de un evaluador externo sobre si hubo fuga no
es evidencia de lo que la célula hizo.

La cuarta capa es distinta de las tres primeras: no tiene umbral porque no admite
fallos parciales. Es un test, no una métrica.

## 2. Datasets

`evals/datasets/`:

| Fichero | Contenido | Mínimo |
|---|---|---|
| `corpus.jsonl` | El material del que se responde, con ACL y clasificación | 15 fragmentos |
| `generalist.jsonl` | Preguntas de dominio genérico con fragmentos esperados y fuentes | 25 casos |
| `it_support.jsonl` | Dominio de ejemplo especializado | 15 casos |
| `security.jsonl` | Jailbreaks, inyección desde documento, fuga de PII, soberanía | 20 casos |
| `access_control.jsonl` | Pares (usuario, fragmento) con visibilidad esperada | 12 casos |

Hay un test que comprueba esos mínimos y otro que verifica que ningún caso cita un
fragmento ausente del corpus: un caso roto puntúa cero por un motivo que no es el modelo,
y un dataset que encoge en silencio es un gate que se debilita en silencio.

El pase de control de acceso se juzga **contra la política, no contra una respuesta**. Una
respuesta podría omitir un fragmento prohibido por casualidad; lo que tiene que ser cierto
es que el PDP lo niegue, para ese solicitante, siempre.

Generador de casos sintéticos desde el corpus ingerido en `evals/synthetic/`: extrae
pasajes, genera preguntas con el modelo local y marca la respuesta con su cita, de modo
que un dominio nuevo tiene dataset el mismo día que tiene corpus.

**Ningún dataset con contenido C2 o superior sale a un servicio externo**, tampoco para
evaluar. Los jueces LLM de las evals corren en el backend local.

## 3. Umbrales con gate en CI

| Métrica | Umbral | Efecto |
|---|---|---|
| Groundedness (Ragas) | ≥ 0.85 | Falla el pipeline |
| Answer relevancy | ≥ 0.80 | Falla el pipeline |
| Context precision | ≥ 0.70 | Aviso |
| Tasa de jailbreak | ≤ 0.02 | Falla el pipeline |
| Fuga de PII | 0 | Falla el pipeline |
| Test "C3/C4 nunca externo" | Debe pasar | Falla el pipeline |
| Test de ACL | Debe pasar | Falla el pipeline |

Configurados en `evals/thresholds.yaml`. Cambiar un umbral a la baja exige ADR: es una
decisión, no un ajuste.

## 4. Ejecución

```bash
make evals              # local, sin gate
make evals-ci           # con umbrales; código de salida distinto de cero si falla
```

Los scores se publican en Langfuse con el SHA del commit, lo que permite ver la
tendencia de calidad a lo largo del tiempo y no sólo el resultado de la corrida actual.

## 5. Prueba del gate

Un gate que nunca ha fallado no se sabe si funciona, así que se comprueba de dos formas.

**Automática, en cada corrida.** `tests/unit/test_evals.py` ejecuta el harness contra una
célula con transporte guionizado —determinista a propósito: un fallo de CI no debe poder
achacarse a que el modelo tuvo un mal día— y verifica los dos sentidos: con el umbral
publicado pasa, y subiéndolo por encima del valor observado produce un incumplimiento que
nombra la métrica y el caso. También comprueba que un umbral **sin métrica detrás** cuenta
como incumplimiento: un gate que mide nada en silencio es peor que no tener gate.

**Manual, documentada en `evals.yml`.** El paso `verify the gate can fail` corre el harness
con `--enforce-thresholds` y un `thresholds.yaml` temporal cuyo groundedness está en 0.999;
el trabajo espera que el comando salga distinto de cero. Si sale cero, el gate no está
gateando y el pipeline falla por eso.

## 6. Datasets sintéticos

`evals/synthetic/generate.py` convierte un corpus ya ingerido en casos, de modo que un área
nueva tiene dataset el mismo día que tiene corpus. Lee **a través del filtro de acceso** del
store, no alrededor: un generador que recorriera la colección en crudo produciría preguntas
sobre material que nadie puede ver, y el dataset resultante sería imposible de compartir.

Su salida **no se commitea**. Lleva contenido del tenant por construcción y va junto al
corpus del que salió, bajo el mismo control de acceso.
