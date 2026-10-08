from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db import session
from app.models import Job, KnowledgeBase, now, uid
from app.queue import claim, enqueue, heartbeat

pytestmark = pytest.mark.integration


def test_two_workers_claim_and_expired_lease_recovery():
    kb_id = uid()
    with session() as db, db.begin():
        db.add(KnowledgeBase(id=kb_id, name="queue-contract"))
        db.flush()
        first = enqueue(db, kb_id, "test", "queue-test-" + kb_id, {}, "dedup-" + kb_id)
        second = enqueue(db, kb_id, "test", "queue-test-" + kb_id, {})
        same = enqueue(db, kb_id, "test", "queue-test-" + kb_id, {}, "dedup-" + kb_id)
        assert same.id == first.id
        first_id, second_id = first.id, second.id
    try:
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(claim, ["owner-a", "owner-b"]))
        assert sum(x is not None for x in results) == 1
        identity = next(x for x in results if x)
        assert identity in (first_id, second_id)
        with session() as db, db.begin():
            running = db.get(Job, identity)
            old_owner = running.owner
            running.lease_until = now() - timedelta(seconds=10)
        recovered = claim("owner-c")
        assert recovered == identity
        assert not heartbeat(identity, old_owner)
        assert heartbeat(identity, "owner-c")
    finally:
        with session() as db, db.begin():
            for job in db.scalars(select(Job).where(Job.kb_id == kb_id)):
                job.status = "cancelled"
            db.get(KnowledgeBase, kb_id).status = "deleted"
