# Verificacion en vivo de la PoC

Ejecutado: 2026-09-09 03:13 (hora local), commit 2f7a6b7
Comando: uv run python scripts/demo/verify_live.py

```
Wed Sep  9 03:12:22     2026
Verificacion en vivo · http://127.0.0.1:8080
==============================================================================
[PASA ] ana (finanzas) obtiene el limite de viaticos
         8.8s · El límite diario de gasto en alimentos para viajes nacionales es de **1,500 MXN por día** [folder:///data/corpus/finanzas/politica-viaticos.md#p0].
[PASA ] lo encuentra tambien con otras palabras (embeddings semanticos)
         17.7s · El límite de gasto en comidas para viajes dentro del país es de **1,500 MXN por día** [folder:///data/corpus/finanzas/politica-viaticos.md#p0]. Todos los gastos requieren
[PASA ] beto (ventas) no obtiene un dato de finanzas
         No hay información disponible en los registros de finanzas de ACME sobre límites diarios de alimentos en viajes nacionales. La política de gastos relacionada con alimenta
[PASA ] beto no obtiene ningun dato del documento reservado
         datos filtrados=ninguno · delata=no · No tengo documentación disponible sobre el Proyecto Zafiro ni sobre la empresa Nortec en el área de Finanzas de ACME. No
[PASA ] carla (direccion) si obtiene el contenido C3
         15.6s · El multiplo objetivo de EBITDA del Proyecto Zafiro es 6.2x [folder:///data/corpus/direccion/plan-adquisicion.md#p0].
[PASA ] dani (sin grupos) obtiene una respuesta sin datos del corpus
         No hay información documentada sobre un límite diario de alimentos en viajes nacionales.
[PASA ] una peticion sin credencial es rechazada
         HTTP 401
==============================================================================
7/7 comprobaciones pasan
EXIT=0
Wed Sep  9 03:13:40     2026
```
