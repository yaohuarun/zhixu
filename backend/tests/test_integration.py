import asyncio
import hashlib
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.db import session
from app.errors import AppError
from app.indexing import index_version, rebuild_index
from app.ingestion import mark_deleted
from app.main import app
from app.models import Document, IndexConfig, Job, KnowledgeBase, now, uid
from app.queue import finish
from app.retrieval import retrieve
from app.worker import sync_directory

pytestmark = pytest.mark.integration


class DeterministicProvider:
    """Contract-only provider; never evidence of real external API quality."""
    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    async def embed(self, texts, config, query=False):
        if self.fail:
            raise AppError("test_failure", "模拟 Embedding 故障", 503)
        self.calls += len(texts)
        output = []
        for text in texts:
            digest = hashlib.sha256(text.encode()).digest()
            output.append([(digest[i % 32] + 1) / 256 for i in range(config["dimension"])])
        return output

    async def rerank(self, query, documents):
        return [(i, 0.9 - i * 0.01) for i in range(len(documents))]


def running(job_id):
    with session() as db, db.begin():
        job = db.get(Job, job_id)
        job.status, job.owner = "running", "test-owner"
        job.lease_until = now() + timedelta(minutes=10)
        return job.payload


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(settings(), "storage_root", tmp_path / "storage")
    monkeypatch.setattr(settings(), "sources_root", tmp_path / "sources")
    settings().sources_root.mkdir()
    client = TestClient(app)
    kb = client.post("/api/knowledge-bases", json={"name": "integration-" + uid()}).json()
    source = client.post(f"/api/knowledge-bases/{kb['id']}/sources", json={"name": "上传", "kind": "upload"}).json()
    yield client, kb, source
    deletion = client.delete(f"/api/knowledge-bases/{kb['id']}").json()
    if "id" in deletion:
        from app.worker import delete_kb
        payload = running(deletion["id"])
        delete_kb(deletion["id"], "test-owner", payload)
        finish(deletion["id"], "test-owner")


def upload_and_index(client, source, content, provider):
    result = client.post(f"/api/data-sources/{source['id']}/upload", files=[
        ("files", ("guide.txt", content.encode(), "text/plain")),
    ], data={"paths": "docs/guide.txt"}).json()["files"][0]
    assert result["status"] in ("added", "modified", "unchanged"), result
    if result["job_id"]:
        payload = running(result["job_id"])
        index_version(result["job_id"], "test-owner", payload, provider=provider)
        finish(result["job_id"], "test-owner")
    return result


def test_upload_incremental_publish_failure_and_delete(workspace):
    client, kb, source = workspace
    provider = DeterministicProvider()
    original = "系统支持 PDF、Word 和 TXT。知识库使用增量更新。" + uid()
    first = upload_and_index(client, source, original, provider)
    assert provider.calls > 0
    calls = provider.calls
    repeated = upload_and_index(client, source, original, provider)
    assert repeated["status"] == "unchanged" and provider.calls == calls
    with session() as db:
        old_version = db.get(Document, first["document_id"]).active_version_id
    changed = client.post(f"/api/data-sources/{source['id']}/upload", files={
        "files": ("guide.txt", ("系统增加扫描件 PDF 支持。" + uid()).encode(), "text/plain")},
        data={"paths": "docs/guide.txt"}).json()["files"][0]
    payload = running(changed["job_id"])
    with pytest.raises(AppError):
        index_version(changed["job_id"], "test-owner", payload, provider=DeterministicProvider(fail=True))
    with session() as db:
        assert db.get(Document, first["document_id"]).active_version_id == old_version
    found = asyncio.run(retrieve(kb["id"], "支持格式", provider))
    assert found["chunks"] and all(c["version_id"] == old_version for c in found["chunks"])
    index_version(changed["job_id"], "test-owner", payload, provider=provider)
    finish(changed["job_id"], "test-owner")
    found = asyncio.run(retrieve(kb["id"], "扫描件 PDF", provider))
    assert found["chunks"] and all(c["version_id"] != old_version for c in found["chunks"])
    with session() as db, db.begin():
        doc = db.get(Document, first["document_id"])
        mark_deleted(db, doc)
    assert not asyncio.run(retrieve(kb["id"], "扫描件 PDF", provider))["chunks"]
    running(changed["job_id"])
    with pytest.raises(AppError, match="删除"):
        index_version(changed["job_id"], "test-owner", payload, provider=provider)


