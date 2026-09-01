# Versionado

## Esquema

**SemVer 2.0.0** para el paquete `agent-forge` y para las imágenes de contenedor.

* **MAJOR** — cambio incompatible en: contrato del perfil (`schema_version`), interfaz
  `BaseConnector`, contrato `DomainSubgraph`, o esquema de un evento sin nueva versión.
* **MINOR** — funcionalidad compatible: canal nuevo, conector nuevo, nodo nuevo del grafo.
* **PATCH** — corrección sin cambio de contrato.

## Artefactos versionados por separado

| Artefacto | Esquema | Regla |
|---|---|---|
| Paquete e imágenes | SemVer | `v0.1.0`, tag git `v0.1.0` |
| Perfil (`agent.profile.yaml`) | `schema_version` entero | Se incrementa sólo ante cambio incompatible; el cargador acepta N y N-1 con aviso |
| Eventos CloudEvents | sufijo en el `type` | `com.peak.task.result.v1` pasa a `.v2`. **Nunca** se modifica un `.v1` ya publicado |
| Prompts | directorio versionado en `configs/prompts/` | Promoción manual; jamás auto-deploy |
| Políticas Rego | versión en el paquete Rego | Cambio de decisión implica nueva versión y entrada en el ledger |

## Changelog

[Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/). Cada PR que cambia
comportamiento añade una línea bajo `## [Unreleased]`. La skill `.claude/skills/release/`
mueve `Unreleased` a la versión, crea el tag y dispara CI.

## Compatibilidad de eventos

Un consumidor debe ignorar campos desconocidos. Un productor nunca elimina ni cambia el
tipo de un campo existente dentro de la misma versión mayor del evento. Los esquemas
viven en `src/agent_forge/events/schemas/` y se validan en tests.
