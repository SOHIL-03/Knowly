"""Semantic recommendations through the real pipeline (Firestore emulator, fake embedder and LLM)."""

from __future__ import annotations

import httpx
import pytest

from app.learner.model import topic_key
from app.pipeline import personal
from app.recommend.facets import sync_facets
from app.recommend.feedback import apply_feedback, idempotency_key
from app.recommend.service import semantic_rank
from app.recommend.story_embeddings import ensure_story_embeddings
from tests.integration.conftest import FS, PROJECT, User
from tests.integration.test_pipeline import today

FORBIDDEN_KEYS = ("rerank", "llm", "gemini", "classification", "explanation")


@pytest.fixture
def briefing(asha):
    asha.post("/briefings/generate", {})
    return today(asha)


def items_of(svc) -> list[dict]:
    return [{"id": s.id, **(s.to_dict() or {})} for s in svc.paths.items().stream()]


def doc_requests(svc) -> int:
    return sum(n for task, n in svc.embedder.requests if task == "document")


def pick(b, facet=True):
    return next(r for r in b["ranked"] if bool(r.get("facetId")) == facet and not r["wildcard"])


# --- end to end -------------------------------------------------------------------------------------

def test_briefing_is_ranked_semantically_and_persists_the_matches(briefing, svc, asha):
    b = briefing
    assert b["status"] == "ready" and b["rankingMode"] == "semantic"
    ranked = b["ranked"]
    assert ranked and len(ranked) <= svc.settings.rec_final_count + 1
    facet_ids = {f["id"] for f in svc.paths.profile(asha.uid).get().to_dict()["facets"]}
    for r in ranked:
        assert {"itemId", "score", "semanticScore", "facetId", "matchedFacets", "rankingVersion", "noveltyScore", "reason"} <= set(r)
        assert r["rankingVersion"] == "semantic-v1" and r["facetId"] in facet_ids
        assert not any(bad in key.lower() for key in r for bad in FORBIDDEN_KEYS)
        assert r["matchedFacets"][0]["facetId"] == r["facetId"]
        assert r["rawSimilarity"] is not None and r["semanticScore"] is not None
    assert len({r["itemId"] for r in ranked}) == len(ranked)  # nothing twice
    # everything the plan used came from the ranked list (plus review material, absent in a first briefing)
    planned = {i for s in b["segments"] for i in s["itemIds"]}
    assert planned <= {r["itemId"] for r in ranked}
    # the explanation is the user's own facet text, never generated
    texts = {f["text"] for f in svc.paths.profile(asha.uid).get().to_dict()["facets"]}
    explained = [r for r in ranked if r["reason"].startswith(("Recommended because", "Matches your"))]
    assert explained and all(any(t in r["reason"] for t in texts) for r in explained)
    shown = [src for s in b["segments"] for src in s["sources"] if src.get("matchedFacet")]
    assert shown and all(src["matchedFacet"]["text"] in src["reason"] for src in shown)
    assert sum(1 for r in ranked if r["wildcard"]) <= 1


def test_declared_facets_are_stored_separately_with_weights_embeddings_and_timestamps(briefing, svc, asha):
    facets = svc.paths.profile(asha.uid).get().to_dict()["facets"]
    assert [(f["type"], f["text"]) for f in facets] == [
        ("project", "RAG app on Firestore vector search and Cloud Run with FastAPI"), ("goal", "vector search"), ("goal", "LLM evals")]
    s = svc.settings
    assert [f["weight"] for f in facets] == [s.rec_weight_project, s.rec_weight_goal, s.rec_weight_goal]
    for f in facets:
        assert len(list(f["embedding"])) == s.embed_dim and f["createdAt"] and f["updatedAt"] and f["textHash"]
        assert f["model"] == s.gemini_embed_model and f["dim"] == s.embed_dim


