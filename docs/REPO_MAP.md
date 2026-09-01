# Mapa del repositorio

> **Archivo generado por `make repo-graph`.** No lo edites a mano: el próximo
> `make repo-graph` sobrescribirá los cambios. La fuente de verdad es el código.

## Resumen

- Módulos Python: **43**
- Líneas de código: **1056**
- Clases: **2** · Funciones y métodos de nivel superior: **54**
- Dependencias externas importadas directamente: **16**

## Dependencias entre paquetes

```mermaid
flowchart LR
```

## Módulos

### `scripts`

| Módulo | Descripción | LOC | Símbolos |
|---|---|---:|---|
| [`coverage_gate.py`](../scripts/coverage_gate.py) | Enforce a per-package coverage floor from coverage.xml. | 75 | `package_coverage`, `main` |
| [`docs_check.py`](../scripts/docs_check.py) | Verify that every document required by the spec exists and is not a stub. | 170 | `check_file`, `_strip_code`, `_has_heading`, `check_adrs`, `check_decisions_log_per_phase`, `main` |
| [`repo_graph.py`](../scripts/repo_graph.py) | Build a knowledge graph of this repository from its AST. | 365 | `Symbol`, `Module`, `_decorator_name`, `_first_line`, `parse_module`, `discover` (+9) |

### `src/agent_forge`

| Módulo | Descripción | LOC | Símbolos |
|---|---|---:|---|
| [`__init__.py`](../src/agent_forge/__init__.py) | Agent Forge - reusable enterprise agent cell for the PEAK architecture. | 7 | — |

### `src/agent_forge/analytics`

| Módulo | Descripción | LOC | Símbolos |
|---|---|---:|---|
| [`__init__.py`](../src/agent_forge/analytics/__init__.py) | Agent Forge package. | 2 | — |

### `src/agent_forge/api`

| Módulo | Descripción | LOC | Símbolos |
|---|---|---:|---|
| [`__init__.py`](../src/agent_forge/api/__init__.py) | Agent Forge package. | 2 | — |

### `src/agent_forge/channels`

| Módulo | Descripción | LOC | Símbolos |
|---|---|---:|---|
| [`__init__.py`](../src/agent_forge/channels/__init__.py) | Agent Forge package. | 2 | — |

### `src/agent_forge/channels/copilot_studio`

| Módulo | Descripción | LOC | Símbolos |
|---|---|---:|---|
| [`__init__.py`](../src/agent_forge/channels/copilot_studio/__init__.py) | Agent Forge package. | 2 | — |

### `src/agent_forge/channels/openai_api`

| Módulo | Descripción | LOC | Símbolos |
|---|---|---:|---|
| [`__init__.py`](../src/agent_forge/channels/openai_api/__init__.py) | Agent Forge package. | 2 | — |

### `src/agent_forge/channels/openwebui`

| Módulo | Descripción | LOC | Símbolos |
|---|---|---:|---|
| [`__init__.py`](../src/agent_forge/channels/openwebui/__init__.py) | Agent Forge package. | 2 | — |

### `src/agent_forge/channels/slack`

| Módulo | Descripción | LOC | Símbolos |
|---|---|---:|---|
| [`__init__.py`](../src/agent_forge/channels/slack/__init__.py) | Agent Forge package. | 2 | — |

### `src/agent_forge/channels/teams`

| Módulo | Descripción | LOC | Símbolos |
|---|---|---:|---|
| [`__init__.py`](../src/agent_forge/channels/teams/__init__.py) | Agent Forge package. | 2 | — |

### `src/agent_forge/channels/websocket`

| Módulo | Descripción | LOC | Símbolos |
|---|---|---:|---|
| [`__init__.py`](../src/agent_forge/channels/websocket/__init__.py) | Agent Forge package. | 2 | — |

### `src/agent_forge/connectors`

| Módulo | Descripción | LOC | Símbolos |
|---|---|---:|---|
| [`__init__.py`](../src/agent_forge/connectors/__init__.py) | Agent Forge package. | 2 | — |

### `src/agent_forge/connectors/databases`

