"""Kalshi request signing.

Each authenticated request (REST or the WebSocket handshake) carries three
headers. The signature covers ``timestamp_ms + METHOD + path`` where path is
the URL path without host or query string. RSA keys sign with RSA-PSS/SHA-256
(salt = digest length); Ed25519 keys sign the message directly.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

PrivateKey = rsa.RSAPrivateKey | ed25519.Ed25519PrivateKey


class KalshiSigner:
    def __init__(
        self,
        key_id: str,
        private_key: PrivateKey,
        clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
    ) -> None:
        self.key_id = key_id
        self._key = private_key
        self._clock_ms = clock_ms

    @classmethod
    def from_pem(cls, key_id: str, pem: bytes) -> KalshiSigner:
        key = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(key, rsa.RSAPrivateKey | ed25519.Ed25519PrivateKey):
            raise TypeError(f"unsupported Kalshi key type: {type(key).__name__}")
        return cls(key_id, key)

    def sign(self, message: bytes) -> bytes:
        if isinstance(self._key, ed25519.Ed25519PrivateKey):
            return self._key.sign(message)
        return self._key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )

    def headers(self, method: str, url_or_path: str) -> dict[str, str]:
        path = urlsplit(url_or_path).path
        ts = str(self._clock_ms())
        signature = self.sign(f"{ts}{method.upper()}{path}".encode())
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        }
