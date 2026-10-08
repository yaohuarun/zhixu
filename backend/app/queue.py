from datetime import timedelta

from sqlalchemy import exists, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import aliased

from .config import settings
from .db import session
from .errors import AppError
from .models import Job, KnowledgeBase, now, uid


def enqueue(db, kb_id: str, kind: str, resource: str, payload: dict, dedup_key: str | None = None):
    job_id = uid()
    key = dedup_key or job_id
    db.execute(insert(Job).values(
        id=job_id, kb_id=kb_id, kind=kind, resource=resource, payload=payload, dedup_key=key,
    ).on_conflict_do_nothing(index_elements=[Job.dedup_key]))
    return db.scalar(select(Job).where(Job.dedup_key == key))


def claim(owner: str):
    cfg = settings()
    other = aliased(Job)
    with session() as db, db.begin():
        # All contenders hold this advisory lock only while claiming; processing remains parallel by resource.
        from sqlalchemy import text
        db.execute(text("SELECT pg_advisory_xact_lock(73401821)"))
        expired = list(db.scalars(select(Job).where(Job.status == "running", Job.lease_until < now())))
        for job in expired:
            job.status = "failed" if job.attempts >= cfg.max_job_attempts else "queued"
            job.owner = None
            job.error = "任务租约过期，已回收" if job.status == "failed" else ""
        db.flush()
        candidate = db.scalar(select(Job).join(KnowledgeBase, Job.kb_id == KnowledgeBase.id).where(
            Job.status == "queued",
            or_(KnowledgeBase.status == "active", Job.kind == "delete_kb"),
            ~exists(select(other.id).where(other.resource == Job.resource, other.status == "running")),
        ).order_by(Job.created_at).with_for_update(skip_locked=True).limit(1))
        if not candidate:
            return None
        candidate.status, candidate.owner = "running", owner
        candidate.attempts += 1
        candidate.lease_until = now() + timedelta(seconds=cfg.lease_seconds)
        return candidate.id


def heartbeat(job_id: str, owner: str):
    with session() as db, db.begin():
        result = db.execute(update(Job).where(
            Job.id == job_id, Job.owner == owner, Job.status == "running",
        ).values(lease_until=now() + timedelta(seconds=settings().lease_seconds)))
        return result.rowcount == 1


def check_lease(db, job_id: str, owner: str):
    job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
    if not job or job.status != "running" or job.owner != owner or job.lease_until < now():
        raise AppError("job_superseded", "任务已取消或租约失效", 409)
    return job


def finish(job_id: str, owner: str, error: str = ""):
    with session() as db, db.begin():
        db.execute(update(Job).where(Job.id == job_id, Job.owner == owner, Job.status == "running").values(
            status="failed" if error else "succeeded", phase="failed" if error else "complete",
            error=error, progress=100 if not error else Job.progress, lease_until=None,
            owner=None, finished_at=now(),
        ))


def phase(job_id: str, owner: str, name: str, progress: int):
    with session() as db, db.begin():
        job = check_lease(db, job_id, owner)
        job.phase, job.progress = name, progress
