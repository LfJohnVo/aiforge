# Gobernanza

La gobernanza es **externa**: el Policy Decision Point (PDP) vive en la plataforma PEAK.
La célula es un Policy Enforcement Point (PEP) que consulta, cachea y **niega por
defecto**.

> Estado: implementado en F6.

## 1. Qué decide el PDP

Cuatro preguntas, un mismo contrato:

| Decisión | Cuándo se pregunta | Efecto de `deny` |
|---|---|---|
| `knowledge_access` | Antes de construir el filtro de recuperación | El chunk no entra en la consulta |
| `tool_use` | Antes de exponer la tool al modelo y otra vez antes de invocarla | La tool no existe para el modelo |
| `external_model` | Antes de elegir backend en `gateway/model_policy.py` | Se fuerza backend local |
| `autonomy` | Antes de ejecutar una acción con efecto | Se abre HITL o se aborta |

Petición y respuesta son `PolicyRequest` y `PolicyVerdict`, en
`governance/decisions.py`. El veredicto lleva `allow`, `reasons`, `obligations` (p. ej.
`redact_pii`, `cite_source`, `human_approval`) y, cuando la decisión estrecha alguno,
`ceiling` y `autonomy`.

**Siempre lleva razones.** Una denegación sin motivo no se puede explicar al usuario,
defender en una revisión ni depurar; hay un test que comprueba que ningún veredicto sale
sin al menos una.

### Base local + overlay remoto

`GovernanceService.decide` evalúa **las dos**: la base Rego local, siempre, y el PDP
remoto cuando hay `pdp_url`. Se combinan con `decisions.combine`, y la combinación tiene
una sola dirección:

* `allow` es la conjunción: el overlay puede prohibir lo que la base permite.
* El techo se estrecha por **mínimo**; el requisito de autonomía sube por **máximo**.

El overlay **nunca** puede permitir lo que la base prohíbe. Si pudiera, una mala
configuración remota bastaría para abrir un tenant entero.

Una célula sin `pdp_url` decide con la base local sola. Es un despliegue soportado —un
portátil, una sede aislada—, no uno degradado.

## 2. Fail-closed

`governance.fail_mode` en el perfil (`closed` | `permissive_c0c1`):

* `closed` (**default**) — sin respuesta del PDP y sin decisión cacheada válida:
  se deniega todo lo que sea C2 o superior, y toda acción A2 o superior. Las peticiones
  C0/C1 sin efectos laterales siguen atendiéndose.
* `permissive_c0c1` — igual, pero además permite usar decisiones cacheadas **expiradas**
  para C0/C1. Requiere activación explícita en el perfil y queda registrado en el ledger
  en cada uso.

En ningún modo se permite C3/C4 ni A2+ sin decisión fresca. Esto no es configurable:
lo impone `PolicyRequest.needs_fresh_decision`, que `CachingPdp` consulta antes de mirar
siquiera la caché. Una decisión servida fuera de plazo se marca `stale=True` y llega al
ledger marcada como tal.

La caché tiene TTL de 30 s por defecto: existe para ahorrarle al PDP una llamada por cada
chunk recuperado, no para conservar decisiones. Su clave incluye la identidad completa del
solicitante, o el `allow` de un usuario se convertiría en el de todos.

## 3. Políticas locales (Rego)

Cuatro paquetes: `peak.knowledge`, `peak.tools`, `peak.models`, `peak.autonomy`, más
`peak.common` con las escalas C0–C4 y A0–A4. Todos empiezan con `default deny`.

Hay **dos implementaciones de la misma base**: los `.rego` y `governance/pdp.py::LocalPdp`,
que es su traducción a Python. La duplicación es deliberada —la célula tiene que seguir
decidiendo con OPA caído, y un intérprete de Rego en el camino caliente no compensa— y se
mantiene honesta con una sola tabla de casos, `tests/policies/cases.py`, que se ejecuta
por ambos caminos. Una divergencia es un test en rojo, no una sorpresa en producción. Ver
[ADR-008](adr/ADR-008-policy-two-implementations.md).

Los `.rego` se evalúan con el OPA real (imagen fijada, la misma que despliega Compose);
donde no hay Docker, ese módulo se salta con motivo explícito y la tabla sigue cubierta
por el camino Python.

Dos trampas de Rego que la base evita a propósito:

* **Las escalas se comparan como enteros**, nunca como cadenas. `"C10" < "C2"` como texto,
  y un valor desconocido se mapea al extremo **más estricto** (4), no a 0.
* **`default deny` no basta.** Si una regla referencia un campo ausente su cuerpo queda
  *indefinido*, la denegación no dispara y el paquete permite un documento vacío. Por eso
  los campos obligatorios se comprueban con un helper positivo negado
  (`not common.has_tenant`), que deniega tanto si el campo falta como si viene vacío.

