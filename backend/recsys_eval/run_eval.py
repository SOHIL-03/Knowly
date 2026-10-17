"""Offline evaluation of story recommendation: keyword ranking versus embedding ranking.

Same 50 labelled stories, same two profiles, same labels for every method. No language model is used.

    python -m recsys_eval.run_eval                 # offline: deterministic lexical-hash embeddings, no network
    python -m recsys_eval.run_eval --write         # also write results.<mode>.json and RESULTS.<mode>.md
    python -m recsys_eval.run_eval --live          # real Gemini embeddings (needs GEMINI_API_KEY_1 in backend/.env)
    python -m recsys_eval.run_eval --live --price-per-million 0.15   # also estimate cost, at the price you give

Offline mode has no semantic model: its "embedding" is a hashed bag of words and bigrams, so it can only
match shared vocabulary. Numbers from it measure the ranking logic (facet scoring, mutes, duplicates, MMR)
and say nothing about Gemini embedding quality. Use --live for that. Live vectors are cached in
recsys_eval/embedding_cache.json, so a second run makes no API call and gives the same numbers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.pipeline.score import Story
from app.recommend.embedding import CHARS_PER_TOKEN, embed_resilient
from app.recommend.facets import declared_facets, norm_text, reconcile
from app.recommend.ranking import rank_semantic
from app.recommend.settings import RecConfig
from app.recommend.story_embeddings import story_document

HERE = Path(__file__).resolve().parent
K = 5
GAIN = {"relevant": 2, "adjacent": 1, "irrelevant": 0}
AT = datetime(2026, 10, 15, tzinfo=UTC)
STOP = set("a an and are as at be by can for from has have in into is it its of on or that the their this to was were will with your you "
           "how new now not more most than then them they about after before over under per via what when where which who why".split())


# --- embedders --------------------------------------------------------------------------------------

class HashedLexicalEmbedder:
    """Deterministic stand-in with no semantic knowledge: hashed unigrams and bigrams, L2 normalised."""

    model = "offline-hashed-lexical"

    def __init__(self, dim: int) -> None:
        self.dim = dim
        self.calls = 0
        self.texts = 0

    def embed(self, texts: list[str], *, task: str = "document") -> list[list[float]]:
        self.calls += 1
        self.texts += len(texts)
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        toks = [stem(t) for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in STOP]
        grams = toks + [f"{a}_{b}" for a, b in zip(toks, toks[1:], strict=False)]
        v = [0.0] * self.dim
        for g in grams:
            h = int(hashlib.sha1(g.encode()).hexdigest(), 16)
            v[h % self.dim] += 1.0 if (h >> 20) & 1 else -1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]


class CachedEmbedder:
    """Wraps a real embedder with an on-disk cache keyed by model, dimension, task and text."""

    def __init__(self, inner: Any, path: Path, model: str, dim: int) -> None:
        self.inner, self.path, self.model, self.dim = inner, path, model, dim
        self.cache: dict[str, list[float]] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.calls = 0
        self.texts = 0

    def _key(self, task: str, text: str) -> str:
        return hashlib.sha256(f"{self.model}|{self.dim}|{task}|{text}".encode()).hexdigest()

    def embed(self, texts: list[str], *, task: str = "document") -> list[list[float]]:
        missing = [t for t in dict.fromkeys(texts) if self._key(task, t) not in self.cache]
        if missing:
            self.calls += 1
            self.texts += len(missing)
            for t, v in zip(missing, self.inner.embed(missing, task=task), strict=True):
                self.cache[self._key(task, t)] = [round(x, 6) for x in v]
            self.path.write_text(json.dumps(self.cache), encoding="utf-8")
        return [self.cache[self._key(task, t)] for t in texts]


def stem(tok: str) -> str:
    return tok[:-1] if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss") else tok


# --- keyword baseline -------------------------------------------------------------------------------------

def tokens(text: str) -> set[str]:
    return {stem(t) for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in STOP}


def keyword_rank(stories: list[dict], facets: list[tuple[str, str]], mutes: list[str], weights: dict[str, float]) -> list[str]:
    """Share of a facet's words found in the story, best facet x type weight; a muted phrase anywhere drops the story."""
    facet_tokens = [(weights[t], tokens(x)) for t, x in facets]
    scored = []
    for s in stories:
        hay = " " + norm_text(" ".join([s["headline"], s["blurb"], s["category"], *s["entities"]])) + " "
        if any(f" {norm_text(m)} " in hay for m in mutes):
            continue
        st = tokens(" ".join([s["headline"], s["blurb"], *s["entities"]]))
        scored.append((max((w * len(ft & st) / len(ft) for w, ft in facet_tokens if ft), default=0.0), s["id"]))
    return [sid for _, sid in sorted(scored, key=lambda t: (-t[0], t[1]))]


