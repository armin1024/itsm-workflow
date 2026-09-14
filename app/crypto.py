from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import settings


class SecretBox:
    def __init__(self, key: bytes | None = None):
        self.key = key or settings.encryption_key()
        self.cipher = AESGCM(self.key)

    def seal(self, value: Any, *, purpose: str) -> bytes:
        nonce = os.urandom(12)
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return nonce + self.cipher.encrypt(nonce, data, purpose.encode("utf-8"))

    def open(self, value: bytes, *, purpose: str) -> Any:
        if len(value) < 29:
            raise ValueError("无效密文")
        data = self.cipher.decrypt(value[:12], value[12:], purpose.encode("utf-8"))
        return json.loads(data.decode("utf-8"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_hash(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256_bytes(data)
