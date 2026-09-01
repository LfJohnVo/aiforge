# Lecciones aprendidas

Lo que costó tiempo o cambió el plan. Se escribe para no repetirlo.

## F0

### El lockfile es parte del diseño, no del empaquetado

Fijar `uv.lock` reveló dos incompatibilidades que ningún documento de arquitectura habría
anticipado: `llm-guard` contra `lightrag-hku` (a través de `json-repair`), y `llm-guard`
limitando el intérprete a `<3.13`. Ambas cambiaron decisiones reales: el stack de DLP
(ADR-006) y la versión de Python del proyecto.

**Lección:** resolver el lockfile **antes** de escribir código, no después. Una tabla de
stack en un documento no es una verificación de compatibilidad.

### "Cero placeholders" y "dependencias pesadas opcionales" chocan si no se diseña

Un extra ausente no puede producir un `NotImplementedError`: eso es un placeholder con
otro nombre. La salida fue que cada extra tiene un **fallback de primera clase** que es,
además, el camino que ejercitan los tests unitarios. Efecto secundario útil: los
fallbacks no son código muerto, están permanentemente probados.

### Un control de seguridad que necesita descargar un modelo no es un control

El prompt firewall tiene que funcionar en arranque en frío, sin red y en modo degradado.
Eso lo convierte en código propio y determinista, con los escáneres ML como refuerzo
opcional que sólo puede endurecer el veredicto, nunca relajarlo.

### Filtrar después de recuperar es una fuga, no una ineficiencia

"El usuario sin permiso no recibe ni la existencia del documento" parece un requisito de
producto y es en realidad una restricción de arquitectura: obliga a que el filtro entre
en la consulta al almacén. Es lo que descartó pgvector y lo que fijó Qdrant (ADR-004).
Se descubrió leyendo el requisito con atención, no probando implementaciones.
