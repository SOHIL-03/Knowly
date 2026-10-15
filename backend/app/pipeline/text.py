"""HTML cleaning, link extraction and URL normalisation used by B2-B4."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "mc_cid", "mc_eid", "fbclid", "gclid", "_hsenc", "_hsmi", "mkt_tok", "ref_src",
}
SKIP_LINK_HINTS = ("unsubscribe", "manage preferences", "view in browser", "update your preferences")


def normalize_url(url: str | None) -> str | None:
    """Strips tracking parameters and fragments. Never invents or repairs a destination."""
    if not url:
        return None
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in TRACKING_PARAMS]
    host = parts.netloc.lower()
    return urlunsplit((parts.scheme.lower(), host, parts.path or "/", urlencode(query), ""))


def url_key(url: str | None) -> str | None:
    """Comparison key for de-duplication only (never shown or stored as a link)."""
    return url.rstrip("/").lower() if url else None


def clean_issue_html(html: str) -> tuple[str, list[str]]:
    """Removes scripts, styles, tracking pixels and footers.

    Returns plain text with links written inline as "text (url)" so the model can copy URLs
    exactly, plus the list of URLs that appear in the issue.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "head", "meta", "link"]):
        tag.decompose()
    for img in soup.find_all("img"):
        w, h = img.get("width"), img.get("height")
        if w in ("1", "0") or h in ("1", "0"):
            img.decompose()
        else:
            img.replace_with(img.get("alt") or "")
    urls: list[str] = []
    for a in soup.find_all("a"):
        href = (a.get("href") or "").strip()
        label = a.get_text(" ", strip=True)
        if any(h in label.lower() for h in SKIP_LINK_HINTS) or href.startswith("mailto:"):
            a.replace_with(label)
            continue
        norm = normalize_url(href)
        if norm:
            urls.append(norm)
            a.replace_with(f"{label} ({norm})" if label else norm)
        else:
            a.replace_with(label)
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(["p", "div", "li", "tr", "h1", "h2", "h3", "h4", "table", "section"]):
        block.insert_after("\n")
    text = soup.get_text("")
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    # Drop legal/unsubscribe footers after the last story.
    cut = re.search(r"(?im)^.*(unsubscribe|you are receiving this|you received this email).*$", text)
    if cut and cut.start() > len(text) * 0.6:
        text = text[: cut.start()].rstrip()
    return text, list(dict.fromkeys(urls))


def is_newsletter(headers: dict[str, str]) -> bool:
    """Newsletter header check (List-Unsubscribe or List-Id) that guards against personal mail."""
    keys = {k.lower() for k in headers}
    return "list-unsubscribe" in keys or "list-id" in keys


def url_in_issue(url: str | None, issue_urls: list[str]) -> str | None:
    """Accept a model-returned URL only if it really appears in the issue (no invented links)."""
    norm = normalize_url(url)
    if not norm:
        return None
    return norm if norm in issue_urls else None
