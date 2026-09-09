"""Prueba de carga sobre una célula viva. `uv run python scripts/demo/load_test.py`.

Sin k6 ni locust: lo que hace falta medir son dos números —el percentil 95 de la latencia
y el punto donde el modelo empieza a encolar— y eso son cuarenta líneas de asyncio. Una
herramienta más que instalar, versionar y explicar no compra nada aquí.

Lo que **no** mide, y conviene decirlo antes de que alguien use el número para dimensionar:
esto es una tarjeta sirviendo chat y embeddings a la vez, en WSL2, con el modelo cargado en
Ollama. Sirve para saber en qué concurrencia se degrada *esta* máquina, no para extrapolar
a una GPU de servidor.

La caché semántica se vacía antes de cada nivel: sin eso, la segunda petición idéntica
vuelve en milisegundos y el número mide la caché, no el modelo.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import statistics
import subprocess
import sys
import time

import httpx

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
TOKENS = REPO_ROOT / "data" / "demo-oidc" / "tokens.json"
BASE = "http://127.0.0.1:8080"
LEVELS = (1, 2, 4, 8)
REQUEST_TIMEOUT = 900

# Preguntas distintas entre sí: repetir una sola mediría el camino de caché, no el modelo.
QUESTIONS = [
    "¿Cual es el limite diario de alimentos en viaje nacional?",
    "¿Que plazo hay para comprobar los gastos de un viaje?",
    "¿Cuando vence una factura emitida el dia 20?",
    "¿Cuantos dias de vacaciones corresponden en el quinto ano?",
    "¿Que descuento puede autorizar un ejecutivo de cuenta sin consultar?",
    "¿Cuanto dura la vigencia de una cotizacion?",
    "¿Cuando se ejecuta el cierre contable mensual?",
    "¿Cada cuanto caduca la sesion de VPN?",
]


def flush_cache() -> None:
    subprocess.run(
        [
            "docker",
            "exec",
            "poc-redis-1",
            "sh",
            "-c",
            'redis-cli -a "$REDIS_PASSWORD" --no-auth-warning FLUSHALL',
        ],
        capture_output=True,
        check=False,
    )


async def one(client: httpx.AsyncClient, token: str, question: str) -> tuple[float, bool]:
    body = {
        "model": "agent-forge",
        "stream": False,
        "messages": [{"role": "user", "content": question}],
    }
    started = time.monotonic()
    try:
        response = await client.post(
            "/v1/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
            timeout=REQUEST_TIMEOUT,
        )
        ok = response.status_code == 200
    except (httpx.HTTPError, TimeoutError):
        ok = False
    return time.monotonic() - started, ok


async def level(token: str, concurrency: int) -> dict[str, float]:
    flush_cache()
    async with httpx.AsyncClient(base_url=BASE) as client:
        started = time.monotonic()
        results = await asyncio.gather(
            *(one(client, token, QUESTIONS[i % len(QUESTIONS)]) for i in range(concurrency))
        )
        wall = time.monotonic() - started

    times = sorted(t for t, ok in results if ok)
    failed = sum(1 for _, ok in results if not ok)
    if not times:
        return {"concurrency": concurrency, "failed": failed, "p50": 0, "p95": 0, "rps": 0}
    return {
        "concurrency": concurrency,
        "failed": failed,
        "p50": statistics.median(times),
        # Con pocas muestras el p95 es el peor caso; se dice, en vez de fingir precision.
        "p95": times[max(0, int(len(times) * 0.95) - 1)] if len(times) > 2 else times[-1],
        "max": times[-1],
        "rps": len(times) / wall,
    }


async def main() -> int:
    if not TOKENS.is_file():
        print("faltan los tokens: uv run python scripts/demo/mint_tokens.py", file=sys.stderr)
        return 1
    token = json.loads(TOKENS.read_text(encoding="utf-8"))["ana"]

    print(f"Carga contra {BASE} · una GPU sirviendo chat y embeddings")
    print("=" * 78)
    print(f"{'concurrencia':>12} {'p50':>8} {'p95':>8} {'max':>8} {'req/s':>8} {'fallos':>7}")

    rows = []
    for concurrency in LEVELS:
        row = await level(token, concurrency)
        rows.append(row)
        print(
            f"{row['concurrency']:>12} {row['p50']:>7.1f}s {row['p95']:>7.1f}s "
            f"{row.get('max', 0):>7.1f}s {row['rps']:>8.2f} {row['failed']:>7}"
        )

    print("=" * 78)
    baseline = rows[0]["p50"]
    worst = rows[-1]
    if baseline:
        print(
            f"De 1 a {worst['concurrency']} peticiones simultaneas, la mediana se multiplica "
            f"por {worst['p50'] / baseline:.1f}."
        )
    print("Una sola tarjeta: a partir de ahi el modelo encola, no se paraleliza.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