def test_ranking_makes_no_ai_calls_at_all(briefing, svc, asha):
    class Exploding:
        def __getattr__(self, name):
            raise AssertionError(f"ranking called the language model ({name})")

    class Silent:
        def embed(self, *a, **k):
            raise AssertionError("ranking called the embedding API")

    svc.llm, svc.embedder = Exploding(), Silent()
    items = [i for i in items_of(svc) if not i.get("isSponsor")]
    stories = personal.unique_stories(items, {i["id"]: (i.get("clusterId") or i["id"], i.get("coveredByCount") or 1) for i in items})
    signals = personal.user_signals(svc, asha.uid, "none")
    ranked = semantic_rank(svc, asha.uid, "none", stories, signals, True)
    assert ranked and all(r.ranking_version == "semantic-v1" for r in ranked)


# --- embeddings are cached --------------------------------------------------------------------------------

def test_story_embeddings_are_made_once_and_reused_by_later_briefings(asha, svc):
    asha.post("/briefings/generate", {})
    first = doc_requests(svc)
    stored = [i for i in items_of(svc) if not i.get("isSponsor")]
    assert first >= 1 and all(i.get("embeddingSig") and i.get("embeddingDim") == svc.settings.embed_dim for i in stored)
    asha.post("/briefings/generate", {"regenerate": True})
    assert doc_requests(svc) == first  # no story was embedded again


def test_story_embeddings_are_shared_between_users(asha, client, svc):
    asha.post("/briefings/generate", {})
    first = doc_requests(svc)
    ravi = User(client)
    ravi.onboard("Python web API with Pydantic models", ["fastapi dependencies"])
    ravi.post("/briefings/generate", {})
    assert doc_requests(svc) == first and today(ravi)["rankingMode"] == "semantic"


def test_only_changed_stories_are_embedded_again(asha, svc):
    asha.post("/briefings/generate", {})
    items = [i for i in items_of(svc) if not i.get("isSponsor")]
    before = doc_requests(svc)
    assert ensure_story_embeddings(svc, items).embedded == 0 and doc_requests(svc) == before
    victim = items[0]
    svc.paths.item(victim["id"]).update({"blurb": victim["blurb"] + " A new paragraph about index warm-up."})
    items = [i for i in items_of(svc) if not i.get("isSponsor")]
    stats = ensure_story_embeddings(svc, items)
    assert (stats.embedded, stats.cached, stats.api_calls) == (1, len(items) - 1, 1)
    assert svc.embedder.requests[-1] == ("document", 1)


def test_article_text_arriving_later_changes_the_story_signature(asha, svc):
    asha.post("/briefings/generate", {})
    items = [i for i in items_of(svc) if not i.get("isSponsor")]
    target = next(i for i in items if not i.get("fullTextPath"))
    svc.blobs.put_text("articles/late.txt", "A long opening paragraph. " * 40)
    svc.paths.item(target["id"]).update({"fullTextPath": "articles/late.txt", "linkOnly": False})
    items = [i for i in items_of(svc) if not i.get("isSponsor")]
    assert ensure_story_embeddings(svc, items).embedded == 1


def test_facets_reuse_embeddings_until_their_text_changes(asha, svc):
    asha.put("/profile", {"project": {"text": "RAG app on Firestore vector search and Cloud Run with FastAPI"},
                          "goals": ["vector search", "LLM evals"], "stack": ["Gemini and pgvector"]})
    n = len(svc.embedder.requests)
    state = sync_facets(svc, asha.uid)
    assert (state.embedded, state.api_calls) == (1, 1) and len(svc.embedder.requests) == n + 1  # only the new stack facet
    state = sync_facets(svc, asha.uid)
    assert (state.embedded, state.api_calls) == (0, 0) and len(svc.embedder.requests) == n + 1
    stored = {f["text"]: f for f in svc.paths.profile(asha.uid).get().to_dict()["facets"]}
    old_vector = list(stored["Gemini and pgvector"]["embedding"])
    asha.put("/profile", {"project": {"text": "RAG app on Firestore vector search and Cloud Run with FastAPI"},
                          "goals": ["vector search", "LLM evals"], "stack": ["PostgreSQL and pgvector"]})
    state = sync_facets(svc, asha.uid)
    assert state.embedded == 1  # the changed text was embedded again
    stored = {f["text"]: f for f in svc.paths.profile(asha.uid).get().to_dict()["facets"]}
    assert "Gemini and pgvector" not in stored and list(stored["PostgreSQL and pgvector"]["embedding"]) != old_vector
    assert [f["type"] for f in stored.values()].count("stack") == 1


