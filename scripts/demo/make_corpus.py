"""Synthetic corpus for the PoC. Every fact here is invented.

Shaped to make three things testable rather than plausible-looking: distinct facts that
appear in exactly one document (so a citation can be checked), several ACL groups (so a
user without the group must get nothing), and two C3 documents (so sovereignty can be
seen end to end).
"""

import pathlib

ROOT = pathlib.Path("E:/programs/laragon/contenedores/agentforge/data/corpus")

DOCS = {
    "finanzas/politica-viaticos.md": """---
title: Politica de viaticos
acl_groups: [finanzas]
classification: C1
---

# Politica de viaticos

Vigente desde el 1 de marzo de 2026.

## Limites por dia

El limite de gasto en alimentos es de **1,500 MXN por dia** en viajes nacionales y de
**95 USD por dia** en viajes internacionales. El limite de hospedaje es de 3,200 MXN por
noche en zona metropolitana y 2,100 MXN en el resto del pais.

## Comprobacion

Todo gasto requiere factura (CFDI 4.0) a nombre de la empresa. Los gastos sin comprobante
fiscal se rechazan, salvo transporte publico hasta 250 MXN por viaje, que se comprueba con
el reporte de gastos.

El plazo para entregar la comprobacion es de **10 dias habiles** desde el regreso. Pasado
ese plazo el anticipo se descuenta de la nomina siguiente.

## Anticipos

Se otorga anticipo cuando el viaje excede 3 dias. El monto maximo de anticipo es del 80%
del gasto estimado.
""",
    "finanzas/proceso-facturacion.md": """---
title: Proceso de facturacion a clientes
acl_groups: [finanzas]
classification: C1
---

# Proceso de facturacion a clientes

## Ciclo

La facturacion corre los dias **5 y 20 de cada mes**. Una factura emitida el dia 5 vence a
los 30 dias naturales; una emitida el 20 vence a 45 dias, por acuerdo comercial heredado
del contrato marco de 2024.

## Cartera vencida

A los 15 dias de vencimiento se envia el primer recordatorio automatico. A los 30 dias la
cuenta pasa a cobranza y se suspende la emision de nuevos pedidos. A los 90 dias el caso
se turna a juridico.

El responsable de autorizar una excepcion a la suspension es el director comercial, y la
autorizacion debe quedar por escrito en el expediente del cliente.
""",
    "finanzas/cierre-mensual.md": """---
title: Cierre contable mensual
acl_groups: [finanzas]
classification: C1
---

# Cierre contable mensual

El cierre se ejecuta en los primeros **7 dias habiles** del mes siguiente.

Secuencia: conciliacion bancaria, provisiones, depreciacion, revaluacion de moneda
extranjera al tipo de cambio DOF del ultimo dia habil, y por ultimo el reporte de
resultados.

Ninguna poliza puede modificarse despues del cierre. Una correccion posterior se registra
como poliza de ajuste en el mes corriente, nunca reabriendo el mes cerrado.
""",
    "rrhh/vacaciones.md": """---
title: Vacaciones y permisos
acl_groups: [rrhh]
classification: C1
---

# Vacaciones y permisos

## Dias por antiguedad

Conforme a la reforma de 2023: 12 dias al cumplir el primer ano, y dos dias mas por cada
ano subsecuente hasta llegar a 20 dias en el quinto ano. A partir del sexto ano se suman
dos dias por cada cinco anos de servicio.

## Solicitud

La solicitud se captura con **15 dias naturales de anticipacion**. El jefe directo tiene 5
dias habiles para responder; si no responde, la solicitud se considera aprobada.

## Permisos sin goce

Se otorgan hasta 30 dias al ano, sujetos a autorizacion del director del area. No generan
antiguedad ni acumulan vacaciones.
""",
    "rrhh/onboarding.md": """---
title: Alta de personal nuevo
acl_groups: [rrhh]
classification: C1
---

# Alta de personal nuevo

El expediente debe estar completo **antes** del primer dia: acta de nacimiento, CURP, RFC,
comprobante de domicilio no mayor a 3 meses, y constancia de situacion fiscal.

El equipo de sistemas crea las cuentas con 48 horas de anticipacion. El acceso a sistemas
productivos requiere la firma del acuerdo de confidencialidad, que se archiva en el
expediente digital.

El periodo de prueba es de 30 dias para posiciones operativas y 90 dias para posiciones de
direccion.
""",
    "ti/restablecer-contrasena.md": """---
title: Restablecer contrasena
acl_groups: [todos]
classification: C0
---

# Restablecer contrasena

1. Entrar al portal de autoservicio.
2. Elegir "Olvide mi contrasena".
3. Confirmar con el segundo factor registrado.

La contrasena debe tener al menos 14 caracteres. El sistema rechaza las ultimas 5
contrasenas usadas. La cuenta se bloquea tras 8 intentos fallidos y se desbloquea sola a
los 20 minutos.

Si el segundo factor se perdio, hay que abrir un ticket presencial: la mesa de ayuda no
restablece el segundo factor por telefono, nunca, bajo ninguna circunstancia.
""",
    "ti/vpn.md": """---
title: Acceso VPN
acl_groups: [todos]
classification: C0
---

# Acceso VPN

La VPN corporativa usa el cliente oficial y el segundo factor de la cuenta.

La sesion caduca a las **12 horas** y se corta por inactividad de 30 minutos. El acceso
desde fuera del pais requiere autorizacion previa del area de seguridad, que se solicita
con 3 dias habiles de anticipacion.

No esta permitido conectarse desde equipo personal a los segmentos de produccion.
""",
    "ti/impresoras.md": """---
title: Impresion y escaneo
acl_groups: [todos]
classification: C0
---

# Impresion y escaneo

La impresion es por liberacion con gafete: el trabajo espera 8 horas en la cola y despues
se borra.

La cuota mensual es de 500 paginas por persona; el excedente se carga al centro de costos
del area. El escaneo no tiene cuota.

Para imprimir en color hace falta que el jefe del area lo habilite en el portal.
""",
    "direccion/plan-adquisicion.md": """---
title: Plan de adquisicion Proyecto Zafiro
acl_groups: [direccion]
classification: C3
---

# Plan de adquisicion - Proyecto Zafiro

RESERVADO. Este documento no puede salir de la infraestructura propia.

Se evalua la adquisicion de la empresa **Nortec Servicios SA de CV** por un rango de
entre 180 y 210 millones de MXN, sujeto a due diligence.

El multiplo objetivo es 6.2x EBITDA. La oferta indicativa se presenta el 30 de octubre de
2026. El financiamiento contemplado es 60% credito sindicado y 40% recursos propios.

Participan del proceso unicamente el director general, el director de finanzas y el
despacho externo contratado.
""",
    "direccion/presupuesto-2027.md": """---
title: Anteproyecto de presupuesto 2027
acl_groups: [direccion]
classification: C3
---

# Anteproyecto de presupuesto 2027

RESERVADO.

El crecimiento planeado en ingresos es del 18% respecto al cierre estimado de 2026. La
plantilla crece 9%, concentrada en el area de servicios.

Se contempla el cierre de la operacion de Monterrey en el segundo trimestre y la
reubicacion de 14 posiciones a la Ciudad de Mexico.
""",
    "ventas/descuentos.md": """---
title: Politica de descuentos
acl_groups: [ventas]
classification: C2
---

# Politica de descuentos

El ejecutivo de cuenta autoriza hasta **8%** sin consulta. Entre 8% y 15% autoriza el
gerente regional. Arriba de 15% autoriza el director comercial, y arriba de 25% se
requiere ademas el visto bueno de finanzas.

Los descuentos no son acumulables con la promocion de volumen ni con el precio de lista
para gobierno.

Toda excepcion se documenta en el CRM antes de emitir la cotizacion, no despues.
""",
    "ventas/proceso-cotizacion.md": """---
title: Proceso de cotizacion
acl_groups: [ventas]
classification: C2
---

# Proceso de cotizacion

Una cotizacion tiene vigencia de **21 dias naturales**.

Debe incluir: alcance, entregables, supuestos, exclusiones explicitas, condiciones de pago
y vigencia. Una cotizacion sin exclusiones explicitas no pasa revision.

El tiempo objetivo de respuesta a una solicitud es de 3 dias habiles. Para solicitudes que
requieren ingenieria de preventa el objetivo es de 8 dias habiles.
""",
}

