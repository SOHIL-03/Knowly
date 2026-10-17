"""Embedding calls for the recommender: batching, validation, bounded retry with backoff and a time budget.

The Gemini key pool already retries rate limits and overloads per key. This layer adds a few more
attempts only for transient failures that survived that (server/network errors) and stops when the
overall budget is spent. Exhausted quota, bad requests and authentication errors are never retried.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from app.ai.client import AiQuotaExceeded, AiUnavailable, Embedder
from app.logs import Timer, get_logger, log
from app.recommend.settings import RecConfig
from app.recommend.vectors import clean_vector

logger = get_logger(__name__)
RETRYABLE = {"server_unavailable", "network"}
CHARS_PER_TOKEN = 4  # rough estimate; embed_content does not return a token count


@dataclass
class EmbedResult:
    vectors: list[list[float] | None]
    api_calls: int = 0
    chars: int = 0
    invalid: int = 0
    error: str | None = None  # set when some texts could not be embedded (their vectors are None)
    failures: list[str] = field(default_factory=list)

    @property
    def est_tokens(self) -> int:
        return -(-self.chars // CHARS_PER_TOKEN)

    def est_cost(self, price_per_million: float | None) -> float | None:
        return None if price_per_million is None else self.est_tokens * price_per_million / 1_000_000


def embed_resilient(embedder: Embedder, texts: list[str], task: Literal["document", "query"], cfg: RecConfig, dim: int, *,
                    sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> EmbedResult:
    """Embeds texts in batches. A vector that comes back malformed is returned as None, and a batch that
    still fails after its retries leaves its vectors None; the error is reported, not raised."""
    res = EmbedResult(vectors=[None] * len(texts))
    deadline = clock() + cfg.embed_timeout_seconds
    for start in range(0, len(texts), cfg.embed_batch_size):
        batch = texts[start : start + cfg.embed_batch_size]
        attempt = 0
        while True:
            timer = Timer()
            res.api_calls += 1
            try:
                got = embedder.embed(batch, task=task)
            except AiQuotaExceeded as exc:
                res.error = res.error or f"quota:{exc.category}"
                res.failures.append("quota")
                break  # the pool already tried every key; waiting will not help within this request
            except AiUnavailable as exc:
                delay = cfg.embed_backoff_seconds * (2**attempt)
                if exc.category in RETRYABLE and attempt < cfg.embed_max_retries and clock() + delay < deadline:
                    attempt += 1
                    log(logger, logging.WARNING, "rec_embed_retry", attempt=attempt, category=exc.category, delaySec=delay,
                        texts=len(batch), durationMs=timer.ms())
                    sleep(delay)
                    continue
                res.error = res.error or exc.category
                res.failures.append(exc.category)
                break
            if len(got) != len(batch):
                res.error = res.error or "count_mismatch"
                res.failures.append("count_mismatch")
                break
            res.chars += sum(len(t) for t in batch)
            for i, v in enumerate(got):
                good = clean_vector(v, dim)
                if good is None:
                    res.invalid += 1
                res.vectors[start + i] = good
            break
    return res
