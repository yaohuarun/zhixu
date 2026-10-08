import hashlib
from datetime import timedelta
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .errors import AppError, safe_error
from .ingestion import (SUPPORTED, current_index, enqueue_sync, ingest_snapshot, mark_deleted,
                        relative_path, source_directory)
from .models import Chunk, DataSource, Document, Job, JobItem, KnowledgeBase, now, uid
from .queue import enqueue

router = APIRouter(prefix="/api")


def serialize(item):
    return {column.name: getattr(item, column.name) for column in item.__table__.columns
            if column.name not in ("search_vector", "owner", "lease_until")}


def require(db, model, identity):
    item = db.get(model, identity)
    if not item:
        raise AppError("not_found", "记录不存在", 404)
    return item


def active_kb(db, identity, lock=False):
    kb = db.scalar(select(KnowledgeBase).where(KnowledgeBase.id == identity).with_for_update()
                   .execution_options(populate_existing=True)) if lock else require(db, KnowledgeBase, identity)
    if not kb:
        raise AppError("not_found", "知识库不存在", 404)
    if kb.status != "active":
        raise AppError("kb_inactive", "知识库正在删除或已删除", 409)
    return kb


class KBInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=5000)


class SourceInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["upload", "directory"]
    path: str = Field(default="", max_length=1000)
    interval_seconds: int = Field(default=0, ge=0, le=31536000)

    @model_validator(mode="after")
    def validate_kind(self):
        if self.kind == "directory" and not self.path:
            raise ValueError("directory requires path")
        return self


@router.get("/knowledge-bases")
def list_kbs(db: Session = Depends(get_db)):
    return [serialize(kb) for kb in db.scalars(select(KnowledgeBase).where(
        KnowledgeBase.status != "deleted").order_by(KnowledgeBase.created_at.desc()))]


@router.post("/knowledge-bases", status_code=201)
def create_kb(data: KBInput, db: Session = Depends(get_db)):
    kb = KnowledgeBase(id=uid(), **data.model_dump())
    db.add(kb)
    db.flush()
    current_index(db, kb)
    db.commit()
    return serialize(kb)


@router.patch("/knowledge-bases/{kb_id}")
def update_kb(kb_id: str, data: KBInput, db: Session = Depends(get_db)):
    kb = active_kb(db, kb_id, lock=True)
    kb.name, kb.description = data.name, data.description
    db.commit()
    return serialize(kb)


@router.delete("/knowledge-bases/{kb_id}", status_code=202)
def delete_kb(kb_id: str, db: Session = Depends(get_db)):
    kb = db.scalar(select(KnowledgeBase).where(KnowledgeBase.id == kb_id).with_for_update())
    if not kb:
        raise AppError("not_found", "知识库不存在", 404)
    if kb.status in ("deleted", "deleting"):
        existing = db.scalar(select(Job).where(Job.kb_id == kb.id, Job.kind == "delete_kb")
                             .order_by(Job.created_at.desc()).limit(1))
        return serialize(existing) if existing else {"status": kb.status}
    kb.status = "deleting"
    for source in db.scalars(select(DataSource).where(DataSource.kb_id == kb.id)):
        source.status = "deleted"
    for old in db.scalars(select(Job).where(Job.kb_id == kb.id, Job.status.in_(["queued", "running"]))):
        old.status, old.owner = "cancelled", None
    db.flush()
    for doc in db.scalars(select(Document).where(Document.kb_id == kb.id)):
        doc.deleted = True
        doc.generation += 1
    job = enqueue(db, kb.id, "delete_kb", f"kb:{kb.id}", {})
    db.commit()
    return serialize(job)


@router.get("/knowledge-bases/{kb_id}/sources")
def list_sources(kb_id: str, db: Session = Depends(get_db)):
    active_kb(db, kb_id)
    return [serialize(x) for x in db.scalars(select(DataSource).where(
        DataSource.kb_id == kb_id, DataSource.status == "active"))]


