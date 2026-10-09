"""재개 가능한 검색용 자식 임베딩. 완성 전에는 부모 벡터가 계속 검색된다."""
from ..search.vector import pack


def embed_children(db, doc_id, client, should_stop=lambda: False, batch_size=16):
    pending = db.children_needing_embedding(doc_id, client.embed_model)
    for start in range(0, len(pending), batch_size):
        if should_stop():
            return "cancelled"
        batch = pending[start:start + batch_size]
        vectors, error = client.embed([text for _, text in batch])
        if error:
            return error
        if not vectors or len(vectors) != len(batch) or not vectors[0] or any(len(v) != len(vectors[0]) for v in vectors):
            return "자식 임베딩 개수·차원이 요청과 다릅니다."
        with db.transaction():
            for (child_id, _), vector in zip(batch, vectors):
                db.save_child_embedding(child_id, client.embed_model, len(vector), pack(vector))
    return ""
