import asyncio
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import delete, func, select

from .chunking import chunk_elements
from .config import fingerprint, settings
from .db import session
from .errors import AppError
from .models import Chunk, Document, DocumentVersion, EmbeddingCache, IndexBuild, IndexConfig, KnowledgeBase
from .parsing import parse_isolated
from .providers import Providers
from .queue import check_lease, enqueue, phase
from .vectors import ensure_collection, put_chunks, remove_versions, vector_store


def words(value: str) -> str:
    import jieba
    import re
    cfg = settings()
    if cfg.lexical_dictionary:
        jieba.load_userdict(cfg.lexical_dictionary)
    return " ".join(word.lower() for word in jieba.cut_for_search(value) if re.fullmatch(r"[\w]+", word))


def cache_key(chunk, config):
    return fingerprint({"content": chunk.content_hash, "embedding": config.fingerprint})


async def vectors_for(chunks, config, provider=None):
    provider = provider or Providers()
    vectors, missing, missing_keys = {}, [], []
    with session() as db:
        for chunk in chunks:
            key = cache_key(chunk, config)
            entry = db.get(EmbeddingCache, key)
            if entry:
                vectors[chunk.id] = entry.vector
            else:
                missing.append(chunk)
                missing_keys.append(key)
    if missing:
        values = await provider.embed([c.retrieval_text for c in missing], config.config)
        with session() as db, db.begin():
            from sqlalchemy.dialects.postgresql import insert
            for chunk, key, vector in zip(missing, missing_keys, values, strict=True):
                db.execute(insert(EmbeddingCache).values(key=key, vector=vector)
                           .on_conflict_do_nothing(index_elements=[EmbeddingCache.key]))
                vectors[chunk.id] = vector
    return [vectors[c.id] for c in chunks]


