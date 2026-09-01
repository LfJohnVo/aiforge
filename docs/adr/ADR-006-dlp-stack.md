---
status: accepted
date: 2026-09-01
deciders: arquitectura de plataforma, seguridad
---
# ADR-006 · DLP y prompt firewall: motor de reglas propio + Presidio opcional

## Contexto y planteamiento del problema

RF-09 exige DLP y *prompt firewall* en entrada y salida, nombrando **LLM Guard**
(inyección, jailbreak, secretos) y **Presidio** (PII ES/EN). Al fijar el lockfile
aparecieron dos hechos:

1. `llm-guard==0.3.16` fija `json-repair==0.44.1`; `lightrag-hku==1.5.6` (GraphRAG,
   ADR-004/RF-05) exige `>=0.59.9`. **No pueden coexistir.**
2. `llm-guard` fija también `presidio-anonymizer==2.2.358` y limita el intérprete a
   `<3.13`, arrastrando transformers y modelos de varios cientos de MB para escáneres
   que en su mayoría son deterministas.

El punto que decide es arquitectónico, no de empaquetado: el firewall es un control de
seguridad que debe funcionar **siempre**, incluido el modo degradado sin conectividad
(RNF-02) y en el arranque en frío. Un control de seguridad que depende de descargar un
modelo no es un control de seguridad, es una expectativa.

## Motores de la decisión

* El firewall debe estar activo con la instalación base, sin extras y sin red.
* Fail-closed real: si un escáner no puede ejecutarse, la decisión es *bloquear*, no
  *pasar*.
* No romper GraphRAG, que es requisito funcional de primera clase.
* Las reglas deben ser auditables y testeables offline, como las políticas Rego.

## Opciones consideradas

* `llm-guard` obligatorio, renunciando a `lightrag-hku` (usar `neo4j-graphrag`)
* `llm-guard` obligatorio, GraphRAG aislado en su propio proceso/imagen
* **Motor de reglas propio siempre activo + Presidio opcional + `llm-guard` como extra
  mutuamente excluyente**
* NeMo Guardrails

## Resultado de la decisión

Opción elegida: **motor de reglas propio en `governance/dlp.py` como control primario**,
con dos refuerzos opcionales:

* `guardrails` → **Presidio** para reconocimiento de PII ES/EN (mejora la cobertura de
  entidades: nombres, direcciones, CURP/RFC/NIF, IBAN).
* `promptguard` → **LLM Guard**, declarado en `[tool.uv].conflicts` como incompatible
  con `knowledge`, para despliegues que prioricen sus escáneres ML sobre GraphRAG.

El motor propio implementa, de forma determinista y sin red: detección de inyección de
prompt e intentos de anulación de instrucciones, exfiltración por marcado (URLs y
data-URIs en la salida), secretos (claves de API, tokens JWT, claves privadas, DSNs con
credenciales), y PII de patrón fijo (correo, teléfono, tarjeta con Luhn, IBAN, RFC/CURP,
NIF/NIE). Cada regla tiene identificador estable, severidad y una acción declarada
(`block` | `redact` | `flag`), y **cada disparo se escribe en el ledger de evidencia**.

`llm-guard` y Presidio, cuando están presentes, se ejecutan **después** del motor propio
y sólo pueden endurecer el veredicto, nunca relajarlo.

### Consecuencias

* Bueno: GraphRAG y DLP conviven; el conflicto queda documentado en el manifiesto.
* Bueno: el firewall funciona en modo degradado y en tests unitarios sin infraestructura.
* Bueno: las reglas viven en `configs/policies/dlp_rules.yaml`, versionadas y revisables
  por seguridad sin tocar código.
* Malo: menor cobertura frente a jailbreaks novedosos que un clasificador ML entrenado.
  Se mitiga con (a) el extra `promptguard` donde sea prioritario, (b) el `quality_gate`
  del grafo, que evalúa la salida con rúbricas de seguridad, y (c) el corpus de
  regresión de jailbreaks en `evals/datasets/` con gate en CI.
* Malo: mantener reglas propias es trabajo continuo. Aceptado y asignado al RUNBOOK
  (revisión trimestral del fichero de reglas).

## Validación

`tests/unit/test_dlp.py` cubre cada regla con un caso positivo y uno negativo.
`tests/unit/test_dlp_fail_closed.py` simula el fallo del escáner opcional y verifica que
el veredicto es `block`. El dataset `evals/datasets/security.jsonl` mide fuga de PII y
jailbreak con umbral bloqueante en CI.
