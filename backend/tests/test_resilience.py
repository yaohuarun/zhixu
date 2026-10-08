import asyncio
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import fitz
import pytest
from sqlalchemy import select

from app.config import settings
from app.db import session
from app.errors import AppError
from app.indexing import cleanup_document, index_version
from app.ingestion import schedule_due
from app.models import Chunk, DataSource, Document, DocumentVersion, IndexConfig, Job, now, uid
from app.parsing import parse_direct, parse_isolated
from app.queue import finish
from app.retrieval import retrieve
from test_integration import DeterministicProvider, running, upload_and_index


def test_pdf_columns_edges_encryption_and_limits(tmp_path, monkeypatch):
    pdf = fitz.open()
    for i in range(3):
        page = pdf.new_page()
        page.insert_text((70, 30), "Repeated manual header")
        for pos, text in [((70, 90), "Left first"), ((70, 140), "Left second"),
                          ((330, 90), "Right first"), ((330, 140), "Right second")]:
            page.insert_text(pos, text)
    path = tmp_path / "columns.pdf"
    pdf.save(path)
    encrypted = tmp_path / "encrypted.pdf"
    pdf.save(encrypted, encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="owner")
    pdf.close()
    elements = parse_direct(path)
    assert [e["text"] for e in elements if e["page"] == 1 and not e.get("excluded")] == [
        "Left first", "Left second", "Right first", "Right second"]
    assert sum(e.get("excluded", False) for e in elements) == 3
    assert all(e["bbox"] and e["page"] for e in elements)
    with pytest.raises(AppError, match="加密"):
        parse_direct(encrypted)
    monkeypatch.setattr(settings(), "max_pdf_pages", 2)
    with pytest.raises(AppError, match="页数"):
        parse_direct(path)


def test_parse_timeout_cleans_temporary_directory(tmp_path, monkeypatch):
    file = tmp_path / "a.txt"
    file.write_text("data")
    outputs = []
    class TimedOut:
        pid = 12345678
        def __init__(self, command, **kwargs):
            outputs.append(command[4])
        def communicate(self, timeout=None):
            if timeout:
                raise subprocess.TimeoutExpired("parser", 1)
    monkeypatch.setattr("app.parsing.subprocess.Popen", TimedOut)
    monkeypatch.setattr("app.parsing.subprocess.run", lambda *a, **k: None)
    monkeypatch.setattr("app.parsing.os.killpg", lambda *a: None, raising=False)
    with pytest.raises(AppError, match="超时"):
        parse_isolated(file)
    from pathlib import Path
    assert not Path(outputs[0]).parent.exists()


@pytest.mark.integration
def test_scheduler_and_manual_requests_share_one_pending_job(workspace):
    client, kb, _ = workspace
    root = settings().sources_root
    source = client.post(f"/api/knowledge-bases/{kb['id']}/sources", json={
        "name": "scheduled", "kind": "directory", "path": str(root), "interval_seconds": 60}).json()
    with session() as db, db.begin():
        db.get(DataSource, source["id"]).next_scan_at = now() - timedelta(seconds=1)
    def tick(_):
        with session() as db, db.begin():
            schedule_due(db)
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(tick, range(2)))
    requests = [client.post(f"/api/data-sources/{source['id']}/sync").json()["id"] for _ in range(3)]
    with session() as db:
        jobs = list(db.scalars(select(Job).where(Job.resource == f"source:{source['id']}")))
        assert len(jobs) == 1 and set(requests) == {jobs[0].id}


@pytest.mark.integration
def test_fault_after_vector_write_before_publish_recovers(workspace, monkeypatch):
    client, kb, source = workspace
    provider = DeterministicProvider()
    first = upload_and_index(client, source, "旧版本仍然可用。" + uid(), provider)
    changed = client.post(f"/api/data-sources/{source['id']}/upload", files={
        "files": ("guide.txt", ("新版本故障恢复。" + uid()).encode())},
        data={"paths": "docs/guide.txt"}).json()["files"][0]
    payload = running(changed["job_id"])
    from app.indexing import phase
    def fault(job, owner, name, progress):
        if name == "publishing":
            raise RuntimeError("simulated process crash")
        return phase(job, owner, name, progress)
    with monkeypatch.context() as patch:
        patch.setattr("app.indexing.phase", fault)
        with pytest.raises(RuntimeError):
            index_version(changed["job_id"], "test-owner", payload, provider)
    found = asyncio.run(retrieve(kb["id"], "旧版本", provider))
    assert found["chunks"] and all(c["version_id"] != payload["version_id"] for c in found["chunks"])
    index_version(changed["job_id"], "test-owner", payload, provider)
    index_version(changed["job_id"], "test-owner", payload, provider)
    with session() as db:
        assert db.get(Document, first["document_id"]).active_version_id == payload["version_id"]
    finish(changed["job_id"], "test-owner")
    jobs = client.get(f"/api/knowledge-bases/{kb['id']}/jobs").json()
    cleanup = next(j for j in jobs if j["kind"] == "cleanup_document")
    cleanup_payload = running(cleanup["id"])
    with monkeypatch.context() as patch:
        def unavailable(*args):
            raise RuntimeError("cleanup storage failure")
        patch.setattr("app.indexing.remove_versions", unavailable)
        with pytest.raises(RuntimeError):
            cleanup_document(cleanup["id"], "test-owner", cleanup_payload)
    cleanup_document(cleanup["id"], "test-owner", cleanup_payload)
    with session() as db:
        versions = list(db.scalars(select(DocumentVersion).where(DocumentVersion.document_id == first["document_id"])))
        assert [v.id for v in versions] == [payload["version_id"]]
        config = db.get(IndexConfig, kb["index_config_id"])
        chunk = db.scalar(select(Chunk).where(Chunk.version_id == payload["version_id"]))
        valid_id = chunk.id
    from qdrant_client import models as qm
    from app.vectors import vector_store
    from app.worker import reconcile_vectors
    orphan_id = uid()
    store = vector_store()
    store.upsert(config.collection, [qm.PointStruct(id=orphan_id, vector=[0.1] * config.config["dimension"])], wait=True)
    reconcile_vectors()
    assert not store.retrieve(config.collection, [orphan_id])
    assert store.retrieve(config.collection, [valid_id])
    finish(cleanup["id"], "test-owner")


@pytest.mark.integration
def test_model_length_rechunk_and_degraded_retrieval(workspace):
    client, kb, source = workspace
    class Limited(DeterministicProvider):
        async def embed(self, texts, config, query=False):
            if any(len(text.encode()) > 400 for text in texts):
                raise AppError("input_too_long", "模型长度限制")
            return await super().embed(texts, config, query)
    first = upload_and_index(client, source, "中文长文档的增量更新。" * 100 + uid(), Limited())
    fragments = client.get(f"/api/documents/{first['document_id']}/chunks").json()
    assert len(fragments) > 5
    class Down(DeterministicProvider):
        async def embed(self, *args, **kwargs):
            raise AppError("upstream_failed", "向量模型不可用")
        async def rerank(self, *args, **kwargs):
            raise AppError("upstream_failed", "重排模型不可用")
    found = asyncio.run(retrieve(kb["id"], "增量更新", Down()))
    assert found["chunks"] and len(found["degraded"]) == 2