| Módulo | Descripción | LOC | Símbolos |
|---|---|---:|---|
| [`__init__.py`](../src/agent_forge/connectors/databases/__init__.py) | Agent Forge package. | 2 | — |

### `src/agent_forge/connectors/mcp_client`

| Módulo | Descripción | LOC | Símbolos |
|---|---|---:|---|
| [`__init__.py`](../src/agent_forge/connectors/mcp_client/__init__.py) | Agent Forge package. | 2 | — |

### `src/agent_forge/connectors/n8n`

| Módulo | Descripción | LOC | Símbolos |
|---|---|---:|---|
| [`__init__.py`](../src/agent_forge/connectors/n8n/__init__.py) | Agent Forge package. | 2 | — |

### `src/agent_forge/connectors/openconnector`

| Módulo | Descripción | LOC | Símbolos |
|---|---|---:|---|
| [`__init__.py`](../src/agent_forge/connectors/openconnector/__init__.py) | Agent Forge package. | 2 | — |

### `src/agent_forge/core`

| Módulo | Descripción | LOC | Símbolos |
|---|---|---:|---|
| [`__init__.py`](../src/agent_forge/core/__init__.py) | Agent Forge package. | 2 | — |

### `src/agent_forge/core/subgraphs`

| Módulo | Descripción | LOC | Símbolos |
|---|---|---:|---|
| [`__init__.py`](../src/agent_forge/core/subgraphs/__init__.py) | Agent Forge package. | 2 | — |

### `src/agent_forge/core/subgraphs/generalist`

| Módulo | Descripción | LOC | Símbolos |
|---|---|---:|---|
| [`__init__.py`](../src/agent_forge/core/subgraphs/generalist/__init__.py) | Agent Forge package. | 2 | — |

### `src/agent_forge/core/subgraphs/it_support`

| Módulo | Descripción | LOC | Símbolos |
|---|---|---:|---|
| [`__init__.py`](../src/agent_forge/core/subgraphs/it_support/__init__.py) | Agent Forge package. | 2 | — |

### `src/agent_forge/events`

| Módulo | Descripción | LOC | Símbolos |
|---|---|---:|---|
| [`__init__.py`](../src/agent_forge/events/__init__.py) | Agent Forge package. | 2 | — |

### `src/agent_forge/events/schemas`

| Módulo | Descripción | LOC | Símbolos |
|---|---|---:|---|
| [`__init__.py`](../src/agent_forge/events/schemas/__init__.py) | Agent Forge package. | 2 | — |

### `src/agent_forge/gateway`

| Módulo | Descripción | LOC | Símbolos |
|---|---|---:|---|
| [`__init__.py`](../src/agent_forge/gateway/__init__.py) | Agent Forge package. | 2 | — |

### `src/agent_forge/governance`

| Módulo | Descripción | LOC | Símbolos |
|---|---|---:|---|
| [`__init__.py`](../src/agent_forge/governance/__init__.py) | Agent Forge package. | 2 | — |

### `src/agent_forge/knowledge`

| Módulo | Descripción | LOC | Símbolos |
|---|---|---:|---|
| [`__init__.py`](../src/agent_forge/knowledge/__init__.py) | Agent Forge package. | 2 | — |

### `src/agent_forge/knowledge/cag`

| Módulo | Descripción | LOC | Símbolos |
|---|---|---:|---|
| [`__init__.py`](../src/agent_forge/knowledge/cag/__init__.py) | Agent Forge package. | 2 | — |

### `src/agent_forge/knowledge/graphrag`

| Módulo | Descripción | LOC | Símbolos |
|---|---|---:|---|
| [`__init__.py`](../src/agent_forge/knowledge/graphrag/__init__.py) | Agent Forge package. | 2 | — |

### `src/agent_forge/knowledge/ingestion`

| Módulo | Descripción | LOC | Símbolos |
|---|---|---:|---|
| [`__init__.py`](../src/agent_forge/knowledge/ingestion/__init__.py) | Agent Forge package. | 2 | — |

### `src/agent_forge/knowledge/rag`

