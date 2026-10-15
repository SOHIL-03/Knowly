"""B2-B5 helpers: cleaning, URL handling, header check, clustering and article fetching."""

import httpx

from app.pipeline.dedupe import DedupeInput, cluster
from app.pipeline.enrich import fetch_article
from app.pipeline.text import clean_issue_html, is_newsletter, normalize_url, url_in_issue
from tests.fakes import FakeEmbedder


def test_normalize_strips_tracking_but_keeps_destination():
    assert normalize_url("https://Ex.com/a/?utm_source=x&id=7#frag") == "https://ex.com/a/?id=7"
    assert normalize_url("https://github.com/o/r?ref=main") == "https://github.com/o/r?ref=main"
    assert normalize_url("javascript:alert(1)") is None
    assert normalize_url(None) is None


def test_model_urls_must_appear_in_the_issue():
    issue_urls = ["https://ex.com/a"]
    assert url_in_issue("https://ex.com/a?utm_medium=email", issue_urls) == "https://ex.com/a"
    assert url_in_issue("https://ex.com/invented", issue_urls) is None


def test_clean_issue_removes_pixels_scripts_and_footer():
    html = (
        "<html><head><script>x()</script></head><body><img src='p.gif' width='1' height='1'>"
        "<h2>Story</h2><p>Body text. <a href='https://ex.com/s?utm_source=n'>Read</a></p>"
        "<p>More words here to make the body longer than the footer threshold is.</p>"
        "<p>You are receiving this email because... <a href='https://ex.com/unsub'>Unsubscribe</a></p></body></html>"
    )
    text, urls = clean_issue_html(html)
    assert "x()" not in text and "receiving" not in text
    assert "Read (https://ex.com/s)" in text
    assert urls == ["https://ex.com/s"]


def test_newsletter_header_check():
    assert is_newsletter({"List-Unsubscribe": "<mailto:x>"})
    assert is_newsletter({"list-id": "x"})
    assert not is_newsletter({"From": "friend@example.com"})


def test_cluster_by_url_and_embedding_with_url_fallback():
    emb = FakeEmbedder().embed([
        "Firestore vector search nearest neighbour embeddings",
        "Firestore adds vector search nearest neighbour embeddings",
        "Pub/Sub dead letter topics",
    ])
    items = [
        DedupeInput("a", "https://x.dev/v", "s1", emb[0]),
        DedupeInput("b", "https://x.dev/v/", "s2", None),  # same URL, different trailing slash
        DedupeInput("c", None, "s3", emb[1]),
        DedupeInput("d", None, "s3", emb[2]),
    ]
    res = cluster(items, threshold=0.85)
    assert res["a"][0] == res["b"][0] == res["c"][0]
    assert res["a"][1] == 3  # covered by three distinct newsletters
    assert res["d"][0] != res["a"][0] and res["d"][1] == 1
    no_emb = cluster([DedupeInput(i.id, i.url, i.sender_id, None) for i in items], threshold=0.85)
    assert no_emb["a"][0] == no_emb["b"][0] != no_emb["c"][0]


class StubClient:
    def __init__(self, status=200, text="", ctype="text/html"):
        self.status, self.text, self.ctype = status, text, ctype

    def get(self, url):
        return httpx.Response(self.status, text=self.text, headers={"content-type": self.ctype}, request=httpx.Request("GET", url))


ARTICLE = "<html><body><article><h1>Title</h1>" + "".join(
    f"<p>Paragraph {i} explains the change in detail with enough words to count as real article text.</p>" for i in range(20)
) + "<a href='https://other.dev/more'>more</a></article></body></html>"


def test_fetch_article_extracts_text():
    res = fetch_article("https://x.dev/a", timeout=1, max_chars=5000, client=StubClient(text=ARTICLE))
    assert not res.link_only and "Paragraph 3" in res.text
    assert res.inner_links == ["https://other.dev/more"]


def test_fetch_article_marks_paywalls_and_blocks_link_only():
    assert fetch_article("https://x.dev/a", timeout=1, max_chars=100, client=StubClient(status=402)).reason == "blocked:402"
    short = "<html><body><p>Subscribe to continue reading.</p></body></html>"
    res = fetch_article("https://x.dev/a", timeout=1, max_chars=100, client=StubClient(text=short))
    assert res.link_only and res.text is None
    assert fetch_article("https://x.dev/a", timeout=1, max_chars=100, client=StubClient(ctype="application/pdf")).link_only
