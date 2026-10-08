"""Measure SSE arrival through nginx using the explicit synthetic model fixture."""
import argparse
import json
import time

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("kb_id")
    args = parser.parse_args()
    with httpx.Client(base_url="http://127.0.0.1:8080", timeout=60, trust_env=False) as client:
        response = client.post("/api/conversations", json={})
        response.raise_for_status()
        conversation = response.json()["id"]
        try:
            started = time.perf_counter()
            events, deltas = [], []
            with client.stream("POST", f"/api/conversations/{conversation}/messages", json={
                "kb_id": args.kb_id, "content": "知识库支持哪些格式？", "request_key": "proxy-check",
            }) as stream:
                stream.raise_for_status()
                for line in stream.iter_lines():
                    if line.startswith("event: "):
                        event = line[7:]
                        events.append(event)
                        if event == "delta":
                            deltas.append(round(time.perf_counter() - started, 3))
            assert "done" in events and "error" not in events, events
            assert len(deltas) >= 3 and deltas[-1] - deltas[0] >= 0.15, deltas
            print(json.dumps({"status": "passed", "endpoint": "nginx:8080", "delta_arrivals_seconds": deltas,
                              "models": "synthetic HTTP contract"}, indent=2))
        finally:
            client.delete(f"/api/conversations/{conversation}").raise_for_status()


if __name__ == "__main__":
    main()