# --- metrics ----------------------------------------------------------------------------------------------

def dcg(gains: list[int]) -> float:
    return sum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(gains))


def at_k(ids: list[str], by_id: dict[str, dict], profile: str, total_relevant: int, k: int) -> dict[str, float]:
    """Relevance metrics over the first k results (the headline numbers use k = 5)."""
    labels = [by_id[i]["labels"][profile] for i in ids[:k]]
    ideal = sorted((GAIN[s["labels"][profile]] for s in by_id.values()), reverse=True)[:k]
    return {
        f"precision@{k}": sum(1 for lab in labels if lab == "relevant") / k,
        f"ndcg@{k}": dcg([GAIN[lab] for lab in labels]) / dcg(ideal) if dcg(ideal) else 0.0,
        f"recall@{k}": sum(1 for lab in labels if lab == "relevant") / total_relevant if total_relevant else 0.0,
    }


def score_list(top: list[str], by_id: dict[str, dict], profile: str, mute_tags: set[str]) -> dict[str, float]:
    full, top = top, top[:K]
    labels = [by_id[i]["labels"][profile] for i in top]
    all_labels = sorted((GAIN[s["labels"][profile]] for s in by_id.values()), reverse=True)[:K]
    total_relevant = sum(1 for s in by_id.values() if s["labels"][profile] == "relevant")
    def duplicates(ids: list[str]) -> int:
        seen_groups: set[str] = set()
        dupes = 0
        for i in ids:
            g = by_id[i].get("dupGroup")
            if g and g in seen_groups:
                dupes += 1
            if g:
                seen_groups.add(g)
        return dupes

    wide = full[:10]
    return {
        "precision@5": sum(1 for lab in labels if lab == "relevant") / K,
        "precision@5 (relevant+adjacent)": sum(1 for lab in labels if lab != "irrelevant") / K,
        "ndcg@5": dcg([GAIN[lab] for lab in labels]) / dcg(all_labels) if dcg(all_labels) else 0.0,
        "recall@5": sum(1 for lab in labels if lab == "relevant") / total_relevant if total_relevant else 0.0,
        "mute_violation_rate": sum(1 for i in top if set(by_id[i].get("muteTags", [])) & mute_tags) / K,
        "duplicate_rate": duplicates(top) / K,
        "irrelevant_in_top5": sum(1 for lab in labels if lab == "irrelevant") / K,
        **at_k(wide, by_id, profile, total_relevant, 10),
        "mute_violation_rate@10": sum(1 for i in wide if set(by_id[i].get("muteTags", [])) & mute_tags) / max(len(wide), 1),
        "duplicate_rate@10": duplicates(wide) / max(len(wide), 1),
    }


# --- running ----------------------------------------------------------------------------------------------

def build_story(d: dict, emb: list[float] | None) -> Story:
    return Story(d["id"], d["headline"], d["blurb"], d["category"], d["entities"], "s", "Sender", emb, d["id"], 1, False, None)


def mute_diagnostics(pid: str, mutes: list, stories: list[dict], vectors: dict, profile: dict) -> str:
    """Per muted topic: the candidates' median cosine, and the gap above it for the closest stories, with the
    dataset's own ground truth (MUTE = mainly about it, mention = incidental). Use it to pick the mute gaps."""
    from app.recommend.vectors import cos

    tag_of = {norm_text(t): tag for t, tag in zip(profile["muteTopics"], _tags_for(profile), strict=False)}
    out = []
    for m in mutes:
        sims = {s["id"]: cos(vectors[s["id"]], m.embedding) for s in stories if vectors.get(s["id"]) and m.embedding}
        if not sims:
            continue
        med = statistics.median(sims.values())
        tag = tag_of.get(norm_text(m.text))
        out.append(f"profile {pid}, muted topic {m.text!r}: median cosine {med:.3f}")
        for sid, v in sorted(sims.items(), key=lambda t: -t[1])[:8]:
            st = next(x for x in stories if x["id"] == sid)
            kind = "MUTE" if tag in st.get("muteTags", []) else ("mention" if tag in st.get("incidentalMentions", []) else "")
            out.append(f"    gap {v - med:+.3f}  cos {v:.3f}  {kind:8s} {sid} {st['headline'][:50]}")
    return "\n".join(out)


