import asyncio
import logging
import re

from qdrant_client import models as qm
from sqlalchemy import func, select

from .config import settings
from .db import session
from .errors import AppError
from .indexing import words
from .models import Chunk, Document, IndexConfig, KnowledgeBase
from .providers import Providers
from .vectors import vector_store


def lexical(kb_id: str, query: str):
    tokens = list(dict.fromkeys(words(query).split()))
    if not tokens:
        return []
    # Tokens are restricted by words(); SQL is always parameterized.
    tsquery = func.to_tsquery("simple", " | ".join(tokens[:100]))
    with session() as db:
        result = db.execute(select(Chunk.id, func.ts_rank_cd(Chunk.search_vector, tsquery).label("score"))
                            .join(Document, Chunk.document_id == Document.id)
                            .where(Chunk.kb_id == kb_id, Document.deleted.is_(False),
                                   Document.active_version_id == Chunk.version_id,
                                   Chunk.search_vector.op("@@")(tsquery))
                            .order_by(func.ts_rank_cd(Chunk.search_vector, tsquery).desc())
                            .limit(settings().retrieval_limit))
        return [(row.id, float(row.score)) for row in result]


def validate_candidates(kb_id, ids):
    with session() as db:
        kb = db.get(KnowledgeBase, kb_id)
        if not kb or kb.status != "active":
            raise AppError("kb_inactive", "知识库不可检索", 409)
        return {c.id: c for c in db.scalars(select(Chunk).join(Document, Chunk.document_id == Document.id).where(
            Chunk.id.in_(ids), Chunk.kb_id == kb_id, Document.deleted.is_(False),
            Document.active_version_id == Chunk.version_id,
        ))}


def semantic(config, vector):
    cfg = settings()
    limit, accepted = cfg.retrieval_limit, []
    while True:
        result = vector_store().query_points(
            config.collection, query=vector, limit=limit, with_vectors=False,
            query_filter=qm.Filter(must=[
                qm.FieldCondition(key="kb_id", match=qm.MatchValue(value=config.kb_id)),
                qm.FieldCondition(key="index_config_id", match=qm.MatchValue(value=config.id)),
            ]),
        ).points
        valid = validate_candidates(config.kb_id, [str(p.id) for p in result])
        accepted = [(str(p.id), p.score) for p in result if str(p.id) in valid]
        if len(accepted) >= cfg.retrieval_limit or len(result) < limit or limit >= cfg.retrieval_max:
            return accepted[:cfg.retrieval_limit]
        limit = min(limit * 2, cfg.retrieval_max)


def rrf(branches: list[list[tuple[str, float]]], k: int):
    scores: dict[str, float] = {}
    for branch in branches:
        seen = set()
        for rank, (identity, _) in enumerate(branch, 1):
            if identity not in seen:
                scores[identity] = scores.get(identity, 0.0) + 1 / (k + rank)
                seen.add(identity)
    return sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))


async def retrieve(kb_id: str, query: str, provider: Providers | None = None):
    provider, cfg = provider or Providers(), settings()
    with session() as db:
        kb = db.get(KnowledgeBase, kb_id)
        if not kb or kb.status != "active":
            raise AppError("kb_inactive", "知识库不存在或不可检索", 404)
        config = db.get(IndexConfig, kb.index_config_id)
        has_chunks = db.scalar(select(Chunk.id).join(Document, Chunk.document_id == Document.id).where(
            Chunk.kb_id == kb_id, Document.deleted.is_(False), Document.active_version_id == Chunk.version_id,
        ).limit(1))
    if not has_chunks:
        return {"chunks": [], "degraded": [], "debug": {"lexical": [], "vector": [], "fused": [], "reranked": []}}
    degraded = []

    async def vector_branch():
        vector = (await provider.embed([query], config.config, query=True))[0]
        return await asyncio.to_thread(semantic, config, vector)

    results = await asyncio.gather(asyncio.to_thread(lexical, kb_id, query), vector_branch(), return_exceptions=True)
    branches: list[list[tuple[str, float]]] = []
    for name, result in zip(("关键词召回", "向量召回"), results, strict=True):
        if isinstance(result, BaseException):
            logging.getLogger("rag.retrieval").warning("retrieval degraded branch=%s type=%s",
                                                       name, type(result).__name__)
            degraded.append(f"{name}失败，已使用其他可用分支")
            branches.append([])
        else:
            branches.append(result)
    if all(isinstance(x, BaseException) for x in results):
        raise AppError("retrieval_failed", "关键词和向量检索均不可用", 503)
    fused = rrf(branches, cfg.rrf_k)[:cfg.retrieval_limit]
    valid = await asyncio.to_thread(validate_candidates, kb_id, [identity for identity, _ in fused])
    candidates = [valid[identity] for identity, _ in fused if identity in valid]
    ranking: list[tuple[int, float | None]] = [(i, None) for i in range(len(candidates))]
    reranked: list[tuple[str, float | None]] = []
    if candidates:
        # Keep the total conservative rerank budget including the repeated query within the service limit.
        budgeted, budget = [], 0
        for candidate in candidates:
            cost = len(candidate.retrieval_text.encode()) + len(query.encode())
            if budget + cost > 120000:
                break
            budget += cost
            budgeted.append(candidate)
        candidates = budgeted
        ranking = [(i, None) for i in range(len(candidates))]
        try:
            ranking = [(i, score) for i, score in await provider.rerank(query, [c.retrieval_text for c in candidates])]
            reranked = [(candidates[i].id, score) for i, score in ranking]
        except AppError as exc:
            logging.getLogger("rag.retrieval").warning("reranking degraded code=%s", exc.code)
            degraded.append("重排序失败，已使用融合排序")
    output, budget = [], 0
    for index, score in ranking:
        chunk = candidates[index]
        if score is not None and cfg.relevance_threshold is not None and score < cfg.relevance_threshold:
            continue
        cost = len(chunk.retrieval_text.encode())
        if budget + cost > cfg.context_budget:
            continue
        output.append({"id": chunk.id, "document_id": chunk.document_id, "version_id": chunk.version_id,
                       "original": chunk.original, "retrieval_text": chunk.retrieval_text,
                       "provenance": chunk.provenance, "score": score})
        budget += cost
        if len(output) >= cfg.context_chunks:
            break
    final_valid = await asyncio.to_thread(validate_candidates, kb_id, [x["id"] for x in output])
    output = [x for x in output if x["id"] in final_valid]
    with session() as db:
        current = db.get(KnowledgeBase, kb_id)
        if current.index_config_id != config.id:
            raise AppError("index_changed", "检索期间索引发生切换，请重试", 409)
        for item in output:
            item["name"] = db.get(Document, item["document_id"]).name
    return {"chunks": output, "degraded": degraded,
            "debug": {"lexical": branches[0], "vector": branches[1], "fused": fused, "reranked": reranked}}


def sanitize_citations(answer: str, allowed: set[int]):
    return re.sub(r"\[(\d+)\]", lambda m: m.group(0) if int(m[1]) in allowed else "", answer)
