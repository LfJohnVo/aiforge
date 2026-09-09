"""Comprueba una célula viva: citas, filtrado por identidad y soberanía.

    uv run python scripts/demo/verify_live.py

Lo que comprueba no es que el sistema responda. Es que responde **distinto según quién
pregunte**, que es la única forma de demostrar el invariante 2: alguien sin permiso tiene
que recibir algo indistinguible de un corpus vacío.

Usa los tokens de `scripts/demo/mint_tokens.py`, no una API key, y no por comodidad: una
API key identifica al *tenant*, así que `authenticate` devuelve `groups=()` y techo C0
(invariante 4). Con una API key no hay nada que filtrar y la prueba no probaría nada.

Devuelve 0 si todas las comprobaciones pasan.
"""

# ruff: noqa: S310  -- only ever opens the loopback URL this file defines
from __future__ import annotations

import json
import pathlib
import sys
import time
import urllib.error
import urllib.request
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
TOKENS = REPO_ROOT / "data" / "demo-oidc" / "tokens.json"
BASE = "http://127.0.0.1:8080"
TIMEOUT = 600


def load_tokens() -> dict[str, str]:
    if not TOKENS.is_file():
        raise SystemExit("faltan los tokens: uv run python scripts/demo/mint_tokens.py")
    loaded: dict[str, str] = json.loads(TOKENS.read_text(encoding="utf-8"))
    return loaded


def ask(token: str, question: str) -> dict[str, Any]:
    body = json.dumps(
        {
            "model": "agent-forge",
            "stream": False,
            "messages": [{"role": "user", "content": question}],
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE}/v1/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    started = time.monotonic()
    data: dict[str, Any]
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        return {"_error": exc.code, "_detail": exc.read().decode("utf-8", "replace")[:300]}
    except TimeoutError:
        return {"_error": "timeout", "_detail": f"sin respuesta en {TIMEOUT}s"}
    data["_seconds"] = round(time.monotonic() - started, 1)
    return data


def text_of(reply: dict[str, Any]) -> str:
    if "_error" in reply:
        return f"[{reply['_error']}] {reply['_detail']}"
    choices = reply.get("choices") or [{}]
    return str(choices[0].get("message", {}).get("content", ""))


RESULTS: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((ok, name))
    print(f"[{'PASA ' if ok else 'FALLA'}] {name}")
    if detail:
        print(f"         {detail}")


def main() -> int:
    tokens = load_tokens()
    print(f"Verificacion en vivo · {BASE}")
    print("=" * 78)

    # 1. Responde con el dato correcto a quien tiene el grupo.
    reply = ask(tokens["ana"], "¿Cual es el limite diario de alimentos en viaje nacional?")
    body = text_of(reply)
    check(
        "ana (finanzas) obtiene el limite de viaticos",
        "1,500" in body or "1500" in body,
        f"{reply.get('_seconds', '?')}s · {body[:170]}",
    )

    # 2. El mismo dato preguntado de otra forma. Con embeddings lexicos esto falla, que es
    #    lo que hace peligrosa la degradacion silenciosa: la pregunta 1 seguiria pasando.
    reply = ask(tokens["ana"], "¿Cuanto puedo gastar en comidas si viajo dentro del pais?")
    body = text_of(reply)
    check(
        "lo encuentra tambien con otras palabras (embeddings semanticos)",
        "1,500" in body or "1500" in body,
        f"{reply.get('_seconds', '?')}s · {body[:170]}",
    )

    # 3. Invariante 2: sin el grupo, no hay dato.
    reply = ask(tokens["beto"], "¿Cual es el limite diario de alimentos en viaje nacional?")
    body = text_of(reply)
    check(
        "beto (ventas) no obtiene un dato de finanzas",
        "1,500" not in body and "1500" not in body,
        body[:170],
    )

    # 4. La parte dificil del invariante 2: ni siquiera revela que el documento existe.
    #
    # Lo que se comprueba NO es que la respuesta evite los nombres: negar algo obliga a
    # nombrarlo, y "no tengo informacion sobre Zafiro" no revela nada. Se comprueba que no
    # aparezca ningun *dato* del documento, y que la negativa no delate su existencia
    # diciendo que hace falta permiso -- eso ultimo si confirma que hay algo ahi.
    reply = ask(tokens["beto"], "¿Que sabes del Proyecto Zafiro y de la empresa Nortec?")
    body = text_of(reply)
    lowered = body.lower()
    facts = [f for f in ("180", "210", "6.2", "ebitda", "due diligence") if f in lowered]
    confirms = [
        p
        for p in (
            "no tienes permiso",
            "no autorizado",
            "confidencial",
            "reservado",
            "no puedo compartir",
            "acceso restringido",
        )
        if p in lowered
    ]
    check(
        "beto no obtiene ningun dato del documento reservado",
        not facts and not confirms,
        f"datos filtrados={facts or 'ninguno'} · delata={confirms or 'no'} · {body[:120]}",
    )

    # 5. Quien si tiene el grupo obtiene el C3 -- y de un modelo local.
    reply = ask(tokens["carla"], "¿Cual es el multiplo objetivo de EBITDA del Proyecto Zafiro?")
    body = text_of(reply)
    check(
        "carla (direccion) si obtiene el contenido C3",
        "6.2" in body or "6,2" in body,
        f"{reply.get('_seconds', '?')}s · {body[:170]}",
    )

    # 6. Sin grupos, indistinguible de un corpus vacio.
    reply = ask(tokens["dani"], "¿Cual es el limite diario de alimentos en viaje nacional?")
    body = text_of(reply)
    check(
        "dani (sin grupos) obtiene una respuesta sin datos del corpus",
        "1,500" not in body and "1500" not in body,
        body[:170],
    )

    # 7. Sin credencial no se entra.
    try:
        urllib.request.urlopen(urllib.request.Request(f"{BASE}/v1/models"), timeout=30)
        check("una peticion sin credencial es rechazada", False, "respondio 200")
    except urllib.error.HTTPError as exc:
        check("una peticion sin credencial es rechazada", exc.code == 401, f"HTTP {exc.code}")

    print("=" * 78)
    passed = sum(1 for ok, _ in RESULTS if ok)
    print(f"{passed}/{len(RESULTS)} comprobaciones pasan")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
