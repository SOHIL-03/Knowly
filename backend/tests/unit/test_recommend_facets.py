"""Interest facets, vector safety and the embedding call wrapper (app/recommend)."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from app.ai.client import AiQuotaExceeded, AiUnavailable
from app.recommend.embedding import embed_resilient
from app.recommend.facets import Facet, declared_facets, facet_id, norm_text, parse_facets, reconcile, text_hash
from app.recommend.settings import RecConfig
from app.recommend.vectors import clean_vector, cos

DIM = 4
CFG = RecConfig()
AT = datetime(2026, 10, 15, tzinfo=UTC)
MODEL = "gemini-embedding-001"


def unit(*xs: float) -> list[float]:
    n = math.sqrt(sum(x * x for x in xs))
    return [x / n for x in xs]


# --- vectors ------------------------------------------------------------------------------------

@pytest.mark.parametrize("value", [None, [], [0.0, 0.0, 0.0, 0.0], [1.0, 2.0, 3.0], [1.0, float("nan"), 0, 0],
                                   [1.0, float("inf"), 0, 0], ["a", "b", "c", "d"], 5])
def test_clean_vector_rejects_malformed_input(value):
    assert clean_vector(value, DIM) is None


def test_clean_vector_accepts_valid_and_coerces_to_floats():
    assert clean_vector([1, 0, 0, 0], DIM) == [1.0, 0.0, 0.0, 0.0]
    assert clean_vector((0.5, 0.5, 0.5, 0.5)) == [0.5, 0.5, 0.5, 0.5]  # no dimension given: any size


def test_cosine_basics_and_safe_failures():
    assert cos(unit(1, 0), unit(1, 0)) == pytest.approx(1.0)
    assert cos(unit(1, 0), unit(0, 1)) == pytest.approx(0.0)
    assert cos(unit(1, 0), unit(-1, 0)) == pytest.approx(-1.0)
    assert cos(unit(1, 1), unit(1, 0)) == pytest.approx(1 / math.sqrt(2))
    assert cos(None, unit(1, 0)) == 0.0 and cos(unit(1, 0), None) == 0.0
    assert cos([0.0, 0.0], unit(1, 0)) == 0.0  # zero vector
    assert cos(unit(1, 0), unit(1, 0, 0)) == 0.0  # incompatible dimensions


# --- declared facets ----------------------------------------------------------------------------

def types(profile):
    return [(t, text) for t, text, _ in declared_facets(profile)]


def test_projects_and_goals_are_separate_facets_and_nothing_is_invented():
    profile = {"project": {"text": "Building a RAG chatbot using Python"}, "goals": [{"topic": "LLM evaluation"}, {"topic": "AI agents"}],
               "muteTopics": ["Cryptocurrency news"]}
    assert types(profile) == [("project", "Building a RAG chatbot using Python"), ("goal", "LLM evaluation"), ("goal", "AI agents"),
                              ("mute", "Cryptocurrency news")]
    assert declared_facets({}) == [] and declared_facets({"project": {"text": "  "}, "goals": []}) == []


def test_all_supported_facet_types_come_from_declared_fields():
    profile = {"project": {"text": "P"}, "goals": [{"topic": "G"}], "stack": ["Gemini, PostgreSQL, and pgvector"],
               "interests": ["Vector search"], "expertise": ["Python backend development"], "muteTopics": ["M"]}
    assert [t for t, _ in types(profile)] == ["project", "goal", "stack", "interest", "expertise", "mute"]


def test_malformed_onboarding_data_is_skipped_not_fatal():
    profile = {"project": "not a dict", "goals": [None, 5, {"topic": "  "}, {"topic": "RAG"}, {"nope": 1}], "stack": "string",
               "interests": [None, "ok", 3], "muteTopics": "crypto"}
    assert types(profile) == [("goal", "RAG"), ("interest", "ok")]


def test_duplicates_collapse_and_a_mute_beats_the_same_positive_topic():
    profile = {"goals": [{"topic": "Vector Search"}, {"topic": "vector  search!"}, {"topic": "Crypto news"}], "muteTopics": ["crypto NEWS", "x", "X"]}
    assert types(profile) == [("goal", "Vector Search"), ("mute", "crypto NEWS"), ("mute", "x")]


def test_counts_and_lengths_are_bounded():
    profile = {"goals": [{"topic": f"goal {i}"} for i in range(20)], "muteTopics": [f"m{i}" for i in range(50)],
               "project": {"text": "x" * 9000}}
    kinds = [t for t, _ in types(profile)]
    assert kinds.count("goal") == 5 and kinds.count("mute") == 30
    assert len(dict(types(profile))["project"]) == 4000


def test_project_embeds_with_its_readme_summary_but_displays_only_what_the_user_wrote():
    [(t, text, embed)] = declared_facets({"project": {"text": "RAG bot", "summary": "Uses pgvector."}})
    assert (t, text, embed) == ("project", "RAG bot", "RAG bot Uses pgvector.")


def test_facet_ids_are_stable_and_text_based():
    assert facet_id("goal", "LLM evaluation") == facet_id("goal", "llm  EVALUATION!")
    assert facet_id("goal", "LLM evaluation") != facet_id("interest", "LLM evaluation")
    assert facet_id("goal", "LLM evaluation") != facet_id("goal", "agents")
    assert facet_id("project", "anything") == facet_id("project", "something else") == "f_project"
    assert norm_text("  Hello,  WORLD ") == "hello world"


# --- reconcile: embedding reuse and regeneration ---------------------------------------------------

def facet(ftype, text, emb, weight=1.0, created=AT - timedelta(days=3)):
    return Facet(facet_id(ftype, text), text, ftype, weight, emb, created, created, None, text_hash(MODEL, DIM, text))


def run(declared, previous=(), legacy=None):
    return reconcile(declared, list(previous), legacy or {}, CFG, MODEL, DIM, AT)


def test_unchanged_facet_text_reuses_its_embedding_and_timestamps():
    old = facet("goal", "LLM evaluation", unit(1, 0, 0, 0))
    [new] = run([("goal", "LLM evaluation", None)], [old])
    assert new.embedding == old.embedding and new.created_at == old.created_at and new.updated_at == old.updated_at


def test_changed_text_drops_the_embedding_but_keeps_the_project_id_and_creation_time():
    old = facet("project", "RAG bot", unit(1, 0, 0, 0))
    [new] = run([("project", "RAG bot with agents", None)], [old])
    assert new.id == old.id and new.embedding is None and new.created_at == old.created_at and new.updated_at == AT


def test_changed_model_or_dimension_regenerates():
    old = facet("goal", "g", unit(1, 0, 0, 0))
    [same] = reconcile([("goal", "g", None)], [old], {}, CFG, MODEL, DIM, AT)
    [other_model] = reconcile([("goal", "g", None)], [old], {}, CFG, "other-model", DIM, AT)
    [other_dim] = reconcile([("goal", "g", None)], [old], {}, CFG, MODEL, 768, AT)
    assert same.embedding is not None and other_model.embedding is None and other_dim.embedding is None


def test_new_facets_get_configured_initial_weights_and_existing_weights_are_kept():
    old = facet("goal", "kept", unit(1, 0, 0, 0), weight=0.33)
    out = run([("project", "p", None), ("goal", "kept", None), ("stack", "s", None), ("interest", "i", None),
               ("expertise", "e", None), ("mute", "m", None)], [old])
    assert [f.weight for f in out] == [CFG.weights["project"], 0.33, CFG.weights["stack"], CFG.weights["interest"],
                                       CFG.weights["expertise"], 1.0]
    custom = RecConfig(weights={"project": 0.5, "goal": 0.4, "stack": 0.3, "interest": 0.2, "expertise": 0.1})
    [f] = reconcile([("goal", "g", None)], [], {}, custom, MODEL, DIM, AT)
    assert f.weight == 0.4


def test_legacy_profile_embeddings_are_reused_when_the_text_matches():
    legacy = {"vector search": unit(0, 1, 0, 0), "bad": [1.0]}  # the second one has the wrong dimension
    out = run([("goal", "vector search", None), ("goal", "bad", None)], legacy=legacy)
    assert out[0].embedding == unit(0, 1, 0, 0) and out[1].embedding is None


def test_stored_facets_parse_tolerantly_and_wrong_dimension_embeddings_are_dropped():
    raw = [{"id": "f1", "text": "ok", "type": "goal", "weight": 0.9, "embedding": [1, 0, 0, 0], "updatedAt": AT},
           {"id": "f2", "text": "bad dim", "type": "goal", "weight": 1.0, "embedding": [1, 0]},
           {"id": "f3", "text": "bad type", "type": "hobby", "weight": 1.0},
           {"id": "f4", "text": "bad weight", "type": "goal", "weight": "heavy"},
           "garbage", None, {"text": "no id", "type": "goal"}]
    out = parse_facets(raw, DIM)
    assert [f.id for f in out] == ["f1", "f2"]
    assert out[0].embedding == [1.0, 0.0, 0.0, 0.0] and out[1].embedding is None
    assert parse_facets(None) == [] and parse_facets("x") == []


# --- embedding wrapper --------------------------------------------------------------------------

class Scripted:
    """Embedder returning scripted outcomes (exceptions are raised, lists returned) per call."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[list[str]] = []

    def embed(self, texts, *, task="document"):
        self.calls.append(list(texts))
        out = self.outcomes.pop(0) if self.outcomes else [unit(1, 0, 0, 0) for _ in texts]
        if isinstance(out, Exception):
            raise out
        return out


