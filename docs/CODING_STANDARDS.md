# Estándares de código

Idioma: **documentación en español, código en inglés** (identificadores, docstrings,
comentarios, mensajes de commit, mensajes de log).

## Reglas duras

1. **Sin placeholders.** Nada de `TODO`, `pass  # implementar`, ni funciones que sólo
   levanten `NotImplementedError` fuera de un `Protocol`. Todo archivo compila, ejecuta
   y tiene al menos un test que lo ejercita.
2. **Tipado estricto.** `mypy --strict` sobre todo el paquete. Sin `Any` salvo en la
   frontera con librerías sin tipos, y allí acotado en una línea con `cast`.
3. **Async por defecto** en todo I/O. Nada de `requests`, nada de drivers síncronos.
4. **El core no conoce dominios.** Ningún identificador de negocio (`finanzas`, `soc`,
   `jira`) aparece en `core/`, `governance/`, `gateway/` o `api/`.
5. **Fronteras por `Protocol`.** Todo servicio externo (almacén vectorial, LTM, bus,
   PDP, gateway de modelos) se consume por Protocol con al menos dos implementaciones:
   la real y la de desarrollo/test. Es lo que hace posible testear sin infraestructura.
6. **Dependencias opcionales, importación perezosa.** `import docling` sólo dentro del
   adapter que lo usa, nunca a nivel de módulo en el core (ADR-005).
7. **`tenant_id` explícito.** Ninguna función que toque un almacén lo recibe implícito
   por contexto global. Se pasa como argumento y se valida.
8. **Nada de SQL construido por el LLM.** Los conectores de base de datos exponen
   plantillas parametrizadas allowlisted; el modelo elige plantilla y argumentos.

## Convenciones

* Módulos y funciones `snake_case`, clases `PascalCase`, constantes `UPPER_SNAKE`.
* Un módulo, una responsabilidad. Si un archivo supera unas 400 líneas, probablemente
  son dos módulos.
* Los errores de dominio heredan de `AgentForgeError` (`core/errors.py`) y llevan un
  `code` estable, porque acaban en respuestas de API y en el ledger.
* Logging estructurado con `structlog`; nunca `print`. Todo log lleva `trace_id`,
  `tenant_id` y `task_id` cuando existen.
* Los comentarios explican **por qué**, no qué. El qué lo dice el código.

## Formato y linting

`ruff` (línea 100, reglas en `pyproject.toml`) es la autoridad. `make fmt` antes de
commitear; `make check` antes de abrir PR. `pre-commit` ejecuta ambos más `gitleaks`.

## Tests

* `tests/unit/` — sin red, sin contenedores. Debe correr en menos de 60 s.
* `tests/integration/` — marcados `integration`, usan testcontainers.
* `tests/policies/` — evalúan las políticas Rego offline.
* `tests/e2e/` — flujo completo contra `make up PROFILE=full`.
* Cobertura mínima **80 %** en `core/`, `governance/` y `knowledge/` (gate en CI).
* Test de contrato parametrizado: cuando hay un `Protocol` con N implementaciones, la
  misma batería corre N veces.
