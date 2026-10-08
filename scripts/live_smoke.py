"""Real provider smoke check. Requires installed backend and configured root .env."""
import asyncio
import json
import time
import sys

from app.config import settings
from app.providers import Providers
from app.errors import safe_error


async def main():
    cfg, provider = settings(), Providers()
    result = {"kind": "real-external-api", "models": {}}
    started = time.perf_counter()
    vectors = await provider.embed(["知识库支持扫描件 PDF，本地 OCR 提取文本。"], cfg.embedding_config())
    result["models"]["embedding"] = {"seconds": round(time.perf_counter() - started, 3),
                                         "dimension": len(vectors[0]), "model": cfg.embedding_model}
    started = time.perf_counter()
    ranks = await provider.rerank("支持扫描件吗？", ["本地 OCR 处理扫描件 PDF。", "服务器支持定时同步。"])
    result["models"]["rerank"] = {"seconds": round(time.perf_counter() - started, 3), "ranking": ranks,
                                      "model": cfg.rerank_model}
    started = time.perf_counter()
    response = await provider.chat([{"role": "user", "content": "用一句话说明 RAG 的作用。"}])
    result["models"]["deepseek"] = {"seconds": round(time.perf_counter() - started, 3),
                                       "response_chars": len(response), "model": cfg.deepseek_model}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        print(json.dumps({"status": "not_passed", "reason": safe_error(exc)}, ensure_ascii=False))
        sys.exit(1)
