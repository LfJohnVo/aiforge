---
name: goal
description: Reancla la sesión al objetivo y audita el avance contra el Definition of Done. Usar al retomar la sesión, al iniciar el día o si la conversación se desvió del plan.
---

1. Relee la misión en CLAUDE.md y las secciones 11 y 12 de PROMPT_AGENT_FORGE.md.
2. Ejecuta `git log --oneline -15` y revisa docs/memory/DECISIONS_LOG.md.
3. Reporta en máximo 12 líneas: fase actual, cada punto del DoD con ✅/⏳/❌,
   bloqueos encontrados, y el siguiente paso concreto.
4. Si detectas desviación del objetivo, corrígela antes de continuar.
5. No inicies trabajo nuevo hasta emitir este reporte.
