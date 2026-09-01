---
status: accepted
date: 2026-09-01
deciders: arquitectura de plataforma, calidad
---
# ADR-009 · Los scorers propios son el gate; Ragas refina y obliga a fijar `langchain-community`

## Contexto y planteamiento del problema

`docs/EVALS.md` nombra **Ragas** para las metricas RAG y **DeepEval** para las de
seguridad. Al implementar el harness en F7 aparecieron dos hechos.

**Uno: Ragas no importaba.** `ragas==0.4.3` hace `from
langchain_community.chat_models.vertexai import ChatVertexAI`, y `langchain-community`
0.4.x elimino ese modulo. Ragas no declara cota superior, asi que el resolvedor traia
0.4.2 y `import ragas` fallaba con `ModuleNotFoundError`. La extra `evals` entera era
inutilizable, y lo era en silencio: nadie la importa hasta que corre el gate.

**Dos: Ragas elige su propio modelo.** Su factory por defecto va a OpenAI. Un gate que
manda las respuestas del tenant a un proveedor externo para calificarlas rompe la
invariante de soberania —justo la invariante que ese mismo gate debe verificar— y lo hace
con la mejor de las intenciones, que es como se rompen las invariantes.

## Decision

**El gate lo llevan scorers propios y deterministas.** `evals/scorers.py` implementa
groundedness por solapamiento de tokens, answer relevancy contra los fragmentos que el
caso declara, context precision, citation rate, y las metricas de seguridad —fuga de PII,
obediencia a inyeccion, violacion de soberania— calculadas con los **detectores de la
propia celula**. Son mas toscos que Ragas y el codigo lo dice; a cambio corren sin red,
sin modelo y sin extra, que es lo que un gate necesita para no tener excusas.

**Ragas refina cuando esta disponible**, a traves de `evals/ragas_adapter.py`, apuntado al
**proxy LiteLLM de la propia celula**. Ese proxy ya decide que backends existen y que
puede llegar a cada uno, de modo que el juez hereda el enrutado en vez de esquivarlo.
`--ragas` lo activa; sin el, o sin la extra instalada, los scorers propios deciden.

**`langchain-community` queda fijado a `0.3.31`** en la extra `evals`, con el motivo
escrito junto al pin. Es una dependencia de una dependencia, y fijarla es incomodo, pero
la alternativa es una extra que no importa.

**Las metricas de seguridad no se delegan nunca.** `pii_leak_rate` y
`sovereignty_violation_rate` se calculan aqui, a partir de lo que la celula detecto y de
como enruto de verdad. La opinion de un evaluador externo sobre si hubo fuga no es
evidencia de lo que la celula hizo.

## Alternativas consideradas

**Quitar Ragas del proyecto.** Habria sido honesto y mas simple. En contra: sus juicios de
groundedness son mejores que un solapamiento de tokens, y CI —que si instala la extra y
levanta Ollama— puede permitirse un gate mas estricto que un portatil. Mantenerlo como
refinamiento opcional conserva esa ventaja sin hacerla obligatoria.

**Esperar a que Ragas corrija su import.** Deja la extra rota mientras tanto, y el arreglo
depende de un tercero. Un pin se retira en una linea el dia que deje de hacer falta.

**Escribir un adaptador para que Ragas hable con el gateway de la celula en vez de con el
proxy.** Mas puro, y mas codigo: el gateway impone politica por peticion, y Ragas emite
las suyas sin el contexto de una tarea. El proxy es el punto correcto porque es donde vive
la lista de backends.

## Consecuencias

* El gate corre en cualquier sitio, con o sin extras. Lo que cambia con Ragas es la
  precision de tres metricas, no si el pipeline puede bloquear un merge.
* `langchain-community` queda fijado a una version de un paquete que su propio equipo
  declara en fin de vida. Hay que revisarlo cuando Ragas publique una version que no lo
  importe; el comentario junto al pin dice exactamente que comprobar.
* Los umbrales de `evals/thresholds.yaml` estan calibrados contra los scorers propios.
  Activar Ragas puede mover los valores observados, y por eso `--ragas` no esta activado
  por defecto en `make evals`: cambiar de medidor y de umbral a la vez no permite saber
  cual de los dos causo la diferencia.
