"""Respalda el estado de una célula. ``make backup``.

Cuatro cosas, y una de ellas no es como las otras:

* **Postgres** — `pg_dump` comprimido. Lleva los checkpoints de LangGraph, así que sin él
  una tarea pausada esperando aprobación se pierde.
* **Qdrant** — snapshot por la API. Se puede reconstruir reingiriendo, pero reingerir un
  corpus grande cuesta horas de GPU; el snapshot cuesta segundos.
* **Redis** — **no se respalda, a propósito**. Es memoria de corto plazo y caché: perderla
  cuesta una conversación en curso y unos aciertos de caché, no datos. Respaldarla sería
  guardar PII redactada por ahí para nada.
* **El ledger** — copia del fichero, **verificando la cadena antes**. Esa es la que no es
  como las otras: el resto son datos que se pueden regenerar, y el ledger es la prueba de
  lo que el sistema hizo. Un respaldo de una cadena rota no es un respaldo, es una copia
  de un problema, y el invariante 9 dice que se verifica antes de exportar.

Compose no tiene planificador y este script no lo inventa: lo llama el cron del host, o el
Programador de tareas, o el CronJob del chart en Kubernetes. Un contenedor durmiendo en un
bucle para ejecutar una línea al día es un servicio más que vigilar.

    uv run python scripts/backup.py --out var/backups
    uv run python scripts/backup.py --out var/backups --verify-only
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

PROJECT = os.environ.get("COMPOSE_PROJECT_NAME", "agentforge")


def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(args, capture_output=True, check=False, **kwargs)  # type: ignore[call-overload,no-any-return]


def dump_postgres(target: Path) -> tuple[bool, str]:
    """`pg_dump` desde dentro del contenedor: la contraseña no pasa por el host."""
    container = f"{PROJECT}-postgres-1"
    result = _run(
        [
            "docker",
            "exec",
            container,
            "sh",
            "-c",
            'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --clean --if-exists',
        ]
    )
    if result.returncode != 0:
        return False, result.stderr.decode("utf-8", "replace")[:300]
    target.write_bytes(result.stdout)
    return True, f"{len(result.stdout) / 1024:.0f} KiB"


SNAPSHOT_REQUEST = (
    "import json,urllib.request;"
    "r=urllib.request.Request('http://qdrant:6333/collections/{collection}/snapshots',"
    "method='POST');"
    "print(json.load(urllib.request.urlopen(r,timeout=300))['result']['name'])"
)


def snapshot_qdrant(target: Path) -> tuple[bool, str]:
    """Pide la instantanea desde dentro y saca el fichero con `docker cp`.

    Qdrant vive sólo en la red `backend`, que es `internal`, así que desde el host no se
    le puede hablar -- y eso es deliberado: un almacén con datos del tenant no tiene
    egress ni ingress desde fuera. La petición la hace la célula, que sí está en esa red,
    y el fichero sale del volumen con `docker cp`, que no necesita shell en el contenedor.
    """
    collection = os.environ.get("QDRANT_COLLECTION", "agent_knowledge")
    created = _run(
        [
            "docker",
            "exec",
            f"{PROJECT}-agent-api-1",
            "python",
            "-c",
            SNAPSHOT_REQUEST.format(collection=collection),
        ]
    )
    if created.returncode != 0:
        return False, created.stderr.decode("utf-8", "replace")[:300]

    name = created.stdout.decode("utf-8").strip().splitlines()[-1]
    copied = _run(
        [
            "docker",
            "cp",
            f"{PROJECT}-qdrant-1:/qdrant/snapshots/{collection}/{name}",
            str(target),
        ]
    )
    if copied.returncode != 0 or not target.exists():
        return False, copied.stderr.decode("utf-8", "replace")[:300]
    return True, f"{target.stat().st_size / 1024 / 1024:.1f} MiB"


CONTAINER_LEDGER = "/var/lib/agent-forge/ledger"


def copy_ledger(target: Path, ledger: Path | None = None) -> tuple[bool, str]:
    """Verifica la cadena y sólo entonces la copia. Invariante 9.

    Si no se le da una ruta del host, la saca del contenedor: en cualquier despliegue real
    el ledger vive en un volumen, no en el disco de quien lanza el respaldo. Y el orden
    importa -- verificar **antes** de copiar. Un respaldo de una cadena rota no es un
    respaldo: es una copia de un problema, con la apariencia tranquilizadora de un fichero
    en el directorio correcto.
    """
    from agent_forge.events.evidence import ChainError, verify_path

    with tempfile.TemporaryDirectory(prefix="agent-forge-ledger-") as scratch:
        source = ledger
        if source is None or not source.exists():
            extracted = Path(scratch) / "ledger"
            pulled = _run(
                ["docker", "cp", f"{PROJECT}-agent-api-1:{CONTAINER_LEDGER}", str(extracted)]
            )
            if pulled.returncode != 0 or not extracted.exists():
                detail = pulled.stderr.decode("utf-8", "replace")[:200]
                return False, f"no se pudo obtener el ledger del contenedor: {detail}"
            source = extracted

        try:
            results = verify_path(source)
        except ChainError as exc:
            return False, f"cadena rota, NO se respalda: {exc}"

        total = sum(v for v in results.values() if isinstance(v, int))
        with tarfile.open(target, "w:gz") as archive:
            archive.add(source, arcname="ledger")
    return True, f"{len(results)} tenant(s), {total} registros"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "var" / "backups")
    parser.add_argument(
        "--ledger",
        type=Path,
        default=None,
        help="ruta del ledger en el host; por defecto se extrae del contenedor",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="comprueba la cadena y no escribe nada",
    )
    parser.add_argument("--keep", type=int, default=14, help="respaldos a conservar")
    args = parser.parse_args(argv)

    if args.verify_only:
        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as scratch:
            throwaway = Path(scratch.name)
        try:
            ok, detail = copy_ledger(throwaway, args.ledger)
        finally:
            throwaway.unlink(missing_ok=True)
        print(f"[{'OK ' if ok else 'FALLA'}] cadena de evidencia: {detail}")
        return 0 if ok else 1

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = args.out / stamp
    destination.mkdir(parents=True, exist_ok=True)

    steps: list[tuple[str, Callable[[], tuple[bool, str]]]] = [
        ("postgres", lambda: dump_postgres(destination / "postgres.sql")),
        ("qdrant", lambda: snapshot_qdrant(destination / "qdrant.snapshot")),
        ("ledger", lambda: copy_ledger(destination / "ledger.tar.gz", args.ledger)),
    ]

    failures = 0
    manifest: dict[str, object] = {"created_at": stamp, "project": PROJECT, "parts": {}}
    for name, step in steps:
        ok, detail = step()
        failures += 0 if ok else 1
        print(f"[{'OK  ' if ok else 'FALLA'}] {name}: {detail}")
        manifest["parts"][name] = {"ok": ok, "detail": detail}  # type: ignore[index]

    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # Retencion. Se borra por antiguedad y sólo dentro de --out: un respaldo que crece sin
    # limite acaba llenando el disco que aloja lo que respalda.
    existing = sorted(p for p in args.out.iterdir() if p.is_dir())
    for stale in existing[: max(0, len(existing) - args.keep)]:
        shutil.rmtree(stale, ignore_errors=True)
        print(f"[   ] retencion: eliminado {stale.name}")

    print(f"\nrespaldo en {destination}")
    if failures:
        print(f"{failures} parte(s) fallaron: el respaldo esta INCOMPLETO", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
