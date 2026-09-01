---
status: accepted
date: 2026-09-01
deciders: arquitectura de plataforma, operación
---
# ADR-007 · El servidor MCP de la célula se publica sólo por streamable HTTP

## Contexto y planteamiento del problema

`ORCHESTRATORS.md`, escrito en F0, anunciaba que el servidor MCP de la célula ofrecería
**dos** transportes: stdio y streamable HTTP. Al implementarlo en F5 el contraste entre
ambos resultó ser más grande de lo que el contrato sugería.

Un servidor MCP por stdio es un **proceso hijo** que arranca el cliente. Para responder
`ask` o `run_task` necesita exactamente lo mismo que la célula HTTP: perfil cargado,
gateway de modelos, checkpointer, conocimiento, conectores. Es decir, un `build_runtime()`
completo por cada arranque, con su pool de Postgres y su conexión a Qdrant, para un
proceso cuya vida la decide el cliente y cuyo canal de autenticación es "quien pueda
ejecutar el binario".

Eso choca con dos invariantes que ya estaban decididas:

* **La identidad no se hereda.** Toda superficie autentica al solicitante y aplica su
  techo de clasificación. Un transporte cuyo control de acceso es el permiso de
  ejecución del proceso no puede sostener esa invariante: quien lanza el proceso pasa a
  ser, de hecho, el tenant entero.
* **Un despliegue, un punto de observación.** Trazas, ledger y métricas se emiten por
  proceso. Un enjambre de procesos hijo efímeros produce trazas huérfanas y un ledger
  fragmentado justo en las llamadas que más importa auditar: las de un orquestador.

## Decisión

La célula publica su servidor MCP **únicamente por streamable HTTP**, montado en el mismo
proceso y el mismo puerto que el resto de la API, bajo `/mcp/`.

El transporte stdio se mantiene en el **cliente** MCP (`connectors/mcp_client/`), donde
sí es el adecuado: ahí la célula es quien lanza el proceso y quien decide su ciclo de
vida, y el servidor remoto es una herramienta local, no una superficie de acceso.

Consecuencias operativas de montar dentro de FastAPI:

* La sub-aplicación de MCP tiene *lifespan* propio y Starlette **no** lo arranca al
  montarla. Lo arranca el `lifespan` de la aplicación padre; sin eso, toda llamada falla
  en caliente con `Task group is not initialized`.
* El servidor MCP valida la cabecera `Host` para impedir *DNS rebinding*. Sin
  configurar, sólo acepta loopback. La lista se deriva de `AGENT_PUBLIC_URL` y se amplía
  con `MCP_ALLOWED_HOSTS`. **La protección no se desactiva**: se configura.
* El endpoint canónico es `/mcp/`; `/mcp` responde 307 hacia él, redirección que todo
  SDK de MCP sigue.

## Alternativas consideradas

**Ofrecer también stdio con un `python -m agent_forge.upstream.mcp_server`.** Es unas
pocas decenas de líneas, y ése fue el argumento a favor. En contra: duplica la historia
de autenticación en el punto exacto donde el modelo de amenazas es más estricto, y añade
un segundo camino de arranque que hay que mantener sincronizado con `build_runtime()`
para siempre. El coste no está en escribirlo sino en no poder retirarlo después.

**Un proceso stdio delgado que hable HTTP con la célula.** Resuelve la autenticación
—delega en la API— pero entonces es un cliente, no un servidor de la célula, y quien lo
necesite puede usar cualquier puente MCP genérico. No hay razón para que lo publique
este repositorio.

## Consecuencias

* Un cliente que sólo hable stdio —hoy, algunos escritorios— necesita un puente genérico
  hacia HTTP. Es la incomodidad que acepta esta decisión.
* Un despliegue detrás de proxy **debe** fijar `AGENT_PUBLIC_URL`, o el MCP rechazará las
  peticiones por cabecera `Host`. Es un fallo cerrado y con mensaje claro en el log, que
  es como debe fallar un control de este tipo.
* `ORCHESTRATORS.md` queda corregido: anunciaba dos transportes y hay uno.
