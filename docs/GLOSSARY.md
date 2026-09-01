# Glosario

Términos con significado preciso en este repositorio. Si un término aparece en el código
con otro sentido, el código está mal nombrado.

| Término | Definición operativa |
|---|---|
| **Agent Cell** | Una instalación de Agent Forge: un agente para un área, con su perfil, su tenant y sus almacenes namespaced. |
| **Perfil** (`agent.profile.yaml`) | Único artefacto que distingue una célula de otra. Validado con Pydantic al arranque. |
| **Tenant** (`tenant_id`) | Frontera dura de aislamiento. Parte de la clave en Redis, Postgres, Qdrant, Neo4j, NATS y ledger. |
| **Instancia** (`AGENT_FORGE_INSTANCE`) | Discriminador adicional que permite dos células del mismo tenant en el mismo host. |
| **Subgrafo de dominio** | Plugin que implementa `DomainSubgraph`. Contiene toda la lógica específica del área. El core no conoce ningún giro de negocio. |
| **C0–C4** | Clasificación del dato: C0 público, C1 interno, C2 confidencial, C3 restringido, C4 secreto. C3/C4 nunca salen a un modelo externo. |
| **Techo de clasificación** (`classification_ceiling`) | Máximo nivel C que una tarea puede manejar, derivado de la identidad del solicitante y del PDP. |
| **Clasificación acumulada** | Máximo de las clasificaciones de la petición, los chunks recuperados y los resultados de tools. Decide el enrutado del modelo. |
| **A0–A4** | Nivel de autonomía de una acción: A0 sólo lectura, A1 reversible, A2 requiere aprobación, A3 aprobación de rol elevado, A4 doble aprobación. |
| **HITL** | *Human in the loop*. Pausa del grafo con `interrupt()` a la espera de una decisión humana. |
| **PDP** | *Policy Decision Point*. Servicio externo (OPA o AI Governance System) que autoriza acceso a conocimiento, uso de tool, envío a modelo externo y nivel de autonomía. |
| **Fail-closed** | Ante pérdida de contacto con el PDP se deniega todo lo que sea C3/C4 o A2+. Configurable a `permissive_c0c1` bajo responsabilidad explícita. |
| **Judge** | Verificador de la respuesta (groundedness, seguridad, política). Modo `platform` (central, vía eventos) o `local` (LLM-as-judge con rúbricas). |
| **Agregador** | Consumidor central de `task.result`. En modo standalone hay uno in-process. |
| **Ledger de evidencia** | Registro append-only con hash-chain: cada entrada referencia el hash de la anterior. |
| **Event fabric** | NATS JetStream transportando CloudEvents 1.0. |
| **RAG / CAG / GraphRAG** | Recuperación vectorial+BM25 / precarga de corpus estable en contexto / recuperación sobre grafo de entidades. |
| **Identity-aware retrieval** | El filtro por identidad, grupos, ACL y clasificación se aplica **dentro** de la consulta al almacén, no después. |
| **MCP Gateway** | Tool Fabric de la plataforma. La célula es cliente, nunca lo implementa. |
| **Modo degradado** | Operación sin conectividad con la plataforma: fail-closed para C3/C4 y A2+; respuestas C0–C1 con políticas cacheadas si el perfil lo permite. |
