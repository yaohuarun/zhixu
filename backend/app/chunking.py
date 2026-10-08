import hashlib
import re
from dataclasses import dataclass

from .config import settings
from .errors import AppError


class Counter:
    def __init__(self):
        cfg = settings()
        self.tokenizer = None
        self.identifier = "utf8-byte-upper-budget-v1"
        if cfg.tokenizer_path:
            if cfg.tokenizer_model != cfg.embedding_model:
                raise AppError("tokenizer_mismatch", "本地 tokenizer 标识与 Embedding 模型不匹配", 503)
            try:
                from tokenizers import Tokenizer
                self.tokenizer = Tokenizer.from_file(cfg.tokenizer_path)
                self.identifier = f"local:{cfg.tokenizer_model}:" + hashlib.sha256(
                    open(cfg.tokenizer_path, "rb").read()).hexdigest()
            except (ImportError, OSError):
                raise AppError("tokenizer_unavailable", "配置的本地 tokenizer 不可用", 503) from None

    def count(self, text):
        return len(self.tokenizer.encode(text).ids) if self.tokenizer else len(text.encode("utf-8"))

    def take(self, text: str, budget: int):
        if budget <= 0:
            return ""
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.count(text[:mid]) <= budget:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo]

    def tail(self, text: str, budget: int):
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.count(text[mid:]) <= budget:
                hi = mid
            else:
                lo = mid + 1
        return text[lo:]


@dataclass
class Draft:
    original: str
    retrieval_text: str
    provenance: dict
    content_hash: str


def split_long(text: str, budget: int, overlap: int, counter: Counter):
    start = 0
    while start < len(text):
        part = counter.take(text[start:], budget)
        if not part:
            raise AppError("chunk_budget", "块预算不足以容纳一个字符")
        if start + len(part) < len(text):
            boundaries = list(re.finditer(r"\n\s*\n|[。！？.!?；;]\s*|\n", part))
            useful = [m.end() for m in boundaries if m.end() >= len(part) // 2]
            if useful:
                part = part[:useful[-1]]
        end = start + len(part)
        yield part, start, end
        if end >= len(text):
            break
        retained = counter.tail(part, min(overlap, max(0, counter.count(part) // 3)))
        start = max(start + 1, end - len(retained))


def chunk_elements(elements: list[dict], name: str, maximum: int | None = None) -> list[Draft]:
    cfg, counter = settings(), Counter()
    maximum = maximum or cfg.chunk_max
    target = min(cfg.chunk_target, maximum)
    drafts: list[Draft] = []
    pending: list[dict] = []

    def prefix(section):
        title = f"{name}\n{' / '.join(section)}\n"
        return counter.take(title, min(maximum // 3, 250)) + "\n"

    def emit(items, original=None, extra=None):
        content = original if original is not None else "\n\n".join(e["text"] for e in items)
        section = items[0]["section"]
        retrieval = prefix(section) + content
        if counter.count(retrieval) > maximum:
            raise AppError("chunk_budget", "块内容超出配置预算")
        pages = sorted({e["page"] for e in items if e.get("page") is not None})
        provenance = {"section": section, "pages": pages, "start": items[0]["start"],
                      "end": items[-1]["end"], "elements": [e["element_index"] for e in items],
                      "bboxes": [{"page": e.get("page"), "bbox": e.get("bbox")} for e in items],
                      "counter": counter.identifier, "budget_count": counter.count(retrieval),
                      "processing_version": cfg.chunking_version, **(extra or {})}
        drafts.append(Draft(content, retrieval, provenance, hashlib.sha256(retrieval.encode()).hexdigest()))

    def flush():
        if pending:
            emit(pending)
            pending.clear()

    usable = [e for e in elements if not e.get("excluded") and e["text"].strip()]
    for ordinal, element in enumerate(usable):
        section = element["section"]
        budget = maximum - counter.count(prefix(section))
        if element["kind"] == "heading":
            flush()
        if pending and pending[0]["section"] != section:
            flush()
        rows = element.get("rows")
        if element["kind"] == "table" and rows:
            flush()
            header = counter.take(rows[0], max(8, budget // 3))
            group: list[str] = []
            for row in rows[1:] or [""]:
                if counter.count(header + "\n" + row) > budget:
                    if group:
                        emit([element], header + "\n" + "\n".join(group), {"table_header": header})
                        group = []
                    for part, begin, end in split_long(row, budget - counter.count(header + "\n"), 0, counter):
                        emit([element], header + "\n" + part,
                             {"table_header": header, "row_slice": [begin, end]})
                elif counter.count(header + "\n" + "\n".join(group + [row])) > budget:
                    emit([element], header + "\n" + "\n".join(group), {"table_header": header})
                    group = [row]
                else:
                    group.append(row)
            if group:
                emit([element], header + "\n" + "\n".join(group), {"table_header": header})
            continue
        if counter.count(element["text"]) > budget:
            flush()
            for part, begin, end in split_long(element["text"], budget, cfg.chunk_overlap, counter):
                emit([element], part, {"start": element["start"] + begin, "end": element["start"] + end})
            continue
        proposed = prefix(section) + "\n\n".join(e["text"] for e in pending + [element])
        if pending and counter.count(proposed) > maximum:
            flush()
        pending.append(element)
        if counter.count(prefix(section) + "\n\n".join(e["text"] for e in pending)) >= target:
            # Keep a list's introducing paragraph until the list is included where the hard budget allows it.
            following = usable[ordinal + 1] if ordinal + 1 < len(usable) else None
            if following and following["kind"] == "list" and following["section"] == section:
                continue
            flush()
    flush()
    return drafts