def _tags_for(profile: dict) -> list[str]:
    """Dataset tag for each muted topic, in order (the dataset lists them as muteTags in the same order)."""
    return list(profile["muteTags"])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true", help="use the real Gemini embedder (spends a few thousand tokens)")
    ap.add_argument("--write", action="store_true", help="write results.<mode>.json and RESULTS.<mode>.md")
    ap.add_argument("--runs", type=int, default=200, help="repetitions for the latency measurement")
    ap.add_argument("--price-per-million", type=float, default=None, help="USD per million input tokens, to estimate cost")
    ap.add_argument("--mute-exclude-gap", type=float, default=None, help="override REC_MUTE_EXCLUDE_GAP")
    ap.add_argument("--mute-penalty-gap", type=float, default=None, help="override REC_MUTE_PENALTY_GAP")
    ap.add_argument("--mute-diagnostics", action="store_true", help="print each muted topic's similarity gaps to help calibrate them")
    args = ap.parse_args()

    data = json.loads((HERE / "dataset.json").read_text(encoding="utf-8"))
    stories: list[dict] = data["stories"]
    by_id = {s["id"]: s for s in stories}
    cfg = RecConfig(wildcard_enabled=False, embed_price_per_million_tokens=args.price_per_million)
    if args.mute_exclude_gap is not None:
        cfg = replace(cfg, mute_exclude_gap=args.mute_exclude_gap)
    if args.mute_penalty_gap is not None:
        cfg = replace(cfg, mute_penalty_gap=args.mute_penalty_gap)

    if args.live:
        from app.ai.client import GeminiEmbedder
        from app.ai.gemini_keys import GeminiKeyPool
        from app.config import Settings

        settings = Settings()
        model, dim = settings.gemini_embed_model, settings.embed_dim
        embedder: Any = CachedEmbedder(GeminiEmbedder(settings, GeminiKeyPool(settings)), HERE / "embedding_cache.json", model, dim)
    else:
        model, dim = HashedLexicalEmbedder.model, 768
        embedder = HashedLexicalEmbedder(dim)

    # Story embeddings, as the pipeline makes them: headline + blurb, one batched call for the whole set.
    docs = [story_document(s["headline"], s["blurb"], "") for s in stories]
    story_res = embed_resilient(embedder, docs, "document", cfg, dim)
    story_calls = story_res.api_calls  # calls the pipeline code makes (the network calls this run are in network_calls_this_run)
    vectors = dict(zip((s["id"] for s in stories), story_res.vectors, strict=True))

    methods = ("keyword", "embedding_facets_only", "embedding_full")
    per_profile: dict[str, dict[str, dict[str, float]]] = {m: {} for m in methods}
    latency: dict[str, list[float]] = {m: [] for m in methods}
    facet_chars = 0
    facet_calls = 0
    lists: dict[str, dict[str, list[str]]] = {m: {} for m in methods}
    for pid, profile in data["profiles"].items():
        declared = declared_facets(profile)
        facets = reconcile(declared, [], {}, cfg, model, dim, AT)
        fres = embed_resilient(embedder, [f.embed_text or f.text for f in facets], "query", cfg, dim)
        facet_calls += fres.api_calls
        facet_chars += fres.chars
        for f, v in zip(facets, fres.vectors, strict=True):
            f.embedding = v
        mute_tags = set(profile["muteTags"])
        if args.mute_diagnostics:
            print(mute_diagnostics(pid, [f for f in facets if f.type == "mute"], stories, vectors, profile))
        pos = [(f.type, f.text) for f in facets if f.positive]
        mutes = [f.text for f in facets if f.type == "mute"]
        sobjs = [build_story(s, vectors[s["id"]]) for s in stories]
        positives_only = [f for f in facets if f.positive]
        facets_only_cfg = replace(cfg, mmr_lambda=1.0, duplicate_threshold=1.01, final_count=len(stories))
        full_cfg = replace(cfg, final_count=len(stories))

        def run(method: str, _pos=pos, _mutes=mutes, _s=sobjs, _pf=positives_only, _f=facets, _fo=facets_only_cfg, _fu=full_cfg) -> list[str]:
            if method == "keyword":
                return keyword_rank(stories, _pos, _mutes, cfg.weights)
            if method == "embedding_facets_only":  # facet scoring alone: no mutes, no duplicate removal, no diversity
                res = rank_semantic(_s, _pf, {}, {}, [], set(), AT, _fo, wildcard_on=False)
            else:
                res = rank_semantic(_s, _f, {}, {}, [], set(), AT, _fu, wildcard_on=False)
            return [r.story.id for r in res.ranked] if res else []

        for m in methods:
            lists[m][pid] = run(m)
            per_profile[m][pid] = score_list(lists[m][pid], by_id, pid, mute_tags)
            ts = []
            for _ in range(args.runs):
                t0 = time.perf_counter()
                run(m)
                ts.append((time.perf_counter() - t0) * 1000)
            latency[m].append(statistics.mean(ts))

    def mean_over_profiles(m: str, key: str) -> float:
        return statistics.mean(per_profile[m][p][key] for p in per_profile[m])

    metric_keys = list(next(iter(per_profile["keyword"].values())))
    summary = {m: {k: round(mean_over_profiles(m, k), 3) for k in metric_keys} | {"avg_ranking_latency_ms": round(statistics.mean(latency[m]), 3)}
               for m in methods}

    story_chars, est_tokens = story_res.chars, story_res.est_tokens
    all_chars = story_chars + facet_chars
    all_tokens = -(-all_chars // CHARS_PER_TOKEN)
    cost = None if args.price_per_million is None else all_tokens * args.price_per_million / 1_000_000
    cost_stats = {
        "embedding_model": model, "dimension": dim, "stories": len(stories),
        "story_embedding_api_calls": story_calls, "texts_embedded_per_new_story": round(len(docs) / len(stories), 3),
        "api_calls_per_new_story_batched": round(story_calls / len(stories), 3), "api_calls_per_new_story_unbatched": 1.0,
        "facet_embedding_api_calls": facet_calls,
        "network_calls_this_run": embedder.calls if args.live else 0,
        "note": "network_calls_this_run is 0 when every vector came from embedding_cache.json" if args.live else "offline: no network",
        "estimated_story_tokens": est_tokens, "estimated_total_tokens": all_tokens,
        "token_estimate_method": f"characters / {CHARS_PER_TOKEN}; embed_content returns no token count",
        "estimated_cost_usd": None if cost is None else round(cost, 6),
        "cost_note": "no price given (--price-per-million)" if cost is None else f"at ${args.price_per_million} per million tokens, as supplied",
    }
    result = {"mode": "live" if args.live else "offline-lexical-proxy", "config": {
        "mmr_lambda": cfg.mmr_lambda, "duplicate_threshold": cfg.duplicate_threshold,
        "mute_exclude_gap": cfg.mute_exclude_gap, "mute_penalty_gap": cfg.mute_penalty_gap, "mute_baseline": cfg.mute_baseline,
        "weights": cfg.weights, "wildcard": False, "k": K}, "summary": summary,
        "per_profile": per_profile, "cost": cost_stats, "top5": {m: {p: ids[:K] for p, ids in lists[m].items()} for m in methods}}
    print(render(result, data))
    if args.write:
        tag = "live" if args.live else "offline"
        (HERE / f"results.{tag}.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
        (HERE / f"RESULTS.{tag}.md").write_text(render(result, data, markdown=True), encoding="utf-8")
        print(f"\nwrote {HERE / f'results.{tag}.json'} and {HERE / f'RESULTS.{tag}.md'}")


def render(result: dict, data: dict, markdown: bool = False) -> str:
    live = result["mode"] == "live"
    lines = [f"# Recommendation evaluation ({'live Gemini embeddings' if live else 'offline lexical-hash proxy, NOT Gemini'})", ""]
    if not live:
        lines += ["Offline mode matches shared vocabulary only. These numbers measure the ranking logic, not embedding quality;",
                  "run with --live for Gemini. Paraphrases that share no words with a facet cannot be found in this mode.", ""]
    lines += [f"Stories: {len(data['stories'])}; profiles: " + "; ".join(f"{k} = {v['name']}" for k, v in data["profiles"].items()),
              f"Top-{K} per profile, averaged over profiles. Labels: relevant / adjacent / irrelevant, written by the evaluation's author.", ""]
    labels = {"keyword": "Keyword", "embedding_facets_only": "Embedding, facets only", "embedding_full": "Embedding, full ranking"}
    cols = list(result["summary"]["keyword"])
    lines.append("| Metric | " + " | ".join(labels[m] for m in result["summary"]) + " |")
    lines.append("|---|" + "---|" * len(result["summary"]))
    for c in cols:
        lines.append(f"| {c} | " + " | ".join(str(result['summary'][m][c]) for m in result["summary"]) + " |")
    lines += ["", "Per profile (precision@5 / nDCG@5 / recall@5 / mute violations / duplicates):", ""]
    for m, lab in labels.items():
        for p, v in result["per_profile"][m].items():
            lines.append(f"- {lab}, profile {p}: {v['precision@5']:.2f} / {v['ndcg@5']:.2f} / {v['recall@5']:.2f} / "
                         f"{v['mute_violation_rate']:.2f} / {v['duplicate_rate']:.2f}")
    lines += ["", "Embedding cost:", ""]
    lines += [f"- {k}: {v}" for k, v in result["cost"].items()]
    lines += ["", "Config: " + json.dumps(result["config"])]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
