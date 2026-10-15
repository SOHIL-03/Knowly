"""B5 De-duplicate: the same story across newsletters shares a cluster.

Two items join a cluster when their canonical URLs are identical or the cosine similarity of
their headline+blurb embeddings is above the threshold. Without embeddings the URL rule alone
applies (documented fallback).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.ai.client import cosine
from app.pipeline.text import url_key
from app.store import stable_id


@dataclass
class DedupeInput:
    id: str
    url: str | None
    sender_id: str
    embedding: list[float] | None


def cluster(items: list[DedupeInput], threshold: float) -> dict[str, tuple[str, int]]:
    """Returns item id -> (clusterId, coveredByCount = distinct senders in the cluster)."""
    parent = {it.id: it.id for it in items}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    by_url: dict[str, str] = {}
    for it in items:
        key = url_key(it.url)
        if key:
            if key in by_url:
                union(it.id, by_url[key])
            else:
                by_url[key] = it.id
    with_emb = [it for it in items if it.embedding]
    for i, a in enumerate(with_emb):
        for b in with_emb[i + 1 :]:
            if cosine(a.embedding, b.embedding) >= threshold:
                union(a.id, b.id)

    groups: dict[str, list[DedupeInput]] = {}
    for it in items:
        groups.setdefault(find(it.id), []).append(it)
    out: dict[str, tuple[str, int]] = {}
    for members in groups.values():
        cid = stable_id(*sorted(m.id for m in members), prefix="cl_")
        senders = len({m.sender_id for m in members})
        for m in members:
            out[m.id] = (cid, senders)
    return out
