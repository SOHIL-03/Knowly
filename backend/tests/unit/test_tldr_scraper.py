import json

import httpx
import pytest

from app.tldr.scraper import IssueNotFound, TldrScraper

ARCHIVE = json.dumps({"mainEntity": {"itemListElement": [
    {"url": "https://tldr.tech/tech/2026-10-17"},  # listed but not published yet
    {"url": "https://tldr.tech/tech/2026-10-16"},
    {"url": "https://tldr.tech/tech/2026-10-15"},
]}})
ISSUE = """<h1>TLDR 2026-10-16</h1><h2>Roadster, Muse</h2>
<section><header><div>📱</div><h3>Big Tech &amp; Startups</h3></header>
<article><a href="https://x.com/a?utm_source=tldrnewsletter&id=1"><h3>Roadster flies (4 minute read)</h3></a>
<div class="newsletter-html">It <b>flew</b>.</div></article>
<article><a href="https://ad.com"><h3>Join us (Sponsor)</h3></a><div class="newsletter-html">Ad.</div></article></section>"""


def make() -> TldrScraper:
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/tech/archives":
            return httpx.Response(200, text=f'<script type="application/ld+json">{ARCHIVE}</script>')
        if path == "/tech/2026-10-16":
            return httpx.Response(200, text=ISSUE)
        return httpx.Response(404)

    return TldrScraper(client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_latest_skips_unpublished_and_parses_stories():
    issue = make().latest("tech")
    assert issue.date == "2026-10-16"
    assert issue.headline == "Roadster, Muse"
    section = issue.sections[0]
    assert (section.emoji, section.name) == ("📱", "Big Tech & Startups")
    first, second = section.stories
    assert (first.id, second.id) == ("s1", "s2")
    assert (first.title, first.read_minutes, first.sponsored) == ("Roadster flies", 4, False)
    assert first.url == "https://x.com/a?id=1"
    assert first.summary == "It flew ."
    assert (second.title, second.sponsored) == ("Join us", True)


def test_missing_issue_and_bad_input():
    s = make()
    with pytest.raises(IssueNotFound):
        s.get_issue("2026-10-15")
    with pytest.raises(Exception, match="Invalid"):
        s.list_issues("../etc")
