import logging
import threading
import time
from collections.abc import Callable

from sqlalchemy import delete, select, true

from .config import settings
from .db import session
from .errors import AppError, safe_error
from .indexing import cleanup_document, cleanup_index, index_version, rebuild_index
from .ingestion import enumerate_directory, ingest_snapshot, mark_deleted, schedule_due, stable_snapshot
from .models import DataSource, Document, IndexConfig, Job, JobItem, KnowledgeBase, now, uid
from .queue import check_lease, claim, enqueue, finish, heartbeat, phase
from .vectors import vector_store

log = logging.getLogger("rag.worker")


def sync_directory(job_id, owner, payload):
    from .ingestion import source_directory
    with session() as db:
        source = db.get(DataSource, payload["source_id"])
        if not source or source.status != "active":
            raise AppError("source_inactive", "数据源不可用", 409)
        path = source_directory(source.path)
    phase(job_id, owner, "scanning", 5)
    files = enumerate_directory(path)  # Nothing gets deleted if this raises.
    counts = {"added": 0, "modified": 0, "unchanged": 0, "deleted": 0, "failed": 0}
    with session() as db, db.begin():
        check_lease(db, job_id, owner)
        db.execute(delete(JobItem).where(JobItem.job_id == job_id))
    for ordinal, (relative, input_path) in enumerate(files.items()):
        phase(job_id, owner, "snapshotting", 10 + int(70 * ordinal / max(len(files), 1)))
        snapshot = None
        try:
            if not input_path.resolve().is_relative_to(path.resolve()):
                raise AppError("unsafe_symlink", "快照前发现越界文件链接")
            snapshot, digest = stable_snapshot(input_path, input_path.suffix.lower())
            with session() as db, db.begin():
                db.scalar(select(KnowledgeBase).where(KnowledgeBase.id == source.kb_id).with_for_update())
                check_lease(db, job_id, owner)
                _, state, _ = ingest_snapshot(db, db.get(DataSource, source.id), relative, snapshot, digest)
                counts[state] += 1
                db.add(JobItem(job_id=job_id, path=relative, status=state))
        except Exception as exc:
            if snapshot:
                snapshot.unlink(missing_ok=True)
            counts["failed"] += 1
            with session() as db, db.begin():
                check_lease(db, job_id, owner)
                db.add(JobItem(job_id=job_id, path=relative, status="failed", error=safe_error(exc)))
    with session() as db, db.begin():
        kb = db.scalar(select(KnowledgeBase).where(KnowledgeBase.id == source.kb_id).with_for_update())
        check_lease(db, job_id, owner)
        if kb.status != "active":
            raise AppError("kb_inactive", "知识库正在删除", 409)
        current = db.get(DataSource, source.id)
        if current.status != "active":
            raise AppError("source_inactive", "数据源不可用", 409)
        for doc in db.scalars(select(Document).where(Document.source_id == source.id, Document.deleted.is_(False))):
            if doc.relative_path not in files:
                mark_deleted(db, doc)
                counts["deleted"] += 1
        current.scan_complete, current.last_scan_at = True, now()
        db.get(Job, job_id).counts = counts
    if counts["failed"]:
        raise AppError("partial_sync", "目录扫描完整，但部分文件接入失败；查看文件级错误后重试", 422)


def delete_kb(job_id, owner, payload):
    with session() as db:
        kb_id = db.get(Job, job_id).kb_id
        configs = list(db.scalars(select(IndexConfig).where(IndexConfig.kb_id == kb_id)))
        docs = list(db.scalars(select(Document).where(Document.kb_id == kb_id)))
    for doc in docs:
        cleanup_document(job_id, owner, {"document_id": doc.id})
    for config in configs:
        if vector_store().collection_exists(config.collection):
            vector_store().delete_collection(config.collection)
    with session() as db, db.begin():
        check_lease(db, job_id, owner)
        kb = db.get(KnowledgeBase, kb_id)
        kb.status, kb.description = "deleted", ""
        # Tombstones and task audit remain for retry/idempotency; original files, chunks and vectors are gone.
        for config in db.scalars(select(IndexConfig).where(IndexConfig.kb_id == kb_id)):
            config.status, config.config = "cleaned", {}
        for source in db.scalars(select(DataSource).where(DataSource.kb_id == kb_id)):
            source.name, source.path, source.status = "", "", "deleted"
        for document in db.scalars(select(Document).where(Document.kb_id == kb_id)):
            document.name, document.relative_path, document.error = "", f"deleted:{document.id}", ""


