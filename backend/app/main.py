from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from uuid import uuid4

from .db import session
from .errors import AppError, safe_error

app = FastAPI(title="知序 · RAG Workspace", version="0.1.0")
from .api import router  # noqa: E402
app.include_router(router)
from .chat import router as chat_router  # noqa: E402
app.include_router(chat_router)


@app.middleware("http")
async def request_id(request: Request, call_next):
    request.state.request_id = str(uuid4())
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    return response


@app.exception_handler(AppError)
async def app_error(request: Request, exc: AppError):
    return JSONResponse(status_code=exc.status, content={
        "code": exc.code, "message": safe_error(exc), "request_id": request.state.request_id,
    })


@app.exception_handler(RequestValidationError)
async def invalid_input(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={
        "code": "invalid_input", "message": "输入格式或参数范围不正确", "request_id": request.state.request_id,
    })


@app.exception_handler(Exception)
async def unexpected(request: Request, exc: Exception):
    return JSONResponse(status_code=503, content={
        "code": "dependency_error", "message": safe_error(exc), "request_id": request.state.request_id,
    })


@app.get("/api/health/live")
def live():
    return {"status": "ok"}


@app.get("/api/health/ready")
def ready():
    from .vectors import vector_store
    dependencies = {}
    for name, check in {
        "postgres": lambda: _database_ready(), "qdrant": lambda: vector_store().get_collections(),
    }.items():
        try:
            check()
            dependencies[name] = "ok"
        except Exception:
            dependencies[name] = "unavailable"
    return JSONResponse(status_code=200 if all(v == "ok" for v in dependencies.values()) else 503,
                        content={"dependencies": dependencies})


def _database_ready():
    with session() as db:
        db.execute(text("SELECT 1"))