# --- failure paths ----------------------------------------------------------------------------------------

def test_embedding_outage_still_produces_a_briefing_with_the_original_scorer(asha, svc):
    svc.embedder.fail = True
    asha.post("/briefings/generate", {})
    b = today(asha)
    assert b["status"] == "ready" and b["rankingMode"] == "legacy" and b["dedupeMode"] == "url_only"
    assert b["ranked"] and all(r["rankingVersion"] == "legacy-v1" and "facetId" not in r for r in b["ranked"])


@pytest.mark.parametrize("corrupt", ["wrong_dim", "zero", "nan"])
def test_malformed_embeddings_are_never_stored_or_used(asha, svc, corrupt):
    svc.embedder.corrupt = corrupt
    asha.post("/briefings/generate", {})
    b = today(asha)
    assert b["status"] == "ready" and b["rankingMode"] == "legacy"
    assert not any(i.get("embeddingSig") for i in items_of(svc))


def test_a_user_without_a_profile_keeps_the_original_behaviour(client, svc):
    u = User(client)
    u.post("/me/bootstrap", {"timezone": "UTC"})
    u.post("/briefings/generate", {})
    b = today(u)
    assert b["status"] == "ready" and b["rankingMode"] == "legacy" and b["ranked"]
    assert not (svc.paths.profile(u.uid).get().to_dict() or {}).get("facets")


def test_a_profile_with_only_mutes_falls_back_without_error(client, svc):
    u = User(client)
    u.onboard("", [], mutes=["Pydantic"])
    u.post("/briefings/generate", {})
    b = today(u)
    assert b["status"] == "ready" and b["rankingMode"] == "legacy"
    assert not any("Pydantic" in src["headline"] for s in b["segments"] for src in s["sources"])


def test_the_feature_can_be_switched_off(asha, svc):
    svc.settings.rec_enabled = False
    asha.post("/briefings/generate", {})
    b = today(asha)
    assert b["status"] == "ready" and b["rankingMode"] == "legacy"
    assert not any(i.get("embeddingSig") for i in items_of(svc)) and not svc.paths.profile(asha.uid).get().to_dict().get("facets")
    assert apply_feedback(svc, asha.uid, [{"type": "save", "itemId": b["ranked"][0]["itemId"], "briefingId": b["id"]}]) == {}
    assert list(svc.paths.feedback(asha.uid).stream()) == []


def test_muted_topics_are_facets_and_those_stories_never_reach_the_list(client, svc):
    u = User(client)
    u.onboard("Backend on Cloud Run", ["serverless"], mutes=["Pydantic", "Web Speech"])
    u.post("/briefings/generate", {})
    b = today(u)
    assert b["rankingMode"] == "semantic"
    facets = svc.paths.profile(u.uid).get().to_dict()["facets"]
    assert [f["text"] for f in facets if f["type"] == "mute"] == ["Pydantic", "Web Speech"]
    for r in b["ranked"]:
        headline = svc.paths.item(r["itemId"]).get().to_dict()["headline"]
        assert "Pydantic" not in headline and "Speech" not in headline


# --- feedback ---------------------------------------------------------------------------------------------

def post_event(u, b, typ, item):
    r = u.post("/events", {"events": [{"type": typ, "briefingId": b["id"], "segId": "s1", "itemId": item}]})
    assert r.status_code == 200
    return r


def prefs(svc, uid):
    return (svc.paths.profile(uid).get().to_dict() or {}).get("facetPrefs") or {}


