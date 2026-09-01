# Políticas (Rego)

Base local evaluable offline. La plataforma superpone su overlay remoto; ante conflicto
gana **la más restrictiva**, nunca la más permisiva.

## Paquetes

| Paquete | Decide |
|---|---|
| `peak.knowledge` | Qué documentos puede ver un solicitante (identidad x grupos x ACL x C0-C4) |
| `peak.tools` | Qué tools se exponen al modelo y cuáles se pueden invocar |
| `peak.models` | Si una petición puede salir a un backend externo |
| `peak.autonomy` | Nivel de autonomía efectivo y si exige HITL |

## Reglas de escritura

1. **Default deny.** Todo paquete empieza con `default allow := false`.
2. **Una razón por denegación.** Las políticas devuelven `{"allow": bool, "reasons": [],
   "obligations": []}`; el motivo acaba en el ledger y en el mensaje al usuario.
3. **Sin efectos laterales.** Nada de `http.send` dentro de una política: la entrada se
   compone antes de evaluar, para que la decisión sea reproducible en un test.
4. **Evaluable sin OPA en marcha.** `tests/policies/` ejecuta las mismas políticas
   offline. Una regla sin test no se mergea.

## Otros ficheros

* `dlp_rules.yaml` — reglas del motor DLP propio (ADR-006): identificador estable,
  severidad y acción (`block` | `redact` | `flag`). No es Rego porque se evalúa en el
  camino caliente de cada mensaje, dentro y fuera de línea, y debe funcionar sin OPA.

Revisión trimestral de reglas DLP y allowlists (ver `docs/RUNBOOK.md`).
