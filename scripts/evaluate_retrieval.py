"""Run a labeled retrieval set against an imported KB; uses configured external APIs."""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from sqlalchemy import select

from app.config import settings
from app.db import session
from app.errors import safe_error
from app.models import Chunk, Document
from app.providers import Providers
from app.retrieval import retrieve


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("kb_id")
    parser.add_argument("--dataset", default="fixtures/retrieval/questions.json")
    parser.add_argument("--output", default="data/retrieval-report.json")
    args = parser.parse_args()
    cfg, provider = settings(), Providers()
    provider.credentials("embedding")
    provider.credentials("rerank")
    rows = []
    for question in json.loads(Path(args.dataset).read_text(encoding="utf-8")):
        started = time.perf_counter()
        found = await retrieve(args.kb_id, question["query"])
        identities = {identity for branch in found["debug"].values() for identity, _ in branch}
        with session() as db:
            originals = {c.id: c.original for c in db.scalars(select(Chunk).where(Chunk.id.in_(identities)))}
            names = dict(db.execute(select(Chunk.id, Document.name).join(Document, Chunk.document_id == Document.id)
                                   .where(Chunk.id.in_(identities))).all())
        rows.append({**question, "degraded": found["degraded"], "branches": found["debug"],
                     "seconds": round(time.perf_counter() - started, 3), "candidate_originals": originals,
                     "candidate_names": names,
                     "selected": [{"id": c["id"], "name": c["name"], "score": c["score"], "original": c["original"]}
                                  for c in found["chunks"]]})
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"threshold": "uncalibrated; inspect irrelevant queries before enabling"
                                 if cfg.relevance_threshold is None else "configured cutoff; see separate calibration evidence",
                                 "models": {"embedding": cfg.embedding_model, "rerank": cfg.rerank_model},
                                 "parameters": {"retrieval_limit": cfg.retrieval_limit, "rrf_k": cfg.rrf_k,
                                                "context_chunks": cfg.context_chunks,
                                                "relevance_threshold": cfg.relevance_threshold},
                                 "questions": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(target)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        print(json.dumps({"status": "not_passed", "reason": safe_error(exc)}, ensure_ascii=False))
        sys.exit(1)