def test_less_like_this_lowers_the_matched_facets_learned_weight_once(briefing, asha, svc):
    row = pick(briefing)
    for _ in range(3):  # a retry, a double tap and a repeated batch must not stack
        post_event(asha, briefing, "lessLikeThis", row["itemId"])
    assert prefs(svc, asha.uid)[row["facetId"]]["delta"] == pytest.approx(svc.settings.rec_feedback_less)
    [fb] = [d.to_dict() for d in svc.paths.feedback(asha.uid).stream()]
    assert fb["userId"] == asha.uid and fb["storyId"] == row["itemId"] and fb["briefingId"] == briefing["id"]
    assert fb["action"] == "lessLikeThis" and fb["matchedFacetId"] == row["facetId"] and fb["createdAt"]
    assert fb["idempotencyKey"] == idempotency_key(briefing["id"], row["itemId"], "lessLikeThis")
    assert fb["topic"] == svc.paths.item(row["itemId"]).get().to_dict()["category"]


def test_save_ask_and_deeper_raise_the_weight_by_their_configured_steps(briefing, asha, svc):
    rows = [r for r in briefing["ranked"] if r.get("facetId")]
    row = rows[0]
    s = svc.settings
    assert asha.post("/library", {"itemId": row["itemId"], "briefingId": briefing["id"], "segId": "s1"}).status_code == 200
    assert prefs(svc, asha.uid)[row["facetId"]]["delta"] == pytest.approx(s.rec_feedback_save)
    ev = {"briefingId": briefing["id"], "itemId": row["itemId"]}
    assert apply_feedback(svc, asha.uid, [{"type": "ask", **ev}, {"type": "deeper", **ev}, {"type": "ask", **ev}]) == {"applied": 2, "duplicate": 1}
    assert prefs(svc, asha.uid)[row["facetId"]]["delta"] == pytest.approx(s.rec_feedback_save + s.rec_feedback_ask + s.rec_feedback_deeper)
    # saving again is the same submission: no change
    assert asha.post("/library", {"itemId": row["itemId"], "briefingId": briefing["id"], "segId": "s1"}).status_code == 200
    assert prefs(svc, asha.uid)[row["facetId"]]["delta"] == pytest.approx(s.rec_feedback_save + s.rec_feedback_ask + s.rec_feedback_deeper)


def test_the_same_action_in_another_briefing_counts_again(briefing, asha, svc):
    row = pick(briefing)
    post_event(asha, briefing, "lessLikeThis", row["itemId"])
    assert apply_feedback(svc, asha.uid, [{"type": "lessLikeThis", "briefingId": "another", "itemId": row["itemId"]}])["applied"] == 1


def test_knew_this_already_changes_familiarity_not_facet_weights(briefing, asha, svc):
    row = pick(briefing)
    category = svc.paths.item(row["itemId"]).get().to_dict()["category"]
    post_event(asha, briefing, "knewThis", row["itemId"])
    prof = svc.paths.profile(asha.uid).get().to_dict()
    assert prof["familiarity"][topic_key(category)]["w"] == pytest.approx(svc.settings.rec_knew_step)
    assert not prof.get("facetPrefs")
    asha.post("/briefings/generate", {"regenerate": True})
    again = asha.get("/briefings/today").json()
    marked = [r for r in again["ranked"] if svc.paths.item(r["itemId"]).get().to_dict()["category"] == category]
    assert marked and all(r["noveltyScore"] is not None and r["noveltyScore"] <= 1 - svc.settings.rec_knew_step + 1e-6 for r in marked)


def test_declared_weights_stay_separate_from_learned_ones_and_can_be_reset(briefing, asha, svc):
    row = pick(briefing)
    post_event(asha, briefing, "lessLikeThis", row["itemId"])
    facets = asha.get("/profile/facets").json()["facets"]
    hit = next(f for f in facets if f["id"] == row["facetId"])
    assert hit["declaredWeight"] == next(f["weight"] for f in svc.paths.profile(asha.uid).get().to_dict()["facets"] if f["id"] == hit["id"])
    assert hit["learnedDelta"] == pytest.approx(-0.15) and hit["effectiveWeight"] == pytest.approx(hit["declaredWeight"] - 0.15)
    assert all("embedding" not in f for f in facets)
    assert asha.post("/profile/facets/reset-learning").json() == {"ok": True}
    hit = next(f for f in asha.get("/profile/facets").json()["facets"] if f["id"] == row["facetId"])
    assert hit["learnedDelta"] == 0 and hit["effectiveWeight"] == hit["declaredWeight"]