def test_directory_complete_scan_protects_deletion(workspace, monkeypatch):
    client, kb, _ = workspace
    root = settings().sources_root / "manuals"
    root.mkdir()
    file = root / "a.txt"
    file.write_text("服务器目录资料", encoding="utf-8")
    source = client.post(f"/api/knowledge-bases/{kb['id']}/sources", json={
        "name": "目录", "kind": "directory", "path": str(root), "interval_seconds": 60,
    }).json()
    job = client.post(f"/api/data-sources/{source['id']}/sync").json()
    sync_directory(job["id"], "test-owner", running(job["id"]))
    finish(job["id"], "test-owner")
    with session() as db:
        doc = db.scalar(select(Document).where(Document.source_id == source["id"]))
        doc_id = doc.id
    file.unlink()
    failing = client.post(f"/api/data-sources/{source['id']}/sync").json()
    payload = running(failing["id"])
    with monkeypatch.context() as patch:
        def failure(path):
            raise PermissionError("subdirectory unavailable")
        patch.setattr("app.worker.enumerate_directory", failure)
        with pytest.raises(PermissionError):
            sync_directory(failing["id"], "test-owner", payload)
    with session() as db:
        assert not db.get(Document, doc_id).deleted
    sync_directory(failing["id"], "test-owner", payload)
    with session() as db:
        assert db.get(Document, doc_id).deleted
        assert db.get(Job, failing["id"]).counts["deleted"] == 1
    finish(failing["id"], "test-owner")


def test_rebuild_configuration_cutover(workspace, monkeypatch):
    client, kb, source = workspace
    provider = DeterministicProvider()
    first = upload_and_index(client, source, "中文知识库的混合检索与模型配置。", provider)
    monkeypatch.setattr(settings(), "embedding_dimension", 512)
    job = client.post(f"/api/knowledge-bases/{kb['id']}/rebuild").json()
    rebuild_index(job["id"], "test-owner", running(job["id"]), provider)
    with session() as db:
        current = db.get(KnowledgeBase, kb["id"])
        assert current.index_config_id != kb["index_config_id"]
        assert db.get(IndexConfig, current.index_config_id).config["dimension"] == 512
        assert db.get(Document, first["document_id"]).active_version_id
    assert asyncio.run(retrieve(kb["id"], "混合检索", provider))["chunks"]
    finish(job["id"], "test-owner")


def test_api_errors_and_upload_rejection(workspace, monkeypatch):
    from pydantic import SecretStr
    monkeypatch.setattr(settings(), "deepseek_key", SecretStr(""))
    client, kb, source = workspace
    assert client.post("/api/knowledge-bases", json={"name": ""}).status_code == 422
    r = client.post(f"/api/data-sources/{source['id']}/upload", files={"files": ("evil.exe", b"a")})
    assert r.json()["files"][0]["status"] == "rejected"
    r = client.post(f"/api/data-sources/{source['id']}/upload", files={"files": ("a.txt", b"a")},
                    data={"paths": "../escape.txt"})
    assert r.json()["files"][0]["status"] == "rejected"
    assert client.post(f"/api/knowledge-bases/{kb['id']}/sources", json={
        "name": "bad", "kind": "directory", "path": "../outside"}).status_code == 400
    conv = client.post("/api/conversations", json={}).json()
    result = client.post(f"/api/conversations/{conv['id']}/messages", json={
        "kb_id": kb["id"], "content": "问题", "request_key": uid()})
    assert result.status_code == 503 and result.json()["code"] == "model_not_configured"
    client.delete(f"/api/conversations/{conv['id']}")
