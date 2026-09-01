# Registry de prompts

Los prompts son **memoria procedimental versionada** (RF-04). Reglas:

1. **Nunca se editan en caliente.** Cambiar un prompt es un PR con su corrida de evals,
   igual que un cambio de código.
2. **Promoción manual.** El agente puede *proponer* un few-shot a partir de memoria
   episódica, pero nada llega a `configs/prompts/` sin revisión humana.
3. **Versionados por directorio**, no por edición en sitio: `system/v1.md`,
   `system/v2.md`. El perfil o el código apunta a una versión concreta, de modo que un
   rollback es cambiar un puntero.

## Estructura

```
configs/prompts/
  system/          persona base + reglas del agente (parametrizado por el perfil)
  planner/         descomposición de tareas
  judge/           rúbricas: groundedness, seguridad, política
  classifier/      clasificación C0-C4 en la ingesta
  synthesis/       redacción con citas obligatorias
  fewshots/        ejemplos por área, promovidos desde memoria episódica
```

Cada directorio contiene `vN.md` con front-matter YAML:

```yaml
---
id: system
version: 1
inputs: [persona, area, language, classification_ceiling]
owner: plataforma
evals: [generalist, security]
---
```

`inputs` se valida al cargar: si el prompt referencia una variable que el estado no
provee, el arranque falla con un error claro en vez de renderizar una plantilla rota.

## Contenido específico de dominio

Un prompt **no** contiene lógica de negocio de ningún giro. Lo específico del área entra
por variables del perfil (`persona`, `area`) y por los few-shots del subgrafo. Un prompt
con "si el usuario pregunta por facturas..." es un bug de arquitectura.
