"""Run browser contracts in an isolated test DB. Requires migrated rag_test and npm ci."""
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    database = os.environ.get("RAG_TEST_DATABASE_URL", "postgresql+psycopg://rag:rag@127.0.0.1:55432/rag_test")
    if not database.rsplit("/", 1)[-1].endswith("_test"):
        raise SystemExit("Browser tests require a database name ending in _test")
    for port in (8001, 9009, 5174):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))  # Refuse to reuse someone else's server.
    env = {**os.environ, "RAG_DATABASE_URL": database,
           "RAG_EMBEDDING_URL": "http://127.0.0.1:9009/embedding", "RAG_EMBEDDING_KEY": "contract-only",
           "RAG_RERANK_URL": "http://127.0.0.1:9009/rerank", "RAG_RERANK_KEY": "contract-only",
           "RAG_DEEPSEEK_URL": "http://127.0.0.1:9009/chat", "RAG_DEEPSEEK_KEY": "contract-only",
           "RAG_DEEPSEEK_MODEL": "contract-fixture", "RAG_DEV_API_TARGET": "http://127.0.0.1:8001"}
    processes, streams = [], []
    log_root = root / "data" / "contract-logs"
    log_root.mkdir(parents=True, exist_ok=True)
    commands = [
        ([sys.executable, "-m", "uvicorn", "contract_model_server:app", "--app-dir", "backend/tests", "--port", "9009"], root),
        ([sys.executable, "-m", "uvicorn", "app.main:app", "--port", "8001"], root),
        ([sys.executable, "-m", "app.worker"], root),
        (["node", "node_modules/vite/bin/vite.js", "--host", "127.0.0.1", "--port", "5174"], root / "frontend"),
    ]
    try:
        for index, (command, cwd) in enumerate(commands):
            stream = (log_root / f"process-{index}.log").open("wb")
            streams.append(stream)
            processes.append(subprocess.Popen(command, cwd=cwd, env=env, stdout=stream, stderr=stream,
                start_new_session=os.name != "nt",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0))
        deadline = time.monotonic() + 30
        for address in ("http://127.0.0.1:8001/api/health/ready", "http://127.0.0.1:5174"):
            while True:
                try:
                    with urllib.request.urlopen(address, timeout=2):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Test servers did not become ready; inspect data/contract-logs")
                    time.sleep(0.2)
        subprocess.run(["node", "node_modules/@playwright/test/cli.js", "test"],
                       cwd=root / "frontend", env=env, check=True)
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                else:
                    getattr(os, "killpg")(process.pid, signal.SIGTERM)
                process.wait(timeout=10)
        for stream in streams:
            stream.close()


if __name__ == "__main__":
    main()
