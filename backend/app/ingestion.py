import hashlib
import os
from datetime import timedelta
from pathlib import Path, PurePosixPath

from sqlalchemy import select

from .config import fingerprint, settings
from .errors import AppError
from .models import DataSource, Document, DocumentVersion, IndexConfig, Job, KnowledgeBase, now, uid
from .queue import enqueue

SUPPORTED = {".pdf", ".doc", ".docx", ".txt"}


def relative_path(value: str) -> str:
    value = value.replace("\\", "/")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(p in (".", "..") for p in value.split("/")) or ":" in value:
        raise AppError("unsafe_path", "文件相对路径无效")
    if len(value) > 1000 or "\x00" in value:
        raise AppError("unsafe_path", "文件相对路径过长或包含无效字符")
    return str(path)


def source_directory(value: str) -> Path:
    root = settings().sources_root.resolve()
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if not path.is_relative_to(root):
        raise AppError("unsafe_directory", "目录必须位于允许的数据源挂载根内")
    return path


def file_hash(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def stable_snapshot(path: Path, extension: str) -> tuple[Path, str]:
    root = settings().storage_root.resolve() / "snapshots"
    root.mkdir(parents=True, exist_ok=True)
    destination = root / f"{uid()}{extension}"
    try:
        before = path.stat()
        if before.st_size > settings().max_upload_bytes:
            raise AppError("file_too_large", "文件超过大小限制")
        with path.open("rb") as source, destination.open("xb") as target:
            written = 0
            while block := source.read(1024 * 1024):
                written += len(block)
                if written > settings().max_upload_bytes:
                    raise AppError("file_too_large", "文件超过大小限制")
                target.write(block)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (
            after.st_size, after.st_mtime_ns, after.st_ino,
        ):
            raise AppError("unstable_file", "文件读取期间发生变化，请重试")
        return destination, file_hash(destination)
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def current_index(db, kb):
    if kb.index_config_id:
        return db.get(IndexConfig, kb.index_config_id)
    cfg = settings().embedding_config()
    index = IndexConfig(id=uid(), kb_id=kb.id, config=cfg, fingerprint=fingerprint(cfg),
                        collection=f"rag_{uid().replace('-', '')}", status="active")
    db.add(index)
    db.flush()
    kb.index_config_id = index.id
    return index


def ingest_snapshot(db, source: DataSource, relative: str, path: Path, content_hash: str,
                    force_ocr=False, force_reprocess=False):
    kb = db.scalar(select(KnowledgeBase).where(KnowledgeBase.id == source.kb_id).with_for_update())
    if not kb or kb.status != "active" or source.status != "active":
        path.unlink(missing_ok=True)
        raise AppError("source_inactive", "知识库或数据源已停止接入", 409)
    index = current_index(db, kb)
    processing = fingerprint({**settings().processing_config(), "force_ocr": force_ocr})
    doc = db.scalar(select(Document).where(
        Document.source_id == source.id, Document.relative_path == relative,
    ).with_for_update())
    added = not doc or doc.deleted
    if not doc:
        doc = Document(id=uid(), kb_id=kb.id, source_id=source.id, relative_path=relative,
                       name=PurePosixPath(relative).name, generation=0)
        db.add(doc)
        db.flush()
    active = db.get(DocumentVersion, doc.active_version_id) if doc.active_version_id else None
    if not force_reprocess and not doc.deleted and active and active.file_hash == content_hash and active.processing_hash == processing:
        path.unlink(missing_ok=True)
        return doc, "unchanged", None
    # Do not duplicate a pending version with exactly the same processing input.
    pending = db.scalar(select(DocumentVersion).where(
        DocumentVersion.document_id == doc.id, DocumentVersion.file_hash == content_hash,
        DocumentVersion.processing_hash == processing, DocumentVersion.generation == doc.generation,
        DocumentVersion.status == "building",
    ))
    if pending and not doc.deleted:
        path.unlink(missing_ok=True)
        job = db.scalar(select(Job).where(Job.dedup_key == f"index:{pending.id}:{index.id}"))
        return doc, "unchanged", job
    doc.generation += 1
    doc.deleted, doc.status, doc.error = False, "queued", ""
    version = DocumentVersion(id=uid(), document_id=doc.id, file_hash=content_hash,
                              processing_hash=processing, snapshot_path=str(path), generation=doc.generation)
    db.add(version)
    db.flush()
    job = enqueue(db, kb.id, "index", f"doc:{doc.id}", {
        "document_id": doc.id, "version_id": version.id, "index_config_id": index.id,
        "force_ocr": force_ocr,
    }, f"index:{version.id}:{index.id}")
    return doc, "added" if added else "modified", job


def enumerate_directory(root: Path) -> dict[str, Path]:
    if not root.is_dir():
        raise AppError("directory_unavailable", "数据源目录不存在或不可访问", 503)
    files: dict[str, Path] = {}
    visited = set()

    def walk(directory):
        resolved = directory.resolve()
        if not resolved.is_relative_to(root.resolve()) or resolved in visited:
            raise AppError("unsafe_symlink", "目录存在越界或循环符号链接，本轮不执行删除")
        visited.add(resolved)
        # scandir errors propagate: a partially traversed tree is never treated as a complete scan.
        with os.scandir(directory) as entries:
            for entry in entries:
                candidate = Path(entry.path)
                if not candidate.resolve().is_relative_to(root.resolve()):
                    raise AppError("unsafe_symlink", "文件链接越过数据源目录，本轮不执行删除")
                if entry.is_dir(follow_symlinks=True):
                    walk(candidate)
                elif entry.is_file(follow_symlinks=True) and candidate.suffix.lower() in SUPPORTED:
                    files[relative_path(candidate.relative_to(root).as_posix())] = candidate
    try:
        walk(root)
    except OSError:
        raise AppError("scan_incomplete", "目录或子目录无法完整枚举，请检查挂载、权限和文件链接；本轮不执行删除", 503) from None
    return files


def mark_deleted(db, document):
    document.deleted = True
    document.generation += 1
    document.status = "deleting"
    enqueue(db, document.kb_id, "cleanup_document", f"doc:{document.id}", {"document_id": document.id})


def enqueue_sync(db, source_id, dedup_key=None):
    source = db.scalar(select(DataSource).where(DataSource.id == source_id).with_for_update())
    active = db.scalar(select(Job).where(Job.resource == f"source:{source.id}", Job.kind == "sync",
                                        Job.status.in_(["queued", "running"])))
    return active or enqueue(db, source.kb_id, "sync", f"source:{source.id}",
                             {"source_id": source.id}, dedup_key)


def schedule_due(db):
    due = list(db.execute(select(DataSource.id, DataSource.kb_id).join(KnowledgeBase).where(
        DataSource.kind == "directory", DataSource.status == "active", KnowledgeBase.status == "active",
        DataSource.interval_seconds > 0, DataSource.next_scan_at <= now(),
    ).limit(100)))
    for source_id, kb_id in due:
        # Always lock KB before source, matching deletion and manual sync.
        kb = db.scalar(select(KnowledgeBase).where(KnowledgeBase.id == kb_id)
                       .with_for_update(skip_locked=True).execution_options(populate_existing=True))
        if not kb or kb.status != "active":
            continue
        source = db.scalar(select(DataSource).where(DataSource.id == source_id)
                           .with_for_update(skip_locked=True).execution_options(populate_existing=True))
        if not source or source.status != "active" or not source.next_scan_at or source.next_scan_at > now():
            continue
        enqueue_sync(db, source.id, f"scheduled:{source.id}:{source.next_scan_at.isoformat()}")
        source.next_scan_at = now() + timedelta(seconds=source.interval_seconds)
