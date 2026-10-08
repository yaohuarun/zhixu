import re


class AppError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def safe_error(exc: Exception) -> str:
    from .config import settings
    if isinstance(exc, AppError):
        message = exc.message
    else:
        message = f"{type(exc).__name__}: operation failed; inspect dependency readiness and configuration"
    for key in (settings().deepseek_key, settings().embedding_key, settings().rerank_key):
        if key.get_secret_value():
            message = message.replace(key.get_secret_value(), "[redacted]")
    return re.sub(r"Bearer\s+\S+", "Bearer [redacted]", message)[:1000]
