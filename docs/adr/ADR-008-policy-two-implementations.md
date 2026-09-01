---
status: accepted
date: 2026-09-01
deciders: arquitectura de plataforma, seguridad
---
# ADR-008 · La base de políticas existe dos veces, y una tabla de casos las mantiene iguales

## Contexto y planteamiento del problema

RF-09 pide dos cosas que tiran en direcciones opuestas.

La primera: **policy-as-code en Rego**, con base local y overlay remoto, porque un equipo
de plataforma tiene que poder leer, revisar y desplegar la política sin leer Python, y
porque el PDP remoto de PEAK habla Rego.

La segunda: **la célula sigue decidiendo con el PDP caído**, y decide por cada chunk
recuperado y antes de cada invocación de tool. Eso descarta consultar OPA en el camino
caliente aunque estuviera arriba, y descarta por completo depender de que esté arriba.

Se necesita, entonces, evaluar la misma base de políticas en dos sitios: dentro del
proceso, siempre, y en OPA, cuando la plataforma lo aporta. Las opciones para no duplicar
resultaron todas peores que duplicar:

* **Un intérprete de Rego en Python.** No hay ninguno maduro. Escribirlo sería sustituir
  un problema de mantenimiento acotado por un motor de políticas propio.
* **OPA embebido como sidecar obligatorio.** Convierte una decisión en una llamada de red
  y hace que la célula no arranque sin él. Contradice directamente el requisito de seguir
  funcionando aislada, que es el que justifica la base local.
* **Sólo Python, y los `.rego` como documentación.** Es lo que ocurre por accidente cuando
  nadie ejecuta los `.rego`: dejan de ser política y pasan a ser comentarios largos que se
  desincronizan del código en la primera prisa.

## Decisión

La base de políticas se escribe **dos veces**, y se declara que así es:

1. `configs/policies/*.rego` — el artefacto que revisa y despliega la plataforma.
2. `governance/pdp.py::LocalPdp` — su traducción a Python, que decide en proceso.

Y **una sola tabla de casos**, `tests/policies/cases.py`, se ejecuta por ambos caminos:
`test_local_pdp.py` contra el Python y `test_rego.py` contra el OPA real, con la imagen
que despliega Compose. Cada caso nombra el invariante que protege. Una divergencia entre
las dos implementaciones es un test en rojo, no una sorpresa el día que el PDP remoto se
cae y la célula empieza a decidir distinto.

Donde no hay Docker, `test_rego.py` se salta con motivo explícito y la tabla sigue
ejecutándose por el camino Python: los invariantes nunca quedan sin comprobar, sólo se
comprueba uno de los dos motores.

El remoto es **overlay**, no reemplazo: `GovernanceService.decide` evalúa siempre la base
local y combina con `decisions.combine`, que sólo permite estrechar. Un overlay puede
prohibir lo que la base permite; nunca al revés.

## Consecuencias

* Cambiar una regla es cambiar dos ficheros y añadir un caso. Es el coste, y está puesto
  donde se ve: el docstring de `LocalPdp` lo dice, y el de `cases.py` también.
* El primer caso escrito con esta disciplina —un documento de entrada vacío— encontró que
  `peak.autonomy` **permitía**: cada regla de denegación referenciaba un campo ausente, su
  cuerpo quedaba indefinido, no había denegaciones y `count(denials) == 0` daba `allow`.
  `default deny` no protege de eso, porque `decision` sí estaba definido. La corrección
  (comprobar los campos obligatorios con un helper positivo negado) está ahora en los
  cuatro paquetes.
* La tabla lleva un `EXPECTED_CASE_COUNT` comprobado al importar. Una tabla que encoge en
  silencio es una suite que se debilita en silencio.

## Alternativa que se reconsiderará

Si aparece un intérprete de Rego para Python con respaldo real, la traducción manual
desaparece y `LocalPdp` pasa a ser un envoltorio sobre él. La tabla de casos sigue siendo
útil en ese mundo: pasaría a comparar el intérprete embebido con OPA.
