"""Run in the Worker image with --network none after OCR initialization."""
import json
from pathlib import Path

from app.parsing import parse_direct

report = {}
for name in ("scanned.pdf", "mixed.pdf", "text.pdf"):
    elements = parse_direct(Path("/fixtures") / name, force_ocr=name == "text.pdf")
    if name != "text.pdf":
        recognized = "".join(e["text"] for e in elements if e.get("extraction") == "ocr")
        assert "扫描件" in recognized and "OCR" in recognized, recognized
    if name == "mixed.pdf":
        assert {e["page"] for e in elements} == {1, 2}
        assert {e.get("extraction") for e in elements} == {"text", "ocr"}
    else:
        assert all(e.get("extraction") == "ocr" for e in elements)
    report[name] = [{"text": e["text"], "page": e["page"], "extraction": e["extraction"]}
                    for e in elements]
print(json.dumps({"status": "passed", "network": "none", "documents": report}, ensure_ascii=False, indent=2))
