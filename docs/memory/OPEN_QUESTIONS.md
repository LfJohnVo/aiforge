# Preguntas abiertas

Cosas sin resolver que **no** bloquean el avance. Cada una lleva la decisión provisional
que se está aplicando mientras tanto y qué la cerraría.

| # | Pregunta | Decisión provisional | Qué la cerraría | Abierta desde |
|---|---|---|---|---|
| Q-01 | ¿Qué familia de modelos abiertos concreta para los dos tamaños (7–14B y ~70B)? | Alias `local/fast` y `local/quality` en `litellm.yaml`; el perfil nunca nombra un modelo real | Medición de VRAM disponible en el hardware de destino y revisión de licencia; se cerrará con ADR en F7 | 2026-09-01 |
| Q-02 | ¿LightRAG o `neo4j-graphrag` para GraphRAG? | LightRAG (incremental, ligero); el consumo va tras `Protocol` | Prueba sobre corpus real en F3, midiendo coste de indexación incremental | 2026-09-01 |
| Q-03 | ¿El PDP corporativo expone API compatible con OPA o la suya propia? | Cliente contra API OPA; se añadirá un adapter si difiere | Especificación del AI Governance System de la plataforma | 2026-09-01 |
| Q-04 | ¿Qué directorio mapea usuarios de Teams/Slack a grupos del tenant? | Mapeo declarativo en el perfil | Definición de la fuente de identidad corporativa | 2026-09-01 |
| Q-05 | ¿Retención del ledger local antes de rotar al Audit Ledger central? | Sin rotación; el fichero crece y se exporta por tenant | Política de retención de auditoría del cliente | 2026-09-01 |
| Q-06 | ¿Umbral de similitud de caché semántica realista? | 0.92 por defecto (valor del perfil de ejemplo) | Medición de falsos positivos sobre tráfico real en F7 | 2026-09-01 |