@router.post("/knowledge-bases/{kb_id}/sources", status_code=201)
def create_source(kb_id: str, data: SourceInput, db: Session = Depends(get_db)):
    active_kb(db, kb_id, lock=True)
    values = data.model_dump()
    if data.kind == "directory":
        values["path"] = str(source_directory(data.path))
    source = DataSource(id=uid(), kb_id=kb_id, **values,
                        next_scan_at=now() + timedelta(seconds=data.interval_seconds) if data.interval_seconds else None)
    db.add(source)
    db.commit()
    return serialize(source)


@router.patch("/data-sources/{source_id}")
def update_source(source_id: str, data: SourceInput, db: Session = Depends(get_db)):
    source = require(db, DataSource, source_id)
    active_kb(db, source.kb_id, lock=True)
    if data.kind != source.kind or (source.kind == "directory" and str(source_directory(data.path)) != source.path):
        raise AppError("immutable_source", "数据源类型与根路径不可修改，请添加新数据源")
    source.name, source.interval_seconds = data.name, data.interval_seconds
    source.next_scan_at = now() + timedelta(seconds=data.interval_seconds) if data.interval_seconds else None
    db.commit()
    return serialize(source)


@router.post("/data-sources/{source_id}/sync", status_code=202)
def sync_source(source_id: str, db: Session = Depends(get_db)):
    source = require(db, DataSource, source_id)
    active_kb(db, source.kb_id, lock=True)
    if source.kind != "directory" or source.status != "active":
        raise AppError("invalid_source", "只有活动服务器目录数据源可以同步")
    job = enqueue_sync(db, source.id)
    db.commit()
    return serialize(job)


@router.post("/data-sources/{source_id}/upload", status_code=202)
async def upload(source_id: str, files: list[UploadFile] = File(...),
                 paths: list[str] = Form(default=[]), db: Session = Depends(get_db)):
    source = require(db, DataSource, source_id)
    active_kb(db, source.kb_id)
    if source.kind != "upload" or source.status != "active":
        raise AppError("invalid_source", "请选择活动上传数据源")
    if paths and len(paths) != len(files):
        raise AppError("invalid_paths", "路径数量必须与文件数量相同")
    results = []
    for index, file in enumerate(files):
        destination = None
        try:
            relative = relative_path(paths[index] if paths else file.filename or "")
            extension = Path(relative).suffix.lower()
            if extension not in SUPPORTED:
                raise AppError("unsupported_format", "仅支持 PDF、DOC、DOCX、TXT")
            root = settings().storage_root.resolve() / "snapshots"
            root.mkdir(parents=True, exist_ok=True)
            destination = root / f"{uid()}{extension}"
            size, digest = 0, hashlib.sha256()
            with destination.open("xb") as output:
                while data := await file.read(1024 * 1024):
                    size += len(data)
                    if size > settings().max_upload_bytes:
                        raise AppError("file_too_large", "文件超过大小限制")
                    output.write(data)
                    digest.update(data)
            doc, state, job = ingest_snapshot(db, source, relative, destination, digest.hexdigest())
            db.commit()
            results.append({"path": relative, "status": state, "document_id": doc.id,
                            "job_id": job.id if job else None})
        except Exception as exc:
            db.rollback()
            if destination:
                destination.unlink(missing_ok=True)
            results.append({"path": file.filename or "", "status": "rejected", "error": safe_error(exc)})
        finally:
            await file.close()
    return {"files": results}


@router.get("/knowledge-bases/{kb_id}/documents")
def list_documents(kb_id: str, db: Session = Depends(get_db)):
    active_kb(db, kb_id)
    return [serialize(x) for x in db.scalars(select(Document).where(
        Document.kb_id == kb_id, Document.deleted.is_(False)).order_by(Document.relative_path))]


@router.delete("/documents/{doc_id}", status_code=202)
def delete_document(doc_id: str, db: Session = Depends(get_db)):
    doc = require(db, Document, doc_id)
    active_kb(db, doc.kb_id, lock=True)
    doc = db.scalar(select(Document).where(Document.id == doc_id).with_for_update()
                    .execution_options(populate_existing=True))
    if doc.deleted:
        return {"status": doc.status}
    mark_deleted(db, doc)
    db.commit()
    return {"status": "deleting"}


