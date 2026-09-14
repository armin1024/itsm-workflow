from __future__ import annotations

import math
from typing import Any

import httpx

from app.config import settings


class RetrievalError(RuntimeError):
    pass


def retrieval_text(record) -> str:
    definition = record.draft_definition or {}
    node_semantics = []
    for node in definition.get("nodes", []):
        node_semantics.append(str(node.get("title") or ""))
        node_semantics.extend(str(item.get("name") or "") for item in node.get("inputs", []))
    return "\n".join(item for item in [record.summary, record.name, " ".join(record.match_phrases), " ".join(record.system_keys), *node_semantics] if item)


async def _post(url: str, body: dict[str, Any], api_key: str = "") -> Any:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, json=body, headers=headers)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise RetrievalError(f"模型服务请求失败：{type(exc).__name__}") from exc


async def embed(texts: list[str]) -> list[list[float]]:
    if not settings.embedding_base_url:
        raise RetrievalError("Embedding服务未配置")
    payload = await _post(settings.embedding_base_url.rstrip("/") + "/embeddings", {"model": settings.embedding_model, "input": texts}, settings.embedding_api_key)
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) != len(texts):
        raise RetrievalError("Embedding响应数量不一致")
    vectors = []
    for row in rows:
        vector = row.get("embedding") if isinstance(row, dict) else None
        if not isinstance(vector, list) or not vector or not all(isinstance(item, (int, float)) and math.isfinite(item) for item in vector):
            raise RetrievalError("Embedding响应向量无效")
        norm = math.sqrt(sum(float(item) ** 2 for item in vector))
        if not norm:
            raise RetrievalError("Embedding响应零向量")
        vectors.append([float(item) / norm for item in vector])
    return vectors


def cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True))


async def rerank(query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
    if not settings.rerank_base_url:
        raise RetrievalError("Rerank服务未配置")
    base = settings.rerank_base_url.rstrip("/")
    path = "/" + settings.rerank_path.strip("/")
    url = base if base.endswith("/rerank") else base + path
    formats = [settings.rerank_api_format] if settings.rerank_api_format != "auto" else ["openai", "tei", "legacy"]
    errors = []
    for api_format in formats:
        if api_format == "tei":
            body = {"query": query, "texts": documents, "truncate": True}
        elif api_format == "legacy":
            body = {"model": settings.rerank_model, "query": query, "documents": [{"id": str(index), "text": text} for index, text in enumerate(documents)], "top_n": top_n}
        else:
            body = {"model": settings.rerank_model, "query": query, "documents": documents, "top_n": top_n}
        try:
            payload = await _post(url, body, settings.rerank_api_key)
            rows = payload if isinstance(payload, list) else payload.get("results", payload.get("data")) if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                raise RetrievalError("响应缺少结果")
            result = []
            for row in rows:
                index = row.get("index") if isinstance(row, dict) else None
                score = row.get("relevance_score", row.get("score")) if isinstance(row, dict) else None
                if isinstance(index, int) and 0 <= index < len(documents) and isinstance(score, (int, float)):
                    result.append((index, float(score)))
            if result:
                return result[:top_n]
            raise RetrievalError("响应没有有效分数")
        except RetrievalError as exc:
            errors.append(f"{api_format}: {exc}")
    raise RetrievalError("；".join(errors))
