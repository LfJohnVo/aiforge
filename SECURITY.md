# Política de seguridad

## Reportar una vulnerabilidad

**No abras un issue público.** Escribe a `security@silent4business.com` con:
descripción, impacto, pasos de reproducción y versión afectada. Acuse de recibo en
48 horas hábiles; evaluación inicial en 5 días hábiles. Divulgación coordinada:
publicamos tras el parche o a los 90 días, lo que ocurra antes.

## Versiones soportadas

| Versión | Soporte |
|---|---|
| 0.x | Sólo la última minor |

## Superficie de ataque

El modelo de amenazas completo (STRIDE) está en
[`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md). Los riesgos que gobiernan el diseño:

1. **Inyección de prompt** desde contenido recuperado o resultados de tool. Mitigación:
   DLP de entrada y salida, separación entre canal de instrucciones y datos,
   `quality_gate` con rúbrica de seguridad.
2. **Exfiltración vía tools.** Mitigación: allowlist por tenant, scopes mínimos,
   autonomía A0–A4 con HITL, ningún SQL ni comando arbitrario generado por el modelo.
3. **Fuga de datos C3/C4 a modelos externos.** Mitigación: un único punto de decisión
   (`gateway/model_policy.py`) y un test que recorre todas las rutas de código.
4. **Fuga por canal lateral en la recuperación.** Mitigación: filtrado dentro de la
   consulta al almacén, no después (ADR-004).
5. **Cadena de suministro.** Mitigación: `uv.lock` fijado, SBOM con syft, escaneo con
   trivy, gate de CRITICAL en CI, `gitleaks` en pre-commit.

## Reglas operativas

* Ningún secreto en el repositorio. Todo por `.env`; `.env.example` documenta cada
  variable con defaults **seguros** (bind a loopback, contraseñas marcadas `change-me`).
* Contenedores non-root, sistema de archivos de sólo lectura donde es posible,
  capacidades mínimas.
* Rotación de claves: `LEDGER_SIGNING_KEY` y `LITELLM_MASTER_KEY` trimestral.
  Procedimiento en [`docs/RUNBOOK.md`](docs/RUNBOOK.md).
* El ledger de evidencia es append-only; `make verify-ledger` valida la cadena y debe
  ejecutarse antes de cualquier exportación de auditoría.