def index_version(job_id: str, owner: str, payload: dict, provider=None, parser=None):
    version_id, config_id = payload["version_id"], payload["index_config_id"]
    phase(job_id, owner, "parsing", 10)
    with session() as db:
        version = db.get(DocumentVersion, version_id)
        if not version:
            raise AppError("version_missing", "待处理文档版本不存在", 409)
        document = db.get(Document, version.document_id)
        config = db.get(IndexConfig, config_id)
        if not document or document.deleted or document.generation != version.generation:
            raise AppError("version_superseded", "文件已更新或删除，旧任务不能发布", 409)
        name, snapshot = document.name, Path(version.snapshot_path)
        if version.status == "active" and document.active_version_id == version.id:
            return
    elements = (parser or parse_isolated)(snapshot, payload.get("force_ocr", False))
    phase(job_id, owner, "chunking", 35)
    maximum = settings().chunk_max
    for attempt in range(5):
        drafts = chunk_elements(elements, name, maximum)
        with session() as db, db.begin():
            check_lease(db, job_id, owner)
            version = db.get(DocumentVersion, version_id)
            version.elements = elements
            version.processing_hash = fingerprint({**settings().processing_config(),
                                                   "force_ocr": payload.get("force_ocr", False)})
            # Recreating the same version is safe: IDs are deterministic, no active version is overwritten.
            db.execute(delete(Chunk).where(Chunk.version_id == version_id))
            chunks = []
            for ordinal, draft in enumerate(drafts):
                chunk = Chunk(id=str(uuid5(NAMESPACE_URL, f"rag:{version_id}:{ordinal}:{draft.content_hash}")),
                              kb_id=config.kb_id, document_id=version.document_id, version_id=version_id,
                              ordinal=ordinal, original=draft.original, retrieval_text=draft.retrieval_text,
                              content_hash=draft.content_hash, provenance=draft.provenance,
                              search_vector=func.to_tsvector("simple", words(draft.retrieval_text)))
                db.add(chunk)
                chunks.append(chunk)
        phase(job_id, owner, "embedding", 55)
        try:
            vectors = asyncio.run(vectors_for(chunks, config, provider))
            break
        except AppError as exc:
            if exc.code != "input_too_long" or attempt == 4:
                raise
            maximum = max(100, maximum // 2)
            phase(job_id, owner, "input_limit_rechunking", 35)
    phase(job_id, owner, "vector_write", 75)
    # Clean possible points from a previous attempt with different adaptive chunk sizes.
    remove_versions(config, [version_id])
    put_chunks(config, chunks, vectors)
    phase(job_id, owner, "publishing", 90)
    with session() as db, db.begin():
        kb = db.scalar(select(KnowledgeBase).where(KnowledgeBase.id == config.kb_id).with_for_update())
        check_lease(db, job_id, owner)
        document = db.scalar(select(Document).where(Document.id == payload["document_id"]).with_for_update())
        version = db.get(DocumentVersion, version_id)
        if not kb or kb.status != "active" or not document or document.deleted or (
            document.generation != version.generation or kb.index_config_id != config_id
        ):
            raise AppError("version_superseded", "知识库索引或文件已变化，旧任务不能发布", 409)
        old_id = document.active_version_id
        document.active_version_id, document.status, document.error = version_id, "ready", ""
        version.status = "active"
        _record_build(db, config_id, version_id)
        if old_id and old_id != version_id:
            db.get(DocumentVersion, old_id).status = "obsolete"
        enqueue(db, kb.id, "cleanup_document", f"doc:{document.id}", {"document_id": document.id})


def _record_build(db, config_id, version_id):
    from sqlalchemy.dialects.postgresql import insert
    db.execute(insert(IndexBuild).values(index_config_id=config_id, version_id=version_id, state="ready")
               .on_conflict_do_nothing(index_elements=[IndexBuild.index_config_id, IndexBuild.version_id]))


def cleanup_document(job_id, owner, payload):
    with session() as db:
        document = db.get(Document, payload["document_id"])
        if not document:
            return
        versions = list(db.scalars(select(DocumentVersion).where(DocumentVersion.document_id == document.id)))
        removable = [v for v in versions if document.deleted or v.id != document.active_version_id
                     and (v.status == "obsolete" or v.generation < document.generation)]
        configs = list(db.scalars(select(IndexConfig).where(IndexConfig.kb_id == document.kb_id)))
        hashes = set(db.scalars(select(Chunk.content_hash).where(
            Chunk.version_id.in_([v.id for v in removable])))) if document.deleted else set()
    for config in configs:
        remove_versions(config, [v.id for v in removable])
    with session() as db, db.begin():
        check_lease(db, job_id, owner)
        doc = db.scalar(select(Document).where(Document.id == document.id).with_for_update())
        for version in removable:
            if not doc.deleted and version.id == doc.active_version_id:
                continue
            db.execute(delete(IndexBuild).where(IndexBuild.version_id == version.id))
            db.execute(delete(Chunk).where(Chunk.version_id == version.id))
            db.execute(delete(DocumentVersion).where(DocumentVersion.id == version.id))
            path = Path(version.snapshot_path).resolve()
            if path.is_relative_to(settings().storage_root.resolve()):
                path.unlink(missing_ok=True)
        if doc.deleted:
            doc.active_version_id, doc.status = None, "deleted"
            for content_hash in hashes:
                if not db.scalar(select(Chunk.id).where(Chunk.content_hash == content_hash).limit(1)):
                    for config in configs:
                        key = fingerprint({"content": content_hash, "embedding": config.fingerprint})
                        db.execute(delete(EmbeddingCache).where(EmbeddingCache.key == key))


def rebuild_index(job_id, owner, payload, provider=None):
    target_id = payload["index_config_id"]
    with session() as db:
        config = db.get(IndexConfig, target_id)
    ensure_collection(config)
    # Each pass includes documents published during the preceding pass; KB lock serializes the final cutover.
    for round_number in range(100):
        with session() as db:
            docs = list(db.scalars(select(Document).where(Document.kb_id == config.kb_id,
                                                         Document.deleted.is_(False),
                                                         Document.active_version_id.is_not(None))))
            missing = [d for d in docs if not db.scalar(select(IndexBuild.id).where(
                IndexBuild.index_config_id == config.id, IndexBuild.version_id == d.active_version_id))]
        for doc in missing:
            phase(job_id, owner, "rebuilding_vectors", min(90, 10 + round_number))
            with session() as db:
                chunks = list(db.scalars(select(Chunk).where(Chunk.version_id == doc.active_version_id)
                                         .order_by(Chunk.ordinal)))
            values = asyncio.run(vectors_for(chunks, config, provider))
            put_chunks(config, chunks, values)
            with session() as db, db.begin():
                check_lease(db, job_id, owner)
                _record_build(db, config.id, doc.active_version_id)
        with session() as db, db.begin():
            kb = db.scalar(select(KnowledgeBase).where(KnowledgeBase.id == config.kb_id).with_for_update())
            check_lease(db, job_id, owner)
            if kb.status != "active":
                raise AppError("kb_inactive", "知识库已停止索引重建", 409)
            uncovered = db.scalar(select(Document.id).where(
                Document.kb_id == kb.id, Document.deleted.is_(False), Document.active_version_id.is_not(None),
                ~select(IndexBuild.id).where(IndexBuild.version_id == Document.active_version_id,
                                            IndexBuild.index_config_id == config.id).exists(),
            ).limit(1))
            if uncovered:
                continue
            previous = kb.index_config_id
            kb.index_config_id = config.id
            db.get(IndexConfig, config.id).status = "active"
            if previous != config.id:
                db.get(IndexConfig, previous).status = "obsolete"
                enqueue(db, kb.id, "cleanup_index", f"kb:{kb.id}", {"index_config_id": previous})
            return
    raise AppError("rebuild_busy", "资料持续变化，索引切换未完成；请稍后重试", 409)


def cleanup_index(job_id, owner, payload):
    with session() as db:
        config = db.get(IndexConfig, payload["index_config_id"])
        kb = db.get(KnowledgeBase, config.kb_id)
        if kb.index_config_id == config.id:
            raise AppError("index_active", "不能清理生效索引", 409)
    if vector_store().collection_exists(config.collection):
        vector_store().delete_collection(config.collection)
    with session() as db, db.begin():
        check_lease(db, job_id, owner)
        db.get(IndexConfig, config.id).status = "cleaned"
        db.execute(delete(IndexBuild).where(IndexBuild.index_config_id == config.id))