HANDLERS: dict[str, Callable] = {"sync": sync_directory, "index": index_version, "cleanup_document": cleanup_document,
            "rebuild": rebuild_index, "cleanup_index": cleanup_index, "delete_kb": delete_kb}


def recover_orphans():
    from .models import DocumentVersion
    with session() as db, db.begin():
        for document in db.scalars(select(Document).join(KnowledgeBase).where(
            KnowledgeBase.status == "active",
        )):
            obsolete = db.scalar(select(DocumentVersion.id).where(
                DocumentVersion.document_id == document.id,
                DocumentVersion.generation < document.generation,
                DocumentVersion.id != document.active_version_id if document.active_version_id else true(),
            ).limit(1))
            if document.deleted or obsolete:
                active = db.scalar(select(Job.id).where(Job.resource == f"doc:{document.id}",
                                                       Job.kind == "cleanup_document",
                                                       Job.status.in_(["queued", "running"])))
                if not active:
                    enqueue(db, document.kb_id, "cleanup_document", f"doc:{document.id}",
                            {"document_id": document.id})
    reconcile_vectors()


def reconcile_vectors():
    """Remove points whose SQL chunk disappeared, including interrupted rebuild writes."""
    from qdrant_client import models as qm
    from .models import Chunk
    with session() as db:
        configs = list(db.scalars(select(IndexConfig)))
    store = vector_store()
    for config in configs:
        if not store.collection_exists(config.collection):
            continue
        if config.status == "cleaned":
            store.delete_collection(config.collection)
            continue
        offset = None
        while True:
            points, offset = store.scroll(config.collection, limit=256, offset=offset,
                                          with_payload=False, with_vectors=False)
            identities = [str(point.id) for point in points]
            with session() as db:
                existing = set(db.scalars(select(Chunk.id).where(Chunk.id.in_(identities))))
            missing = [identity for identity in identities if identity not in existing]
            if missing:
                store.delete(config.collection, qm.PointIdsList(points=missing), wait=True)
            if offset is None:
                break


def run_one(job_id, owner):
    stop = threading.Event()

    def keep_alive():
        while not stop.wait(settings().lease_seconds / 3):
            try:
                if not heartbeat(job_id, owner):
                    return
            except Exception:
                log.warning("heartbeat failed job=%s", job_id)
    thread = threading.Thread(target=keep_alive, daemon=True)
    thread.start()
    try:
        with session() as db:
            job = db.get(Job, job_id)
            payload, kind = job.payload, job.kind
        HANDLERS[kind](job_id, owner, payload)
        finish(job_id, owner)
    except Exception as exc:
        error = safe_error(exc)
        log.warning("job failed id=%s reason=%s", job_id, error)
        if kind == "index":
            with session() as db, db.begin():
                document = db.get(Document, payload["document_id"])
                current_job = db.get(Job, job_id)
                from .models import DocumentVersion
                version = db.get(DocumentVersion, payload["version_id"])
                if (current_job and current_job.owner == owner and current_job.status == "running"
                        and document and not document.deleted and version
                        and document.generation == version.generation):
                    document.error = error
                    document.status = "ready" if document.active_version_id else "failed"
        if kind == "sync":
            with session() as db, db.begin():
                source = db.get(DataSource, payload["source_id"])
                if source and getattr(exc, "code", "") != "partial_sync":
                    source.scan_complete = False
        finish(job_id, owner, error)
    finally:
        stop.set()
        thread.join(timeout=5)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    owner, last_recovery = uid(), 0.0
    while True:
        try:
            with session() as db, db.begin():
                schedule_due(db)
            if time.monotonic() - last_recovery > 60:
                recover_orphans()
                last_recovery = time.monotonic()
            identity = claim(owner)
            if identity:
                run_one(identity, owner)
            else:
                time.sleep(settings().worker_poll)
        except KeyboardInterrupt:
            return
        except Exception as exc:
            log.warning("worker cycle failed reason=%s", safe_error(exc))
            time.sleep(settings().worker_poll)


if __name__ == "__main__":
    main()
