from functools import lru_cache

from qdrant_client import QdrantClient, models as qm

from .config import settings


@lru_cache
def vector_store():
    return QdrantClient(url=settings().qdrant_url, timeout=30, trust_env=False)


def ensure_collection(config):
    client = vector_store()
    if not client.collection_exists(config.collection):
        client.create_collection(config.collection, vectors_config=qm.VectorParams(
            size=config.config["dimension"], distance=qm.Distance.COSINE,
        ))
        for field in ("kb_id", "document_id", "version_id", "index_config_id"):
            client.create_payload_index(config.collection, field, qm.PayloadSchemaType.KEYWORD, wait=True)


def put_chunks(config, chunks, vectors):
    ensure_collection(config)
    points = [qm.PointStruct(id=c.id, vector=v, payload={
        "kb_id": c.kb_id, "document_id": c.document_id, "version_id": c.version_id,
        "index_config_id": config.id,
    }) for c, v in zip(chunks, vectors, strict=True)]
    client = vector_store()
    for start in range(0, len(points), 64):
        client.upsert(config.collection, points[start:start + 64], wait=True)
    for start in range(0, len(points), 64):
        found = client.retrieve(config.collection, [p.id for p in points[start:start + 64]], with_vectors=False)
        if len(found) != len(points[start:start + 64]):
            raise RuntimeError("Vector write verification failed")


def remove_versions(config, versions: list[str]):
    if versions and vector_store().collection_exists(config.collection):
        vector_store().delete(config.collection, qm.FilterSelector(filter=qm.Filter(must=[
            qm.FieldCondition(key="version_id", match=qm.MatchAny(any=versions)),
        ])), wait=True)
