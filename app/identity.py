from __future__ import annotations

import ssl
from pathlib import Path
from typing import Any

import httpx

from app.config import settings


class IdentityError(RuntimeError):
    pass


class AopsIdentityClient:
    def __init__(self, base_url: str | None = None, ca_bundle: str | None = None):
        self.base_url = (base_url if base_url is not None else settings.aops_base_url).rstrip("/")
        self.ca_bundle = ca_bundle if ca_bundle is not None else settings.aops_ca_bundle

    async def resolve(self, api_key: str) -> dict[str, Any]:
        key = str(api_key or "").strip()
        if not key:
            raise IdentityError("AOPS_API_KEY 不能为空")
        if not self.base_url:
            raise IdentityError("AOPS_BASE_URL 未配置")
        verify: bool | ssl.SSLContext = True
        if self.base_url.lower().startswith("https://"):
            verify = ssl.create_default_context()
            for candidate in ("/etc/pki/tls/certs/ca-bundle.crt", "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem", "/etc/ssl/certs/ca-certificates.crt", "/etc/ssl/cert.pem"):
                if Path(candidate).is_file():
                    verify.load_verify_locations(cafile=candidate)
        if self.ca_bundle:
            path = Path(self.ca_bundle)
            if not path.is_file():
                raise IdentityError("AOPS_CA_BUNDLE 不存在")
            verify = ssl.create_default_context(cafile=str(path))
        try:
            async with httpx.AsyncClient(verify=verify, timeout=15) as client:
                response = await client.get(self.base_url + "/v2/user/self", headers={"Authorization": "Bearer " + key, "Accept": "application/json"})
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise IdentityError("无法获取 AOPS 用户信息") from exc
        if not isinstance(payload, dict) or payload.get("status") != 0 or not isinstance(payload.get("data"), dict):
            raise IdentityError("AOPS 用户信息接口返回失败")
        uid = str(payload["data"].get("uid") or "").strip()
        if not uid:
            raise IdentityError("AOPS 用户信息缺少 data.uid")
        return {"uid": uid, "profile": payload["data"]}
