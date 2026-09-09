# Preguntas abiertas

Cosas sin resolver que **no** bloquean el avance. Cada una lleva la decisión provisional
que se está aplicando mientras tanto y qué la cerraría.

| # | Pregunta | Decisión provisional | Qué la cerraría | Abierta desde |
|---|---|---|---|---|
| Q-01 | ¿Qué familia de modelos abiertos concreta para los dos tamaños (7–14B y ~70B)? | Alias `local/fast` y `local/quality` en `litellm.yaml`; el perfil nunca nombra un modelo real. Propuesta del 2026-09-07: Qwen3-8B-FP8 (≥16 GB VRAM) y Qwen3-32B-FP8 (≥48 GB), a medir en la PoC | ADR-010 con los números de `PRODUCTION_PLAN.md` A1/A7 (tarea B4) | 2026-09-01 |
| Q-02 | ¿LightRAG o `neo4j-graphrag` para GraphRAG? | LightRAG (incremental, ligero); el consumo va tras `Protocol` | Prueba sobre corpus real en F3, midiendo coste de indexación incremental | 2026-09-01 |
| Q-03 | ¿El PDP corporativo expone API compatible con OPA o la suya propia? | Cliente contra API OPA; se añadirá un adapter si difiere | Especificación del AI Governance System de la plataforma | 2026-09-01 |
| Q-04 | ¿Qué directorio mapea usuarios de Teams/Slack a grupos del tenant? | Mapeo declarativo en el perfil | Definición de la fuente de identidad corporativa | 2026-09-01 |
| Q-05 | ¿Retención del ledger local antes de rotar al Audit Ledger central? | Sin rotación; el fichero crece y se exporta por tenant | Política de retención de auditoría del cliente | 2026-09-01 |
| Q-06 | ¿Umbral de similitud de caché semántica realista? | 0.92 por defecto (valor del perfil de ejemplo) | Medición de falsos positivos sobre tráfico real en F7 | 2026-09-01 |
| Q-07 | ¿Dónde corre producción v1: AWS, Azure u on-prem? | El plan está escrito para AWS/EKS por ser la huella existente; las filas valen para cualquier destino | Decisión 1 de `PRODUCTION_PLAN.md` §7 | 2026-09-07 |
| Q-08 | ¿Un endpoint privado de Bedrock/Azure OpenAI cuenta como modelo «externo» (tope C2)? | Sí: `gateway/model_policy.py` sólo trata como local lo que corre en la propia infraestructura | Dictamen de Legal/Seguridad y ADR | 2026-09-07 |
| Q-09 | ¿Debe la caché semántica guardar una respuesta que no encontró nada? | Hoy sí, y con TTL de 72 h: un fallo transitorio de recuperación se sirve tres días. Medido el 2026-09-09 | Decidir si «sin resultados» se cachea, con qué TTL, o no se cachea | 2026-09-09 |
| Q-10 | Con el PDP caído, ¿C0/C1 debe seguir respondiendo? | El RUNBOOK §3.1 dice que sí; el código deniega todo. Es el lado seguro del error, pero uno de los dos miente | Revisar `governance/decisions.py` y alinear código y RUNBOOK | 2026-09-09 |
| Q-11 | ¿La inyección por documento funciona contra la célula? | **No se sabe.** El scorer compara subcadenas y no distingue obedecer de citar; además la corrida del 2026-09-09 tenía el índice contaminado con dos corpus | Un scorer que separe las dos cosas (¿obedeció la instrucción?, no ¿aparece la cadena?) y una corrida contra una colección limpia | 2026-09-09 |
