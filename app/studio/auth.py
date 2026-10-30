from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

from fastapi import Cookie, HTTPException

from app.config import settings


COOKIE_NAME = "itsm_runtime_studio"


def verify_admin_token(value: str) -> bool:
    return bool(value) and secrets.compare_digest(value, settings.studio_admin_token)


def create_session() -> str:
    expires = int(time.time()) + settings.studio_session_hours * 3600
    nonce = secrets.token_urlsafe(12)
    payload = f"{expires}.{nonce}"
    signature = hmac.new(settings.studio_admin_token.encode(), payload.encode(), hashlib.sha256).digest()
    return payload + "." + base64.urlsafe_b64encode(signature).decode().rstrip("=")


def valid_session(value: str | None) -> bool:
    if not value:
        return False
    try:
        expires_text, nonce, signature_text = value.split(".", 2)
        if int(expires_text) <= int(time.time()) or not nonce:
            return False
        payload = f"{expires_text}.{nonce}"
        expected = hmac.new(settings.studio_admin_token.encode(), payload.encode(), hashlib.sha256).digest()
        actual = base64.urlsafe_b64decode(signature_text + "=" * (-len(signature_text) % 4))
        return hmac.compare_digest(expected, actual)
    except (ValueError, TypeError):
        return False


async def require_studio_admin(itsm_runtime_studio: str | None = Cookie(default=None)) -> None:
    if not valid_session(itsm_runtime_studio):
        raise HTTPException(401, "需要管理Token或登录已过期")
