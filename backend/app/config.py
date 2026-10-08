import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=Path(__file__).resolve().parents[2] / ".env",
                                     extra="ignore", env_prefix="RAG_")
    database_url: str = "postgresql+psycopg://rag:rag@127.0.0.1:55432/rag"
    qdrant_url: str = "http://127.0.0.1:56333"
    storage_root: Path = Path("data/storage")
    sources_root: Path = Path("data/sources")
    deepseek_url: str = "https://api.deepseek.com/chat/completions"
    deepseek_key: SecretStr = SecretStr("")
    deepseek_model: str = ""
    deepseek_thinking: Literal["enabled", "disabled"] = "disabled"
    embedding_url: str = ""
    embedding_key: SecretStr = SecretStr("")
    embedding_model: str = "qwen3.7-text-embedding-flash"
    embedding_dimension: int = Field(1024, ge=1)
    embedding_batch: int = Field(20, ge=1, le=20)
    embedding_instruction: str = "Given a query, retrieve passages relevant to the query."
    rerank_url: str = ""
    rerank_key: SecretStr = SecretStr("")
    rerank_model: str = "qwen3.7-text-rerank"
    model_timeout: float = Field(60, gt=0)
    model_retries: int = Field(2, ge=0, le=5)
    model_concurrency: int = Field(4, ge=1)
    deepseek_timeout: float | None = Field(None, gt=0)
    embedding_timeout: float | None = Field(None, gt=0)
    rerank_timeout: float | None = Field(None, gt=0)
    deepseek_retries: int | None = Field(None, ge=0, le=5)
    embedding_retries: int | None = Field(None, ge=0, le=5)
    rerank_retries: int | None = Field(None, ge=0, le=5)
    chunk_target: int = Field(600, ge=50)
    chunk_max: int = Field(900, ge=100)
    chunk_overlap: int = Field(80, ge=0)
    tokenizer_path: str = ""
    tokenizer_model: str = ""
    parsing_version: str = "structure-v1"
    chunking_version: str = "structure-v1"
    lexical_dictionary: str = ""
    max_upload_bytes: int = Field(50 * 1024 * 1024, gt=0)
    max_pdf_pages: int = Field(500, ge=1)
    pdf_ocr_min_chars: int = Field(20, ge=0)
    pdf_ocr_image_coverage: float = Field(0.6, ge=0, le=1)
    pdf_ocr_image_max_chars: int = Field(150, ge=0)
    pdf_ocr_min_readable: float = Field(0.8, ge=0, le=1)
    render_dpi: int = Field(144, ge=72, le=300)
    max_render_pixels: int = Field(20_000_000, ge=1)
    parse_timeout: int = Field(900, ge=1)
    convert_timeout: int = Field(120, ge=1)
    ocr_model_root: Path = Path("data/models")
    soffice_binary: str = "soffice"
    worker_poll: float = Field(2, gt=0)
    lease_seconds: int = Field(90, ge=15)
    max_job_attempts: int = Field(5, ge=1)
    retrieval_limit: int = Field(40, ge=1, le=500)
    retrieval_max: int = Field(640, ge=1)
    rrf_k: int = Field(60, ge=1)
    context_chunks: int = Field(8, ge=1, le=40)
    context_budget: int = Field(12000, ge=1000)
    history_budget: int = Field(6000, ge=0)
    generation_context: int = Field(32000, ge=4000)
    output_tokens: int = Field(4096, ge=128)
    relevance_threshold: float | None = Field(None, ge=0, le=1)

    @model_validator(mode="after")
    def budgets(self):
        if self.chunk_target > self.chunk_max or self.chunk_overlap >= self.chunk_max // 2:
            raise ValueError("chunk_target must be <= chunk_max; overlap must be < half chunk_max")
        if self.history_budget + self.context_budget + self.output_tokens + 2000 > self.generation_context:
            raise ValueError("generation_context must cover history, sources, output and prompt margin")
        return self

    def embedding_config(self):
        return {
            "url": self.embedding_url, "model": self.embedding_model,
            "dimension": self.embedding_dimension, "instruction": self.embedding_instruction,
        }

    def processing_config(self):
        def asset_hash(value: str):
            if not value:
                return ""
            path = Path(value)
            return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "missing"
        return {
            "parse": self.parsing_version, "chunk": self.chunking_version,
            "target": self.chunk_target, "max": self.chunk_max, "overlap": self.chunk_overlap,
            "counter": self.tokenizer_path or "utf8-byte-upper-budget-v1",
            "tokenizer_model": self.tokenizer_model, "dictionary": self.lexical_dictionary,
            "tokenizer_hash": asset_hash(self.tokenizer_path), "dictionary_hash": asset_hash(self.lexical_dictionary),
            "ocr": "pp-structure-3.0.3-cpu", "render_dpi": self.render_dpi,
            "ocr_detection": [self.pdf_ocr_min_chars, self.pdf_ocr_image_coverage,
                              self.pdf_ocr_image_max_chars, self.pdf_ocr_min_readable],
        }


def fingerprint(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@lru_cache
def settings() -> Settings:
    return Settings()
