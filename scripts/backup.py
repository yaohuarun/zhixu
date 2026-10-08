"""Coordinated backup of this Compose project. Binary output never passes through a shell."""
import argparse
import json
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def compose(*args, **kwargs):
    return subprocess.run(["docker", "compose", *args], check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="backups/" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    parser.add_argument("--qdrant", default="http://127.0.0.1:56333")
    args = parser.parse_args()
    target = Path(args.output).resolve()
    target.mkdir(parents=True, exist_ok=False)
    # Query line-delimited Compose JSON without relying on its optional array format.
    raw = compose("ps", "--format", "json", capture_output=True, text=True).stdout.strip()
    services = json.loads(raw) if raw.startswith("[") else [json.loads(line) for line in raw.splitlines()]
    running = [s["Service"] for s in services if s["Service"] in ("api", "worker", "frontend")
               and s["State"] == "running"]
    try:
        compose("stop", *running) if running else None
        with (target / "postgres.dump").open("wb") as out:
            compose("exec", "-T", "postgres", "pg_dump", "-U", "rag", "-d", "rag", "-Fc", stdout=out)
        with urllib.request.urlopen(args.qdrant + "/collections") as response:
            collections = json.load(response)["result"]["collections"]
        snapshots = []
        for collection in collections:
            name = collection["name"]
            request = urllib.request.Request(args.qdrant + f"/collections/{name}/snapshots", method="POST")
            with urllib.request.urlopen(request) as response:
                snapshot = json.load(response)["result"]["name"]
            file = target / (name + ".snapshot")
            urllib.request.urlretrieve(args.qdrant + f"/collections/{name}/snapshots/{snapshot}", file)
            snapshots.append({"collection": name, "file": file.name})
        with (target / "files-models.tar.gz").open("wb") as out:
            compose("run", "--rm", "--no-deps", "-T", "--entrypoint", "tar", "api",
                    "-C", "/data", "-czf", "-", "storage", "models", stdout=out)
        (target / "manifest.json").write_text(json.dumps({"snapshots": snapshots,
            "created_at": datetime.now(timezone.utc).isoformat(), "configuration": "Save deployment .env separately"},
            indent=2), encoding="utf-8")
        print(target)
    finally:
        if running:
            compose("start", *running)


if __name__ == "__main__":
    main()
