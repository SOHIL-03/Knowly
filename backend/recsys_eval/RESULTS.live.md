# Recommendation evaluation (live Gemini embeddings)

Stories: 50; profiles: A = RAG developer (Python, Gemini, PostgreSQL, pgvector); B = AI agent learner (tool calling, orchestration, evaluation)
Top-5 per profile, averaged over profiles. Labels: relevant / adjacent / irrelevant, written by the evaluation's author.

| Metric | Keyword | Embedding, facets only | Embedding, full ranking |
|---|---|---|---|
| precision@5 | 1.0 | 1.0 | 1.0 |
| precision@5 (relevant+adjacent) | 1.0 | 1.0 | 1.0 |
| ndcg@5 | 1.0 | 1.0 | 1.0 |
| recall@5 | 0.387 | 0.387 | 0.387 |
| mute_violation_rate | 0.0 | 0.0 | 0.0 |
| duplicate_rate | 0.2 | 0.0 | 0.1 |
| irrelevant_in_top5 | 0.0 | 0.0 | 0.0 |
| precision@10 | 0.75 | 0.9 | 0.85 |
| ndcg@10 | 0.879 | 0.955 | 0.934 |
| recall@10 | 0.583 | 0.702 | 0.667 |
| mute_violation_rate@10 | 0.0 | 0.0 | 0.0 |
| duplicate_rate@10 | 0.15 | 0.1 | 0.05 |
| avg_ranking_latency_ms | 3.121 | 40.714 | 46.1 |

Per profile (precision@5 / nDCG@5 / recall@5 / mute violations / duplicates):

- Keyword, profile A: 1.00 / 1.00 / 0.36 / 0.00 / 0.00
- Keyword, profile B: 1.00 / 1.00 / 0.42 / 0.00 / 0.40
- Embedding, facets only, profile A: 1.00 / 1.00 / 0.36 / 0.00 / 0.00
- Embedding, facets only, profile B: 1.00 / 1.00 / 0.42 / 0.00 / 0.00
- Embedding, full ranking, profile A: 1.00 / 1.00 / 0.36 / 0.00 / 0.00
- Embedding, full ranking, profile B: 1.00 / 1.00 / 0.42 / 0.00 / 0.20

Embedding cost:

- embedding_model: gemini-embedding-001
- dimension: 768
- stories: 50
- story_embedding_api_calls: 1
- texts_embedded_per_new_story: 1.0
- api_calls_per_new_story_batched: 0.02
- api_calls_per_new_story_unbatched: 1.0
- facet_embedding_api_calls: 2
- network_calls_this_run: 0
- note: network_calls_this_run is 0 when every vector came from embedding_cache.json
- estimated_story_tokens: 2136
- estimated_total_tokens: 2204
- token_estimate_method: characters / 4; embed_content returns no token count
- estimated_cost_usd: None
- cost_note: no price given (--price-per-million)

Config: {"mmr_lambda": 0.7, "duplicate_threshold": 0.93, "mute_exclude_gap": 0.11, "mute_penalty_gap": 0.06, "mute_baseline": 0.54, "weights": {"project": 1.0, "goal": 0.85, "stack": 0.8, "interest": 0.7, "expertise": 0.5}, "wildcard": false, "k": 5}
