# Contribuir

## Preparación

```bash
uv sync --extra dev --extra databases
uv run pre-commit install
make check
```

Requisitos: Python 3.12 (lo instala `uv`), Docker con Compose v2, GNU Make.

> El proyecto está anclado a **Python 3.12** deliberadamente: el extra `promptguard`
> (`llm-guard`) no soporta 3.13+. Ver ADR-006.

## Flujo de trabajo

1. Rama desde `main`: `feat/...`, `fix/...`, `docs/...`, `chore/...`.
2. Un commit por unidad lógica de trabajo, con **Conventional Commits**:
   `feat(core): add HITL interrupt on A2 actions`.
   Ámbitos válidos: `core`, `channels`, `upstream`, `connectors`, `memory`,
   `knowledge`, `governance`, `gateway`, `events`, `observability`, `api`, `deploy`,
   `docs`, `evals`, `ci`.
3. `make check` en verde antes de abrir PR.
4. Toda decisión significativa implica un **ADR** en `docs/adr/` (formato MADR, ver
   ADR-000).
5. Todo cierre de fase implica entrada en `docs/memory/DECISIONS_LOG.md`, nota en
   `docs/memory/SESSION_NOTES/` y línea en `CHANGELOG.md`.

## Definition of Done de un PR

- [ ] `make check` verde (ruff + mypy strict + tests unitarios).
- [ ] Cobertura no baja; 80 % o más si toca `core/`, `governance/` o `knowledge/`.
- [ ] Código nuevo con al menos un test que falla si se revierte el cambio.
- [ ] Sin `TODO`, sin funciones vacías, sin secretos.
- [ ] Documentación actualizada en el mismo PR: si cambia el comportamiento, cambia el
      documento que lo describe. Documentación aspiracional es motivo de rechazo.
- [ ] `CHANGELOG.md` actualizado bajo `## [Unreleased]`.
- [ ] Si añade dependencia: justificada, fijada, `uv.lock` actualizado, y si es pesada
      va a un extra (ADR-005).
- [ ] Si añade o cambia un evento: esquema versionado en `events/schemas/`.

## Añadir cosas

* **Conector** → `make new-connector NAME=x`, luego documentar en `docs/CONNECTORS.md`.
* **Canal** → `docs/CHANNELS.md` describe el contrato paso a paso.
* **Subgrafo de dominio** → subclase de `DomainSubgraph` más entry point en
  `agent_forge.subgraphs`. Nunca se toca `core/graph.py`.

## Seguridad

Vulnerabilidades: **no abras un issue público**. Ver [SECURITY.md](SECURITY.md).
