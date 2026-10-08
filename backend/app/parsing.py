import json
import os
import signal
import re
import statistics
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

import fitz
from bs4 import BeautifulSoup
from docx import Document as WordDocument
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from .config import settings
from .errors import AppError


def element(text, kind="paragraph", page=None, section=None, bbox=None, **extra):
    return {"text": text.strip(), "kind": kind, "page": page, "section": section or [],
            "bbox": bbox, **extra}


def parse_txt(path):
    data = path.read_bytes()
    text = None
    for encoding in ("utf-8-sig", "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8", "gb18030"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeError:
            continue
    if text is None or "\x00" in text or any(ord(c) < 9 for c in text):
        raise AppError("invalid_encoding", "TXT 文本编码无效或包含二进制内容")
    import re
    return [element(p) for p in re.split(r"\n\s*\n", text.replace("\r\n", "\n")) if p.strip()]


def table_rows(html):
    soup = BeautifulSoup(html, "html.parser")
    return [" | ".join(cell.get_text(" ", strip=True) for cell in row.find_all(["td", "th"]))
            for row in soup.find_all("tr")]


def join_ocr_lines(text):
    text = re.sub(r"(?<=[A-Za-z])-\n(?=[a-z])", "", text)
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s*\n\s*(?=[\u4e00-\u9fff])", "", text)
    return re.sub(r"(?<=[A-Za-z0-9])\s*\n\s*(?=[A-Za-z0-9])", " ", text)


def parse_docx(path):
    document = WordDocument(path)
    elements: list[dict] = []
    headings: list[str] = []
    for child in document.element.body:
        if child.tag == qn("w:p"):
            paragraph = Paragraph(child, document)
            text = paragraph.text.strip()
            if not text:
                continue
            name = paragraph.style.name if paragraph.style else ""
            if name.startswith("Heading") and name.split()[-1].isdigit():
                level = int(name.split()[-1])
                headings = headings[:level - 1] + [text]
                elements.append(element(text, "heading", section=headings, level=level))
            else:
                numbered = paragraph._p.find(".//" + qn("w:numPr")) is not None
                elements.append(element(text, "list" if numbered or "List" in name else "paragraph",
                                        section=list(headings)))
        elif child.tag == qn("w:tbl"):
            table = Table(child, document)
            rows = [" | ".join(cell.text.replace("\n", " ") for cell in row.cells) for row in table.rows]
            elements.append(element("\n".join(rows), "table", section=list(headings), rows=rows))
    return elements


def convert_doc(path, directory):
    cfg = settings()
    profile = (directory / "profile").resolve().as_uri()
    try:
        process = subprocess.Popen([cfg.soffice_binary, f"-env:UserInstallation={profile}", "--headless",
                                    "--convert-to", "docx", "--outdir", str(directory), str(path.resolve())],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        process.communicate(timeout=cfg.convert_timeout)
    except FileNotFoundError:
        raise AppError("converter_missing", "DOC 转换依赖 LibreOffice 未安装", 503) from None
    except subprocess.TimeoutExpired:
        terminate_tree(process)
        process.communicate()
        raise AppError("conversion_timeout", "DOC 转换超时", 422) from None
    converted = directory / f"{path.stem}.docx"
    if process.returncode or not converted.exists():
        raise AppError("conversion_failed", "DOC 文件无法转换，请检查是否损坏或加密", 422)
    return converted


def terminate_tree(process):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
    else:
        def stop(pid):
            try:
                children = Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
            except OSError:
                children = []
            for child in children:
                stop(int(child))
            try:
                os.kill(pid, getattr(signal, "SIGKILL"))
            except ProcessLookupError:
                pass
        stop(process.pid)


def ocr_engine(allow_download=False):
    root = settings().ocr_model_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    os.environ["PADDLE_PDX_CACHE_HOME"] = str(root)
    os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "BOS")
    if not allow_download and not (root / ".ready").exists():
        raise AppError("ocr_not_initialized", "本地 OCR 模型未初始化，请执行模型初始化命令", 503)
    try:
        from paddleocr import PPStructureV3
    except ImportError:
        raise AppError("ocr_missing", "本地 OCR 依赖未安装", 503) from None
    try:
        return PPStructureV3(device="cpu", use_doc_orientation_classify=False,
                             use_doc_unwarping=False, use_textline_orientation=False,
                             use_formula_recognition=False, use_chart_recognition=False,
                             use_seal_recognition=False)
    except Exception:
        raise AppError("ocr_runtime_error", "本地 OCR 模型无法加载，请检查兼容版本、模型资产和可用内存", 503) from None


def initialize_ocr():
    from PIL import Image, ImageDraw
    root = settings().ocr_model_root.resolve()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "init.png"
        image = Image.new("RGB", (600, 200), "white")
        ImageDraw.Draw(image).text((20, 60), "Local OCR initialization document", fill="black")
        image.save(path)
        pipeline = ocr_engine(allow_download=True)
        list(pipeline.predict(str(path)))
    (root / ".ready").write_text("pp-structure-cpu-v1", encoding="utf-8")


def parse_pdf(path, force_ocr=False):
    cfg = settings()
    results, engine = [], None
    with tempfile.TemporaryDirectory() as tmp, fitz.open(path) as document:
        if document.needs_pass:
            raise AppError("encrypted_pdf", "PDF 已加密，无法解析", 422)
        if len(document) > cfg.max_pdf_pages:
            raise AppError("too_many_pages", "PDF 页数超过限制", 422)
        for page_number, page in enumerate(document, 1):
            raw_text = page.get_text().strip()
            chars = [c for c in raw_text if not c.isspace()]
            readable = sum(c.isprintable() and c != "\ufffd" for c in chars) / len(chars) if chars else 1.0
            images = page.get_images()
            image_area = sum((rect & page.rect).get_area() for image in images
                             for rect in page.get_image_rects(image[0]))
            coverage = min(1.0, image_area / max(page.rect.get_area(), 1))
            needs_ocr = (force_ocr or len(chars) < cfg.pdf_ocr_min_chars and bool(images)
                         or coverage >= cfg.pdf_ocr_image_coverage and bool(images)
                         and len(chars) < cfg.pdf_ocr_image_max_chars or readable < cfg.pdf_ocr_min_readable)
            if not needs_ocr and not chars:
                continue  # Genuine empty pages do not invoke OCR.
            if needs_ocr:
                scale = cfg.render_dpi / 72
                if page.rect.width * page.rect.height * scale ** 2 > cfg.max_render_pixels:
                    raise AppError("page_too_large", "PDF 页面渲染像素超过限制", 422)
                image_path = Path(tmp) / f"page-{page_number}.png"
                page.get_pixmap(matrix=fitz.Matrix(scale, scale)).save(image_path)
                engine = engine or ocr_engine()
                parsed = False
                for prediction in engine.predict(str(image_path)):
                    data = prediction.json
                    data = json.loads(data) if isinstance(data, str) else data
                    data = data.get("res", data)
                    blocks = data.get("parsing_res_list", [])
                    for block in blocks:
                        raw_block = block.get("block_content", "")
                        text = raw_block
                        label = block.get("block_label", "text")
                        rows = table_rows(text) if label == "table" else None
                        if rows:
                            text = "\n".join(rows)
                        else:
                            text = join_ocr_lines(text)
                        if text.strip():
                            box = [round(float(v) / scale, 2) for v in block.get("block_bbox", [])]
                            results.append(element(text, "table" if rows else "paragraph", page_number,
                                                   bbox=box, rows=rows, extraction="ocr",
                                                   layout_label=label, structure_quality="inferred"))
                            if text != raw_block and not rows:
                                results[-1]["cleaning"] = "ocr-soft-line-join"
                                results[-1]["raw_text"] = raw_block
                            parsed = True
                image_path.unlink(missing_ok=True)
                if not parsed and page.get_images():
                    raise AppError("ocr_empty", f"第 {page_number} 页 OCR 未识别出内容，请检查扫描质量", 422)
            else:
                data = page.get_text("dict", sort=True)
                text_blocks = [b for b in data["blocks"] if b.get("type") == 0]
                sizes = [span["size"] for b in text_blocks for line in b["lines"] for span in line["spans"]]
                median = statistics.median(sizes) if sizes else 12
                # Cluster left/right columns when both columns exist; spanning headings come first.
                midpoint = page.rect.width / 2
                left = [b for b in text_blocks if b["bbox"][2] <= midpoint + 15]
                right = [b for b in text_blocks if b["bbox"][0] >= midpoint - 15]
                if len(left) >= 2 and len(right) >= 2:
                    spans = [b for b in text_blocks if b not in left and b not in right]
                    text_blocks = sorted(spans, key=lambda b: b["bbox"][1]) + sorted(
                        left, key=lambda b: b["bbox"][1]) + sorted(right, key=lambda b: b["bbox"][1])
                for block in text_blocks:
                    lines = ["".join(s["text"] for s in line["spans"]) for line in block["lines"]]
                    text = "\n".join(lines).strip()
                    max_size = max((s["size"] for line in block["lines"] for s in line["spans"]), default=median)
                    heading = max_size > median * 1.25 and len(text) < 150
                    if text:
                        results.append(element(text, "heading" if heading else "paragraph", page_number,
                                               bbox=list(block["bbox"]), extraction="text", level=1 if heading else None,
                                               edge=block["bbox"][1] < 50 or block["bbox"][3] > page.rect.height - 40))
    # Strip only repeated short edge elements. Retain their original text in a cleaning record.
    edges = Counter(e["text"] for e in results if e.get("edge") and len(e["text"]) < 150)
    for e in results:
        if e.get("edge") and edges[e["text"]] >= 3:
            e["cleaning"] = "repeated-page-edge"
            e["excluded"] = True
    return results


def parse_direct(path: Path, force_ocr=False):
    extension = path.suffix.lower()
    try:
        if extension == ".txt":
            elements = parse_txt(path)
        elif extension == ".pdf":
            elements = parse_pdf(path, force_ocr)
        elif extension == ".docx":
            elements = parse_docx(path)
        elif extension == ".doc":
            with tempfile.TemporaryDirectory() as tmp:
                elements = parse_docx(convert_doc(path, Path(tmp)))
        else:
            raise AppError("unsupported_format", "不支持此文件格式")
    except AppError:
        raise
    except Exception:
        raise AppError("parse_failed", "文档损坏或无法解析，请检查输入文件", 422) from None
    offset, section = 0, []
    for ordinal, item in enumerate(elements):
        if item["kind"] == "heading" and not item["section"]:
            section = [item["text"]]
        elif item["section"]:
            section = item["section"]
        item["section"] = list(section)
        item["element_index"], item["start"], item["end"] = ordinal, offset, offset + len(item["text"])
        offset = item["end"] + 2
    if not any(e["text"] and not e.get("excluded") for e in elements):
        raise AppError("empty_document", "文档没有可索引的文本", 422)
    return elements


def parse_isolated(path: Path, force_ocr=False):
    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / "parsed.json"
        command = [sys.executable, "-m", "app.parsing", str(path.resolve()), str(output)]
        if force_ocr:
            command.append("--force-ocr")
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=os.name != "nt")
        try:
            process.communicate(timeout=settings().parse_timeout)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
            else:
                getattr(os, "killpg")(process.pid, getattr(signal, "SIGKILL"))
            process.communicate()
            raise AppError("parse_timeout", "文档解析或 OCR 超时", 422) from None
        if not output.exists():
            raise AppError("parser_crashed", "解析子进程异常退出，请检查内存和 OCR 依赖", 503)
        data = json.loads(output.read_text(encoding="utf-8"))
        if process.returncode or "error" in data:
            raise AppError(data.get("code", "parse_failed"), data.get("error", "文档解析失败"), 422)
        return data["elements"]


if __name__ == "__main__":
    if sys.argv[1:] == ["--init-ocr"]:
        initialize_ocr()
        print("Local OCR assets initialized")
    else:
        try:
            parsed = parse_direct(Path(sys.argv[1]), "--force-ocr" in sys.argv)
            Path(sys.argv[2]).write_text(json.dumps({"elements": parsed}, ensure_ascii=False), encoding="utf-8")
        except AppError as exc:
            Path(sys.argv[2]).write_text(json.dumps({"code": exc.code, "error": exc.message}), encoding="utf-8")
            sys.exit(1)
