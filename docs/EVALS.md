# Harness de evaluación

> Estado: contrato y umbrales definidos en F0; implementación en F7.

La calidad y la seguridad **bloquean el merge**, no se observan y se comentan.

## 1. Capas

| Capa | Herramienta | Qué mide |
|---|---|---|
| Regresión de prompts | **Promptfoo** | Que un cambio de prompt no rompa respuestas conocidas |
| Calidad RAG | **Ragas** | Groundedness, context precision, context recall, answer relevancy |
| Seguridad | **DeepEval** + suites propias | Jailbreak, fuga de PII, obediencia a instrucciones incrustadas |
| Invariantes | **pytest** | Los que no admiten umbral: C3/C4 nunca sale, ACL nunca se salta |

La cuarta capa es distinta de las tres primeras: no tiene umbral porque no admite
fallos parciales. Es un test, no una métrica.

## 2. Datasets

`evals/datasets/`:

| Fichero | Contenido | Mínimo |
|---|---|---|
| `generalist.jsonl` | Preguntas de dominio genérico con respuesta esperada y fuentes | 25 casos |
| `it_support.jsonl` | Dominio de ejemplo especializado | 15 casos |
| `security.jsonl` | Jailbreaks, inyección desde documento, intentos de exfiltración | 20 casos |
| `access_control.jsonl` | Pares (usuario, pregunta) con visibilidad esperada | 12 casos |

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

El propio gate se verifica: `evals.yml` incluye un paso manual documentado en el que se
baja el umbral de groundedness por encima del valor observado, se comprueba que el
pipeline falla, y se revierte. Un gate que nunca ha fallado no se sabe si funciona.
