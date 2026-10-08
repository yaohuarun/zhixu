"""Local HTTP contract fixture. Synthetic responses do not measure external model quality."""
import hashlib
import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
embedding_fails = False


@app.post("/control")
async def control(request: Request):
    global embedding_fails
    embedding_fails = bool((await request.json()).get("embedding_fails"))
    return {"contract_fixture": True}


@app.post("/embedding")
async def embedding(request: Request):
    if embedding_fails:
        return JSONResponse({"code": "ServiceUnavailable"}, status_code=503)
    body = await request.json()
    rows = []
    for index, text in enumerate(body["input"]["texts"]):
        digest = hashlib.sha256(text.encode()).digest()
        rows.append({"text_index": index, "embedding": [
            (digest[i % 32] + 1) / 256 for i in range(body["parameters"]["dimension"])]})
    return {"output": {"embeddings": rows}}


@app.post("/rerank")
async def rerank(request: Request):
    body = await request.json()
    return {"output": {"results": [{"index": i, "relevance_score": 0.9 - i * 0.001}
                                    for i in range(len(body["input"]["documents"]))]}}


@app.post("/chat")
async def chat(request: Request):
    body = await request.json()
    if not body.get("stream"):
        return {"choices": [{"message": {"content": "知识库支持哪些文件格式？"}}]}
    async def stream():
        import asyncio
        for piece in ["知识库支持 PDF、", "Word 和 TXT。", "扫描件通过本地 OCR 处理。[1]"]:
            yield "data: " + json.dumps({"choices": [{"delta": {"content": piece}}]}, ensure_ascii=False) + "\n\n"
            await asyncio.sleep(0.1)
        yield "data: [DONE]\n\n"
    return StreamingResponse(stream(), media_type="text/event-stream")
