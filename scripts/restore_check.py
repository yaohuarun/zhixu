"""Restore a backup into a separate disposable Compose project; never restores production."""
import argparse
import json
import socket
import subprocess
import time
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("backup")
    args = parser.parse_args()
    backup = Path(args.backup).resolve()
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    for port in (55433, 56334):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))
    prefix = ["docker", "compose", "-f", str(Path(__file__).with_name("compose.restore-check.yaml")),
              "-p", "rag-restore-check"]
    def compose(*args, **kwargs):
        return subprocess.run([*prefix, *args], check=True, **kwargs)
    existing = compose("ps", "-a", "-q", capture_output=True, text=True).stdout.strip()
    if existing:
        raise SystemExit("Restore-check project already exists; inspect it before attempting another restore")
    compose("up", "-d", "--wait", "postgres", "qdrant")
    with (backup / "postgres.dump").open("rb") as source:
        compose("exec", "-T", "postgres", "pg_restore", "-U", "rag", "-d", "rag", "--no-owner",
                "--exit-on-error", stdin=source)
    with (backup / "files-models.tar.gz").open("rb") as source:
        compose("run", "--rm", "--no-deps", "-T", "files", "-C", "/data", "-xzf", "-", stdin=source)
    with httpx.Client(base_url="http://127.0.0.1:56334", timeout=120, trust_env=False) as client:
        deadline = time.monotonic() + 30
        while True:
            try:
                client.get("/collections").raise_for_status()
                break
            except httpx.HTTPError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)
        restored = []
        for snapshot in manifest["snapshots"]:
            with (backup / snapshot["file"]).open("rb") as file:
                client.post(f"/collections/{snapshot['collection']}/snapshots/upload?priority=snapshot",
                            files={"snapshot": (snapshot["file"], file)}).raise_for_status()
            info = client.get(f"/collections/{snapshot['collection']}").json()["result"]
            restored.append({"collection": snapshot["collection"], "points": info["points_count"]})
    counts = compose("exec", "-T", "postgres", "psql", "-U", "rag", "-d", "rag", "-Atc",
        "SELECT 'knowledge_bases='||count(*) FROM knowledge_bases UNION ALL SELECT 'chunks='||count(*) FROM chunks",
        capture_output=True, text=True).stdout.strip()
    # Verify every published version's source snapshot, and every active chunk's vector.
    sql = """SELECT json_build_object('snapshots', COALESCE((SELECT json_agg(v.snapshot_path)
      FROM documents d JOIN document_versions v ON v.id=d.active_version_id
      WHERE NOT d.deleted), '[]'::json), 'points', COALESCE((SELECT json_agg(json_build_object(
      'collection', i.collection, 'id', c.id)) FROM chunks c JOIN documents d ON d.id=c.document_id
      JOIN knowledge_bases k ON k.id=d.kb_id JOIN index_configs i ON i.id=k.index_config_id
      WHERE NOT d.deleted AND c.version_id=d.active_version_id AND k.status='active'), '[]'::json))"""
    business = json.loads(compose("exec", "-T", "postgres", "psql", "-U", "rag", "-d", "rag", "-Atc",
        sql, capture_output=True, text=True).stdout.strip())
    check_sources = "import json,pathlib,sys; paths=json.load(sys.stdin); missing=[p for p in paths if not pathlib.Path(p).is_file()]; assert not missing,missing; print(len(paths))"
    compose("run", "--rm", "--no-deps", "-T", "--entrypoint", "python", "files", "-c", check_sources,
            input=json.dumps(business["snapshots"]), text=True, capture_output=True)
    with httpx.Client(base_url="http://127.0.0.1:56334", timeout=30, trust_env=False) as client:
        for point in business["points"]:
            result = client.get(f"/collections/{point['collection']}/points/{point['id']}")
            result.raise_for_status()
    report = {"database": counts, "collections": restored, "project": "rag-restore-check",
              "ports": [55433, 56334], "status": "passed", "active_source_files": len(business["snapshots"]),
              "active_chunk_vectors": len(business["points"])}
    (backup / "restore-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    compose("stop", "postgres", "qdrant")


if __name__ == "__main__":
    main()
