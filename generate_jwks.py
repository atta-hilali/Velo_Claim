import base64
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def base64url_uint(value: int) -> str:
    byte_length = (value.bit_length() + 7) // 8
    raw = value.to_bytes(byte_length, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def ensure_private_key(path: Path) -> rsa.RSAPrivateKey:
    if path.exists():
        return serialization.load_pem_private_key(
            path.read_bytes(),
            password=None,
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=3072,
    )
    path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return private_key


def create_jwks(private_key_path: str, kid: str, output_path: str) -> None:
    private_key = ensure_private_key(Path(private_key_path))
    public_numbers = private_key.public_key().public_numbers()

    jwks = {
        "keys": [
            {
                "kty": "RSA",
                "kid": kid,
                "use": "sig",
                "alg": "RS384",
                "n": base64url_uint(public_numbers.n),
                "e": base64url_uint(public_numbers.e),
            }
        ]
    }

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(jwks, indent=2) + "\n", encoding="utf-8")
    print(f"JWKS created: {output}")


create_jwks(
    private_key_path="keys/nonprod/private.pem",
    kid="velo-claim-nonprod-key-1",
    output_path="public/nonprod/jwks.json",
)

create_jwks(
    private_key_path="keys/prod/private.pem",
    kid="velo-claim-prod-key-1",
    output_path="public/prod/jwks.json",
)
