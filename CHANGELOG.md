# Changelog

Formato [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/); versionado
[SemVer](https://semver.org/lang/es/).

## [Unreleased]

### Added
- **F1 · Núcleo agéntico + canal base**: grafo LangGraph completo
  (`intake → governance_gate → planner → domain_subgraph → tools → synthesis →
  quality_gate → respond`), `AgentState` tipado, checkpointer Postgres/Redis/memoria,
  autonomía A0–A4 con HITL por `interrupt()`, subgrafos `generalist` e `it_support`,
  registry de prompts versionado, router de intents, planner, model gateway con la
  invariante de soberanía C0–C4, API OpenAI-compatible con streaming SSE,
  `/admin/approvals`, `/admin/config`, `/health/{live,ready}`, y Compose con perfiles.
- **F0 · Fundación**: estructura completa del monorepo, tooling (uv + ruff + mypy
  strict + pre-commit), CI base, esqueleto documental y memoria de proyecto.
- ADR-001 (LangGraph), ADR-002 (memoria), ADR-003 (NATS JetStream), ADR-004 (Qdrant).
- ADR-005 (estratificación de dependencias) y ADR-006 (stack DLP), derivados de dos
  incompatibilidades reales detectadas al fijar `uv.lock`.

[Unreleased]: https://github.com/silent4business/agent-forge/compare/v0.1.0...HEAD
