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
mueve `Unreleased` a la versión y crea el tag.

## Qué pasa al empujar el tag

El tag dispara [`release.yml`](../.github/workflows/release.yml), que es lo que convierte
un commit en algo desplegable. Antes de esto, CI construía la imagen sólo para escanearla y
la descartaba: nada de lo que verificaba llegaba a un registro, así que el artefacto que se
desplegaba era el que alguien hubiera construido en su portátil.

1. **El gate completo, otra vez, sobre el tag.** Que `main` estuviera verde no dice nada
   del commit que se está etiquetando.
2. **Publica en GHCR** las dos imágenes (`api`, `worker`) con procedencia y SBOM.
3. **Escanea la imagen ya publicada**, no una local. Escanear una y publicar otra es como
   se cuela una diferencia; una imagen publicada sin firmar todavía es inerte, porque nada
   la despliega.
4. **Firma con `cosign` en modo keyless.** La identidad que firma es la del propio
   workflow y queda en el log de transparencia; no hay clave privada que rotar ni que se
   pueda filtrar.
5. **Adjunta la atestación de procedencia** al registro.

Desplegar **por digest, nunca por tag**: un tag puede apuntar a otra imagen mañana, y eso
convierte un rollback en una lotería. `values-prod.yaml` lo espera así.

Verificar antes de desplegar:

```bash
cosign verify ghcr.io/<owner>/<repo>/api@sha256:<digest>   --certificate-identity-regexp '^https://github.com/<owner>/<repo>/'   --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

## Compatibilidad de eventos

Un consumidor debe ignorar campos desconocidos. Un productor nunca elimina ni cambia el
tipo de un campo existente dentro de la misma versión mayor del evento. Los esquemas
viven en `src/agent_forge/events/schemas/` y se validan en tests.
