"""Run inside the Worker image against real stores and explicit synthetic HTTP model endpoints."""
import argparse
import json
import subprocess
import tempfile
import time
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import session
from app.indexing import index_version
from app.main import app
from app.models import Document, DocumentVersion, Job, now
from app.parsing import convert_doc, parse_docx
from app.queue import finish
from app.worker import delete_kb


def running(identity):
    with session() as db, db.begin():
        job = db.get(Job, identity)
        job.status, job.owner, job.lease_until = "running", "docker-acceptance", now() + timedelta(hours=1)
        return job.payload


def cleanup(client, identity):
    result = client.delete(f"/api/knowledge-bases/{identity}").json()
    if result.get("status") != "succeeded":
        delete_kb(result["id"], "docker-acceptance", running(result["id"]))
        finish(result["id"], "docker-acceptance")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", default="/fixtures")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--cleanup")
    parser.add_argument("--real-models", action="store_true")
    args = parser.parse_args()
    client = TestClient(app)
    if args.cleanup:
        cleanup(client, args.cleanup)
        return
    kb = client.post("/api/knowledge-bases", json={"name": "docker-contract-acceptance"}).json()
    source = client.post(f"/api/knowledge-bases/{kb['id']}/sources", json={"name": "formats", "kind": "upload"}).json()
    from app.config import settings
    cfg = settings()
    report = {"models": {"kind": "real-external-api" if args.real_models else "synthetic HTTP contracts; not real service quality",
                         "embedding": cfg.embedding_model, "rerank": cfg.rerank_model, "generation": cfg.deepseek_model},
              "kb_id": kb["id"], "documents": [], "chat_rounds": []}
    success = False
    try:
        root = Path(args.fixtures)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            # Writer's DOCX→DOC direct export flattens tables in this version. Create
            # the legacy fixture via its native ODT format; production accepts DOC.
            subprocess.run(["soffice", "-env:UserInstallation=" + (directory / "odt-profile").as_uri(),
                            "--headless", "--convert-to", "odt", "--outdir", str(directory),
                            str(root / "guide.docx")], check=True, capture_output=True, timeout=120)
            subprocess.run(["soffice", "-env:UserInstallation=" + (directory / "create-profile").as_uri(),
                            "--headless", "--convert-to", "doc:MS Word 97", "--outdir", str(directory),
                            str(directory / "guide.odt")], check=True, capture_output=True, timeout=120)
            legacy = directory / "guide.doc"
            from concurrent.futures import ThreadPoolExecutor
            def concurrent_conversion(_):
                with tempfile.TemporaryDirectory() as tmp:
                    return parse_docx(convert_doc(legacy, Path(tmp)))
            with ThreadPoolExecutor(2) as pool:
                converted = list(pool.map(concurrent_conversion, range(2)))
            assert all(any(e["kind"] == "table" for e in elements) for elements in converted), converted
            assert all(any(e["kind"] == "heading" and e["section"] for e in elements) for elements in converted)
            for file in [root / "guide.txt", root / "guide.docx", legacy, root / "text.pdf",
                         root / "scanned.pdf", root / "mixed.pdf"]:
                started = time.perf_counter()
                result = client.post(f"/api/data-sources/{source['id']}/upload",
                    files={"files": (file.name, file.read_bytes())}).json()["files"][0]
                assert result["job_id"], result
                payload = running(result["job_id"])
                index_version(result["job_id"], "docker-acceptance", payload)
                finish(result["job_id"], "docker-acceptance")
                with session() as db:
                    document = db.get(Document, result["document_id"])
                    elements = db.get(DocumentVersion, document.active_version_id).elements
                if file.name in ("scanned.pdf", "mixed.pdf"):
                    recognized = "".join(e["text"] for e in elements if e.get("extraction") == "ocr")
                    assert "扫描件" in recognized and "OCR" in recognized, recognized
                if file.name == "mixed.pdf":
                    assert {e["page"] for e in elements} == {1, 2}
                    assert {e.get("extraction") for e in elements} == {"ocr", "text"}
                report["documents"].append({"name": file.name, "seconds": round(time.perf_counter() - started, 3),
                    "elements": len(elements), "extraction": sorted({e.get("extraction", "word/txt") for e in elements})})
        repeated = client.post(f"/api/data-sources/{source['id']}/upload",
            files={"files": ("guide.txt", (root / "guide.txt").read_bytes())}).json()["files"][0]
        assert repeated["status"] == "unchanged" and not repeated["job_id"]
        conv = client.post("/api/conversations", json={}).json()
        report["conversation_id"] = conv["id"]
        try:
            for ordinal, question in enumerate(("知识库支持哪些格式？", "它支持扫描件吗？")):
                started = time.perf_counter()
                result = client.post(f"/api/conversations/{conv['id']}/messages", json={
                    "kb_id": kb["id"], "content": question, "request_key": f"docker-{ordinal}"})
                assert "event: done" in result.text and "event: sources" in result.text, result.text
                events = {}
                for block in result.text.split("\n\n"):
                    lines = block.splitlines()
                    if len(lines) >= 2 and lines[0].startswith("event: ") and lines[1].startswith("data: "):
                        events[lines[0][7:]] = json.loads(lines[1][6:])
                assert events["sources"] and not events["meta"]["degraded"], events
                report["chat_rounds"].append({"seconds": round(time.perf_counter() - started, 3),
                    "query": events["meta"]["query"], "source_count": len(events["sources"]),
                    "answer": events["done"]["content"]})
        finally:
            if not args.keep:
                client.delete(f"/api/conversations/{conv['id']}")
        with session() as db:
            assert len(list(db.scalars(select(Document).where(Document.kb_id == kb["id"])))) == 6
        success = True
        report["status"] = "passed"
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        if not args.keep or not success:
            cleanup(client, kb["id"])


if __name__ == "__main__":
    main()
