"""Assert retained document, job and conversation data after Compose recreation."""
import argparse
import json

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("kb_id")
    parser.add_argument("conversation_id")
    args = parser.parse_args()
    with httpx.Client(base_url="http://127.0.0.1:8080", timeout=30, trust_env=False) as client:
        def read(path):
            response = client.get("/api" + path)
            response.raise_for_status()
            return response.json()
        assert any(k["id"] == args.kb_id for k in read("/knowledge-bases"))
        documents = read(f"/knowledge-bases/{args.kb_id}/documents")
        assert len(documents) == 6 and all(d["status"] == "ready" for d in documents), documents
        chunk_count = 0
        for document in documents:
            chunks = read(f"/documents/{document['id']}/chunks")
            assert chunks, document
            chunk_count += len(chunks)
        messages = read(f"/conversations/{args.conversation_id}/messages")
        assert len(messages) == 4, messages
        assert all(m["status"] == "complete" for m in messages if m["role"] == "assistant"), messages
        jobs = read(f"/knowledge-bases/{args.kb_id}/jobs")
        assert len([j for j in jobs if j["kind"] == "index" and j["status"] == "succeeded"]) == 6, jobs
        print(json.dumps({"status": "passed", "documents": len(documents), "chunks": chunk_count,
                          "messages": len(messages), "index_jobs": 6}, indent=2))


if __name__ == "__main__":
    main()
