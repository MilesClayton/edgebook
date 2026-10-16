import base64

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa

from edgebook.venues.kalshi.auth import KalshiSigner


def pem(key: rsa.RSAPrivateKey | ed25519.Ed25519PrivateKey | ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def test_rsa_pss_signature_covers_ts_method_path_without_query() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    signer = KalshiSigner.from_pem("kid", pem(key))
    signer._clock_ms = lambda: 1_700_000_000_000
    h = signer.headers("get", "https://external-api.kalshi.com/trade-api/v2/portfolio/balance?x=1")
    assert h["KALSHI-ACCESS-KEY"] == "kid"
    assert h["KALSHI-ACCESS-TIMESTAMP"] == "1700000000000"
    key.public_key().verify(
        base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]),
        b"1700000000000GET/trade-api/v2/portfolio/balance",
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_ed25519_signature() -> None:
    key = ed25519.Ed25519PrivateKey.generate()
    signer = KalshiSigner("kid", key, clock_ms=lambda: 42)
    h = signer.headers("GET", "/trade-api/ws/v2")
    key.public_key().verify(
        base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]), b"42GET/trade-api/ws/v2"
    )


def test_rejects_unsupported_key_type() -> None:
    with pytest.raises(TypeError, match="unsupported"):
        KalshiSigner.from_pem("kid", pem(ec.generate_private_key(ec.SECP256R1())))