@router.post("/documents/{doc_id}/reprocess", status_code=202)
def reprocess(doc_id: str, force_ocr: bool = False, db: Session = Depends(get_db)):
    from .ingestion import stable_snapshot
    from .models import DocumentVersion
    doc = require(db, Document, doc_id)
    active_kb(db, doc.kb_id)
    if doc.deleted or not doc.active_version_id:
        raise AppError("no_active_version", "没有可重新处理的生效版本")
    version = require(db, DocumentVersion, doc.active_version_id)
    snapshot, digest = stable_snapshot(Path(version.snapshot_path), Path(doc.name).suffix.lower())
    _, _, job = ingest_snapshot(db, require(db, DataSource, doc.source_id), doc.relative_path,
                               snapshot, digest, force_ocr, force_reprocess=True)
    db.commit()
    return serialize(job)


@router.get("/documents/{doc_id}/chunks")
def document_chunks(doc_id: str, db: Session = Depends(get_db)):
    doc = require(db, Document, doc_id)
    if doc.deleted:
        raise AppError("document_deleted", "文档已删除", 404)
    return [serialize(c) for c in db.scalars(select(Chunk).where(
        Chunk.version_id == doc.active_version_id).order_by(Chunk.ordinal))]


@router.get("/knowledge-bases/{kb_id}/jobs")
def list_jobs(kb_id: str, db: Session = Depends(get_db)):
    return [serialize(j) for j in db.scalars(select(Job).where(Job.kb_id == kb_id)
                                           .order_by(Job.created_at.desc()).limit(100))]


@router.get("/jobs/{job_id}/items")
def job_items(job_id: str, db: Session = Depends(get_db)):
    require(db, Job, job_id)
    return [serialize(i) for i in db.scalars(select(JobItem).where(JobItem.job_id == job_id))]


@router.post("/jobs/{job_id}/retry", status_code=202)
def retry_job(job_id: str, db: Session = Depends(get_db)):
    job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
    if not job:
        raise AppError("not_found", "任务不存在", 404)
    if job.status != "failed":
        raise AppError("not_retryable", "仅失败任务可重试", 409)
    if job.kind != "delete_kb":
        active_kb(db, job.kb_id)
    job.status, job.error, job.attempts, job.owner = "queued", "", 0, None
    job.finished_at = None
    db.commit()
    return serialize(job)


@router.get("/system")
def system_config():
    cfg = settings()
    return {"sources_root": str(cfg.sources_root), "max_upload_bytes": cfg.max_upload_bytes,
            "models": {kind: {"configured": bool(getattr(cfg, f"{kind}_key").get_secret_value()
                       and getattr(cfg, f"{kind}_url") and getattr(cfg, f"{kind}_model")),
                       "model": getattr(cfg, f"{kind}_model")} for kind in ("deepseek", "embedding", "rerank")},
            "chunking": cfg.processing_config()}


@router.post("/knowledge-bases/{kb_id}/rebuild", status_code=202)
def rebuild(kb_id: str, db: Session = Depends(get_db)):
    from .config import fingerprint
    from .models import IndexConfig
    kb = active_kb(db, kb_id, lock=True)
    pending = db.scalar(select(Job).where(Job.kb_id == kb_id, Job.kind == "rebuild",
                                         Job.status.in_(["queued", "running"])))
    if pending:
        return serialize(pending)
    cfg = settings().embedding_config()
    index = IndexConfig(id=uid(), kb_id=kb.id, config=cfg, fingerprint=fingerprint(cfg),
                        collection=f"rag_{uid().replace('-', '')}", status="building")
    db.add(index)
    db.flush()
    job = enqueue(db, kb.id, "rebuild", f"kb:{kb.id}", {"index_config_id": index.id})
    db.commit()
    return serialize(job)
