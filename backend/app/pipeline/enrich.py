"""B4 Enrich: fetch the linked article's main text, or mark the item linkOnly."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from app.logs import Timer, get_logger, log
from app.pipeline.text import normalize_url

logger = get_logger(__name__)

PAYWALL_HINTS = ("subscribe to continue", "subscribers only", "sign in to read", "this article is for paid", "create a free account to")
MIN_ARTICLE_CHARS = 600
USER_AGENT = "KnowlyBot/1.0 (+personal newsletter briefing; fetches linked articles once)"


@dataclass
class EnrichResult:
    text: str | None
    link_only: bool
    reason: str
    inner_links: list[str]


def extract_main_text(html: str, base_url: str) -> tuple[str | None, list[str]]:
    import trafilatura

    text = trafilatura.extract(html, url=base_url, include_comments=False, include_tables=False, favor_precision=True)
    links: list[str] = []
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        host = httpx.URL(base_url).host
        for a in soup.select("article a[href], main a[href]"):
            norm = normalize_url(httpx.URL(base_url).join(a["href"]).__str__())
            if norm and httpx.URL(norm).host != host and norm not in links:
                links.append(norm)
            if len(links) >= 2:
                break
    except Exception:
        pass
    return text, links


def fetch_article(url: str, *, timeout: float, max_chars: int, client: httpx.Client | None = None) -> EnrichResult:
    own = client is None
    timer = Timer()
    client = client or httpx.Client(follow_redirects=True, timeout=timeout, headers={"User-Agent": USER_AGENT})
    try:
        res = client.get(url)
    except httpx.HTTPError as exc:
        log(logger, logging.INFO, "article_skipped", host=httpx.URL(url).host, reason=f"fetch_error:{type(exc).__name__}",
            durationMs=timer.ms())
        return EnrichResult(None, True, f"fetch_error:{type(exc).__name__}", [])
    finally:
        if own:
            client.close()
    def skipped(reason: str) -> EnrichResult:
        log(logger, logging.INFO, "article_skipped", host=res.url.host, reason=reason, status=res.status_code,
            redirects=len(res.history), durationMs=timer.ms())
        return EnrichResult(None, True, reason, [])

    if res.status_code in (401, 402, 403, 451):
        return skipped(f"blocked:{res.status_code}")
    if res.status_code >= 400:
        return skipped(f"http:{res.status_code}")
    if "html" not in res.headers.get("content-type", ""):
        return skipped("not_html")
    text, links = extract_main_text(res.text, str(res.url))
    if not text or len(text) < MIN_ARTICLE_CHARS:
        return skipped("too_short_or_paywalled")
    lowered = text[:3000].lower()
    if any(h in lowered for h in PAYWALL_HINTS) and len(text) < 3000:
        return skipped("paywall")
    log(logger, logging.INFO, "article_fetched", host=res.url.host, chars=len(text), status=res.status_code,
        durationMs=timer.ms())
    return EnrichResult(text[:max_chars], False, "ok", links)
