"""Seed a demo corpus so a fresh cell has something to answer from.

Writes a small, deliberately *multi-ACL* corpus: two documents a plain area member can
read and one only the leaders can. That is what makes the access-control behaviour
visible from the first minute instead of after someone builds a corpus.

Nothing here is fictional-looking-but-plausible in a way that could be mistaken for a
real company's data: the tenant is `acme-mx` and the numbers are round.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from agent_forge.observability.logging import configure_logging, get_logger  # noqa: E402

log = get_logger("scripts.seed")


@dataclass(frozen=True)
class SeedDocument:
    path: str
    body: str


CORPUS: tuple[SeedDocument, ...] = (
    SeedDocument(
        "politicas/viaticos.md",
        """# Politica de viaticos

USO INTERNO

## Limites

El limite de viaticos nacionales es de 1500 MXN por dia, con comprobante fiscal.
Para viajes internacionales el limite es de 120 USD por dia.

## Comprobacion

Los gastos se comprueban dentro de los 10 dias habiles posteriores al viaje. Sin
comprobante fiscal valido el gasto no es reembolsable.

## Excepciones

Cualquier excepcion requiere autorizacion previa del lider de area.
""",
    ),
    SeedDocument(
        "politicas/proveedores.md",
        """# Alta de proveedores

USO INTERNO

Un proveedor nuevo requiere: constancia de situacion fiscal, comprobante de domicilio y
una cuenta bancaria a nombre de la razon social. El alta la valida Finanzas y la aprueba
el lider de area antes de la primera orden de compra.

El plazo estandar de pago es de 30 dias naturales desde la recepcion de la factura.
""",
    ),
    SeedDocument(
        "reservado/nomina-directiva.md",
        """# Nomina directiva

RESTRINGIDO

La revision salarial del comite directivo se realiza cada trimestre. El detalle por
persona no se comparte fuera del grupo de lideres de Finanzas.
""",
    ),
)


def seed(root: Path, *, force: bool) -> int:
    """Write the demo corpus. Never overwrites unless asked."""
    written = 0
    for document in CORPUS:
        target = root / document.path
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and not force:
            log.info("seed.skipped_existing", path=document.path)
            continue
        target.write_text(document.body, encoding="utf-8")
        written += 1
        log.info("seed.wrote", path=document.path, bytes=len(document.body))

    print(f"seeded {written} document(s) into {root}")
    if written:
        print("next: make ingest   (then ask about the travel policy)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--path",
        type=Path,
        default=Path(os.environ.get("LOCAL_CORPUS_PATH", "data/corpus")),
        help="where to write the demo corpus",
    )
    parser.add_argument("--force", action="store_true", help="overwrite existing files")
    args = parser.parse_args(argv)

    configure_logging(level="INFO", fmt="console", service="seed")
    return seed(args.path, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
