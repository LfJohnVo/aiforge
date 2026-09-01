---
name: release
description: Corta una release de agent-forge: decide el bump SemVer, cierra el CHANGELOG, actualiza la versión, verifica el Definition of Done y crea el tag. Úsala cuando haya que publicar una versión o preparar un release candidate.
---

# Skill · Release

## 1. Decidir el bump

| Bump | Cuándo |
|---|---|
| **MAJOR** | Cambio incompatible en `schema_version` del perfil, `BaseConnector`, `DomainSubgraph`, o esquema de evento sin nueva versión |
| **MINOR** | Canal nuevo, conector nuevo, nodo nuevo del grafo, capacidad nueva |
| **PATCH** | Corrección sin cambio de contrato |

Ante la duda entre MINOR y MAJOR, revisa si algún **perfil existente** deja de cargar.
Si deja de cargar, es MAJOR.

## 2. Comprobaciones antes de cortar

```bash
make check                 # lint + mypy strict + unit
make test                  # suite completa
make cov                   # gate 80% en core/governance/knowledge
make evals-ci              # umbrales de calidad y seguridad
make docs-check            # documentación completa
make repo-graph            # REPO_MAP.md al día
make verify-ledger         # cadena de evidencia intacta
uv lock --check            # lockfile coherente con pyproject
```

Y el gate de contrato: `make up PROFILE=full` con todos los healthchecks verdes.

## 3. Cerrar el changelog

Mueve el contenido de `## [Unreleased]` a `## [X.Y.Z] - AAAA-MM-DD` agrupado en
`Added` / `Changed` / `Deprecated` / `Removed` / `Fixed` / `Security`, deja
`## [Unreleased]` vacío arriba, y actualiza los enlaces de comparación al pie.

Cada línea describe **el efecto para quien usa la célula**, no el refactor interno.
"Añadido conector ServiceNow con tools de incidencias" sí; "refactorizado registry" no.

## 4. Versionar y etiquetar

```bash
# version = fuente única en pyproject.toml
uv run python - <<'PY'
import pathlib, re
p = pathlib.Path("pyproject.toml")
s = p.read_text(encoding="utf-8")
p.write_text(re.sub(r'^version = ".*"$', 'version = "X.Y.Z"', s, count=1, flags=re.M),
             encoding="utf-8")
PY
uv lock
git add -A
git commit -m "chore(release): vX.Y.Z"
git tag -a vX.Y.Z -m "vX.Y.Z"
```

No hagas push del tag hasta que CI esté verde en el commit de release.

## 5. Después

* Verifica que `security.yml` generó SBOM y no reporta CRITICAL.
* Comprueba que las imágenes quedan referenciables **por digest**, no por tag móvil.
* Si la release cambia el `schema_version` del perfil, documenta la migración en
  `docs/VERSIONING.md` y avisa en las notas de release: es lo único que rompe
  instalaciones existentes.