## 4. Autonomía A0–A4

| Nivel | Significado | Requisito |
|---|---|---|
| A0 | Sólo lectura, sin efectos | Ninguno |
| A1 | Acción reversible de bajo impacto | Ninguno |
| A2 | Acción con efecto externo visible | **Aprobación humana** |
| A3 | Acción sobre sistema de registro | **Aprobación de rol elevado** |
| A4 | Acción irreversible o de alto impacto | **Doble aprobación** |

Hay **dos cantidades distintas** y confundirlas es la forma habitual de dejar un hueco:

| Cantidad | Qué es | Cómo se combina |
|---|---|---|
| **Nivel requerido** por una acción | Cuánta autonomía exige *esa* acción | **Máximo** de: `ToolSpec.autonomy_min`, `autonomy.overrides[categoría]` del perfil, y lo que imponga el PDP. Ninguna capa puede bajarlo. |
| **Nivel concedido** al solicitante | Cuánto puede ejercer *este* contexto sin intervención humana | **Mínimo**: por defecto A1 con identidad verificada; **A0 sin identidad**; el PDP sólo puede reducirlo. |

Una acción se ejecuta sin aprobación humana únicamente si:

```
requerido < A2   Y   requerido <= concedido
```

La primera condición es la regla de gobernanza (A2+ siempre pasa por un humano). La
segunda es la que impide que un solicitante anónimo ejecute siquiera una acción A1.

En el código: `core/autonomy.py` (`resolve` = máximo, para requisitos),
`GateOutcome.autonomy_granted` (la concesión) y el nodo `tools` de `core/graph.py`, que
aplica la conjunción.

## 5. HITL

Una acción que exige aprobación ejecuta `interrupt()`: el grafo persiste su checkpoint y
la tarea queda `awaiting_approval` indefinidamente. La cola se atiende en
`/admin/approvals`. Sólo miembros de `governance.hitl_approvers_group` pueden decidir;
A4 exige dos aprobadores distintos. Cada decisión entra en el ledger con actor, tiempo y
digest de la acción aprobada.

## 6. DLP y prompt firewall

Motor de reglas propio siempre activo (`governance/dlp.py`), con Presidio y LLM Guard
como refuerzos opcionales que sólo pueden endurecer el veredicto (ADR-006). Se aplica en la
entrada (`governance_gate`) y en la salida (el juez), y cada disparo se registra.

La detección de PII **no** se reimplementa aquí: la aporta `memory/scrubbing.py`, que ya
tiene regex con checksum y Presidio opcional encima. Dos detectores de PII serían dos
juegos de reglas que se desincronizan, y el día que discrepan uno de los dos está mal.

Reglas en `configs/policies/dlp_rules.yaml`, con identificador estable, severidad y acción
(`block` | `redact` | `flag`) por dirección. Revisión trimestral (RUNBOOK).

Orden dentro de la puerta: **DLP primero, PDP después.** El DLP inspecciona texto y puede
terminar la petición; el PDP decide sobre hechos. Preguntar al PDP por algo que se va a
bloquear de todos modos gasta una llamada y, peor, acerca el texto sin depurar a un
servicio remoto.

Un hallazgo **nunca** lleva el texto que lo disparó: un registro sobre un secreto no puede
contener ese secreto.

## 7. Allowlist de tools por tenant

`connectors/registry.py` no expone al modelo ninguna tool que no esté en
`connectors.mcp_gateway.allowlist` del perfil **y** autorizada por el PDP. La allowlist
se evalúa dos veces: al construir el catálogo de tools y justo antes de invocar, porque
entre ambas puede haber pasado tiempo y el PDP puede haber cambiado de opinión.

## 8. Evidencia

Toda decisión de política, tool call, aprobación humana y veredicto entra en el ledger
hash-chain (`events/evidence.py`) y se emite como `com.peak.evidence.record.v1`.
`make verify-ledger` valida la cadena completa; `--export --tenant` produce el JSONL
verificado que recibe un auditor.

Qué se guarda: **digests**, nunca el contenido. Ni el prompt, ni la respuesta, ni los
argumentos de la tool. Un ledger se conserva años y lo leen personas que no tienen derecho
al contenido del tenant; guardarlo ahí convertiría la pista de auditoría en la mayor copia
sin clasificar de todo lo que la célula ha visto.

Una cadena por tenant (el export es por tenant) y un fichero por día (un fichero único sin
límite es el que nadie puede rotar). El hash cubre **todos** los campos, metadatos
incluidos: lo que quede fuera del hash se puede editar libremente, y el nombre de la tool
en un registro de tool call es justo el campo que alguien querría cambiar.

La emisión al ledger central es asíncrona y su fallo **no** detiene a la célula: la cadena
local es la fuente de verdad y el ledger central, un espejo.
