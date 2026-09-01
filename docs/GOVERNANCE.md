# Gobernanza

La gobernanza es **externa**: el Policy Decision Point (PDP) vive en la plataforma PEAK.
La célula es un Policy Enforcement Point (PEP) que consulta, cachea y **niega por
defecto**.

> Estado: contrato y política local definidos en F0; implementación en F6.

## 1. Qué decide el PDP

Cuatro preguntas, un mismo contrato:

| Decisión | Cuándo se pregunta | Efecto de `deny` |
|---|---|---|
| `knowledge_access` | Antes de construir el filtro de recuperación | El chunk no entra en la consulta |
| `tool_use` | Antes de exponer la tool al modelo y otra vez antes de invocarla | La tool no existe para el modelo |
| `external_model` | Antes de elegir backend en `gateway/model_policy.py` | Se fuerza backend local |
| `autonomy` | Antes de ejecutar una acción con efecto | Se abre HITL o se aborta |

Petición y respuesta viven en `governance/pdp_client.py`. La respuesta incluye
`allow`, `obligations` (p. ej. "redactar PII", "exigir cita") y `ttl_seconds`.

## 2. Fail-closed

`GOVERNANCE_FAIL_MODE`:

* `closed` (**default**) — sin respuesta del PDP y sin decisión cacheada válida:
  se deniega todo lo que sea C2 o superior, y toda acción A2 o superior. Las peticiones
  C0/C1 sin efectos laterales siguen atendiéndose.
* `permissive_c0c1` — igual, pero además permite usar decisiones cacheadas **expiradas**
  para C0/C1. Requiere activación explícita en el perfil y queda registrado en el ledger
  en cada uso.

En ningún modo se permite C3/C4 ni A2+ sin decisión fresca. Esto no es configurable.

## 3. Políticas locales (Rego)

`configs/policies/*.rego` es la base evaluable offline; la plataforma superpone su
overlay. Las mismas políticas se ejecutan en `tests/policies/` sin OPA en marcha, lo que
convierte cada regla en un test.

Paquetes previstos: `peak.knowledge`, `peak.tools`, `peak.models`, `peak.autonomy`.

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

Motor de reglas propio siempre activo, con Presidio y LLM Guard como refuerzos
opcionales que sólo pueden endurecer el veredicto (ADR-006). Se aplica en la entrada
(`governance_gate`) y en la salida (`quality_gate`), y cada disparo se registra.

Reglas en `configs/policies/dlp_rules.yaml`, con identificador estable, severidad y
acción (`block` | `redact` | `flag`). Revisión trimestral (RUNBOOK).

## 7. Allowlist de tools por tenant

`connectors/registry.py` no expone al modelo ninguna tool que no esté en
`connectors.mcp_gateway.allowlist` del perfil **y** autorizada por el PDP. La allowlist
se evalúa dos veces: al construir el catálogo de tools y justo antes de invocar, porque
entre ambas puede haber pasado tiempo y el PDP puede haber cambiado de opinión.

## 8. Evidencia

Toda decisión de política, tool call, aprobación humana y veredicto entra en el ledger
hash-chain (`events/evidence.py`) y se emite como `com.peak.evidence.record.v1`.
`make verify-ledger` valida la cadena completa.
