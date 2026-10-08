import asyncio
import json
import math
from collections.abc import AsyncIterator

import httpx

from .config import Settings, settings
from .errors import AppError


class Providers:
    def __init__(self, config: Settings | None = None, transport=None):
        self.config = config or settings()
        self.transport = transport
        self.semaphore = asyncio.Semaphore(self.config.model_concurrency)

    def credentials(self, kind: str, override: dict | None = None):
        cfg = self.config
        url = (override or {}).get("url", getattr(cfg, f"{kind}_url"))
        model = (override or {}).get("model", getattr(cfg, f"{kind}_model"))
        key = getattr(cfg, f"{kind}_key").get_secret_value()
        if not url or not model or not key or "{WorkspaceId}" in url:
            raise AppError("model_not_configured", f"{kind} 模型地址、模型 ID 或密钥未配置", 503)
        return url, model, {"Authorization": f"Bearer {key}"}

    async def post(self, kind: str, body: dict, override: dict | None = None) -> dict:
        url, _, headers = self.credentials(kind, override)
        retries = getattr(self.config, f"{kind}_retries")
        retries = self.config.model_retries if retries is None else retries
        timeout = getattr(self.config, f"{kind}_timeout") or self.config.model_timeout
        for attempt in range(retries + 1):
            try:
                async with self.semaphore, httpx.AsyncClient(
                    timeout=timeout, transport=self.transport,
                ) as client:
                    response = await client.post(url, headers=headers, json=body)
                if response.status_code in (429, 502, 503, 504):
                    if attempt < retries:
                        await asyncio.sleep(min(2 ** attempt, 4))
                        continue
                if response.status_code == 400:
                    # Do not expose provider bodies: they can echo secrets or private input.
                    try:
                        error_body = response.json()
                        code = str(error_body.get("code", "")) if isinstance(error_body, dict) else ""
                    except ValueError:
                        code = ""
                    if any(term in code.lower() for term in ("length", "token", "toolong")):
                        raise AppError("input_too_long", f"{kind} 输入超出服务限制", 422)
                if response.status_code >= 400:
                    raise AppError("model_http_error", f"{kind} 服务返回 HTTP {response.status_code}", 503)
                try:
                    result = response.json()
                except ValueError:
                    raise AppError("invalid_model_response", f"{kind} 返回无效 JSON", 503) from None
                if not isinstance(result, dict):
                    raise AppError("invalid_model_response", f"{kind} 返回无效响应结构", 503)
                if result.get("code"):
                    raise AppError("model_response_error", f"{kind} 服务返回错误状态", 503)
                return result
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt == retries:
                    raise AppError("model_timeout", f"{kind} 服务连接失败或超时", 503) from None
                await asyncio.sleep(min(2 ** attempt, 4))
        raise AppError("model_unavailable", f"{kind} 服务不可用", 503)

    async def embed(self, texts: list[str], config: dict, query=False) -> list[list[float]]:
        if not texts:
            return []
        vectors = []
        _, model, _ = self.credentials("embedding", config)
        for start in range(0, len(texts), self.config.embedding_batch):
            batch = texts[start:start + self.config.embedding_batch]
            parameters = {"dimension": config["dimension"], "text_type": "query" if query else "document"}
            if query:
                parameters["instruct"] = config.get("instruction", self.config.embedding_instruction)
            result = await self.post("embedding", {
                "model": model, "input": {"texts": batch}, "parameters": parameters,
            }, config)
            try:
                def row_index(row):
                    legacy, current = row.get("text_index"), row.get("index")
                    if legacy is not None and current is not None and legacy != current:
                        raise ValueError("conflicting indexes")
                    value = legacy if legacy is not None else current
                    if type(value) is not int:
                        raise ValueError("invalid index")
                    return value
                rows = sorted(result["output"]["embeddings"], key=row_index)
                if [row_index(row) for row in rows] != list(range(len(batch))):
                    raise ValueError("incorrect indexes")
                for row in rows:
                    vector = row["embedding"]
                    if len(vector) != config["dimension"] or not all(
                        isinstance(x, (int, float)) and math.isfinite(x) for x in vector
                    ) or not any(vector):
                        raise ValueError("incorrect vector")
                    vectors.append(vector)
            except (KeyError, TypeError, ValueError):
                raise AppError("invalid_embedding", "Embedding 返回维度、顺序或内容不正确", 503) from None
        return vectors

    async def rerank(self, query: str, documents: list[str]) -> list[tuple[int, float]]:
        _, model, _ = self.credentials("rerank")
        # UTF-8 bytes are an explicitly conservative input budget, not reported model token counts.
        if len(documents) > 500 or len(query.encode()) > 4000 or any(
            len(x.encode()) > 30000 for x in documents
        ) or sum(len(x.encode()) + len(query.encode()) for x in documents) > 120000:
            raise AppError("rerank_budget", "重排序输入超出本地保守预算", 422)
        result = await self.post("rerank", {
            "model": model, "input": {"query": query, "documents": documents},
            "parameters": {"top_n": len(documents), "return_documents": False,
                           "instruct": "Given a query, rank relevant passages that answer the query."},
        })
        try:
            rows = result["output"]["results"]
            pairs = [(row["index"], float(row["relevance_score"])) for row in rows]
            if len(pairs) != len(documents) or len({i for i, _ in pairs}) != len(pairs) or any(
                not isinstance(i, int) or i < 0 or i >= len(documents) or not 0 <= score <= 1
                for i, score in pairs
            ):
                raise ValueError("invalid reranking")
            return sorted(pairs, key=lambda row: row[1], reverse=True)
        except (KeyError, TypeError, ValueError):
            raise AppError("invalid_rerank", "重排序返回索引或分数不正确", 503) from None

    async def chat(self, messages: list[dict], max_tokens: int = 512) -> str:
        _, model, _ = self.credentials("deepseek")
        result = await self.post("deepseek", {
            "model": model, "messages": messages, "max_tokens": max_tokens, "stream": False,
            "thinking": {"type": "disabled"},
        })
        try:
            if result["choices"][0].get("finish_reason") == "length":
                raise AppError("output_truncated", "问题改写达到输出长度上限", 503)
            content = result["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("empty content")
            return content
        except (KeyError, IndexError, TypeError, ValueError):
            raise AppError("invalid_chat", "生成模型返回内容不正确", 503) from None

    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        url, model, headers = self.credentials("deepseek")
        retries = self.config.deepseek_retries
        retries = self.config.model_retries if retries is None else retries
        timeout = self.config.deepseek_timeout or self.config.model_timeout
        # Retry connection/status failures only before emitting the first delta.
        for attempt in range(retries + 1):
            emitted = False
            try:
                async with self.semaphore, httpx.AsyncClient(
                    timeout=timeout, transport=self.transport,
                ) as client:
                    async with client.stream("POST", url, headers=headers, json={
                        "model": model, "messages": messages, "stream": True,
                        "max_tokens": self.config.output_tokens,
                        "thinking": {"type": self.config.deepseek_thinking},
                    }) as response:
                        if response.status_code in (429, 502, 503, 504) and attempt < retries:
                            await asyncio.sleep(min(2 ** attempt, 4))
                            continue
                        if response.status_code >= 400:
                            raise AppError("model_http_error", f"生成服务返回 HTTP {response.status_code}", 503)
                        complete = False
                        async for line in response.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            payload = line[5:].strip()
                            if payload == "[DONE]":
                                complete = True
                                break
                            try:
                                choice = json.loads(payload)["choices"][0]
                                content = choice.get("delta", {}).get("content")
                                if content:
                                    emitted = True
                                    yield content
                                if choice.get("finish_reason") == "length":
                                    raise AppError("output_truncated", "回答达到输出长度上限，请重试或缩小问题范围", 503)
                            except (ValueError, KeyError, IndexError, TypeError):
                                raise AppError("invalid_stream", "生成服务返回无效流事件", 503) from None
                        if not complete:
                            raise AppError("incomplete_stream", "生成服务流异常结束", 503)
                        return
            except (httpx.TimeoutException, httpx.NetworkError):
                if emitted or attempt == retries:
                    raise AppError("model_timeout", "生成服务连接中断或超时", 503) from None
                await asyncio.sleep(min(2 ** attempt, 4))
