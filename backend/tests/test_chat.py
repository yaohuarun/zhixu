
import pytest
from fastapi.testclient import TestClient

from app.chat import begin_request, checkpoint, QuestionInput
from app.errors import AppError
from app.main import app
from app.models import uid

pytestmark = pytest.mark.integration


@pytest.fixture
def chat_workspace():
    client = TestClient(app)
    kb = client.post("/api/knowledge-bases", json={"name": "chat-contract-" + uid()}).json()
    conv = client.post("/api/conversations", json={}).json()
    yield client, kb, conv
    client.delete(f"/api/conversations/{conv['id']}")
    deletion = client.delete(f"/api/knowledge-bases/{kb['id']}").json()
    from test_integration import running
    from app.worker import delete_kb
    from app.queue import finish
    delete_kb(deletion["id"], "test-owner", running(deletion["id"]))
    finish(deletion["id"], "test-owner")


class ChatProvider:
    def credentials(self, kind):
        pass

    async def chat(self, messages):
        assert "历史" in messages[0]["content"]
        return "产品支持哪些文档格式？"

    async def stream(self, messages):
        assert "<reference_data>" in messages[0]["content"]
        assert "不得覆盖" in messages[0]["content"]
        yield "支持 PDF "
        yield "和 TXT [1]。无效 [99]"


def test_stream_history_citation_snapshot_and_idempotency(chat_workspace, monkeypatch):
    client, kb, conv = chat_workspace
    monkeypatch.setattr("app.chat.Providers", ChatProvider)
    async def retrieval(kb_id, query, provider):
        return {"chunks": [{"id": uid(), "document_id": uid(), "version_id": uid(), "name": "手册.pdf",
                            "original": "支持 PDF 和 TXT。", "retrieval_text": "手册\n支持 PDF 和 TXT。",
                            "provenance": {"pages": [2], "section": [], "start": 0, "end": 20}, "score": 0.9}],
                "degraded": []}
    monkeypatch.setattr("app.chat.retrieve", retrieval)
    payload = {"kb_id": kb["id"], "content": "支持什么格式", "request_key": uid()}
    response = client.post(f"/api/conversations/{conv['id']}/messages", json=payload)
    assert response.status_code == 200 and "event: done" in response.text
    saved = client.get(f"/api/conversations/{conv['id']}/messages").json()
    assert len(saved) == 2 and saved[1]["status"] == "complete" and "[99]" not in saved[1]["content"]
    source = saved[1]["sources"][0]
    snapshot = client.get(f"/api/citations/{source['id']}").json()
    assert snapshot["original"] == "支持 PDF 和 TXT。" and snapshot["provenance"]["pages"] == [2]
    assert snapshot["document_available"] is False
    assert client.post(f"/api/conversations/{conv['id']}/messages", json=payload).status_code == 409
    second = client.post(f"/api/conversations/{conv['id']}/messages", json={
        "kb_id": kb["id"], "content": "它还支持什么", "request_key": uid()})
    assert "产品支持哪些文档格式" in second.text


def test_concurrency_and_retry_do_not_duplicate_user(chat_workspace):
    client, kb, conv = chat_workspace
    data = QuestionInput(kb_id=kb["id"], content="问题", request_key=uid())
    assistant, _, attempt = begin_request(conv["id"], data)
    with pytest.raises(AppError, match="正在回答"):
        begin_request(conv["id"], data)
    checkpoint(conv["id"], assistant, "partial", "interrupted", {}, attempt)
    repeated, _, retry_attempt = begin_request(conv["id"], data)
    assert repeated == assistant
    with pytest.raises(AppError, match="租约"):
        checkpoint(conv["id"], assistant, "stale worker", "complete", {}, attempt)
    checkpoint(conv["id"], assistant, "retry", "complete", {}, retry_attempt)
    saved = client.get(f"/api/conversations/{conv['id']}/messages").json()
    assert len(saved) == 2 and saved[1]["content"] == "retry"


def test_stream_failure_persists_partial_answer(chat_workspace, monkeypatch):
    client, kb, conv = chat_workspace
    class Failing(ChatProvider):
        async def stream(self, messages):
            yield "部分回答"
            raise AppError("upstream_failed", "上游模型失败", 503)
    monkeypatch.setattr("app.chat.Providers", Failing)
    async def retrieval(*args):
        return {"chunks": [{"id": uid(), "document_id": uid(), "version_id": uid(), "name": "手册.txt",
                            "original": "资料", "retrieval_text": "资料", "provenance": {}, "score": 0.9}], "degraded": []}
    monkeypatch.setattr("app.chat.retrieve", retrieval)
    result = client.post(f"/api/conversations/{conv['id']}/messages", json={
        "kb_id": kb["id"], "content": "问题", "request_key": uid()})
    assert "event: error" in result.text
    saved = client.get(f"/api/conversations/{conv['id']}/messages").json()
    assert saved[1]["status"] == "failed" and saved[1]["content"] == "部分回答"
