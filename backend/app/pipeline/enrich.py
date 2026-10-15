"""B4 Enrich: fetch the linked article's main text, or mark the item linkOnly."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from app.logs import get_logger, log
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
    client = client or httpx.Client(follow_redirects=True, timeout=timeout, headers={"User-Agent": USER_AGENT})
    try:
        res = client.get(url)
    except httpx.HTTPError as exc:
        return EnrichResult(None, True, f"fetch_error:{type(exc).__name__}", [])
    finally:
        if own:
            client.close()
    if res.status_code in (401, 402, 403, 451):
        return EnrichResult(None, True, f"blocked:{res.status_code}", [])
    if res.status_code >= 400:
        return EnrichResult(None, True, f"http:{res.status_code}", [])
    if "html" not in res.headers.get("content-type", ""):
        return EnrichResult(None, True, "not_html", [])
    text, links = extract_main_text(res.text, str(res.url))
    if not text or len(text) < MIN_ARTICLE_CHARS:
        return EnrichResult(None, True, "too_short_or_paywalled", [])
    lowered = text[:3000].lower()
    if any(h in lowered for h in PAYWALL_HINTS) and len(text) < 3000:
        return EnrichResult(None, True, "paywall", [])
    log(logger, logging.INFO, "article_fetched", host=res.url.host, chars=len(text))
    return EnrichResult(text[:max_chars], False, "ok", links)
