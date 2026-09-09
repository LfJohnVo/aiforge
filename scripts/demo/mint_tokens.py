"""Un emisor OIDC de juguete para la PoC: genera la clave, el JWKS y unos tokens.

Existe porque el filtrado por identidad **no se puede demostrar con una API key**, y eso
es una propiedad del diseño, no una carencia: una API key identifica al *tenant*, así que
`authenticate` devuelve `groups=()` y techo C0 (invariante 4). Sin usuario no hay grupos,
y sin grupos la recuperación por identidad no tiene nada que filtrar.

Así que la PoC necesita tokens de verdad, firmados, verificados contra un JWKS. Esto los
produce.

    uv run python scripts/demo/mint_tokens.py

Genera en `data/demo-oidc/`:
  * `jwks.json`   -- la clave pública, que sirve el contenedor `oidc-mock`
  * `tokens.json` -- un token por usuario de ejemplo

**Esto no es un emisor OIDC.** No hay `/authorize`, ni `/token`, ni rotación, ni
revocación, y la clave privada queda en disco sin proteger. Sirve para una demostración
local y para nada más; producción usa Entra ID o Cognito. `data/` está en `.gitignore`
justamente para que estos artefactos no lleguen a ningún sitio.
"""

from __future__ import annotations

import json
import pathlib
import time
import uuid
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
OUT = REPO_ROOT / "data" / "demo-oidc"

# El emisor tal y como lo ve la célula desde dentro de la red de Compose.
ISSUER = "http://oidc-mock"
AUDIENCE = "agent-forge"
TENANT = "acme-mx"
KEY_ID = "poc-2026-09"

# Los usuarios de la demostración. Los grupos son los que separan las carpetas del corpus,
# así que estas cuatro filas son la prueba entera del invariante 2.
USERS: dict[str, dict[str, Any]] = {
    "ana": {"sub": "ana@acme.test", "groups": ["finanzas"], "ceiling": "C2"},
    "beto": {"sub": "beto@acme.test", "groups": ["ventas"], "ceiling": "C2"},
    "carla": {"sub": "carla@acme.test", "groups": ["direccion"], "ceiling": "C4"},
    # Sin grupos: debe ver exactamente lo mismo que vería con un corpus vacío.
    "dani": {"sub": "dani@acme.test", "groups": [], "ceiling": "C2"},
}


def _b64(value: int) -> str:
    import base64

    length = (value.bit_length() + 7) // 8
    return base64.urlsafe_b64encode(value.to_bytes(length, "big")).rstrip(b"=").decode()


def generate() -> tuple[str, dict[str, Any]]:
    """Devuelve (PEM privado, JWKS público)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

    numbers = key.public_key().public_numbers()
    jwks = {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": KEY_ID,
                "n": _b64(numbers.n),
                "e": _b64(numbers.e),
            }
        ]
    }
    return pem, jwks


def mint(pem: str, *, sub: str, groups: list[str], ceiling: str, hours: int = 12) -> str:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": sub,
        "tenant_id": TENANT,
        "groups": groups,
        "classification_ceiling": ceiling,
        "iat": now,
        "exp": now + hours * 3600,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(claims, pem, algorithm="RS256", headers={"kid": KEY_ID})


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    pem, jwks = generate()

    (OUT / "private.pem").write_text(pem, encoding="utf-8")
    (OUT / "jwks.json").write_text(json.dumps(jwks, indent=2), encoding="utf-8")
    # El descubrimiento OIDC estándar, por si algún cliente lo busca. La célula sólo
    # necesita el JWKS, pero un fichero de más aquí ahorra una pregunta.
    (OUT / "openid-configuration.json").write_text(
        json.dumps(
            {
                "issuer": ISSUER,
                "jwks_uri": f"{ISSUER}/jwks.json",
                "id_token_signing_alg_values_supported": ["RS256"],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    tokens = {
        name: mint(pem, sub=spec["sub"], groups=spec["groups"], ceiling=spec["ceiling"])
        for name, spec in USERS.items()
    }
    (OUT / "tokens.json").write_text(json.dumps(tokens, indent=2), encoding="utf-8")

    print(f"escrito en {OUT}")
    for name, spec in USERS.items():
        groups = ", ".join(spec["groups"]) or "(sin grupos)"
        print(f"  {name:6} {spec['sub']:18} techo {spec['ceiling']}  grupos: {groups}")
    print("\nEl token completo NO se imprime: esta en tokens.json, y data/ esta en .gitignore.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