| Módulo | Descripción | LOC | Símbolos |
|---|---|---:|---|
| [`__init__.py`](../src/agent_forge/knowledge/rag/__init__.py) | Agent Forge package. | 2 | — |

### `src/agent_forge/knowledge/sources`

| Módulo | Descripción | LOC | Símbolos |
|---|---|---:|---|
| [`__init__.py`](../src/agent_forge/knowledge/sources/__init__.py) | Agent Forge package. | 2 | — |

### `src/agent_forge/memory`

| Módulo | Descripción | LOC | Símbolos |
|---|---|---:|---|
| [`__init__.py`](../src/agent_forge/memory/__init__.py) | Agent Forge package. | 2 | — |

### `src/agent_forge/observability`

| Módulo | Descripción | LOC | Símbolos |
|---|---|---:|---|
| [`__init__.py`](../src/agent_forge/observability/__init__.py) | Agent Forge package. | 2 | — |

### `src/agent_forge/profile`

| Módulo | Descripción | LOC | Símbolos |
|---|---|---:|---|
| [`__init__.py`](../src/agent_forge/profile/__init__.py) | Agent Forge package. | 2 | — |

### `src/agent_forge/upstream`

| Módulo | Descripción | LOC | Símbolos |
|---|---|---:|---|
| [`__init__.py`](../src/agent_forge/upstream/__init__.py) | Agent Forge package. | 2 | — |

### `src/agent_forge/upstream/a2a`

| Módulo | Descripción | LOC | Símbolos |
|---|---|---:|---|
| [`__init__.py`](../src/agent_forge/upstream/a2a/__init__.py) | Agent Forge package. | 2 | — |

### `src/agent_forge/upstream/mcp_server`

| Módulo | Descripción | LOC | Símbolos |
|---|---|---:|---|
| [`__init__.py`](../src/agent_forge/upstream/mcp_server/__init__.py) | Agent Forge package. | 2 | — |

### `src/agent_forge/upstream/openapi`

| Módulo | Descripción | LOC | Símbolos |
|---|---|---:|---|
| [`__init__.py`](../src/agent_forge/upstream/openapi/__init__.py) | Agent Forge package. | 2 | — |

### `tests`

| Módulo | Descripción | LOC | Símbolos |
|---|---|---:|---|
| [`conftest.py`](../tests/conftest.py) | Shared pytest fixtures. | 57 | `repo_root`, `sample_package` |

### `tests/unit`

| Módulo | Descripción | LOC | Símbolos |
|---|---|---:|---|
| [`test_docs_check.py`](../tests/unit/test_docs_check.py) | Tests for the documentation completeness gate. | 119 | `test_this_repository_passes_the_gate`, `test_missing_file_is_reported`, `test_stub_is_reported`, `test_placeholder_markers_are_rejected`, `test_missing_heading_is_reported`, `test_fewer_than_four_accepted_adrs_fails` (+6) |
| [`test_docs_check_markers.py`](../tests/unit/test_docs_check_markers.py) | Regression tests for the two false positives the docs gate hit on its first run. | 73 | `_doc`, `test_spanish_word_todo_is_not_a_placeholder`, `test_uppercase_todo_in_prose_is_still_a_placeholder`, `test_marker_inside_inline_code_is_a_reference_not_a_marker`, `test_marker_inside_fenced_block_is_ignored`, `test_stripping_code_preserves_line_numbers` (+2) |
| [`test_repo_graph.py`](../tests/unit/test_repo_graph.py) | Tests for the repository graph builder (RF-12). | 120 | `test_discover_skips_noise`, `test_parse_module_extracts_symbols_and_docs`, `test_parse_module_marks_async_functions`, `test_parse_module_returns_none_on_syntax_error`, `test_module_id_strips_src_and_init`, `test_build_graph_links_internal_imports` (+5) |

## Dependencias externas

`__future__`, `argparse`, `ast`, `collections`, `coverage_gate`, `dataclasses`, `docs_check`, `json`, `networkx`, `pathlib`, `pytest`, `re`, `repo_graph`, `sys`, `typing`, `xml`