def test_editing_the_profile_keeps_what_feedback_taught_and_the_facet_embeddings(briefing, asha, svc):
    row = pick(briefing)
    post_event(asha, briefing, "lessLikeThis", row["itemId"])
    asha.put("/profile", {"project": {"text": "RAG app on Firestore vector search and Cloud Run with FastAPI"},
                          "goals": ["vector search", "LLM evals"], "muteTopics": ["Crypto"]})
    prof = svc.paths.profile(asha.uid).get().to_dict()
    assert prof["facetPrefs"][row["facetId"]]["delta"] == pytest.approx(-0.15) and len(prof["facets"]) == 3
    n = len(svc.embedder.requests)
    sync_facets(svc, asha.uid)
    assert len(svc.embedder.requests) == n + 1  # only the new mute topic needed an embedding


def test_feedback_failures_never_break_the_request(briefing, asha, svc, monkeypatch):
    import app.recommend.feedback as fb

    monkeypatch.setattr(fb, "_record", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    row = pick(briefing)
    assert post_event(asha, briefing, "lessLikeThis", row["itemId"]).json()["accepted"] == 1
    assert apply_feedback(svc, asha.uid, [{"type": "save"}, {"type": "play", "itemId": "x"}, {"type": "knewThis", "itemId": ""}]) == {"applied": 0, "duplicate": 0}


# --- isolation ---------------------------------------------------------------------------------------------

def test_feedback_is_private_to_the_user_and_deleted_with_the_account(briefing, asha, client, svc):
    row = pick(briefing)
    post_event(asha, briefing, "lessLikeThis", row["itemId"])
    other = User(client)
    other.onboard("Python web API", ["fastapi"])
    assert list(svc.paths.feedback(other.uid).stream()) == [] and not prefs(svc, other.uid)
    key = idempotency_key(briefing["id"], row["itemId"], "lessLikeThis")
    url = f"{FS}/v1/projects/{PROJECT}/databases/(default)/documents/users/{asha.uid}/feedback/{key}"
    assert httpx.get(url, headers={"Authorization": f"Bearer {asha.token}"}).status_code == 200
    assert httpx.get(url, headers={"Authorization": f"Bearer {other.token}"}).status_code == 403
    assert httpx.patch(url, headers={"Authorization": f"Bearer {asha.token}"}, json={"fields": {}}).status_code == 403
    export = asha.get("/export").json()
    assert key in {d["id"] for d in export["user"]["collections"]["feedback"]}
    assert asha.delete("/account").status_code == 200
    assert list(svc.paths.feedback(asha.uid).stream()) == []


# --- B5 clustering threshold ---------------------------------------------------------------------------------

def test_clustering_uses_the_configurable_duplicate_threshold_when_recommendations_are_on(svc):
    from app.pipeline.ingest import embed_and_cluster

    def make(i, sender):
        it = {"id": f"it_{i}", "headline": "pgvector gets iterative scans", "blurb": "Filtered vector search stops returning empty results.",
              "senderId": sender, "url": f"https://example.com/{i}", "isSponsor": False}
        svc.paths.item(it["id"]).set({k: v for k, v in it.items() if k != "id"})
        return it

    items = [make(1, "s1"), make(2, "s2")]  # the same story from two newsletters, different URLs: identical vectors
    clusters, mode = embed_and_cluster(svc, items)
    assert mode == "embedding" and clusters["it_1"][0] == clusters["it_2"][0] and clusters["it_1"][1] == 2
    svc.settings.rec_duplicate_threshold = 1.01  # nothing counts as a duplicate any more
    clusters, _ = embed_and_cluster(svc, [make(3, "s1"), make(4, "s2")])
    assert clusters["it_3"][0] != clusters["it_4"][0]
    svc.settings.rec_enabled = False  # the original threshold (0.88) applies again
    clusters, _ = embed_and_cluster(svc, [make(5, "s1"), make(6, "s2")])
    assert clusters["it_5"][0] == clusters["it_6"][0]
