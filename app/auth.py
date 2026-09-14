from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import Cookie, Header, HTTPException, Request

from app.config import settings
from app.crypto import SecretBox
from app.identity import AopsIdentityClient, IdentityError


@dataclass(frozen=True)
class Principal:
    uid: str
    api_key: str
    is_admin: bool
    is_operator: bool


identity = AopsIdentityClient()


def create_session_cookie(uid: str, api_key: str) -> str:
    expires = datetime.now(UTC) + timedelta(hours=settings.credential_ttl_hours)
    payload = {"uid": uid, "apiKey": api_key, "exp": int(expires.timestamp())}
    return SecretBox().seal(payload, purpose="browser-session").hex()


def principal_from_cookie(value: str) -> Principal:
    try:
        payload = SecretBox().open(bytes.fromhex(value), purpose="browser-session")
        if int(payload["exp"]) < int(datetime.now(UTC).timestamp()):
            raise ValueError("expired")
        uid = str(payload["uid"])
        return Principal(uid, str(payload["apiKey"]), uid in settings.admin_uids, uid in settings.operator_uids)
    except Exception as exc:
        raise HTTPException(401, "登录已过期") from exc


async def current_principal(
    request: Request,
    authorization: str | None = Header(default=None),
    x_aops_api_key: str | None = Header(default=None, alias="X-AOPS-Api-Key"),
    workflow_session: str | None = Cookie(default=None),
) -> Principal:
    if workflow_session:
        return principal_from_cookie(workflow_session)
    expected = "Bearer " + settings.workflow_api_token
    if not authorization or not secrets.compare_digest(authorization, expected) or not x_aops_api_key:
        raise HTTPException(401, "需要服务 Token 和 X-AOPS-Api-Key")
    try:
        profile = await identity.resolve(x_aops_api_key)
    except IdentityError as exc:
        raise HTTPException(401, str(exc)) from exc
    uid = profile["uid"]
    return Principal(uid, x_aops_api_key, uid in settings.admin_uids, uid in settings.operator_uids)


def require_operator(principal: Principal) -> Principal:
    if not principal.is_operator:
        raise HTTPException(403, "当前 UID 没有执行权限")
    return principal


def require_admin(principal: Principal) -> Principal:
    if not principal.is_admin:
        raise HTTPException(403, "当前 UID 没有管理权限")
    return principal