def ok(n):
    return [unit(1, 0, 0, 0) for _ in range(n)]


def cfg(**kw):
    return RecConfig(**{"embed_backoff_seconds": 1.0, "embed_max_retries": 2, "embed_batch_size": 50, **kw})


def test_embeds_in_batches_and_counts_calls_chars_and_tokens():
    e = Scripted()
    res = embed_resilient(e, ["a" * 40] * 5, "document", cfg(embed_batch_size=2), DIM)
    assert [len(c) for c in e.calls] == [2, 2, 1] and res.api_calls == 3 and res.error is None
    assert all(v == unit(1, 0, 0, 0) for v in res.vectors)
    assert res.chars == 200 and res.est_tokens == 50 and res.est_cost(0.2) == pytest.approx(50 * 0.2 / 1e6) and res.est_cost(None) is None


def test_transient_errors_retry_with_exponential_backoff_then_succeed():
    sleeps: list[float] = []
    e = Scripted(AiUnavailable("busy", "server_unavailable"), AiUnavailable("net", "network"), ok(1))
    res = embed_resilient(e, ["x"], "query", cfg(), DIM, sleep=sleeps.append)
    assert sleeps == [1.0, 2.0] and res.api_calls == 3 and res.vectors == ok(1) and res.error is None


def test_retries_are_bounded_and_the_error_is_reported_not_raised():
    sleeps: list[float] = []
    e = Scripted(*[AiUnavailable("busy", "server_unavailable")] * 5)
    res = embed_resilient(e, ["x"], "query", cfg(embed_max_retries=2), DIM, sleep=sleeps.append)
    assert res.api_calls == 3 and res.vectors == [None] and res.error == "server_unavailable" and sleeps == [1.0, 2.0]