README = """# Corpus de la PoC

**Todo el contenido de esta carpeta es inventado.** No hay ningun dato real de ningun
cliente ni de esta empresa. Existe para que la PoC pueda demostrar tres cosas que un
corpus de juguete no demuestra:

* **Citas verificables.** Cada dato concreto (un limite, un plazo, un porcentaje) aparece
  en exactamente un documento, asi que una cita se puede comprobar.
* **Filtrado por identidad.** Cinco grupos: `finanzas`, `rrhh`, `ventas`, `direccion` y
  `todos`. Un usuario de `ventas` no debe poder saber siquiera que existe
  `direccion/plan-adquisicion.md`.
* **Soberania.** Los dos documentos de `direccion/` son C3: ninguna pregunta que los
  recupere puede resolverse con un modelo externo, aunque haya uno configurado.

Se regenera con `scripts/demo/make_corpus.py`.
"""

if __name__ == "__main__":
    for relative, body in DOCS.items():
        target = ROOT / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8", newline="\n")
    (ROOT / "README.md").write_text(README, encoding="utf-8", newline="\n")

    print(f"{len(DOCS)} documentos en {ROOT}")
    areas: dict[str, int] = {}
    for path in DOCS:
        area = path.split("/")[0]
        areas[area] = areas.get(area, 0) + 1
    for area, count in sorted(areas.items()):
        print(f"  {area}: {count}")
