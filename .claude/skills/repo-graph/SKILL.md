---
name: repo-graph
description: Consulta el grafo del propio repositorio (módulos, clases, funciones, imports, dependencias) para responder dónde vive algo, qué importa a qué, y qué se rompe si cambias un módulo. Úsala antes de buscar a ciegas con grep en agent-forge.
---

# Skill · Grafo del repositorio

El repositorio se conoce a sí mismo. `scripts/repo_graph.py` parsea el AST y produce
`docs/graphs/repo-graph.json`, `repo-graph.graphml`, un resumen Mermaid y
`docs/REPO_MAP.md`.

**Doble uso (RF-12):** esta skill es el lado de desarrollo; el agente en runtime consulta
el mismo grafo con la tool `repo_graph.query`.

## Cuándo usarla

* "¿Dónde se decide el enrutado por clasificación?" → busca en el grafo antes que en grep.
* "¿Qué se rompe si cambio `AgentState`?" → dependientes de `agent_forge.core.state`.
* "¿Hay algo en `core/` que importe un dominio?" → detecta violaciones del invariante 5.
* Antes de un refactor: mide el grado de entrada del módulo que vas a mover.

## Regenerar

```bash
make repo-graph
```

Corre también por cron en el perfil `maintenance` y semanalmente en
`.github/workflows/repo-graph.yml`.

## Consultar

```bash
# Quién importa un módulo (radio de impacto de un cambio)
uv run python -c "
import json,sys
g=json.load(open('docs/graphs/repo-graph.json',encoding='utf-8'))
t=sys.argv[1]
print('\n'.join(sorted(e['source'] for e in g['edges']
      if e['target']==t and e.get('kind')=='imports')) or '(nadie)')
" agent_forge.core.state

# Dónde está definido un símbolo
uv run python -c "
import json,sys
g=json.load(open('docs/graphs/repo-graph.json',encoding='utf-8'))
n=sys.argv[1].lower()
for x in g['nodes']:
    if x['kind'] in ('class','function') and n in x['id'].lower():
        print(f\"{x['id']}  ->  {x['path']}:{x.get('lineno')}\")
" model_policy

# Módulos más importados (los que hay que tratar con cuidado)
uv run python -c "
import json,collections
g=json.load(open('docs/graphs/repo-graph.json',encoding='utf-8'))
c=collections.Counter(e['target'] for e in g['edges'] if e.get('kind')=='imports')
[print(f'{n:4d}  {m}') for m,n in c.most_common(15)]
"
```

## Comprobación de invariantes

El grafo permite verificar mecánicamente el invariante "el core no conoce dominios":

```bash
uv run python -c "
import json
g=json.load(open('docs/graphs/repo-graph.json',encoding='utf-8'))
bad=[e for e in g['edges']
     if e['source'].startswith(('agent_forge.core','agent_forge.governance',
                                'agent_forge.gateway','agent_forge.api'))
     and 'subgraphs.' in e['target'] and 'subgraphs.base' not in e['target']]
print('VIOLACIONES:', bad or 'ninguna')
"
```

## Reglas

* `docs/REPO_MAP.md` y `docs/graphs/*` son **generados**. Nunca los edites a mano.
* Si el grafo y tu intuición discrepan, el grafo tiene razón: viene del AST.
* Tras un refactor grande, regenera antes de documentar.