@pytest.mark.parametrize("exc", [AiQuotaExceeded("used up", "quota_exhausted"), AiUnavailable("denied", "auth"),
                                 AiUnavailable("bad", "bad_request"), AiUnavailable("other", "other")])
def test_quota_auth_and_bad_requests_are_never_retried(exc):
    sleeps: list[float] = []
    res = embed_resilient(Scripted(exc, ok(1)), ["x"], "query", cfg(), DIM, sleep=sleeps.append)
    assert res.api_calls == 1 and sleeps == [] and res.vectors == [None] and res.error


def test_the_time_budget_stops_retries():
    now = [0.0]
    e = Scripted(*[AiUnavailable("busy", "server_unavailable")] * 5)
    res = embed_resilient(e, ["x"], "query", cfg(embed_timeout_seconds=1.5), DIM, sleep=lambda s: now.__setitem__(0, now[0] + s),
                          clock=lambda: now[0])
    assert res.api_calls == 2 and res.vectors == [None]  # one retry (1s) fits in 1.5s, the next (2s) does not


def test_a_failed_batch_does_not_lose_the_other_batches():
    e = Scripted(ok(2), AiUnavailable("down", "other"), ok(1))
    res = embed_resilient(e, ["a", "b", "c", "d", "e"], "document", cfg(embed_batch_size=2), DIM)
    assert [v is not None for v in res.vectors] == [True, True, False, False, True] and res.error == "other"


@pytest.mark.parametrize("bad", [[1.0] * 5, [0.0] * DIM, [float("nan")] * DIM, []])
def test_malformed_vectors_come_back_as_none_and_are_counted(bad):
    res = embed_resilient(Scripted([unit(1, 0, 0, 0), bad]), ["a", "b"], "document", cfg(), DIM)
    assert res.vectors[0] is not None and res.vectors[1] is None and res.invalid == 1


def test_a_wrong_number_of_vectors_is_an_error():
    res = embed_resilient(Scripted(ok(1)), ["a", "b"], "document", cfg(), DIM)
    assert res.vectors == [None, None] and res.error == "count_mismatch"
