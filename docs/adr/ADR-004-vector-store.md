---
status: accepted
date: 2026-09-01
deciders: arquitectura de plataforma
---
# ADR-004 · Vector store: Qdrant

## Contexto y planteamiento del problema

RF-05 exige **recuperación identity-aware**: antes de sintetizar, cada chunk se filtra
por (identidad + grupos del solicitante) x (ACL del documento + clasificación C0-C4) x
(decisión del PDP). El requisito no negociable es que "el usuario sin permiso no recibe
ni la existencia del documento". Eso convierte el filtrado en parte de la **consulta**,
no en un post-proceso: si el filtro se aplica después del `top_k`, un documento
prohibido consume un slot y su ausencia es observable (el usuario ve menos resultados de
los que debería, o distintos según quién pregunte). Filtrar después es, además, una fuga
de metadatos por canal lateral.

## Motores de la decisión

* Filtrado por payload **dentro** de la búsqueda vectorial, con índices sobre los campos
  de filtro, y `top_k` calculado *después* del filtro.
* Aislamiento multi-tenant: `tenant_id` como campo indexado y obligatorio en cada punto.
* Snapshots y restauración por colección (requisito de backup del RUNBOOK).
* Operable en Compose sin extensión de base de datos ni tuning de Postgres.

## Opciones consideradas

* **Qdrant**
* **pgvector** sobre el Postgres que ya existe para el checkpointer
* **Weaviate**
* **Milvus**

## Resultado de la decisión

Opción elegida: **Qdrant**. Su modelo de filtros sobre payload con índices dedicados
(`keyword`, `integer`) resuelve exactamente el requisito: la condición
`tenant_id = X AND classification <= ceiling AND acl_groups ANY OF [grupos]` se evalúa
durante la búsqueda HNSW, y el `top_k` resultante ya está pre-filtrado. Los snapshots por
colección cubren el backup, y el contenedor arranca sin configuración.

El acceso queda encapsulado en `knowledge/rag/vector_store.py` tras un `Protocol`, y
**ninguna capa superior construye filtros a mano**: `access_control.py` es el único punto
que traduce (identidad, grupos, techo de clasificación) a un filtro de almacén. Esto es
lo que hace auditable el requisito.

### Consecuencias

* Bueno: la fuga por canal lateral queda cerrada por construcción, no por disciplina.
* Bueno: colección `repo_knowledge` separada para el grafo del repositorio (RF-12) con
  el mismo mecanismo.
* Malo: un servicio más que operar frente a reutilizar Postgres. Aceptado: el coste de
  operación es bajo y el requisito de filtrado no admite atajos.
* Neutro: los embeddings (bge-m3, 1024 dim) se calculan fuera del almacén, en el worker
  de ingesta, lo que mantiene el almacén intercambiable.

## Validación

`tests/unit/test_access_control.py` comprueba que el filtro generado excluye por tenant,
por ACL y por clasificación. `tests/integration/test_retrieval_acl.py` ingiere dos
documentos con ACLs distintas y verifica que un usuario sin permiso recibe **cero**
resultados y ninguna señal de existencia (mismo mensaje que ante un corpus vacío).

## Pros y contras de las opciones

### Qdrant
* Bueno: filtros indexados en la búsqueda, snapshots, cero configuración.
* Malo: servicio adicional.

### pgvector
* Bueno: sin infraestructura nueva; SQL para filtrar; transacciones.
* Malo: el filtro exacto sobre HNSW degrada el recall de forma difícil de acotar
  (iterative scan) y requiere tuning por consulta; mezclar el plano de estado con el de
  conocimiento complica el escalado independiente.

### Weaviate
* Bueno: filtrado y modelo de módulos potentes.
* Malo: mayor huella de recursos y más superficie de configuración para el mismo
  resultado.

### Milvus
* Bueno: rendimiento a gran escala.
* Malo: dependencias (etcd, MinIO) que contradicen el objetivo "laptop".
