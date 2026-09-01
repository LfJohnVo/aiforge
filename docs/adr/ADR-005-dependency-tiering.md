---
status: accepted
date: 2026-09-01
deciders: arquitectura de plataforma
---
# ADR-005 · Estratificación de dependencias: núcleo liviano + extras + fallbacks locales

## Contexto y planteamiento del problema

El stack de la sección 6 del prompt maestro incluye paquetes que arrastran PyTorch,
transformers y modelos: `docling`, `mem0ai`, `graphiti-core`, `presidio-analyzer`,
`llm-guard`, `deepeval`, `ragas`. Instalarlos todos como dependencia obligatoria produce
un entorno de varios gigabytes y una imagen de contenedor igualmente grande. Eso choca
con tres cosas del propio prompt: el objetivo "de cero a chat funcionando en <15 min"
(QUICKSTART), imágenes slim (RNF-04) y CI rápido con gates (RF-13).

Segundo problema, descubierto al resolver el lockfile: `llm-guard==0.3.16` fija
`json-repair==0.44.1` mientras `lightrag-hku==1.5.6` exige `>=0.59.9`. Es una
incompatibilidad **real**, no evitable eligiendo versiones.

## Motores de la decisión

* `uv sync` del entorno base debe completarse en menos de un minuto.
* La imagen de `agent-api` no debe contener PyTorch: el `agent-api` no calcula
  embeddings; eso vive en el worker de ingesta.
* Cero placeholders: un extra ausente no puede producir un `NotImplementedError` en
  runtime. Debe haber un comportamiento correcto y documentado.
* El lockfile universal tiene que seguir siendo resoluble.

## Opciones consideradas

* Todas las dependencias obligatorias en `[project.dependencies]`
* **Núcleo liviano + extras opcionales + fallbacks de primera clase**
* Repositorios separados por capa (multi-paquete)

## Resultado de la decisión

Opción elegida: **núcleo liviano con extras y fallbacks**. Concretamente:

| Extra | Contenido | Si falta |
|---|---|---|
| `knowledge` | qdrant-client, neo4j, docling, lightrag-hku | Parser de texto plano/Markdown incorporado + almacén vectorial en memoria; RAG funciona sobre corpus pequeño |
| `memory` | mem0ai, graphiti-core | Adapter LTM propio sobre Redis, mismo `Protocol` (ADR-002) |
| `guardrails` | presidio-analyzer/anonymizer | Scrubber determinista por reglas para PII ES/EN (ADR-006) |
| `promptguard` | llm-guard | Prompt firewall por reglas, **siempre activo** (ADR-006) |
| `databases` | sqlalchemy, asyncmy, motor | Los conectores MySQL/Mongo reportan `health() == False` y no se registran |
| `sources` | msgraph-sdk, azure-identity, aiobotocore | Fuente `folder` disponible; `sharepoint`/`s3` no se registran |
| `evals` | langfuse, deepeval, ragas | Los jueces por rúbrica LLM propios siguen corriendo |
| `analytics` | statsforecast | Módulo desactivado (RF-14 lo pide desactivado por defecto) |

Regla de implementación: **cada extra se importa de forma perezosa y en un solo lugar**
(el adapter correspondiente). El core nunca hace `import docling`. La disponibilidad se
publica en `/health` y en `/admin/config`, de modo que un operador ve qué capacidades
tiene realmente instaladas la célula.

El conflicto `llm-guard` / `lightrag-hku` se declara explícitamente en
`[tool.uv].conflicts`, lo que mantiene el lock resoluble y documenta la exclusión mutua
en el propio manifiesto.

### Consecuencias

* Bueno: `uv sync --extra dev` produce un entorno de desarrollo utilizable en segundos.
* Bueno: tres imágenes distintas (`agent-api` liviana, `ingestion-worker` con
  `knowledge`, `evals` en CI) en lugar de una imagen monolítica.
* Bueno: los fallbacks no son código muerto; son el camino que corre en cada test
  unitario, así que están permanentemente ejercitados.
* Malo: dos rutas de código por capacidad. Se mitiga con tests de contrato
  parametrizados que corren la misma batería contra el adapter real y el fallback.
* Malo: un operador puede desplegar creyendo tener GraphRAG sin el extra. Se mitiga con
  el reporte de capacidades en `/health` y un aviso al arrancar si el perfil pide una
  capacidad no instalada.

## Validación

`tests/unit/test_capabilities.py` verifica que importar `agent_forge.api.app` no
importa ninguna dependencia de extras, y que cada capacidad declarada en un perfil sin
su extra produce un aviso explícito al arranque, no un fallo tardío.
