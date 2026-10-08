from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def uid() -> str:
    return str(uuid4())


def now():
    return datetime.now(timezone.utc)


class Entity:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class KnowledgeBase(Entity, Base):
    __tablename__ = "knowledge_bases"
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="active")
    index_config_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class IndexConfig(Entity, Base):
    __tablename__ = "index_configs"
    kb_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    config: Mapped[dict] = mapped_column(JSON)
    fingerprint: Mapped[str] = mapped_column(String(64))
    collection: Mapped[str] = mapped_column(String(100), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="building")


class DataSource(Entity, Base):
    __tablename__ = "data_sources"
    kb_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20))
    path: Mapped[str] = mapped_column(Text, default="")
    interval_seconds: Mapped[int] = mapped_column(Integer, default=0)
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scan_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="active")


class Document(Entity, Base):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("source_id", "relative_path"),)
    kb_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("data_sources.id"), index=True)
    relative_path: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(String(500))
    active_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    error: Mapped[str] = mapped_column(Text, default="")


class DocumentVersion(Entity, Base):
    __tablename__ = "document_versions"
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    file_hash: Mapped[str] = mapped_column(String(64))
    processing_hash: Mapped[str] = mapped_column(String(64))
    snapshot_path: Mapped[str] = mapped_column(Text)
    generation: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="building")
    elements: Mapped[list] = mapped_column(JSON, default=list)


class Chunk(Entity, Base):
    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("version_id", "ordinal"),
        Index("ix_chunks_search_vector", "search_vector", postgresql_using="gin"),
    )
    kb_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    original: Mapped[str] = mapped_column(Text)
    retrieval_text: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    provenance: Mapped[dict] = mapped_column(JSON)
    search_vector: Mapped[str | None] = mapped_column(TSVECTOR, nullable=True)


class EmbeddingCache(Base):
    __tablename__ = "embedding_cache"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    vector: Mapped[list] = mapped_column(JSON)


class IndexBuild(Entity, Base):
    __tablename__ = "index_builds"
    __table_args__ = (UniqueConstraint("index_config_id", "version_id"),)
    index_config_id: Mapped[str] = mapped_column(ForeignKey("index_configs.id"), index=True)
    version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id"), index=True)
    state: Mapped[str] = mapped_column(String(20), default="ready")


class Job(Entity, Base):
    __tablename__ = "jobs"
    kb_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    resource: Mapped[str] = mapped_column(String(100), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    dedup_key: Mapped[str] = mapped_column(String(200), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    phase: Mapped[str] = mapped_column(String(50), default="queued")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    counts: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    owner: Mapped[str | None] = mapped_column(String(36), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class JobItem(Entity, Base):
    __tablename__ = "job_items"
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    path: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20))
    error: Mapped[str] = mapped_column(Text, default="")


class Conversation(Entity, Base):
    __tablename__ = "conversations"
    title: Mapped[str] = mapped_column(String(200), default="新对话")
    active_request: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Message(Entity, Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("conversation_id", "request_key", "role"),)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    request_key: Mapped[str] = mapped_column(String(100))
    kb_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="complete")
    meta: Mapped[dict] = mapped_column(JSON, default=dict)


class Citation(Entity, Base):
    __tablename__ = "citations"
    message_id: Mapped[str] = mapped_column(ForeignKey("messages.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    chunk_id: Mapped[str] = mapped_column(String(36))
    document_id: Mapped[str] = mapped_column(String(36))
    version_id: Mapped[str] = mapped_column(String(36))
    name: Mapped[str] = mapped_column(Text)
    original: Mapped[str] = mapped_column(Text)
    provenance: Mapped[dict] = mapped_column(JSON)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
